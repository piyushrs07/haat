import random
import logging
import threading
from datetime import timedelta

import requests
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.hashers import make_password, check_password
from django.conf import settings
from django.utils import timezone

from .models import (Customer, Seller, Product, ProductImage, Order, Review, ShopReview,
                     Wishlist, Message, Complaint, StoredFile, Notification)
from django.http import HttpResponse, Http404

logger = logging.getLogger(__name__)


# ---------- Helper functions ----------

def generate_otp():
    return str(random.randint(100000, 999999))


def _send_via_brevo(to_email, subject, message):
    """
    Sends email through Brevo's HTTPS API instead of SMTP, because Render's
    free web services block outbound SMTP ports (25/465/587) as of Sept 2025.
    Returns True on success, False on failure (never raises).
    """
    if not settings.BREVO_API_KEY:
        logger.error("BREVO_API_KEY is not set — email not sent to %s", to_email)
        return False

    # DEFAULT_FROM_EMAIL looks like "Haat <marketplacehaat@gmail.com>" — split it
    from_name, from_email = "Haat", settings.DEFAULT_FROM_EMAIL
    if '<' in settings.DEFAULT_FROM_EMAIL and '>' in settings.DEFAULT_FROM_EMAIL:
        from_name = settings.DEFAULT_FROM_EMAIL.split('<')[0].strip()
        from_email = settings.DEFAULT_FROM_EMAIL.split('<')[1].split('>')[0].strip()

    try:
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={
                'api-key': settings.BREVO_API_KEY,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            json={
                'sender': {'name': from_name, 'email': from_email},
                'to': [{'email': to_email}],
                'subject': subject,
                'textContent': message,
            },
            timeout=10,
        )
        if response.status_code >= 400:
            logger.error("Brevo send failed (%s) to %s: %s", response.status_code, to_email, response.text)
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Brevo send raised an exception for %s: %s", to_email, exc)
        return False


def send_otp_email(to_email, name, otp):
    subject = 'Your Haat verification code'
    message = f'Hi {name},\n\nYour verification code is: {otp}\n\nThis code expires in 10 minutes.\n\n- Haat'
    return _send_via_brevo(to_email, subject, message)


def send_notification_email(to_email, subject, message):
    """Fire-and-forget: sends in a background thread so pages never wait on email."""
    threading.Thread(target=_send_via_brevo, args=(to_email, subject, message), daemon=True).start()
    return True


def notify(recipient_type, recipient_id, message, link=''):
    """Creates an in-app notification (the bell icon). Never breaks the page if it fails."""
    try:
        Notification.objects.create(
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            message=message[:255],
            link=link,
        )
    except Exception as exc:
        logger.error("Could not create notification: %s", exc)


def current_recipient(request):
    if request.session.get('customer_id'):
        return 'customer', request.session['customer_id']
    if request.session.get('seller_id'):
        return 'seller', request.session['seller_id']
    return None, None


def send_admin_notification(subject, message):
    admin_email = getattr(settings, 'ADMIN_EMAIL', None)
    if admin_email:
        send_notification_email(admin_email, subject, message)


def otp_is_valid(otp_created_at):
    if not otp_created_at:
        return False
    return timezone.now() - otp_created_at <= timedelta(minutes=10)


# ---------- Home ----------

def home(request):
    available = Product.objects.filter(is_available=True)
    latest = available.select_related('seller').order_by('-id')[:6]
    categories = []
    for c in available.values_list('category', flat=True):
        c = (c or '').strip()
        if c and c.lower() not in [x.lower() for x in categories]:
            categories.append(c)
        if len(categories) >= 8:
            break
    return render(request, 'core/home.html', {
        'latest_products': latest,
        'categories': categories,
        'product_count': available.count(),
        'seller_count': Seller.objects.filter(is_approved=True, is_suspended=False).count(),
    })


# ---------- Media (photos stored in the database) ----------

def serve_media(request, path):
    try:
        stored = StoredFile.objects.get(name=path)
    except StoredFile.DoesNotExist:
        raise Http404("File not found")
    response = HttpResponse(bytes(stored.data), content_type=stored.content_type)
    response['Cache-Control'] = 'public, max-age=86400'
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


# ---------- Customer registration + OTP ----------

def register_customer(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')
        phone = request.POST.get('phone')
        street_address = request.POST.get('street_address')
        city = request.POST.get('city')
        state = request.POST.get('state')
        pincode = request.POST.get('pincode')

        if Customer.objects.filter(email=email).exists():
            return render(request, 'core/register_customer.html', {
                'error': 'This email is already registered. Please log in instead.'
            })

        otp = generate_otp()
        customer = Customer.objects.create(
            name=name,
            email=email,
            password=make_password(password),
            phone=phone,
            street_address=street_address,
            city=city,
            state=state,
            pincode=pincode,
            otp_code=otp,
            otp_created_at=timezone.now(),
        )
        email_sent = send_otp_email(email, name, otp)

        request.session['pending_customer_id'] = customer.id
        if email_sent:
            messages.success(request, 'A verification code has been sent to your email.')
        else:
            messages.warning(request, 'We could not send the verification email right now. Please try "Resend code" in a moment, or contact support if this keeps happening.')
        return redirect('verify_customer_otp')

    return render(request, 'core/register_customer.html')


def verify_customer_otp(request):
    pending_id = request.session.get('pending_customer_id')
    if not pending_id:
        return redirect('register_customer')

    customer = Customer.objects.get(id=pending_id)

    if request.method == 'POST':
        entered_otp = request.POST.get('otp')

        if not otp_is_valid(customer.otp_created_at):
            messages.error(request, 'Your code has expired. Please request a new one.')
            return render(request, 'core/verify_otp.html', {'email': customer.email})

        if entered_otp == customer.otp_code:
            customer.is_verified = True
            customer.otp_code = None
            customer.otp_created_at = None
            customer.save()
            del request.session['pending_customer_id']

            send_admin_notification(
                'New customer registered on Haat',
                f'{customer.name} ({customer.email}) just registered as a customer.'
            )

            request.session['customer_id'] = customer.id
            notify('customer', customer.id, 'Welcome to Haat! Browse local shops and place your first order.', '/products/')
            messages.success(request, 'Email verified! Welcome to Haat.')
            return redirect('product_list')

        messages.error(request, 'Incorrect code. Please try again.')

    return render(request, 'core/verify_otp.html', {'email': customer.email})


def resend_customer_otp(request):
    pending_id = request.session.get('pending_customer_id')
    if not pending_id:
        return redirect('register_customer')

    customer = Customer.objects.get(id=pending_id)
    otp = generate_otp()
    customer.otp_code = otp
    customer.otp_created_at = timezone.now()
    customer.save()
    email_sent = send_otp_email(customer.email, customer.name, otp)
    if email_sent:
        messages.success(request, 'A new code has been sent to your email.')
    else:
        messages.warning(request, 'We could not send the verification email right now. Please try again in a moment.')
    return redirect('verify_customer_otp')


# ---------- Seller registration + OTP ----------

def register_seller(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')
        phone = request.POST.get('phone')
        shop_name = request.POST.get('shop_name')
        product_type = request.POST.get('product_type')
        street_address = request.POST.get('street_address')
        city = request.POST.get('city')
        state = request.POST.get('state')
        pincode = request.POST.get('pincode')
        description = request.POST.get('description')

        if Seller.objects.filter(email=email).exists():
            return render(request, 'core/register_seller.html', {
                'error': 'This email is already registered. Please log in instead.'
            })

        otp = generate_otp()
        seller = Seller.objects.create(
            name=name,
            email=email,
            password=make_password(password),
            phone=phone,
            shop_name=shop_name,
            product_type=product_type,
            street_address=street_address,
            city=city,
            state=state,
            pincode=pincode,
            description=description,
            otp_code=otp,
            otp_created_at=timezone.now(),
        )
        email_sent = send_otp_email(email, name, otp)

        request.session['pending_seller_id'] = seller.id
        if email_sent:
            messages.success(request, 'A verification code has been sent to your email.')
        else:
            messages.warning(request, 'We could not send the verification email right now. Please try "Resend code" in a moment, or contact support if this keeps happening.')
        return redirect('verify_seller_otp')

    return render(request, 'core/register_seller.html')


def verify_seller_otp(request):
    pending_id = request.session.get('pending_seller_id')
    if not pending_id:
        return redirect('register_seller')

    seller = Seller.objects.get(id=pending_id)

    if request.method == 'POST':
        entered_otp = request.POST.get('otp')

        if not otp_is_valid(seller.otp_created_at):
            messages.error(request, 'Your code has expired. Please request a new one.')
            return render(request, 'core/verify_otp.html', {'email': seller.email})

        if entered_otp == seller.otp_code:
            seller.is_verified = True
            seller.otp_code = None
            seller.otp_created_at = None
            seller.save()
            del request.session['pending_seller_id']

            send_admin_notification(
                'New seller awaiting approval on Haat',
                f'{seller.shop_name} ({seller.email}) just registered and is waiting for approval.'
            )

            messages.success(request, 'Email verified! Your account now needs admin approval before you can log in.')
            return redirect('login_seller')

        messages.error(request, 'Incorrect code. Please try again.')

    return render(request, 'core/verify_otp.html', {'email': seller.email})


def resend_seller_otp(request):
    pending_id = request.session.get('pending_seller_id')
    if not pending_id:
        return redirect('register_seller')

    seller = Seller.objects.get(id=pending_id)
    otp = generate_otp()
    seller.otp_code = otp
    seller.otp_created_at = timezone.now()
    seller.save()
    email_sent = send_otp_email(seller.email, seller.name, otp)
    if email_sent:
        messages.success(request, 'A new code has been sent to your email.')
    else:
        messages.warning(request, 'We could not send the verification email right now. Please try again in a moment.')
    return redirect('verify_seller_otp')


# ---------- Login / Logout ----------

def login_customer(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        try:
            customer = Customer.objects.get(email=email)
        except Customer.DoesNotExist:
            return render(request, 'core/login_customer.html', {'error': 'Invalid email or password'})

        if not check_password(password, customer.password):
            return render(request, 'core/login_customer.html', {'error': 'Invalid email or password'})

        if not customer.is_verified:
            request.session['pending_customer_id'] = customer.id
            messages.error(request, 'Please verify your email first.')
            return redirect('verify_customer_otp')

        request.session['customer_id'] = customer.id
        return redirect('product_list')

    return render(request, 'core/login_customer.html')


def login_seller(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        try:
            seller = Seller.objects.get(email=email)
        except Seller.DoesNotExist:
            return render(request, 'core/login_seller.html', {'error': 'Invalid email or password'})

        if not check_password(password, seller.password):
            return render(request, 'core/login_seller.html', {'error': 'Invalid email or password'})

        if not seller.is_verified:
            request.session['pending_seller_id'] = seller.id
            messages.error(request, 'Please verify your email first.')
            return redirect('verify_seller_otp')
        if seller.is_suspended:
            return render(request, 'core/login_seller.html', {'error': 'Your account has been suspended'})
        if not seller.is_approved:
            return render(request, 'core/login_seller.html', {'error': 'Your account is pending approval'})

        request.session['seller_id'] = seller.id
        return redirect('seller_dashboard')

    return render(request, 'core/login_seller.html')


def logout_user(request):
    request.session.flush()
    messages.success(request, 'You have been logged out.')
    return redirect('home')


# ---------- Products ----------

def add_product(request):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    seller = Seller.objects.get(id=seller_id)

    if request.method == 'POST':
        name = request.POST.get('name')
        description = request.POST.get('description')
        price = request.POST.get('price')
        category = request.POST.get('category')
        photo = request.FILES.get('photo') or request.FILES.get('photo_camera')
        extra_images = request.FILES.getlist('images') + request.FILES.getlist('images_camera')

        product = Product.objects.create(
            seller=seller,
            name=name,
            description=description,
            price=price,
            category=category,
            photo=photo
        )

        for img in extra_images:
            ProductImage.objects.create(product=product, image=img)

        messages.success(request, f'"{name}" was added successfully.')
        return redirect('seller_dashboard')

    return render(request, 'core/add_product.html')


def product_list(request):
    products = Product.objects.filter(is_available=True)

    category = request.GET.get('category')
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    search = request.GET.get('search')

    if category:
        products = products.filter(category__icontains=category)
    if min_price:
        products = products.filter(price__gte=min_price)
    if max_price:
        products = products.filter(price__lte=max_price)
    if search:
        products = products.filter(name__icontains=search)

    return render(request, 'core/product_list.html', {'products': products})


def edit_product(request, product_id):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    product = Product.objects.get(id=product_id, seller_id=seller_id)

    if request.method == 'POST':
        product.name = request.POST.get('name')
        product.description = request.POST.get('description')
        product.price = request.POST.get('price')
        product.category = request.POST.get('category')
        new_photo = request.FILES.get('photo') or request.FILES.get('photo_camera')
        if new_photo:
            product.photo = new_photo
        product.save()

        extra_images = request.FILES.getlist('images') + request.FILES.getlist('images_camera')
        for img in extra_images:
            ProductImage.objects.create(product=product, image=img)

        messages.success(request, f'"{product.name}" was updated.')
        return redirect('seller_dashboard')

    return render(request, 'core/edit_product.html', {'product': product})


def delete_product_image(request, image_id):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    image = ProductImage.objects.get(id=image_id, product__seller_id=seller_id)
    product_id = image.product_id
    if request.method == 'POST':
        image.image.delete(save=False)
        image.delete()
        messages.success(request, 'Photo removed.')
    return redirect('edit_product', product_id=product_id)


def delete_product(request, product_id):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    product = Product.objects.get(id=product_id, seller_id=seller_id)

    if request.method == 'POST':
        name = product.name
        product.delete()
        messages.success(request, f'"{name}" was deleted.')
        return redirect('seller_dashboard')

    return render(request, 'core/confirm_delete.html', {
        'message': 'Delete "' + product.name + '"? This cannot be undone.',
        'cancel_url': '/seller/dashboard/',
    })


# ---------- Orders ----------

def place_order(request, product_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    product = Product.objects.get(id=product_id)

    if request.method == 'POST':
        quantity = int(request.POST.get('quantity', 1))
        payment_method = request.POST.get('payment_method')
        delivery_address = request.POST.get('delivery_address')
        distance_zone = request.POST.get('distance_zone')

        delivery_charges = {'near': 0, 'medium': 30, 'far': 60}
        delivery_charge = delivery_charges.get(distance_zone, 0)

        delivery_days = {'near': 1, 'medium': 3, 'far': 5}
        estimated_delivery_date = timezone.now().date() + timedelta(days=delivery_days.get(distance_zone, 3))

        total_price = (product.price * quantity) + delivery_charge

        order = Order.objects.create(
            customer=customer,
            product=product,
            quantity=quantity,
            total_price=total_price,
            delivery_charge=delivery_charge,
            distance_zone=distance_zone,
            payment_method=payment_method,
            delivery_address=delivery_address,
            estimated_delivery_date=estimated_delivery_date,
        )

        send_notification_email(
            product.seller.email,
            f'New order on Haat - {product.name}',
            f'{customer.name} just ordered {quantity} x {product.name}. Total: Rs. {total_price}. '
            f'Check your seller dashboard for details.'
        )

        notify('seller', product.seller.id,
               f'New order: {customer.name} ordered {quantity} x {product.name} (Rs. {total_price}).',
               '/seller/dashboard/')
        notify('customer', customer.id,
               f'Your order for {product.name} was placed. Estimated delivery: {estimated_delivery_date:%d %b}.',
               '/my-orders/')

        messages.success(request, 'Order placed successfully!')
        return redirect('my_orders')

    return render(request, 'core/place_order.html', {'product': product})


def seller_dashboard(request):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    seller = Seller.objects.get(id=seller_id)
    products = Product.objects.filter(seller=seller)
    orders = Order.objects.filter(product__seller=seller).order_by('-created_at')

    return render(request, 'core/seller_dashboard.html', {
        'seller': seller,
        'products': products,
        'orders': orders
    })


def update_order_status(request, order_id):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    order = Order.objects.get(id=order_id, product__seller_id=seller_id)

    if request.method == 'POST':
        new_status = request.POST.get('status')
        order.status = new_status
        order.save()

        send_notification_email(
            order.customer.email,
            f'Your Haat order #{order.id} status: {order.get_status_display()}',
            f'Hi {order.customer.name}, your order for {order.product.name} is now "{order.get_status_display()}".'
        )

        notify('customer', order.customer.id,
               f'Order #{order.id} ({order.product.name}) is now: {order.get_status_display()}.',
               '/my-orders/')

        messages.success(request, f'Order #{order.id} status updated.')

    return redirect('seller_dashboard')


def my_orders(request):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    orders = Order.objects.filter(customer=customer).order_by('-created_at')

    return render(request, 'core/my_orders.html', {'orders': orders})


# ---------- Reviews (product) ----------

def add_review(request, product_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    product = Product.objects.get(id=product_id)

    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment')

        Review.objects.create(
            customer=customer,
            product=product,
            rating=rating,
            comment=comment
        )

        send_notification_email(
            product.seller.email,
            f'New review on {product.name}',
            f'{customer.name} left a {rating}-star review on {product.name}: "{comment}"'
        )

        notify('seller', product.seller.id,
               f'{customer.name} left a {rating}-star review on {product.name}.',
               '/seller/dashboard/')

        messages.success(request, 'Review submitted, thank you!')
        return redirect('product_list')

    return render(request, 'core/add_review.html', {'product': product})


# ---------- Shop reviews (overall seller rating) ----------

def add_shop_review(request, seller_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    seller = Seller.objects.get(id=seller_id)

    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment')

        ShopReview.objects.create(
            customer=customer,
            seller=seller,
            rating=rating,
            comment=comment
        )
        messages.success(request, f'Thanks for rating {seller.shop_name}!')
        return redirect('product_list')

    return render(request, 'core/add_shop_review.html', {'seller': seller})


# ---------- Wishlist ----------

def toggle_wishlist(request, product_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    product = Product.objects.get(id=product_id)

    existing = Wishlist.objects.filter(customer=customer, product=product).first()
    if existing:
        existing.delete()
        messages.success(request, f'Removed "{product.name}" from wishlist.')
    else:
        Wishlist.objects.create(customer=customer, product=product)
        messages.success(request, f'Added "{product.name}" to wishlist.')

    return redirect('product_list')


def view_wishlist(request):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    wishlist_items = Wishlist.objects.filter(customer=customer)

    return render(request, 'core/wishlist.html', {'wishlist_items': wishlist_items})


# ---------- Messaging ----------

def chat_with_seller(request, seller_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    seller = Seller.objects.get(id=seller_id)

    if request.method == 'POST':
        content = request.POST.get('content')
        Message.objects.create(
            customer=customer,
            seller=seller,
            sender='customer',
            content=content
        )
        notify('seller', seller.id, f'New message from {customer.name}.', f'/messages/customer/{customer.id}/')
        return redirect('chat_with_seller', seller_id=seller_id)

    chat_messages = Message.objects.filter(customer=customer, seller=seller).order_by('created_at')
    return render(request, 'core/chat.html', {
        'chat_messages': chat_messages,
        'chat_title': seller.shop_name,
        'me': 'customer',
    })


def chat_with_customer(request, customer_id):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    seller = Seller.objects.get(id=seller_id)
    customer = Customer.objects.get(id=customer_id)

    if request.method == 'POST':
        content = request.POST.get('content')
        Message.objects.create(
            customer=customer,
            seller=seller,
            sender='seller',
            content=content
        )
        notify('customer', customer.id, f'New message from {seller.shop_name}.', f'/messages/seller/{seller.id}/')
        return redirect('chat_with_customer', customer_id=customer_id)

    chat_messages = Message.objects.filter(customer=customer, seller=seller).order_by('created_at')
    return render(request, 'core/chat.html', {
        'chat_messages': chat_messages,
        'chat_title': customer.name,
        'me': 'seller',
    })


# ---------- Account deletion ----------

def delete_seller_account(request):
    seller_id = request.session.get('seller_id')
    if not seller_id:
        return redirect('login_seller')

    seller = Seller.objects.get(id=seller_id)

    if request.method == 'POST':
        seller.delete()
        request.session.flush()
        messages.success(request, 'Your shop account has been deleted.')
        return redirect('home')

    return render(request, 'core/confirm_delete.html', {
        'message': 'Delete your shop account and all your products? This cannot be undone.',
        'cancel_url': '/seller/dashboard/',
    })


# ---------- Complaints ----------

def file_complaint(request, order_id):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    order = Order.objects.get(id=order_id, customer=customer)

    if request.method == 'POST':
        description = request.POST.get('description')
        Complaint.objects.create(
            order=order,
            customer=customer,
            description=description
        )

        send_notification_email(
            order.product.seller.email,
            f'Complaint filed on order #{order.id}',
            f'{customer.name} filed a complaint on order #{order.id} ({order.product.name}): "{description}"'
        )

        notify('seller', order.product.seller.id,
               f'Complaint filed by {customer.name} on order #{order.id} ({order.product.name}).',
               '/seller/dashboard/')

        messages.success(request, 'Complaint filed. We will look into it.')
        return redirect('my_orders')

    return render(request, 'core/file_complaint.html', {'order': order})