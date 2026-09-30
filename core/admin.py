from django.contrib import admin
from .models import Customer, Seller, Product, Order, Review, Message, Wishlist, Complaint

admin.site.register(Customer)
admin.site.register(Seller)
admin.site.register(Product)
admin.site.register(Order)
admin.site.register(Review)
admin.site.register(Message)
admin.site.register(Wishlist)
admin.site.register(Complaint)