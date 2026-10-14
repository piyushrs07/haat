import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from PIL import Image

from .models import (Complaint, Customer, Message, Notification, Order, Product, ProductImage, Review,
                     Seller, ShopReview, StoredFile, ThrottleEvent, Wishlist)

PASSWORD = 'Str0ng-Passw0rd!'


def png_bytes(size=(40, 30), mode='RGB', color=(200, 50, 50), fmt='PNG'):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def image_upload(name='photo.png', **kw):
    return SimpleUploadedFile(name, png_bytes(**kw), content_type='image/png')


def make_customer(email='cust@example.com', verified=True, **kw):
    return Customer.objects.create(
        name=kw.pop('name', 'Test Customer'), email=email, password=make_password(PASSWORD), phone='9876543210',
        street_address='1 Main Rd', city='Panaji', state='Goa', pincode='403001', is_verified=verified, **kw)


def make_seller(email='seller@example.com', approved=True, suspended=False, verified=True, shop='Test Shop'):
    return Seller.objects.create(
        name='Test Seller', email=email, password=make_password(PASSWORD), phone='9123456780',
        shop_name=shop, product_type='crafts', street_address='2 Market St', city='Margao', state='Goa',
        pincode='403601', is_verified=verified, is_approved=approved, is_suspended=suspended)


def make_product(seller, name='Clay Pot', price='100.00', **kw):
    return Product.objects.create(seller=seller, name=name, description='A nice pot', price=Decimal(price),
                                  category=kw.pop('category', 'Pottery'), **kw)


def login_customer(client, email='cust@example.com'):
    return client.post('/login/customer/', {'email': email, 'password': PASSWORD})


def login_seller(client, email='seller@example.com'):
    return client.post('/login/seller/', {'email': email, 'password': PASSWORD})


REG = {'name': 'New Person', 'email': 'new@example.com', 'password': PASSWORD, 'phone': '9876543210',
       'street_address': '5 Beach Rd', 'city': 'Calangute', 'state': 'Goa', 'pincode': '403516'}
SELLER_REG = dict(REG, shop_name='New Shop', product_type='food', description='Tasty')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])   # fast tests only
class BaseCase(TestCase):
    def setUp(self):
        patcher = mock.patch('core.mail.send_via_brevo', return_value=True)
        self.mail = patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------- registration / OTP
class RegistrationTests(BaseCase):
    def test_customer_registration_sends_otp_and_validates_it(self):
        r = self.client.post('/register/customer/', REG)
        self.assertRedirects(r, '/verify/customer/')
        c = Customer.objects.get(email='new@example.com')
        self.assertFalse(c.is_verified)
        self.assertRegex(c.otp_code, r'^\d{6}$')
        self.assertNotEqual(c.password, PASSWORD)               # hashed

        r = self.client.post('/verify/customer/', {'otp': c.otp_code})
        self.assertRedirects(r, '/products/', fetch_redirect_response=False)
        c.refresh_from_db()
        self.assertTrue(c.is_verified)
        self.assertEqual(self.client.session['customer_id'], c.id)

    def test_validation_errors_are_shown_and_values_kept(self):
        bad = dict(REG, email='not-an-email', phone='abc', pincode='12', password='123')
        r = self.client.post('/register/customer/', bad)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Customer.objects.count(), 0)
        for text in ('valid email', 'phone', 'Pincode', 'too short'):
            self.assertContains(r, text, msg_prefix=text)
        self.assertContains(r, 'value="New Person"')             # form not wiped on error
        self.assertNotContains(r, PASSWORD)

    def test_over_long_fields_rejected_instead_of_crashing(self):
        r = self.client.post('/register/customer/', dict(REG, name='x' * 500))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'too long')

    def test_email_is_case_insensitive(self):
        make_customer('cust@example.com')
        r = self.client.post('/register/customer/', dict(REG, email='CUST@Example.com'))
        self.assertContains(r, 'already registered')
        self.assertEqual(Customer.objects.count(), 1)

    def test_unverified_signup_can_be_restarted(self):
        self.client.post('/register/customer/', REG)
        first = Customer.objects.get().otp_code
        client2 = Client()
        r = client2.post('/register/customer/', dict(REG, name='Real Owner'))
        self.assertRedirects(r, '/verify/customer/')
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Customer.objects.get().name, 'Real Owner')
        self.assertTrue(first)

    def test_wrong_otp_is_locked_after_five_attempts_even_with_right_code(self):
        self.client.post('/register/customer/', REG)
        c = Customer.objects.get()
        wrong = '000000' if c.otp_code != '000000' else '111111'
        for _ in range(5):
            self.client.post('/verify/customer/', {'otp': wrong})
        r = self.client.post('/verify/customer/', {'otp': c.otp_code})    # correct, but too late
        self.assertContains(r, 'Too many wrong attempts')
        c.refresh_from_db()
        self.assertFalse(c.is_verified)

    def test_resend_is_post_only_has_cooldown_and_resets_attempts(self):
        self.client.post('/register/customer/', REG)
        self.assertRedirects(self.client.get('/verify/customer/resend/'), '/verify/customer/')   # GET does nothing
        self.assertEqual(self.mail.call_count, 1)
        self.client.post('/verify/customer/resend/')                      # within cooldown
        self.assertEqual(self.mail.call_count, 1)
        Customer.objects.update(otp_created_at=timezone.now() - timedelta(minutes=2), otp_attempts=4)
        self.client.post('/verify/customer/resend/')
        self.assertEqual(self.mail.call_count, 2)
        self.assertEqual(Customer.objects.get().otp_attempts, 0)

    def test_otp_emails_are_rate_limited_per_address(self):
        for i in range(5):
            Customer.objects.all().delete()
            self.client.post('/register/customer/', REG)
        Customer.objects.all().delete()
        r = self.client.post('/register/customer/', REG)
        self.assertContains(r, 'Too many verification emails')
        self.assertEqual(Customer.objects.count(), 0)

    def test_expired_otp_rejected(self):
        self.client.post('/register/customer/', REG)
        c = Customer.objects.get()
        Customer.objects.update(otp_created_at=timezone.now() - timedelta(minutes=11))
        r = self.client.post('/verify/customer/', {'otp': c.otp_code})
        self.assertContains(r, 'expired')

    def test_non_ascii_otp_does_not_crash(self):
        self.client.post('/register/customer/', REG)
        r = self.client.post('/verify/customer/', {'otp': '١٢٣٤٥٦'})
        self.assertEqual(r.status_code, 200)

    def test_seller_registration_requires_admin_approval(self):
        r = self.client.post('/register/seller/', SELLER_REG)
        self.assertRedirects(r, '/verify/seller/')
        s = Seller.objects.get()
        r = self.client.post('/verify/seller/', {'otp': s.otp_code})
        self.assertRedirects(r, '/login/seller/')
        s.refresh_from_db()
        self.assertTrue(s.is_verified)
        self.assertFalse(s.is_approved)
        r = login_seller(self.client, 'new@example.com')
        self.assertContains(r, 'pending approval')


# ---------------------------------------------------------------- login / sessions
class LoginTests(BaseCase):
    def test_login_rotates_session_key_and_is_case_insensitive(self):
        make_customer('cust@example.com')
        self.client.get('/')                       # establish an anonymous session
        before = self.client.session.session_key
        r = login_customer(self.client, 'CUST@Example.COM')
        self.assertRedirects(r, '/products/', fetch_redirect_response=False)
        self.assertNotEqual(self.client.session.session_key, before)

    def test_only_one_role_active_per_session(self):
        make_customer(); make_seller()
        login_customer(self.client)
        login_seller(self.client)
        s = self.client.session
        self.assertIn('seller_id', s)
        self.assertNotIn('customer_id', s)

    def test_bad_logins_are_throttled(self):
        make_customer()
        for _ in range(6):
            r = self.client.post('/login/customer/', {'email': 'cust@example.com', 'password': 'wrong'})
            self.assertContains(r, 'Invalid email or password')
        r = login_customer(self.client)            # correct password, but locked out
        self.assertContains(r, 'Too many failed attempts')
        self.assertNotIn('customer_id', self.client.session)

    def test_successful_login_clears_failures(self):
        make_customer()
        for _ in range(3):
            self.client.post('/login/customer/', {'email': 'cust@example.com', 'password': 'wrong'})
        login_customer(self.client)
        self.assertEqual(ThrottleEvent.objects.filter(key__startswith='login:').count(), 0)

    def test_unknown_email_gives_same_message(self):
        r = self.client.post('/login/customer/', {'email': 'nobody@example.com', 'password': 'x'})
        self.assertContains(r, 'Invalid email or password')

    def test_unverified_customer_is_sent_to_verification(self):
        make_customer(verified=False)
        r = login_customer(self.client)
        self.assertRedirects(r, '/verify/customer/')

    def test_logout_requires_post(self):
        make_customer(); login_customer(self.client)
        self.client.get('/logout/')
        self.assertIn('customer_id', self.client.session)
        self.client.post('/logout/')
        self.assertNotIn('customer_id', self.client.session)

    def test_suspension_takes_effect_on_existing_session(self):
        s = make_seller(); login_seller(self.client)
        self.assertEqual(self.client.get('/seller/dashboard/').status_code, 200)
        s.is_suspended = True; s.save()
        r = self.client.get('/seller/dashboard/')
        self.assertRedirects(r, '/login/seller/')

    def test_deleted_account_session_does_not_500(self):
        c = make_customer(); login_customer(self.client)
        c.delete()
        self.assertRedirects(self.client.get('/my-orders/'), '/login/customer/')

    def test_pages_require_login(self):
        for url in ('/my-orders/', '/wishlist/', '/seller/dashboard/', '/seller/add-product/'):
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_csrf_enforced(self):
        make_customer()
        strict = Client(enforce_csrf_checks=True)
        r = strict.post('/login/customer/', {'email': 'cust@example.com', 'password': PASSWORD})
        self.assertEqual(r.status_code, 403)


# ---------------------------------------------------------------- products / uploads / media
class ProductTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.seller = make_seller()
        login_seller(self.client)
        self.form = {'name': 'Basket', 'description': 'Woven', 'price': '250.50', 'category': 'Crafts'}

    def test_add_product_with_photo_is_stored_as_jpeg(self):
        r = self.client.post('/seller/add-product/', dict(self.form, photo=image_upload('big.png', size=(3000, 2000))))
        self.assertRedirects(r, '/seller/dashboard/')
        p = Product.objects.get()
        self.assertTrue(p.photo.name.endswith('.jpg'))
        stored = StoredFile.objects.get(name=p.photo.name)
        img = Image.open(io.BytesIO(bytes(stored.data)))
        self.assertEqual(img.format, 'JPEG')
        self.assertLessEqual(max(img.size), 1200)

    def test_html_upload_is_rejected(self):
        evil = SimpleUploadedFile('evil.html', b'<script>alert(1)</script>', content_type='text/html')
        r = self.client.post('/seller/add-product/', dict(self.form, photo=evil))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'not a valid image')
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_html_disguised_as_jpg_is_rejected(self):
        evil = SimpleUploadedFile('cute.jpg', b'<html><script>alert(1)</script></html>', content_type='image/jpeg')
        r = self.client.post('/seller/add-product/', dict(self.form, photo=evil))
        self.assertContains(r, 'not a valid image')
        self.assertEqual(StoredFile.objects.count(), 0)

    def test_script_hidden_after_valid_image_is_stripped_on_reencode(self):
        data = png_bytes() + b'<script>alert(1)</script>'
        up = SimpleUploadedFile('trick.png', data, content_type='image/png')
        self.client.post('/seller/add-product/', dict(self.form, photo=up))
        stored = StoredFile.objects.get()
        self.assertNotIn(b'<script>', bytes(stored.data))
        r = self.client.get('/media/' + stored.name)
        self.assertEqual(r['Content-Type'], 'image/jpeg')
        self.assertEqual(r['X-Content-Type-Options'], 'nosniff')

    def test_oversized_upload_rejected(self):
        with self.settings(MAX_UPLOAD_BYTES=100):
            r = self.client.post('/seller/add-product/', dict(self.form, photo=image_upload(size=(300, 300))))
        self.assertContains(r, 'too large')
        self.assertEqual(Product.objects.count(), 0)

    def test_bad_price_is_a_form_error_not_a_crash(self):
        for bad in ('abc', '-5', '0', '1e999', 'NaN', ''):
            r = self.client.post('/seller/add-product/', dict(self.form, price=bad))
            self.assertEqual(r.status_code, 200, bad)
            self.assertContains(r, 'valid price')
        self.assertEqual(Product.objects.count(), 0)

    def test_form_values_preserved_on_error(self):
        r = self.client.post('/seller/add-product/', dict(self.form, price='abc'))
        self.assertContains(r, 'value="Basket"')

    def test_extra_photo_limit(self):
        files = [image_upload(f'{i}.png') for i in range(settings.MAX_EXTRA_PHOTOS + 1)]
        r = self.client.post('/seller/add-product/', dict(self.form, images=files))
        self.assertContains(r, 'at most')

    def test_edit_replaces_photo_and_removes_old_file(self):
        self.client.post('/seller/add-product/', dict(self.form, photo=image_upload('a.png')))
        p = Product.objects.get(); old = p.photo.name
        self.client.post(f'/seller/product/edit/{p.id}/', dict(self.form, name='Basket 2', is_available='on',
                                                              photo=image_upload('b.png', color=(1, 2, 3))))
        p.refresh_from_db()
        self.assertEqual(p.name, 'Basket 2')
        self.assertNotEqual(p.photo.name, old)
        self.assertFalse(StoredFile.objects.filter(name=old).exists())
        self.assertTrue(StoredFile.objects.filter(name=p.photo.name).exists())

    def test_availability_checkbox_hides_product(self):
        p = make_product(self.seller)
        self.client.post(f'/seller/product/edit/{p.id}/', dict(self.form))      # checkbox absent -> hidden
        p.refresh_from_db(); self.assertFalse(p.is_available)
        self.assertNotContains(Client().get('/products/'), 'Basket')

    def test_cannot_edit_or_delete_someone_elses_product(self):
        other = make_seller('other@example.com', shop='Other')
        p = make_product(other)
        self.assertEqual(self.client.get(f'/seller/product/edit/{p.id}/').status_code, 404)
        self.assertEqual(self.client.post(f'/seller/product/delete/{p.id}/').status_code, 404)
        self.assertTrue(Product.objects.filter(id=p.id).exists())

    def test_gallery_image_delete_is_post_only_and_removes_file(self):
        p = make_product(self.seller)
        self.client.post(f'/seller/product/edit/{p.id}/', dict(self.form, is_available='on', images=[image_upload()]))
        img = ProductImage.objects.get(); name = img.image.name
        self.client.get(f'/seller/product-image/delete/{img.id}/')
        self.assertTrue(ProductImage.objects.exists())
        self.client.post(f'/seller/product-image/delete/{img.id}/')
        self.assertFalse(ProductImage.objects.exists())
        self.assertFalse(StoredFile.objects.filter(name=name).exists())

    def test_delete_product_without_orders_removes_it_and_its_photo(self):
        self.client.post('/seller/add-product/', dict(self.form, photo=image_upload()))
        p = Product.objects.get()
        self.client.post(f'/seller/product/delete/{p.id}/')
        self.assertFalse(Product.objects.exists())
        self.assertFalse(StoredFile.objects.exists())

    def test_delete_product_with_orders_hides_it_and_keeps_customer_history(self):
        p = make_product(self.seller); c = make_customer()
        Order.objects.create(customer=c, product=p, quantity=1, total_price=100, payment_method='online',
                             delivery_address='x')
        self.client.post(f'/seller/product/delete/{p.id}/')
        p.refresh_from_db()
        self.assertFalse(p.is_available)
        self.assertEqual(Order.objects.count(), 1)

    def test_order_status_validated_and_notifies_once(self):
        p = make_product(self.seller); c = make_customer()
        o = Order.objects.create(customer=c, product=p, quantity=1, total_price=100, payment_method='online',
                                 delivery_address='x')
        self.client.post(f'/order/update-status/{o.id}/', {'status': 'hacked'})
        o.refresh_from_db(); self.assertEqual(o.status, 'pending')
        self.client.post(f'/order/update-status/{o.id}/', {'status': 'preparing'})
        o.refresh_from_db(); self.assertEqual(o.status, 'preparing')
        self.assertEqual(Notification.objects.filter(recipient_type='customer').count(), 1)
        self.client.post(f'/order/update-status/{o.id}/', {'status': 'preparing'})    # no change -> no new alert
        self.assertEqual(Notification.objects.filter(recipient_type='customer').count(), 1)

    def test_seller_cannot_update_other_sellers_order(self):
        other = make_seller('o@example.com', shop='O'); p = make_product(other); c = make_customer()
        o = Order.objects.create(customer=c, product=p, quantity=1, total_price=1, payment_method='online',
                                 delivery_address='x')
        self.assertEqual(self.client.post(f'/order/update-status/{o.id}/', {'status': 'delivered'}).status_code, 404)

    def test_delete_account_blocked_while_orders_open(self):
        p = make_product(self.seller); c = make_customer()
        Order.objects.create(customer=c, product=p, quantity=1, total_price=1, payment_method='online',
                             delivery_address='x')
        self.client.post('/seller/delete-account/')
        self.assertTrue(Seller.objects.filter(id=self.seller.id).exists())
        Order.objects.update(status='delivered')
        self.client.post('/seller/delete-account/')
        self.assertFalse(Seller.objects.filter(id=self.seller.id).exists())


class MediaTests(BaseCase):
    def test_media_paths_and_caching(self):
        seller = make_seller(); login_seller(self.client)
        self.client.post('/seller/add-product/', {'name': 'A', 'description': 'd', 'price': '5', 'category': 'c',
                                                  'photo': image_upload()})
        name = StoredFile.objects.get().name
        r = Client().get('/media/' + name)
        self.assertEqual(r.status_code, 200)
        self.assertIn('max-age', r['Cache-Control'])
        r2 = Client().get('/media/' + name, HTTP_IF_NONE_MATCH=r['ETag'])
        self.assertEqual(r2.status_code, 304)
        self.assertEqual(Client().get('/media/products/nope.jpg').status_code, 404)
        StoredFile.objects.create(name='secret/other.bin', data=b'x')
        self.assertEqual(Client().get('/media/secret/other.bin').status_code, 404)      # only products/ is served


# ---------------------------------------------------------------- public catalogue
class CatalogueTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.good = make_seller(shop='Good Shop')
        self.bad = make_seller('bad@example.com', suspended=True, shop='Suspended Shop')
        self.pending = make_seller('p@example.com', approved=False, shop='Pending Shop')
        make_product(self.good, 'Visible Item', '50')
        make_product(self.bad, 'Suspended Item'); make_product(self.pending, 'Pending Item')
        make_product(self.good, 'Hidden Item', is_available=False)

    def test_only_available_products_from_active_sellers_are_listed(self):
        body = self.client.get('/products/').content.decode()
        self.assertIn('Visible Item', body)
        for hidden in ('Suspended Item', 'Pending Item', 'Hidden Item'):
            self.assertNotIn(hidden, body)

    def test_home_counts_only_visible(self):
        r = self.client.get('/')
        self.assertEqual(r.context['product_count'], 1)
        self.assertEqual(r.context['seller_count'], 1)

    def test_invalid_filters_do_not_crash(self):
        for qs in ('min_price=abc', 'max_price=%00', 'min_price=1e999', 'page=zzz', 'page=-4', 'search=%E0%A4%A8'):
            self.assertEqual(self.client.get('/products/?' + qs).status_code, 200, qs)
        self.assertContains(self.client.get('/products/?min_price=abc'), 'ignored')

    def test_price_filters_work(self):
        self.assertContains(self.client.get('/products/?min_price=40&max_price=60'), 'Visible Item')
        self.assertNotContains(self.client.get('/products/?min_price=60'), 'Visible Item')

    def test_pagination(self):
        for i in range(30):
            make_product(self.good, f'Bulk {i:02d}')
        r = self.client.get('/products/')
        self.assertEqual(len(r.context['products']), 12)
        self.assertContains(r, 'Page 1 of 3')
        self.assertEqual(len(self.client.get('/products/?page=3').context['products']), 7)    # 31 total
        self.assertContains(self.client.get('/products/?search=Bulk&page=2'), 'Page 2 of 3')

    def test_listing_query_count_does_not_grow_with_products(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get('/products/').status_code, 200)
            return len(ctx)
        few = count()
        for i in range(11):
            make_product(self.good, f'Q{i}')
        self.assertEqual(count(), few)       # N+1 queries would make this grow
        self.assertLessEqual(few, 6)

    def test_unknown_urls_give_friendly_404(self):
        r = self.client.get('/no/such/page/')
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, 'Page not found', status_code=404)


# ---------------------------------------------------------------- orders, reviews, wishlist, chat
class CustomerFlowTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.seller = make_seller()
        self.product = make_product(self.seller, price='120.00')
        self.customer = make_customer()
        login_customer(self.client)
        self.order = {'quantity': '2', 'distance_zone': 'medium', 'payment_method': 'offline',
                      'delivery_address': '12 Beach Rd, Panaji'}

    def test_place_order_computes_total_and_notifies(self):
        r = self.client.post(f'/order/{self.product.id}/', self.order)
        self.assertRedirects(r, '/my-orders/')
        o = Order.objects.get()
        self.assertEqual(o.total_price, Decimal('270.00'))          # 2 x 120 + 30 delivery
        self.assertEqual(o.delivery_charge, 30)
        self.assertEqual(o.estimated_delivery_date, timezone.localdate() + timedelta(days=3))
        self.assertEqual(Notification.objects.filter(recipient_type='seller').count(), 1)
        self.assertEqual(Notification.objects.filter(recipient_type='customer').count(), 1)

    def test_order_form_is_prefilled_with_profile_address(self):
        self.assertContains(self.client.get(f'/order/{self.product.id}/'), '1 Main Rd, Panaji, Goa - 403001')

    def test_bad_order_input_is_an_error_not_a_500(self):
        for field, bad in (('quantity', 'abc'), ('quantity', '-5'), ('quantity', '0'), ('quantity', '100000'),
                           ('quantity', '1.5'), ('distance_zone', 'moon'), ('payment_method', 'barter'),
                           ('delivery_address', '   ')):
            r = self.client.post(f'/order/{self.product.id}/', dict(self.order, **{field: bad}))
            self.assertEqual(r.status_code, 200, (field, bad))
            self.assertContains(r, 'error-box')
        self.assertEqual(Order.objects.count(), 0)

    def test_cannot_order_hidden_or_missing_product(self):
        self.product.is_available = False; self.product.save()
        self.assertEqual(self.client.get(f'/order/{self.product.id}/').status_code, 404)
        self.assertEqual(self.client.post(f'/order/{self.product.id}/', self.order).status_code, 404)
        self.assertEqual(self.client.get('/order/99999/').status_code, 404)
        self.seller.is_suspended = True; self.seller.save(); self.product.is_available = True; self.product.save()
        self.assertEqual(self.client.get(f'/order/{self.product.id}/').status_code, 404)

    def test_wishlist_changes_only_on_post(self):
        self.client.get(f'/product/{self.product.id}/wishlist/')
        self.assertEqual(Wishlist.objects.count(), 0)
        self.client.post(f'/product/{self.product.id}/wishlist/')
        self.assertEqual(Wishlist.objects.count(), 1)
        self.client.post(f'/product/{self.product.id}/wishlist/')
        self.assertEqual(Wishlist.objects.count(), 0)

    def test_wishlist_next_redirect_cannot_leave_site(self):
        r = self.client.post(f'/product/{self.product.id}/wishlist/', {'next': 'https://evil.example/'})
        self.assertRedirects(r, '/products/', fetch_redirect_response=False)
        r = self.client.post(f'/product/{self.product.id}/wishlist/', {'next': '/wishlist/'})
        self.assertRedirects(r, '/wishlist/', fetch_redirect_response=False)

    def test_review_rating_bounds_and_single_review(self):
        for bad in ('999', '0', '-1', 'abc', ''):
            self.client.post(f'/product/{self.product.id}/review/', {'rating': bad, 'comment': 'x'})
        self.assertEqual(Review.objects.count(), 0)
        self.client.post(f'/product/{self.product.id}/review/', {'rating': '5', 'comment': 'great'})
        self.client.post(f'/product/{self.product.id}/review/', {'rating': '3', 'comment': 'ok'})
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Review.objects.get().rating, 3)

    def test_shop_review(self):
        self.client.post(f'/seller/{self.seller.id}/review/', {'rating': '9'})
        self.assertEqual(ShopReview.objects.count(), 0)
        self.client.post(f'/seller/{self.seller.id}/review/', {'rating': '4', 'comment': 'nice'})
        self.assertEqual(self.seller.average_rating(), 4)
        self.assertContains(self.client.get('/products/'), 'Shop rating: ★ 4.0')

    def test_complaint(self):
        o = Order.objects.create(customer=self.customer, product=self.product, quantity=1, total_price=1,
                                 payment_method='online', delivery_address='x')
        self.client.post(f'/order/{o.id}/complaint/', {'description': '  '})
        self.assertEqual(Complaint.objects.count(), 0)
        self.client.post(f'/order/{o.id}/complaint/', {'description': 'Broken'})
        self.assertEqual(Complaint.objects.count(), 1)

    def test_cannot_complain_about_someone_elses_order(self):
        other = make_customer('o@example.com')
        o = Order.objects.create(customer=other, product=self.product, quantity=1, total_price=1,
                                 payment_method='online', delivery_address='x')
        self.assertEqual(self.client.get(f'/order/{o.id}/complaint/').status_code, 404)

    def test_my_orders_only_shows_own(self):
        other = make_customer('o@example.com')
        Order.objects.create(customer=other, product=self.product, quantity=1, total_price=777, payment_method='online',
                             delivery_address='x')
        self.assertNotContains(self.client.get('/my-orders/'), '777')

    def test_chat_both_directions_and_validation(self):
        self.client.post(f'/messages/seller/{self.seller.id}/', {'content': 'Hello shop'})
        self.client.post(f'/messages/seller/{self.seller.id}/', {'content': '   '})
        self.client.post(f'/messages/seller/{self.seller.id}/', {'content': 'x' * 5000})
        self.assertEqual(Message.objects.count(), 1)
        self.assertContains(self.client.get(f'/messages/seller/{self.seller.id}/'), 'Hello shop')

        seller_client = Client(); login_seller(seller_client)
        r = seller_client.post(f'/messages/customer/{self.customer.id}/', {'content': 'Hi back'})
        self.assertEqual(Message.objects.count(), 2)
        self.assertContains(self.client.get(f'/messages/seller/{self.seller.id}/'), 'Hi back')

    def test_seller_cannot_open_chat_with_unrelated_customer(self):
        seller_client = Client(); login_seller(seller_client)
        stranger = make_customer('stranger@example.com')
        self.assertEqual(seller_client.get(f'/messages/customer/{stranger.id}/').status_code, 404)
        self.assertEqual(seller_client.post(f'/messages/customer/{stranger.id}/', {'content': 'spam'}).status_code, 404)
        self.assertEqual(Message.objects.count(), 0)

    def test_cannot_chat_with_suspended_seller(self):
        self.seller.is_suspended = True; self.seller.save()
        self.assertEqual(self.client.get(f'/messages/seller/{self.seller.id}/').status_code, 404)

    def test_notifications_page_marks_read(self):
        Notification.objects.create(recipient_type='customer', recipient_id=self.customer.id, message='Hi', link='/x/')
        self.assertContains(self.client.get('/'), 'class="count"')
        self.client.get('/notifications/')
        self.assertNotContains(self.client.get('/'), 'class="count"')


# ---------------------------------------------------------------- PWA + deployment plumbing
class PwaTests(BaseCase):
    def test_manifest_is_installable_shape(self):
        r = self.client.get('/manifest.webmanifest')
        self.assertEqual(r['Content-Type'], 'application/manifest+json')
        m = json.loads(r.content)
        self.assertEqual((m['start_url'].split('?')[0], m['scope'], m['display']), ('/', '/', 'standalone'))
        sizes = {(i['sizes'], i['type'], i['purpose']) for i in m['icons']}
        self.assertIn(('192x192', 'image/png', 'any'), sizes)
        self.assertIn(('512x512', 'image/png', 'any'), sizes)
        self.assertIn(('512x512', 'image/png', 'maskable'), sizes)
        for icon in m['icons']:
            path = os.path.join(settings.BASE_DIR, 'core', 'static', 'core', 'icons', os.path.basename(icon['src']))
            self.assertTrue(os.path.exists(path), icon['src'])

    def test_service_worker_headers_and_content(self):
        r = self.client.get('/service-worker.js')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/javascript')
        self.assertEqual(r['Service-Worker-Allowed'], '/')
        self.assertEqual(r['Cache-Control'], 'no-cache')
        body = r.content.decode()
        self.assertIn(settings.ASSET_VERSION, body)
        self.assertIn('/offline/', body)

    def test_base_template_links_pwa_assets(self):
        body = self.client.get('/').content.decode()
        for needle in ('rel="manifest" href="/manifest.webmanifest"', 'apple-touch-icon', 'viewport-fit=cover',
                       'theme-color', 'core/app.js'):
            self.assertIn(needle, body)

    def test_offline_page_and_health(self):
        self.assertContains(self.client.get('/offline/'), "You're offline")
        r = self.client.get('/healthz')
        self.assertEqual((r.status_code, r.content), (200, b'ok'))

    def test_logout_is_a_post_form_in_nav(self):
        make_customer(); login_customer(self.client)
        body = self.client.get('/').content.decode()
        self.assertIn('action="/logout/"', body)
        self.assertNotIn('href="/logout/"', body)


class AdminTests(BaseCase):
    def test_approve_action_approves_and_emails(self):
        User = get_user_model()
        User.objects.create_superuser('boss', 'b@example.com', 'pw-Boss-12345')
        s = make_seller('new@example.com', approved=False)
        admin = Client(); admin.login(username='boss', password='pw-Boss-12345')
        r = admin.post('/admin/core/seller/', {'action': 'approve_sellers', '_selected_action': [s.id]}, follow=True)
        self.assertContains(r, '1 seller(s) approved')
        s.refresh_from_db(); self.assertTrue(s.is_approved)
        self.assertEqual(Notification.objects.filter(recipient_type='seller').count(), 1)

    def test_admin_pages_load(self):
        User = get_user_model()
        User.objects.create_superuser('boss', 'b@example.com', 'pw-Boss-12345')
        admin = Client(); admin.login(username='boss', password='pw-Boss-12345')
        for model in ('seller', 'customer', 'product', 'order', 'complaint', 'review', 'shopreview', 'message',
                      'wishlist', 'notification'):
            self.assertEqual(admin.get(f'/admin/core/{model}/').status_code, 200, model)

    def test_ensure_superuser_command(self):
        from django.core.management import call_command
        env = {'DJANGO_SUPERUSER_USERNAME': 'root1', 'DJANGO_SUPERUSER_PASSWORD': 'pw-Root-12345',
               'DJANGO_SUPERUSER_EMAIL': 'r@example.com'}
        with mock.patch.dict(os.environ, env):
            call_command('ensure_superuser'); call_command('ensure_superuser')    # idempotent
        u = get_user_model().objects.get(username='root1')
        self.assertTrue(u.is_superuser and u.is_staff and u.check_password('pw-Root-12345'))
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in env: os.environ.pop(k, None)
            call_command('ensure_superuser')                                      # no env -> silently skips


class SettingsTests(TestCase):
    """Settings are evaluated at import time, so check them in a fresh interpreter."""
    def run_settings(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in ('RENDER', 'DEBUG', 'SECRET_KEY', 'DATABASE_URL')}
        base.update({'DJANGO_SETTINGS_MODULE': 'vendorapp.settings', 'PYTHONPATH': str(settings.BASE_DIR)})
        base.update(env)
        code = ('import django.conf as c; s=c.settings; '
                'print(s.DEBUG, s.SECURE_SSL_REDIRECT if hasattr(s,"SECURE_SSL_REDIRECT") else False, '
                's.SESSION_COOKIE_SECURE, s.ALLOWED_HOSTS)')
        return subprocess.run([sys.executable, '-c', code], env=base, capture_output=True, text=True)

    def test_render_without_secret_key_refuses_to_start(self):
        r = self.run_settings(RENDER='true')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('SECRET_KEY', r.stderr)

    def test_render_defaults_to_debug_off_and_secure_cookies(self):
        r = self.run_settings(RENDER='true', SECRET_KEY='x' * 60, RENDER_EXTERNAL_HOSTNAME='haat.onrender.com')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("False True True", r.stdout)
        self.assertIn('haat.onrender.com', r.stdout)

    def test_local_defaults_are_developer_friendly(self):
        r = self.run_settings()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('True'))
