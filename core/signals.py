"""Delete the stored photo when a product / gallery image row is deleted.
Without this, StoredFile rows (image bytes in the DB) pile up forever."""
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import Product, ProductImage


@receiver(post_delete, sender=Product)
def delete_product_photo(sender, instance, **kwargs):
    if instance.photo:
        instance.photo.delete(save=False)


@receiver(post_delete, sender=ProductImage)
def delete_gallery_photo(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
