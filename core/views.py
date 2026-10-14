import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Avg, Count, F, Prefetch
from django.http import Http404, HttpResponse, HttpResponseNotModified, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.utils.http import http_date, url_has_allowed_host_and_scheme

from . import security
from .mail import (generate_otp, notify, send_admin_notification,
                   send_notification_email, send_otp_email)
from .models import (Complaint, Customer, Message, Notification, Order, Product, ProductImage,
                     Review, Seller, ShopReview, StoredFile, Wishlist)
from .security import customer_required, seller_required

logger = logging.getLogger(__name__)

OTP_MINUTES = 10
OTP_MAX_ATTEMPTS = 5            # wrong guesses allowed per code
OTP_RESEND_COOLDOWN_SEC = 30
OTP_SENDS_PER_HOUR = 5          # per email address (registration + resend)

DELIVERY_CHARGES = {'near': 0, 'medium': 30, 'far': 60}
DELIVERY_DAYS = {'near': 1, 'medium': 3, 'far': 5}
ACTIVE_ORDER_STATUSES = ('pending', 'preparing', 'out_for_delivery')
MAX_QUANTITY = 100

PHONE_RE = re.compile(r'^\+?[0-9][0-9\s\-]{5,13}[0-9]$')
PINCODE_RE = re.compile(r'^[0-9]{6}$')


# ---------- Small helpers ----------

def visible_products():
    """Products the public may see/buy: available, from an approved, non-suspended seller."""
    return Product.objects.filter(is_available=True, seller__is_approved=True,
                                  seller__is_suspended=False, seller__is_verified=True)


def otp_is_valid(otp_created_at):
    if not otp_created_at:
        return False
    return timezone.now() - otp_created_at <= timedelta(minutes=OTP_MINUTES)


def safe_next(request, default):
    nxt = request.POST.get('next') or request.GET.get('next')
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default


def parse_decimal(value, minimum=None, maximum=None):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def current_recipient(request):
    if request.session.get('customer_id'):
        return 'customer', request.session['customer_id']
    if request.session.get('seller_id'):
        return 'seller', request.session['seller_id']
    return None, None


# ---------- Health / PWA plumbing ----------

def healthz(request):
    return HttpResponse('ok', content_type='text/plain')


def manifest(request):
    data = {
        'id': '/',
        'name': 'Haat - Local Vendor Marketplace',
        'short_name': 'Haat',
        'description': 'Shop local, support small businesses in Goa.',
        'start_url': '/?source=pwa',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#fdf6ee',
        'theme_color': '#e76f51',
        'lang': 'en-IN',
        'categories': ['shopping'],
        'icons': [
            {'src': static('core/icons/icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('core/icons/icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('core/icons/icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png',
             'purpose': 'maskable'},
        ],
        'shortcuts': [
            {'name': 'Browse products', 'url': '/products/'},
            {'name': 'My orders', 'url': '/my-orders/'},
        ],
    }
    response = JsonResponse(data, json_dumps_params={'indent': 2})
    response['Content-Type'] = 'application/manifest+json'
    response['Cache-Control'] = 'public, max-age=3600'
    return response


def service_worker(request):
    response = render(request, 'service-worker.js', {'version': settings.ASSET_VERSION},
                      content_type='application/javascript')
    response['Cache-Control'] = 'no-cache'          # browsers must always re-check the worker
    response['Service-Worker-Allowed'] = '/'
    return response


def offline(request):
    return render(request, 'core/offline.html')


# ---------- Home ----------

def home(request):
    available = visible_products()
    latest = available.select_related('seller').order_by('-id')[:6]
    categories = []
    seen = set()
    for c in available.values_list('category', flat=True).distinct()[:200]:
        c = (c or '').strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            categories.append(c)
        if len(categories) >= 8:
            break
    return render(request, 'core/home.html', {
        'latest_products': latest,
        'categories': categories,
        'product_count': available.count(),
        'seller_count': Seller.objects.filter(is_approved=True, is_suspended=False, is_verified=True).count(),
    })


# ---------- Media (photos stored in the database) ----------

def serve_media(request, path):
    """Serves product photos. Everything stored is re-encoded to JPEG on upload, so we always
    answer with an image type (never HTML), and ETags make repeat visits cheap on mobile data."""
    if not path.startswith('products/'):
        raise Http404('File not found')
    meta = StoredFile.objects.defer('data').filter(name=path).first()
    if meta is None:
        raise Http404('File not found')

    etag = f'"{meta.pk}-{int(meta.created_at.timestamp())}"'
    if request.headers.get('If-None-Match') == etag:
        response = HttpResponseNotModified()
    else:
        data = StoredFile.objects.values_list('data', flat=True).get(pk=meta.pk)
        content_type = meta.content_type if meta.content_type.startswith('image/') else 'application/octet-stream'
        response = HttpResponse(bytes(data), content_type=content_type)
    response['ETag'] = etag
    response['Last-Modified'] = http_date(meta.created_at.timestamp())
    response['Cache-Control'] = 'public, max-age=86400'
    response['Content-Disposition'] = 'inline'
    return response


# ---------- In-app notifications ----------

def notifications_view(request):
    recipient_type, recipient_id = current_recipient(request)
    if not recipient_type:
        return redirect('home')
    items = list(Notification.objects.filter(
        recipient_type=recipient_type, recipient_id=recipient_id)[:50])
    Notification.objects.filter(
        recipient_type=recipient_type, recipient_id=recipient_id, is_read=False
    ).update(is_read=True)
    return render(request, 'core/notifications.html', {'items': items})


# ---------- Registration (shared by customers and sellers) ----------

def clean_registration(post, seller=False):
    """Validate + normalise the sign-up form. Returns (cleaned_dict, error_list)."""
    errors = []

    def field(name, label, max_len, required=True):
        value = (post.get(name) or '').strip()
        if required and not value:
            errors.append(f'{label} is required.')
        elif len(value) > max_len:
            errors.append(f'{label} is too long (max {max_len} characters).')
        return value

    cleaned = {
        'name': field('name', 'Name', 100),
        'email': (post.get('email') or '').strip().lower(),
        'phone': (post.get('phone') or '').strip(),
        'street_address': field('street_address', 'Street / Area', 255),
        'city': field('city', 'City', 100),
        'state': field('state', 'State', 100),
        'pincode': (post.get('pincode') or '').strip(),
    }
    if seller:
        cleaned['shop_name'] = field('shop_name', 'Shop name', 100)
        cleaned['product_type'] = field('product_type', 'Type of products', 100)
        cleaned['description'] = field('description', 'Business description', 1000, required=False)

    try:
        validate_email(cleaned['email'])
        if len(cleaned['email']) > 254:
            raise ValidationError('too long')
    except ValidationError:
        errors.append('Please enter a valid email address.')

    if not PHONE_RE.match(cleaned['phone']):
        errors.append('Please enter a valid phone number (7-15 digits).')
    if not PINCODE_RE.match(cleaned['pincode']):
        errors.append('Pincode must be exactly 6 digits.')

    password = post.get('password') or ''
    try:
        validate_password(password)
    except ValidationError as exc:
        errors.extend(exc.messages)

    return cleaned, password, errors


def _register(request, Model, role, template, session_key, verify_url_name):
    if request.method != 'POST':
        return render(request, template, {'form': {'state': 'Goa'}})

    cleaned, password, errors = clean_registration(request.POST, seller=(role == 'seller'))
    form = {k: v for k, v in cleaned.items()}   # password is deliberately never echoed back

    def fail(msgs, login_link=False):
        return render(request, template, {'errors': msgs, 'form': form, 'login_link': login_link})

    if errors:
        return fail(errors)

    email = cleaned['email']
    existing = Model.objects.filter(email__iexact=email).first()
    if existing and existing.is_verified:
        return fail(['This email is already registered. Please log in instead.'], login_link=True)

    # Throttle OTP emails per address so the form can't be used to spam someone's inbox.
    send_key = f'otpsend:{email}'
    if security.too_many(send_key, OTP_SENDS_PER_HOUR, 60):
        return fail(['Too many verification emails requested for this address. Please try again in an hour.'])

    otp = generate_otp()
    values = dict(cleaned, password=make_password(password), otp_code=otp,
                  otp_created_at=timezone.now(), otp_attempts=0)
    if existing:
        # An earlier sign-up that was never verified: let the real owner of the email start over.
        for attr, value in values.items():
            setattr(existing, attr, value)
        existing.save()
        user = existing
    else:
        user = Model.objects.create(**values)

    security.record(send_key)
    email_sent = send_otp_email(email, cleaned['name'], otp)

    request.session[session_key] = user.id
    if email_sent:
        messages.success(request, 'A verification code has been sent to your email.')
    else:
        messages.warning(request, 'We could not send the verification email right now. '
                                  'Please try "Resend code" in a moment.')
    return redirect(verify_url_name)


def register_customer(request):
    return _register(request, Customer, 'customer', 'core/register_customer.html',
                     'pending_customer_id', 'verify_customer_otp')


def register_seller(request):
    return _register(request, Seller, 'seller', 'core/register_seller.html',
                     'pending_seller_id', 'verify_seller_otp')


# ---------- OTP verification (shared) ----------

def _pending_user(request, Model, session_key):
    pending_id = request.session.get(session_key)
    if not pending_id:
        return None
    user = Model.objects.filter(id=pending_id, is_verified=False).first()
    if user is None:
        request.session.pop(session_key, None)
    return user


def _verify_otp(request, Model, role, session_key, register_url, on_success):
    user = _pending_user(request, Model, session_key)
    if user is None:
        return redirect(register_url)
    context = {'email': user.email, 'role': role}

    if request.method == 'POST':
        entered = (request.POST.get('otp') or '').strip()

        # Count the attempt atomically *before* checking it, so parallel requests can't
        # get more than OTP_MAX_ATTEMPTS guesses per code.
        Model.objects.filter(pk=user.pk).update(otp_attempts=F('otp_attempts') + 1)
        user.refresh_from_db()

        if user.otp_attempts > OTP_MAX_ATTEMPTS:
            messages.error(request, 'Too many wrong attempts. Please request a new code.')
        elif not otp_is_valid(user.otp_created_at):
            messages.error(request, 'Your code has expired. Please request a new one.')
        elif (user.otp_code and entered.isascii()
              and secrets.compare_digest(entered, user.otp_code)):
            user.is_verified = True
            user.otp_code = None
            user.otp_created_at = None
            user.otp_attempts = 0
            user.save()
            request.session.pop(session_key, None)
            return on_success(request, user)
        else:
            messages.error(request, 'Incorrect code. Please try again.')

    return render(request, 'core/verify_otp.html', context)


def _resend_otp(request, Model, session_key, register_url, verify_url_name):
    if request.method != 'POST':
        return redirect(verify_url_name)
    user = _pending_user(request, Model, session_key)
    if user is None:
        return redirect(register_url)

    send_key = f'otpsend:{user.email}'
    recently = (user.otp_created_at and
                timezone.now() - user.otp_created_at < timedelta(seconds=OTP_RESEND_COOLDOWN_SEC))
    if recently:
        messages.warning(request, f'Please wait {OTP_RESEND_COOLDOWN_SEC} seconds before requesting another code.')
        return redirect(verify_url_name)
    if security.too_many(send_key, OTP_SENDS_PER_HOUR, 60):
        messages.error(request, 'Too many codes requested. Please try again in an hour.')
        return redirect(verify_url_name)

    otp = generate_otp()
    user.otp_code = otp
    user.otp_created_at = timezone.now()
    user.otp_attempts = 0
    user.save()
    security.record(send_key)
    if send_otp_email(user.email, user.name, otp):
        messages.success(request, 'A new code has been sent to your email.')
    else:
        messages.warning(request, 'We could not send the verification email right now. Please try again in a moment.')
    return redirect(verify_url_name)


def _customer_verified(request, customer):
    send_admin_notification('New customer registered on Haat',
                            f'{customer.name} ({customer.email}) just registered as a customer.')
    security.start_session(request, 'customer', customer.id)
    notify('customer', customer.id, 'Welcome to Haat! Browse local shops and place your first order.', '/products/')
    messages.success(request, 'Email verified! Welcome to Haat.')
    return redirect('product_list')


def _seller_verified(request, seller):
    send_admin_notification('New seller awaiting approval on Haat',
                            f'{seller.shop_name} ({seller.email}) just registered and is waiting for approval.')
    messages.success(request, 'Email verified! Your account now needs admin approval before you can log in.')
    return redirect('login_seller')


def verify_customer_otp(request):
    return _verify_otp(request, Customer, 'customer', 'pending_customer_id', 'register_customer', _customer_verified)


def resend_customer_otp(request):
    return _resend_otp(request, Customer, 'pending_customer_id', 'register_customer', 'verify_customer_otp')


def verify_seller_otp(request):
    return _verify_otp(request, Seller, 'seller', 'pending_seller_id', 'register_seller', _seller_verified)


def resend_seller_otp(request):
    return _resend_otp(request, Seller, 'pending_seller_id', 'register_seller', 'verify_seller_otp')


# ---------- Login / Logout ----------

_DUMMY_HASH = None


def _dummy_check(password):
    """Spend the same time hashing when the email doesn't exist (hides which emails are registered)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('not-a-real-password')
    check_password(password, _DUMMY_HASH)


def _login(request, Model, role, template):
    if request.method != 'POST':
        return render(request, template)

    email = (request.POST.get('email') or '').strip().lower()
    password = request.POST.get('password') or ''
    key = security.login_key(role, email)

    if security.too_many(key, security.LOGIN_LIMIT, security.LOGIN_WINDOW_MIN):
        return render(request, template, {
            'error': f'Too many failed attempts. Please try again in {security.LOGIN_WINDOW_MIN} minutes.'})

    user = Model.objects.filter(email__iexact=email).first()
    if user is None:
        _dummy_check(password)
    if user is None or not check_password(password, user.password):
        security.record(key)
        return render(request, template, {'error': 'Invalid email or password'})

    security.clear(key)

    if not user.is_verified:
        request.session['pending_%s_id' % role] = user.id
        messages.error(request, 'Please verify your email first.')
        return redirect('verify_%s_otp' % role)

    if role == 'seller':
        if user.is_suspended:
            return render(request, template, {'error': 'Your account has been suspended'})
        if not user.is_approved:
            return render(request, template, {'error': 'Your account is pending approval'})

    security.start_session(request, role, user.id)
    return redirect('seller_dashboard' if role == 'seller' else 'product_list')


def login_customer(request):
    return _login(request, Customer, 'customer', 'core/login_customer.html')


def login_seller(request):
    return _login(request, Seller, 'seller', 'core/login_seller.html')


def logout_user(request):
    if request.method != 'POST':          # logging out is a state change: POST + CSRF token only
        return redirect('home')
    request.session.flush()
    messages.success(request, 'You have been logged out.')
    return redirect('home')


# ---------- Products ----------

def clean_product_form(post):
    errors = []
    name = (post.get('name') or '').strip()
    description = (post.get('description') or '').strip()
    category = (post.get('category') or '').strip()
    price = parse_decimal(post.get('price'), minimum=Decimal('0.01'), maximum=Decimal('99999999.99'))

    if not name:
        errors.append('Product name is required.')
    elif len(name) > 150:
        errors.append('Product name is too long (max 150 characters).')
    if not description:
        errors.append('Description is required.')
    elif len(description) > 5000:
        errors.append('Description is too long (max 5000 characters).')
    if not category:
        errors.append('Category is required.')
    elif len(category) > 100:
        errors.append('Category is too long (max 100 characters).')
    if price is None:
        errors.append('Please enter a valid price greater than 0.')
    else:
        price = price.quantize(Decimal('0.01'))
    return {'name': name, 'description': description, 'category': category, 'price': price}, errors


def collect_photos(request, existing_extra=0):
    """Returns (main_photo, extra_photos, errors) with every file validated as a real image."""
    errors = []
    main = request.FILES.get('photo') or request.FILES.get('photo_camera')
    extra = request.FILES.getlist('images') + request.FILES.getlist('images_camera')
    if existing_extra + len(extra) > settings.MAX_EXTRA_PHOTOS:
        errors.append(f'You can have at most {settings.MAX_EXTRA_PHOTOS} additional photos per product.')
        extra = []
    for upload in ([main] if main else []) + extra:
        try:
            security.validate_image_upload(upload)
        except ValidationError as exc:
            errors.extend(exc.messages)
    return main, extra, errors


@seller_required
def add_product(request):
    if request.method != 'POST':
        return render(request, 'core/add_product.html')

    data, errors = clean_product_form(request.POST)
    photo, extra_images, photo_errors = collect_photos(request)
    errors += photo_errors

    if not errors:
        try:
            with transaction.atomic():
                product = Product.objects.create(seller=request.seller, photo=photo, **data)
                for img in extra_images:
                    ProductImage.objects.create(product=product, image=img)
        except ValueError:
            errors.append('One of the photos could not be processed. Please try a different photo.')
        else:
            messages.success(request, f'"{product.name}" was added successfully.')
            return redirect('seller_dashboard')

    return render(request, 'core/add_product.html', {'errors': errors, 'form': request.POST})


def product_list(request):
    products = visible_products()
    notices = []

    category = (request.GET.get('category') or '').strip()
    search = (request.GET.get('search') or '').strip()
    min_raw = (request.GET.get('min_price') or '').strip()
    max_raw = (request.GET.get('max_price') or '').strip()

    if category:
        products = products.filter(category__icontains=category)
    if search:
        products = products.filter(name__icontains=search)
    for raw, lookup, label in ((min_raw, 'price__gte', 'Min'), (max_raw, 'price__lte', 'Max')):
        if raw:
            value = parse_decimal(raw, minimum=Decimal('0'), maximum=Decimal('99999999.99'))
            if value is None:
                notices.append(f'"{label} ₹" was not a valid number and was ignored.')
            else:
                products = products.filter(**{lookup: value})

    products = products.select_related('seller').prefetch_related(
        'images',
        Prefetch('reviews', queryset=Review.objects.select_related('customer').order_by('-created_at')),
    ).order_by('-created_at', '-id')

    page = Paginator(products, 12).get_page(request.GET.get('page'))
    items = list(page.object_list)

    # One query for all shop ratings on this page (instead of two queries per product card).
    seller_ids = {p.seller_id for p in items}
    stats = {row['seller_id']: row for row in
             ShopReview.objects.filter(seller_id__in=seller_ids)
             .values('seller_id').annotate(avg=Avg('rating'), n=Count('id'))}
    wished = set()
    if request.session.get('customer_id'):
        wished = set(Wishlist.objects.filter(customer_id=request.session['customer_id'])
                     .values_list('product_id', flat=True))
    for p in items:
        row = stats.get(p.seller_id)
        p.shop_rating = round(row['avg'], 1) if row else None
        p.shop_review_count = row['n'] if row else 0
        reviews = list(p.reviews.all())
        p.review_count = len(reviews)
        p.latest_review = reviews[0] if reviews else None
        p.review_avg = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None
        p.in_wishlist = p.id in wished

    query = request.GET.copy()
    query.pop('page', None)
    return render(request, 'core/product_list.html', {
        'products': items, 'page': page, 'querystring': query.urlencode(), 'notices': notices,
    })


@seller_required
def edit_product(request, product_id):
    product = get_object_or_404(Product, id=product_id, seller=request.seller)

    if request.method != 'POST':
        return render(request, 'core/edit_product.html', {'product': product})

    data, errors = clean_product_form(request.POST)
    new_photo, extra_images, photo_errors = collect_photos(request, existing_extra=product.images.count())
    errors += photo_errors

    if not errors:
        old_photo_name = product.photo.name if product.photo else None
        try:
            with transaction.atomic():
                for attr, value in data.items():
                    setattr(product, attr, value)
                product.is_available = 'is_available' in request.POST
                if new_photo:
                    product.photo = new_photo
                product.save()
                for img in extra_images:
                    ProductImage.objects.create(product=product, image=img)
        except ValueError:
            errors.append('One of the photos could not be processed. Please try a different photo.')
        else:
            if new_photo and old_photo_name and old_photo_name != product.photo.name:
                product.photo.storage.delete(old_photo_name)      # don't leave the replaced image behind
            messages.success(request, f'"{product.name}" was updated.')
            return redirect('seller_dashboard')

    product.refresh_from_db()
    return render(request, 'core/edit_product.html', {'product': product, 'errors': errors, 'form': request.POST})


@seller_required
def delete_product_image(request, image_id):
    image = get_object_or_404(ProductImage, id=image_id, product__seller=request.seller)
    product_id = image.product_id
    if request.method == 'POST':
        image.delete()          # the post_delete signal removes the stored file as well
        messages.success(request, 'Photo removed.')
    return redirect('edit_product', product_id=product_id)


@seller_required
def delete_product(request, product_id):
    product = get_object_or_404(Product, id=product_id, seller=request.seller)

    if request.method == 'POST':
        name = product.name
        if product.orders.exists():
            # Deleting would also delete customers' order history (and complaints), so hide it instead.
            product.is_available = False
            product.save(update_fields=['is_available'])
            messages.success(request, f'"{name}" has past orders, so it was hidden from the shop instead of '
                                      'deleted (customers keep their order history).')
        else:
            product.delete()
            messages.success(request, f'"{name}" was deleted.')
        return redirect('seller_dashboard')

    return render(request, 'core/confirm_delete.html', {
        'message': f'Delete "{product.name}"? This cannot be undone.',
        'cancel_url': '/seller/dashboard/',
    })


# ---------- Orders ----------

@customer_required
def place_order(request, product_id):
    product = get_object_or_404(visible_products().select_related('seller'), id=product_id)
    customer = request.customer

    default_address = ', '.join(p for p in [customer.street_address, customer.city, customer.state] if p)
    if customer.pincode:
        default_address += f' - {customer.pincode}'
    form = {'quantity': 1, 'distance_zone': 'near', 'payment_method': 'online',
            'delivery_address': default_address}

    if request.method == 'POST':
        form = {
            'quantity': (request.POST.get('quantity') or '').strip(),
            'distance_zone': request.POST.get('distance_zone'),
            'payment_method': request.POST.get('payment_method'),
            'delivery_address': (request.POST.get('delivery_address') or '').strip(),
        }
        errors = []
        try:
            quantity = int(form['quantity'])
            if not 1 <= quantity <= MAX_QUANTITY:
                raise ValueError
        except ValueError:
            quantity = None
            errors.append(f'Quantity must be a whole number between 1 and {MAX_QUANTITY}.')
        if form['distance_zone'] not in DELIVERY_CHARGES:
            errors.append('Please choose a delivery distance.')
        if form['payment_method'] not in dict(Order.PAYMENT_CHOICES):
            errors.append('Please choose a payment method.')
        if not form['delivery_address']:
            errors.append('Please enter a delivery address.')
        elif len(form['delivery_address']) > 500:
            errors.append('Delivery address is too long (max 500 characters).')

        if not errors:
            zone = form['distance_zone']
            delivery_charge = DELIVERY_CHARGES[zone]
            estimated = timezone.localdate() + timedelta(days=DELIVERY_DAYS[zone])
            total_price = (product.price * quantity) + delivery_charge

            Order.objects.create(
                customer=customer, product=product, quantity=quantity, total_price=total_price,
                delivery_charge=delivery_charge, distance_zone=zone,
                payment_method=form['payment_method'], delivery_address=form['delivery_address'],
                estimated_delivery_date=estimated,
            )

            send_notification_email(
                product.seller.email,
                f'New order on Haat - {product.name}',
                f'{customer.name} just ordered {quantity} x {product.name}. Total: Rs. {total_price}. '
                f'Check your seller dashboard for details.')
            notify('seller', product.seller.id,
                   f'New order: {customer.name} ordered {quantity} x {product.name} (Rs. {total_price}).',
                   '/seller/dashboard/')
            notify('customer', customer.id,
                   f'Your order for {product.name} was placed. Estimated delivery: {estimated:%d %b}.',
                   '/my-orders/')
            messages.success(request, 'Order placed successfully!')
            return redirect('my_orders')

        return render(request, 'core/place_order.html', {'product': product, 'form': form, 'errors': errors})

    return render(request, 'core/place_order.html', {'product': product, 'form': form})


@seller_required
def seller_dashboard(request):
    seller = request.seller
    products = Product.objects.filter(seller=seller).order_by('-created_at')
    orders = (Order.objects.filter(product__seller=seller)
              .select_related('product', 'customer').order_by('-created_at'))
    return render(request, 'core/seller_dashboard.html', {
        'seller': seller, 'products': products, 'orders': orders,
    })


@seller_required
def update_order_status(request, order_id):
    order = get_object_or_404(Order.objects.select_related('customer', 'product'),
                              id=order_id, product__seller=request.seller)

    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status not in dict(Order.STATUS_CHOICES):
            messages.error(request, 'Please choose a valid status.')
        elif new_status != order.status:
            order.status = new_status
            order.save(update_fields=['status'])

            send_notification_email(
                order.customer.email,
                f'Your Haat order #{order.id} status: {order.get_status_display()}',
                f'Hi {order.customer.name}, your order for {order.product.name} is now '
                f'"{order.get_status_display()}".')
            notify('customer', order.customer.id,
                   f'Order #{order.id} ({order.product.name}) is now: {order.get_status_display()}.',
                   '/my-orders/')
            messages.success(request, f'Order #{order.id} status updated.')

    return redirect('seller_dashboard')


@customer_required
def my_orders(request):
    orders = (Order.objects.filter(customer=request.customer)
              .select_related('product__seller').order_by('-created_at'))
    return render(request, 'core/my_orders.html', {'orders': orders})


# ---------- Reviews (product) ----------

def _clean_rating(post):
    try:
        rating = int(post.get('rating'))
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 5 else None


@customer_required
def add_review(request, product_id):
    product = get_object_or_404(Product.objects.select_related('seller'), id=product_id)
    customer = request.customer

    if request.method == 'POST':
        rating = _clean_rating(request.POST)
        comment = (request.POST.get('comment') or '').strip()
        if rating is None:
            messages.error(request, 'Please choose a rating from 1 to 5.')
            return render(request, 'core/add_review.html', {'product': product})
        if len(comment) > 1000:
            messages.error(request, 'Your comment is too long (max 1000 characters).')
            return render(request, 'core/add_review.html', {'product': product})

        # One review per customer per product: submitting again updates it instead of stacking ratings.
        existing = Review.objects.filter(customer=customer, product=product).first()
        if existing:
            existing.rating, existing.comment = rating, comment
            existing.save()
        else:
            Review.objects.create(customer=customer, product=product, rating=rating, comment=comment)

        send_notification_email(
            product.seller.email, f'New review on {product.name}',
            f'{customer.name} left a {rating}-star review on {product.name}: "{comment}"')
        notify('seller', product.seller.id,
               f'{customer.name} left a {rating}-star review on {product.name}.', '/seller/dashboard/')
        messages.success(request, 'Review submitted, thank you!')
        return redirect('product_list')

    return render(request, 'core/add_review.html', {'product': product})


# ---------- Shop reviews (overall seller rating) ----------

@customer_required
def add_shop_review(request, seller_id):
    seller = get_object_or_404(Seller, id=seller_id, is_approved=True, is_suspended=False)
    customer = request.customer

    if request.method == 'POST':
        rating = _clean_rating(request.POST)
        comment = (request.POST.get('comment') or '').strip()
        if rating is None or len(comment) > 1000:
            messages.error(request, 'Please choose a rating from 1 to 5 (comment up to 1000 characters).')
            return render(request, 'core/add_shop_review.html', {'seller': seller})

        existing = ShopReview.objects.filter(customer=customer, seller=seller).first()
        if existing:
            existing.rating, existing.comment = rating, comment
            existing.save()
        else:
            ShopReview.objects.create(customer=customer, seller=seller, rating=rating, comment=comment)
        messages.success(request, f'Thanks for rating {seller.shop_name}!')
        return redirect('product_list')

    return render(request, 'core/add_shop_review.html', {'seller': seller})


# ---------- Wishlist ----------

@customer_required
def toggle_wishlist(request, product_id):
    # Changing the wishlist is a state change, so only POST (with CSRF token) is accepted;
    # a plain link click / prefetch / cross-site <img> can no longer modify it.
    product = get_object_or_404(Product, id=product_id)
    back = safe_next(request, '/products/')
    if request.method != 'POST':
        return redirect(back)

    existing = Wishlist.objects.filter(customer=request.customer, product=product)
    if existing.exists():
        existing.delete()
        messages.success(request, f'Removed "{product.name}" from wishlist.')
    else:
        Wishlist.objects.create(customer=request.customer, product=product)
        messages.success(request, f'Added "{product.name}" to wishlist.')
    return redirect(back)


@customer_required
def view_wishlist(request):
    wishlist_items = (Wishlist.objects.filter(customer=request.customer, product__in=visible_products())
                      .select_related('product__seller').order_by('-created_at'))
    return render(request, 'core/wishlist.html', {'wishlist_items': wishlist_items})


# ---------- Messaging ----------

MAX_CHAT_LENGTH = 1000


def _recent_messages(customer, seller):
    latest = Message.objects.filter(customer=customer, seller=seller).order_by('-created_at', '-id')[:200]
    return list(latest)[::-1]


@customer_required
def chat_with_seller(request, seller_id):
    customer = request.customer
    seller = get_object_or_404(Seller, id=seller_id, is_approved=True, is_suspended=False)

    if request.method == 'POST':
        content = (request.POST.get('content') or '').strip()
        if not content:
            messages.error(request, 'Please type a message first.')
        elif len(content) > MAX_CHAT_LENGTH:
            messages.error(request, f'Message too long (max {MAX_CHAT_LENGTH} characters).')
        else:
            Message.objects.create(customer=customer, seller=seller, sender='customer', content=content)
            notify('seller', seller.id, f'New message from {customer.name}.', f'/messages/customer/{customer.id}/')
        return redirect('chat_with_seller', seller_id=seller_id)

    return render(request, 'core/chat.html', {
        'chat_messages': _recent_messages(customer, seller),
        'chat_title': seller.shop_name,
        'me': 'customer',
    })


@seller_required
def chat_with_customer(request, customer_id):
    seller = request.seller
    customer = get_object_or_404(Customer, id=customer_id)

    # A seller may only talk to customers they actually deal with (an order or an existing chat).
    related = (Order.objects.filter(customer=customer, product__seller=seller).exists()
               or Message.objects.filter(customer=customer, seller=seller).exists())
    if not related:
        raise Http404('No conversation with this customer')

    if request.method == 'POST':
        content = (request.POST.get('content') or '').strip()
        if not content:
            messages.error(request, 'Please type a message first.')
        elif len(content) > MAX_CHAT_LENGTH:
            messages.error(request, f'Message too long (max {MAX_CHAT_LENGTH} characters).')
        else:
            Message.objects.create(customer=customer, seller=seller, sender='seller', content=content)
            notify('customer', customer.id, f'New message from {seller.shop_name}.', f'/messages/seller/{seller.id}/')
        return redirect('chat_with_customer', customer_id=customer_id)

    return render(request, 'core/chat.html', {
        'chat_messages': _recent_messages(customer, seller),
        'chat_title': customer.name,
        'me': 'seller',
    })


# ---------- Account deletion ----------

@seller_required
def delete_seller_account(request):
    seller = request.seller
    active = Order.objects.filter(product__seller=seller, status__in=ACTIVE_ORDER_STATUSES).count()

    if request.method == 'POST':
        if active:
            messages.error(request, f'You still have {active} open order(s). Mark them Delivered or Cancelled '
                                    'before deleting your shop.')
            return redirect('seller_dashboard')
        seller.delete()
        request.session.flush()
        messages.success(request, 'Your shop account has been deleted.')
        return redirect('home')

    message = 'Delete your shop account and all your products? This cannot be undone.'
    if active:
        message = (f'You have {active} open order(s). Finish or cancel them first - '
                   'the account cannot be deleted until then.')
    return render(request, 'core/confirm_delete.html', {
        'message': message, 'cancel_url': '/seller/dashboard/', 'disable_confirm': bool(active),
    })


# ---------- Complaints ----------

@customer_required
def file_complaint(request, order_id):
    order = get_object_or_404(Order.objects.select_related('product__seller'),
                              id=order_id, customer=request.customer)

    if request.method == 'POST':
        description = (request.POST.get('description') or '').strip()
        if not description or len(description) > 2000:
            messages.error(request, 'Please describe the issue (up to 2000 characters).')
            return render(request, 'core/file_complaint.html', {'order': order})

        Complaint.objects.create(order=order, customer=request.customer, description=description)

        send_notification_email(
            order.product.seller.email, f'Complaint filed on order #{order.id}',
            f'{request.customer.name} filed a complaint on order #{order.id} ({order.product.name}): '
            f'"{description}"')
        notify('seller', order.product.seller.id,
               f'Complaint filed by {request.customer.name} on order #{order.id} ({order.product.name}).',
               '/seller/dashboard/')
        messages.success(request, 'Complaint filed. We will look into it.')
        return redirect('my_orders')

    return render(request, 'core/file_complaint.html', {'order': order})
