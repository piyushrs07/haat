"""Creates (or updates the password of) the /admin/ login from environment variables.

Render's free plan has no shell, so `createsuperuser` can't be run by hand. build.sh calls this
on every deploy; it does nothing unless DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD
are set."""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create/update the admin superuser from DJANGO_SUPERUSER_* environment variables.'

    def handle(self, *args, **options):
        username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
        if not username or not password:
            self.stdout.write('ensure_superuser: DJANGO_SUPERUSER_USERNAME/PASSWORD not set - skipping.')
            return
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        user.is_staff = True
        user.is_superuser = True
        if email:
            user.email = email
        user.set_password(password)
        user.save()
        self.stdout.write(f'ensure_superuser: admin user "{username}" {"created" if created else "updated"}.')
