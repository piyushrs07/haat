from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.hashers import make_password, check_password
from .models import Customer, Seller, Product, Order, Review, Wishlist, Message, Complaint
from django.http import HttpResponse

def home(request):
    return render(request, 'core/home.html')

def register_customer(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')
        phone = request.POST.get('phone')
        address = request.POST.get('address')

        if Customer.objects.filter(email=email).exists():
            return render(request, 'core/register_customer.html', {
                'error': 'This email is already registered. Please log in instead.'
            })

        Customer.objects.create(
            name=name,
            email=email,
            password=make_password(password),
            phone=phone,
            address=address
        )
        messages.success(request, 'Registration successful! Please log in.')
        return redirect('login_customer')

    return render(request, 'core/register_customer.html')

def register_seller(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')
        phone = request.POST.get('phone')
        shop_name = request.POST.get('shop_name')
        product_type = request.POST.get('product_type')
        address = request.POST.get('address')
        description = request.POST.get('description')

        if Seller.objects.filter(email=email).exists():
            return render(request, 'core/register_seller.html', {
                'error': 'This email is already registered. Please log in instead.'
            })

        Seller.objects.create(
            name=name,
            email=email,
            password=make_password(password),
            phone=phone,
            shop_name=shop_name,
            product_type=product_type,
            address=address,
            description=description
        )
        messages.success(request, 'Registration submitted! Your account needs approval before you can log in.')
        return redirect('login_seller')

    return render(request, 'core/register_seller.html')

def login_customer(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        try:
            customer = Customer.objects.get(email=email)
        except Customer.DoesNotExist:
            return render(request, 'core/login_customer.html', {'error': 'Invalid email or password'})

        if check_password(password, customer.password):
            request.session['customer_id'] = customer.id
            return redirect('product_list')
        return render(request, 'core/login_customer.html', {'error': 'Invalid email or password'})

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
        photo = request.FILES.get('photo')

        Product.objects.create(
            seller=seller,
            name=name,
            description=description,
            price=price,
            category=category,
            photo=photo
        )
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

        total_price = (product.price * quantity) + delivery_charge

        Order.objects.create(
            customer=customer,
            product=product,
            quantity=quantity,
            total_price=total_price,
            delivery_charge=delivery_charge,
            distance_zone=distance_zone,
            payment_method=payment_method,
            delivery_address=delivery_address
        )
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
        messages.success(request, f'Order #{order.id} status updated.')

    return redirect('seller_dashboard')

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
        messages.success(request, 'Review submitted, thank you!')
        return redirect('product_list')

    return render(request, 'core/add_review.html', {'product': product})

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
        return redirect('chat_with_customer', customer_id=customer_id)

    chat_messages = Message.objects.filter(customer=customer, seller=seller).order_by('created_at')
    return render(request, 'core/chat.html', {
        'chat_messages': chat_messages,
        'chat_title': customer.name,
        'me': 'seller',
    })

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
        if request.FILES.get('photo'):
            product.photo = request.FILES.get('photo')
        product.save()
        messages.success(request, f'"{product.name}" was updated.')
        return redirect('seller_dashboard')

    return render(request, 'core/edit_product.html', {'product': product})

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
        messages.success(request, 'Complaint filed. We will look into it.')
        return redirect('my_orders')

    return render(request, 'core/file_complaint.html', {'order': order})

def my_orders(request):
    customer_id = request.session.get('customer_id')
    if not customer_id:
        return redirect('login_customer')

    customer = Customer.objects.get(id=customer_id)
    orders = Order.objects.filter(customer=customer).order_by('-created_at')

    return render(request, 'core/my_orders.html', {'orders': orders})

def create_admin_temp(request):
    from django.contrib.auth.models import User
    if not User.objects.filter(username='admin').exists():
        User.objects.create_superuser('admin', 'admin@haat.com', 'Haat@2026')
        return HttpResponse("Superuser created! Username: admin, Password: Haat@2026")
    return HttpResponse("Superuser already exists.")