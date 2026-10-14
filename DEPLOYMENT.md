# Haat - deployment guide (GitHub -> Render)

## 1. One-time Render setup (existing service: just edit its settings)

| Setting (Render dashboard)      | Value |
|---------------------------------|-------|
| Runtime                         | Python 3 |
| Build Command                   | `./build.sh` |
| Start Command                   | `gunicorn vendorapp.wsgi:application --workers 2 --threads 4 --timeout 60 --access-logfile - --log-file -` |
| Health Check Path               | `/healthz` |
| Python version                  | `.python-version` (3.14) is in the repo - nothing to do |

`render.yaml` in the repo holds the same settings if you prefer a Blueprint (it creates a *new* service).

### Environment variables (Render -> Environment)

| Variable | Required | Notes |
|----------|----------|-------|
| `SECRET_KEY` | **yes** | Long random string. The app now refuses to start in production without it. Generate: `python -c "import secrets;print(secrets.token_urlsafe(60))"` |
| `DATABASE_URL` | **yes** | Your Postgres connection string (keep the one you already use). |
| `BREVO_API_KEY` | **yes** | Without it no OTP email is sent, so nobody can finish registering. |
| `DEFAULT_FROM_EMAIL` | recommended | e.g. `Haat <orders@yourdomain.com>`. The sender must be verified in Brevo. A `@gmail.com` sender sent through Brevo is likely to land in spam / be rejected (DMARC) - use an address on a domain you own. |
| `ADMIN_EMAIL` | optional | Receives "new seller/customer" alerts. Defaults to the address that was hard-coded before. |
| `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_PASSWORD`, `DJANGO_SUPERUSER_EMAIL` | **yes, once** | Render's free plan has no shell, so the `/admin/` login is created from these on every deploy (`build.sh`). You approve new sellers at `/admin/core/seller/` -> select -> *Approve selected sellers*. |
| `DEBUG` | no | Leave unset: it is automatically **off** on Render. |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | only with a custom domain | Comma separated, e.g. `www.example.com` / `https://www.example.com`. The `*.onrender.com` hostname is added automatically. |

## 2. Deploy
1. Push this folder to GitHub (`git add . && git commit -m "Hardening" && git push`).
2. Render auto-deploys. `build.sh` installs packages, collects static files, runs migrations (the new migration `0009` only *adds* columns/tables - existing data is untouched), clears expired sessions and creates the admin user.
3. Open `https://<your-service>.onrender.com/healthz` -> `ok`.

## 3. Install on a phone (PWA)
* **Android / Chrome / Edge:** open the site -> tap the **Install the Haat app** button in the footer (or browser menu -> *Install app*).
* **iPhone / iPad:** open in **Safari** -> Share -> **Add to Home Screen**. (iOS has no install button; the footer shows the hint.)
* The app opens full-screen with its own icon. Offline it shows a friendly "You're offline" page (shopping needs a connection; pages with account data are deliberately never cached).

## 4. Things you must know about Render's FREE tier
* **Free Postgres expires 30 days after creation** (14-day grace period) and has **no backups**. This app stores *everything including product photos* in the database, so an expired database = all data and photos gone. Use a paid database or an external provider, and take regular `pg_dump` backups.
* A free web service **sleeps after 15 min idle**; the first visit afterwards takes up to ~1 minute. Upgrade to a paid instance to remove the delay.
* Outbound SMTP is blocked - that's why email goes through Brevo's HTTPS API (already implemented).

## 5. Local development
```
python -m venv venv && venv\Scripts\activate      # (source venv/bin/activate on Mac/Linux)
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```
No env vars are needed locally: DEBUG is on and a SQLite file is used. With no `BREVO_API_KEY`, the sign-up OTP is printed in the terminal. To keep using local MySQL: `pip install mysqlclient` and set `DATABASE_URL=mysql://root:root@localhost:3306/local_vendor_app`.
Run the tests: `python manage.py test`.
