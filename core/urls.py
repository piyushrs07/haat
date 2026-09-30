from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('register/customer/', views.register_customer, name='register_customer'),
    path('register/seller/', views.register_seller, name='register_seller'),
    path('login/customer/', views.login_customer, name='login_customer'),
    path('login/seller/', views.login_seller, name='login_seller'),
    path('logout/', views.logout_user, name='logout'),
    path('seller/add-product/', views.add_product, name='add_product'),
    path('products/', views.product_list, name='product_list'),
    path('order/<int:product_id>/', views.place_order, name='place_order'),
    path('seller/dashboard/', views.seller_dashboard, name='seller_dashboard'),
    path('order/update-status/<int:order_id>/', views.update_order_status, name='update_order_status'),
    path('product/<int:product_id>/review/', views.add_review, name='add_review'),
    path('product/<int:product_id>/wishlist/', views.toggle_wishlist, name='toggle_wishlist'),
    path('wishlist/', views.view_wishlist, name='view_wishlist'),
    path('messages/seller/<int:seller_id>/', views.chat_with_seller, name='chat_with_seller'),
    path('messages/customer/<int:customer_id>/', views.chat_with_customer, name='chat_with_customer'),
    path('seller/product/edit/<int:product_id>/', views.edit_product, name='edit_product'),
    path('seller/product/delete/<int:product_id>/', views.delete_product, name='delete_product'),
    path('seller/delete-account/', views.delete_seller_account, name='delete_seller_account'),
    path('order/<int:order_id>/complaint/', views.file_complaint, name='file_complaint'),
    path('my-orders/', views.my_orders, name='my_orders'),
    path('create-admin-temp-xyz123/', views.create_admin_temp, name='create_admin_temp'),
]