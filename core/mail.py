"""Email (Brevo HTTPS API) and in-app notification helpers."""
import logging
import secrets
import threading

import requests
from django.conf import settings

from .models import Notification

logger = logging.getLogger(__name__)


def generate_otp():
    return f'{secrets.randbelow(1_000_000):06d}'      # cryptographically secure, keeps leading zeros


def send_via_brevo(to_email, subject, message):
    """Send through Brevo's HTTPS API (Render's free plan blocks SMTP ports).
    Returns True on success, False on failure; never raises."""
    if not settings.BREVO_API_KEY:
        logger.error('BREVO_API_KEY is not set - email not sent to %s', to_email)
        return False

    from_name, from_email = 'Haat', settings.DEFAULT_FROM_EMAIL
    if '<' in from_email and '>' in from_email:
        from_name = from_email.split('<')[0].strip() or 'Haat'
        from_email = from_email.split('<')[1].split('>')[0].strip()

    try:
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'api-key': settings.BREVO_API_KEY, 'Content-Type': 'application/json',
                     'Accept': 'application/json'},
            json={'sender': {'name': from_name, 'email': from_email},
                  'to': [{'email': to_email}], 'subject': subject, 'textContent': message},
            timeout=10,
        )
        if response.status_code >= 400:
            logger.error('Brevo send failed (%s) to %s: %s', response.status_code, to_email, response.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        logger.error('Brevo send raised an exception for %s: %s', to_email, exc)
        return False


def send_otp_email(to_email, name, otp):
    subject = 'Your Haat verification code'
    message = f'Hi {name},\n\nYour verification code is: {otp}\n\nThis code expires in 10 minutes.\n\n- Haat'
    sent = send_via_brevo(to_email, subject, message)
    if not sent and settings.DEBUG and not settings.BREVO_API_KEY:
        # Local development only: lets you finish sign-up without configuring email.
        logger.warning('DEV ONLY - OTP for %s is %s', to_email, otp)
    return sent


def send_notification_email(to_email, subject, message):
    """Fire-and-forget so pages never wait on the email provider."""
    threading.Thread(target=send_via_brevo, args=(to_email, subject, message), daemon=True).start()
    return True


def send_admin_notification(subject, message):
    admin_email = getattr(settings, 'ADMIN_EMAIL', None)
    if admin_email:
        send_notification_email(admin_email, subject, message)


def notify(recipient_type, recipient_id, message, link=''):
    """In-app notification (bell icon). Never breaks the page if it fails."""
    try:
        Notification.objects.create(recipient_type=recipient_type, recipient_id=recipient_id,
                                    message=message[:255], link=link)
    except Exception as exc:                       # noqa: BLE001
        logger.error('Could not create notification: %s', exc)
