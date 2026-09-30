from django.contrib import admin
from .models import Customer, Seller, Product, Order, Review, Message, Wishlist, Complaint

admin.site.site_header = "Haat Admin"
admin.site.site_title = "Haat Admin Portal"
admin.site.index_title = "Welcome to Haat Administration"

admin.site.register(Customer)
admin.site.register(Seller)
admin.site.register(Product)
admin.site.register(Order)
admin.site.register(Review)
admin.site.register(Message)
admin.site.register(Wishlist)
admin.site.register(Complaint)