from .models import Notification


def notifications(request):
    """Adds `unread_count` (the bell badge in the navbar) to every template."""
    session = request.session
    if session.get('customer_id'):
        recipient_type, recipient_id = 'customer', session['customer_id']
    elif session.get('seller_id'):
        recipient_type, recipient_id = 'seller', session['seller_id']
    else:
        return {'unread_count': 0}

    count = Notification.objects.filter(
        recipient_type=recipient_type, recipient_id=recipient_id, is_read=False
    ).count()
    return {'unread_count': count}
