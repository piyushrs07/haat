from django.contrib import admin, messages

from .mail import notify, send_notification_email
from .models import (Complaint, Customer, Message, Notification, Order, Product, ProductImage,
                     Review, Seller, ShopReview, Wishlist)

admin.site.site_header = "Haat Admin"
admin.site.site_title = "Haat Admin Portal"
admin.site.index_title = "Welcome to Haat Administration"


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    list_display = ('shop_name', 'name', 'email', 'phone', 'city', 'is_verified', 'is_approved',
                    'is_suspended', 'created_at')
    list_filter = ('is_approved', 'is_suspended', 'is_verified', 'city')
    search_fields = ('shop_name', 'name', 'email', 'phone')
    readonly_fields = ('password', 'otp_code', 'otp_created_at', 'otp_attempts', 'created_at')
    actions = ['approve_sellers', 'suspend_sellers', 'unsuspend_sellers']

    @admin.action(description='Approve selected sellers (and email them)')
    def approve_sellers(self, request, queryset):
        count = 0
        for seller in queryset.filter(is_approved=False, is_verified=True):
            seller.is_approved = True
            seller.save(update_fields=['is_approved'])
            send_notification_email(
                seller.email, 'Your Haat shop has been approved',
                f'Hi {seller.name}, your shop "{seller.shop_name}" has been approved. '
                'You can now log in as a seller and start adding products.\n\n- Haat')
            notify('seller', seller.id, 'Your shop was approved. Add your first product!', '/seller/add-product/')
            count += 1
        skipped = queryset.count() - count
        self.message_user(request, f'{count} seller(s) approved.' + (
            f' {skipped} skipped (already approved or email not verified).' if skipped else ''),
            messages.SUCCESS if count else messages.WARNING)

    @admin.action(description='Suspend selected sellers')
    def suspend_sellers(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_suspended=True)} seller(s) suspended.')

    @admin.action(description='Un-suspend selected sellers')
    def unsuspend_sellers(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_suspended=False)} seller(s) un-suspended.')


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'city', 'is_verified', 'created_at')
    list_filter = ('is_verified', 'city')
    search_fields = ('name', 'email', 'phone')
    readonly_fields = ('password', 'otp_code', 'otp_created_at', 'otp_attempts', 'created_at')


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'seller', 'category', 'price', 'is_available', 'created_at')
    list_filter = ('is_available', 'category')
    search_fields = ('name', 'seller__shop_name')
    inlines = [ProductImageInline]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'product', 'quantity', 'total_price', 'status', 'payment_method', 'created_at')
    list_filter = ('status', 'payment_method', 'distance_zone')
    search_fields = ('customer__name', 'customer__email', 'product__name')


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'customer', 'status', 'created_at')
    list_filter = ('status',)
    list_editable = ('status',)


admin.site.register(Review)
admin.site.register(ShopReview)
admin.site.register(Message)
admin.site.register(Wishlist)
admin.site.register(Notification)
