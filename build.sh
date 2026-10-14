#!/usr/bin/env bash
# Render build command:  ./build.sh
# Runs on every deploy. Fails the deploy (set -e) if anything goes wrong, so a broken build never goes live.
set -o errexit
set -o pipefail

pip install --upgrade pip
pip install -r requirements.txt

python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py clearsessions            # drop expired login sessions
python manage.py ensure_superuser         # creates /admin/ login if DJANGO_SUPERUSER_* env vars are set
