"""Shared security helpers: login-required decorators, rate limiting, upload validation."""
import functools
import io
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from .models import Customer, Seller, ThrottleEvent

# ---------- Session-based login (the app has its own Customer / Seller accounts) ----------


def start_session(request, role, user_id):
    """Log someone in. Rotates the session key (prevents session fixation) and makes sure
    only ONE role is ever active in a session."""
    request.session.cycle_key()
    for key in ('customer_id', 'seller_id', 'pending_customer_id', 'pending_seller_id'):
        request.session.pop(key, None)
    request.session[f'{role}_id'] = user_id


def customer_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        customer_id = request.session.get('customer_id')
        if not customer_id:
            return redirect('login_customer')
        customer = Customer.objects.filter(id=customer_id, is_verified=True).first()
        if customer is None:                       # account deleted since login
            request.session.flush()
            return redirect('login_customer')
        request.customer = customer
        return view(request, *args, **kwargs)
    return wrapper


def seller_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        seller_id = request.session.get('seller_id')
        if not seller_id:
            return redirect('login_seller')
        seller = Seller.objects.filter(id=seller_id, is_verified=True).first()
        if seller is None:
            request.session.flush()
            return redirect('login_seller')
        if seller.is_suspended or not seller.is_approved:
            # Suspension / un-approval takes effect immediately, not only at next login.
            request.session.flush()
            messages.error(request, 'Your shop account is not active. Please contact support.')
            return redirect('login_seller')
        request.seller = seller
        return view(request, *args, **kwargs)
    return wrapper


# ---------- Rate limiting ----------


def too_many(key, limit, window_minutes):
    since = timezone.now() - timedelta(minutes=window_minutes)
    return ThrottleEvent.objects.filter(key=key, created_at__gte=since).count() >= limit


def record(key):
    ThrottleEvent.objects.create(key=key)
    # Opportunistic cleanup so the table never grows without bound.
    if secrets.randbelow(50) == 0:
        ThrottleEvent.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()


def clear(key):
    ThrottleEvent.objects.filter(key=key).delete()


LOGIN_LIMIT = 6          # failed logins ...
LOGIN_WINDOW_MIN = 15    # ... per account per 15 minutes


def login_key(role, email):
    return f'login:{role}:{email.lower()[:150]}'


# ---------- Upload validation ----------


def validate_image_upload(uploaded):
    """Raise ValidationError unless `uploaded` is a real, reasonably sized image.
    (The model was previously saved without any check, so any file type was accepted.)"""
    if uploaded.size > settings.MAX_UPLOAD_BYTES:
        mb = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ValidationError(f'"{uploaded.name}" is too large (max {mb} MB per photo).')
    try:
        uploaded.seek(0)
        with Image.open(io.BytesIO(uploaded.read())) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValidationError(f'"{uploaded.name}" is not a valid image. Please upload a JPG, PNG or WebP photo.')
    finally:
        uploaded.seek(0)
