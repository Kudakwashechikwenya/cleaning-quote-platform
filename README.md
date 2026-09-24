# Cleaning Quote Platform

The first slice of a recurring-revenue product for cleaning businesses: capture a customer quote request, estimate a starting price, and prepare a proposal.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` for the default customer page. Each business has its own public quote page at `/b/<business_id>`, and the owner workspace is at `http://127.0.0.1:5000/dashboard` after login.

## Test

```bash
python -m unittest discover -s tests -v
```

Quote requests are stored in SQLite at `instance/quotes.db` by default, so they survive application restarts. Set `DATABASE_PATH` to use a different database location.

## Managed PostgreSQL

For production, create a managed PostgreSQL database with Render, Neon, Supabase, or another provider. Copy its connection string into the web service environment as `DATABASE_URL`.

Example environment variables:

```text
DATABASE_URL=postgresql://user:password@host:5432/database
SECRET_KEY=<long-random-secret>
DEBUG=False
RATELIMIT_STORAGE_URI=memory://
RESEND_API_KEY=<transactional-email-api-key>
MAIL_FROM=Get Cleaning Quote <quotes@getcleaningquote.co>
```

When `DATABASE_URL` is present, the app uses PostgreSQL instead of SQLite and creates or updates the required schema on startup. Do not commit the connection string. Each cleaning business receives an isolated workspace and a public quote page at `/q/<business-slug>`. For multiple web instances, point `RATELIMIT_STORAGE_URI` at a shared rate-limit store supported by Flask-Limiter rather than its in-memory default.

Transactional email uses Resend's HTTPS API because free Render web services block outbound SMTP ports. Verify the sending domain with Resend, then configure `RESEND_API_KEY` and `MAIL_FROM` in Render. Quote submissions remain available if email is temporarily unavailable, but proposals are only marked `sent` after successful delivery.

### Render setup

1. Create a PostgreSQL instance in Render.
2. Create a Web Service from this repository.
3. Set the build command to `pip install -r requirements.txt`.
4. Set the start command to `gunicorn --workers 2 --bind 0.0.0.0:$PORT app:app`.
5. Add `DATABASE_URL` using the database's internal connection URL.
6. Add a generated `SECRET_KEY` and set `DEBUG=False`.
7. Deploy and check `/api/health`.
8. Submit a quote, confirm its unguessable confirmation URL works, restart or redeploy, and confirm the request remains in the dashboard.

The current SQLite database is not automatically copied into PostgreSQL. For this early MVP, create fresh production data. Before launch, use a proper database dump/import process if existing customer data must be retained.

## Production process

Install dependencies and run the app with Gunicorn instead of Flask's development server:

```bash
pip install -r requirements.txt
gunicorn --workers 2 --bind 0.0.0.0:8000 app:app
```

Set a strong `SECRET_KEY` and `DEBUG=False` in the hosting provider's environment. The `.env.example` file lists the local configuration names. SQLite is suitable for local development; production deployment should use a managed PostgreSQL database before handling real customer data.
