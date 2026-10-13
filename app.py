import os
import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - installed in deployment environments
    psycopg = None
    dict_row = None

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["DEBUG"] = os.getenv("DEBUG", "False").lower() == "true"
app.config["DATABASE_PATH"] = os.getenv("DATABASE_PATH", str(Path(__file__).with_name("instance") / "quotes.db"))
app.config["DATABASE_URL"] = os.getenv("DATABASE_URL")
app.config["DEFAULT_BUSINESS_ID"] = os.getenv("DEFAULT_BUSINESS_ID")
app.config["SESSION_COOKIE_SECURE"] = os.getenv(
    "SESSION_COOKIE_SECURE", "True" if os.getenv("RENDER") else "False"
).lower() == "true"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=32 * 1024,
)
if os.getenv("RENDER") and app.config["SECRET_KEY"] == "dev-only-change-me":
    raise RuntimeError("SECRET_KEY must be set in production")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=[],
)

ALLOWED_PROPERTY_TYPES = {"home", "apartment", "office", "airbnb"}
ALLOWED_FREQUENCIES = {"one-off", "weekly", "biweekly", "monthly"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_PROPOSAL_LENGTH = 10_000
DEFAULT_PRICING = {
    "home": 110,
    "apartment": 85,
    "office": 160,
    "airbnb": 95,
    "extra_bedroom": 15,
    "extra_bathroom": 20,
    "weekly_discount": 18,
    "biweekly_discount": 10,
    "monthly_discount": 4,
}


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "cleaning-business"


def account_token(email: str, purpose: str) -> str:
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=f"account-{purpose}")
    return serializer.dumps(email)


def token_email(token: str, purpose: str, max_age: int) -> str | None:
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=f"account-{purpose}")
    try:
        return serializer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def send_verification_email(business: "Business") -> bool:
    token = account_token(business.email, "verify-email")
    link = request.url_root.rstrip("/") + url_for("verify_email", token=token)
    return send_email(
        business.email,
        "Verify your Get Cleaning Quote email",
        f"Hi {business.name},\n\nVerify your owner email address:\n{link}\n\n"
        "This link expires in 24 hours. If you did not create this account, you can ignore this message.",
    )


def send_email(to_address: str, subject: str, body: str, reply_to: str | None = None) -> bool:
    api_key = os.getenv("RESEND_API_KEY")
    mail_from = os.getenv("MAIL_FROM")
    if not api_key or not mail_from:
        app.logger.warning("Email not sent because RESEND_API_KEY or MAIL_FROM is not configured")
        return False
    payload = {
        "from": mail_from,
        "to": [to_address],
        "subject": re.sub(r"[\r\n]+", " ", subject).strip(),
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    email_request = UrlRequest(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "GetCleaningQuote/1.0 (+https://getcleaningquote.co)",
        },
        method="POST",
    )
    try:
        with urlopen(email_request, timeout=15) as response:
            return 200 <= response.status < 300
    except HTTPError as error:
        try:
            response_body = error.read().decode("utf-8", errors="replace")[:2000]
        except OSError:
            response_body = "Response body unavailable"
        app.logger.error(
            "Transactional email delivery failed: Resend returned HTTP %s: %s",
            error.code,
            response_body,
        )
        return False
    except (URLError, TimeoutError, OSError):
        app.logger.exception("Transactional email delivery failed")
        return False


def database_query(query: str, parameters: tuple = ()) -> tuple[str, tuple]:
    if app.config["DATABASE_URL"]:
        return query.replace("?", "%s"), parameters
    return query, parameters


@dataclass
class QuoteRequest:
    id: int
    business_id: int | None
    customer_name: str
    email: str
    phone: str
    property_type: str
    bedrooms: int
    bathrooms: int
    frequency: str
    notes: str
    status: str = "new"
    estimated_price: int | None = None
    proposal: str = ""
    created_at: str = ""
    confirmation_token: str = ""
    consent_at: str | None = None


@dataclass
class Business:
    id: int
    name: str
    email: str
    password_hash: str
    slug: str | None = None
    pricing_config: str = "{}"
    email_verified: bool = True


def business_pricing(business: Business) -> dict[str, int]:
    pricing = DEFAULT_PRICING.copy()
    try:
        saved_pricing = json.loads(business.pricing_config or "{}")
    except (TypeError, json.JSONDecodeError):
        saved_pricing = {}
    for key in pricing:
        value = saved_pricing.get(key)
        if isinstance(value, int):
            pricing[key] = value
    return pricing


def format_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return value
    return parsed.astimezone(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC").replace(" 0", " ")


app.jinja_env.filters["friendly_time"] = format_timestamp


def get_connection():
    if app.config["DATABASE_URL"]:
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is configured")
        return psycopg.connect(app.config["DATABASE_URL"], row_factory=dict_row)
    database_path = Path(app.config["DATABASE_PATH"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with get_connection() as connection:
        if app.config["DATABASE_URL"]:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS businesses (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    slug TEXT UNIQUE,
                    pricing_config TEXT NOT NULL DEFAULT '{}',
                    email_verified BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )
            connection.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS slug TEXT")
            connection.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS pricing_config TEXT NOT NULL DEFAULT '{}'")
            connection.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE")
            businesses_without_slugs = connection.execute("SELECT id, name FROM businesses WHERE slug IS NULL").fetchall()
            for business in businesses_without_slugs:
                connection.execute(
                    "UPDATE businesses SET slug = %s WHERE id = %s",
                    (f"{slugify(business['name'])}-{business['id']}", business["id"]),
                )
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS businesses_slug_idx ON businesses (slug)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quote_requests (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    business_id INTEGER REFERENCES businesses(id),
                    customer_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    property_type TEXT NOT NULL,
                    bedrooms INTEGER NOT NULL,
                    bathrooms INTEGER NOT NULL,
                    frequency TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    status TEXT NOT NULL,
                    estimated_price INTEGER,
                    proposal TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmation_token TEXT UNIQUE
                    ,consent_at TEXT
                )
                """
            )
            connection.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS confirmation_token TEXT")
            connection.execute("ALTER TABLE quote_requests ADD COLUMN IF NOT EXISTS consent_at TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS quote_requests_confirmation_token_idx ON quote_requests (confirmation_token)"
            )
            return
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quote_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER,
                customer_name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                property_type TEXT NOT NULL,
                bedrooms INTEGER NOT NULL,
                bathrooms INTEGER NOT NULL,
                frequency TEXT NOT NULL,
                notes TEXT NOT NULL,
                status TEXT NOT NULL,
                estimated_price INTEGER,
                proposal TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmation_token TEXT UNIQUE
                ,consent_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                slug TEXT UNIQUE,
                pricing_config TEXT NOT NULL DEFAULT '{}',
                email_verified INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(quote_requests)")}
        if "business_id" not in columns:
            connection.execute("ALTER TABLE quote_requests ADD COLUMN business_id INTEGER")
        if "confirmation_token" not in columns:
            connection.execute("ALTER TABLE quote_requests ADD COLUMN confirmation_token TEXT")
        if "consent_at" not in columns:
            connection.execute("ALTER TABLE quote_requests ADD COLUMN consent_at TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS quote_requests_confirmation_token_idx ON quote_requests (confirmation_token)"
        )
        business_columns = {row["name"] for row in connection.execute("PRAGMA table_info(businesses)")}
        if "slug" not in business_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN slug TEXT")
        if "pricing_config" not in business_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN pricing_config TEXT NOT NULL DEFAULT '{}'")
        if "email_verified" not in business_columns:
            connection.execute("ALTER TABLE businesses ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
        businesses_without_slugs = connection.execute("SELECT id, name FROM businesses WHERE slug IS NULL").fetchall()
        for business in businesses_without_slugs:
            connection.execute(
                "UPDATE businesses SET slug = ? WHERE id = ?",
                (f"{slugify(business['name'])}-{business['id']}", business["id"]),
            )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS businesses_slug_idx ON businesses (slug)")


def get_default_business_id() -> int:
    configured_id = app.config.get("DEFAULT_BUSINESS_ID")
    if configured_id:
        business = get_business(int(configured_id))
        if business is None:
            raise RuntimeError("DEFAULT_BUSINESS_ID does not identify an existing business")
        return business.id
    if os.getenv("RENDER"):
        raise RuntimeError("DEFAULT_BUSINESS_ID must be set in production")
    with get_connection() as connection:
        row = connection.execute(*database_query("SELECT id FROM businesses ORDER BY id LIMIT 1")).fetchone()
        if row:
            business_id = row["id"]
        else:
            cursor = connection.execute(
                *database_query(
                    "INSERT INTO businesses (name, email, password_hash, slug) VALUES (?, ?, ?, ?) RETURNING id",
                    ("Clean Co.", "demo@clean.co", generate_password_hash("demo-password"), "clean-co"),
                )
            )
            business_id = cursor.fetchone()["id"]
        connection.execute(*database_query("UPDATE quote_requests SET business_id = ? WHERE business_id IS NULL", (business_id,)))
        return business_id


def get_business_by_email(email: str) -> Business | None:
    with get_connection() as connection:
        row = connection.execute(*database_query("SELECT * FROM businesses WHERE email = ?", (email,))).fetchone()
    return Business(**dict(row)) if row else None


def get_business(business_id: int) -> Business | None:
    with get_connection() as connection:
        row = connection.execute(*database_query("SELECT * FROM businesses WHERE id = ?", (business_id,))).fetchone()
    return Business(**dict(row)) if row else None


def get_business_by_slug(slug: str) -> Business | None:
    with get_connection() as connection:
        row = connection.execute(*database_query("SELECT * FROM businesses WHERE slug = ?", (slug,))).fetchone()
    return Business(**dict(row)) if row else None


def available_business_slug(name: str) -> str:
    base_slug = slugify(name)
    candidate = base_slug
    suffix = 2
    while get_business_by_slug(candidate):
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def row_to_request(row) -> QuoteRequest:
    return QuoteRequest(**dict(row))


def get_quote_request(request_id: int) -> QuoteRequest | None:
    with get_connection() as connection:
        row = connection.execute(*database_query("SELECT * FROM quote_requests WHERE id = ?", (request_id,))).fetchone()
    return row_to_request(row) if row else None


def get_quote_request_by_token(token: str) -> QuoteRequest | None:
    with get_connection() as connection:
        row = connection.execute(
            *database_query("SELECT * FROM quote_requests WHERE confirmation_token = ?", (token,))
        ).fetchone()
    return row_to_request(row) if row else None


def get_quote_requests() -> list[QuoteRequest]:
    with get_connection() as connection:
        rows = connection.execute(*database_query("SELECT * FROM quote_requests ORDER BY id DESC")).fetchall()
    return [row_to_request(row) for row in rows]


def get_business_quote_requests(business_id: int) -> list[QuoteRequest]:
    with get_connection() as connection:
        rows = connection.execute(*database_query("SELECT * FROM quote_requests WHERE business_id = ? ORDER BY id DESC", (business_id,))).fetchall()
    return [row_to_request(row) for row in rows]


def get_owned_quote_request(request_id: int) -> QuoteRequest | None:
    quote_request = get_quote_request(request_id)
    if quote_request is None or quote_request.business_id != session.get("business_id"):
        return None
    return quote_request


init_db()


def estimate_price(business: Business, property_type: str, bedrooms: int, bathrooms: int, frequency: str) -> int:
    pricing = business_pricing(business)
    base = pricing.get(property_type, pricing["home"])
    size_adjustment = (
        max(0, bedrooms - 2) * pricing["extra_bedroom"]
        + max(0, bathrooms - 1) * pricing["extra_bathroom"]
    )
    discount = pricing.get(f"{frequency}_discount", 0)
    return round((base + size_adjustment) * (1 - discount / 100))


@app.get("/")
def home():
    return render_template("landing.html")


@app.get("/privacy")
def privacy():
    return render_template("legal.html", page="privacy")


@app.get("/terms")
def terms():
    return render_template("legal.html", page="terms")


@app.get("/b/<int:business_id>")
def business_home(business_id: int):
    business = get_business(business_id)
    if business is None:
        return "Business not found", 404
    return redirect(url_for("business_quote_page", slug=business.slug), code=301)


@app.get("/q/<slug>")
def business_quote_page(slug: str):
    business = get_business_by_slug(slug)
    if business is None:
        return "Business not found", 404
    return render_template("home.html", business_id=business.id, business=business)


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if request.method == "GET":
        return render_template("auth.html", mode="register")
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not name or len(name) > 120 or not EMAIL_PATTERN.fullmatch(email) or len(email) > 254 or len(password) < 10:
        return render_template("auth.html", mode="register", error="Enter a valid name, email, and password of at least 10 characters."), 400
    if get_business_by_email(email):
        return render_template("auth.html", mode="register", error="An account with that email already exists."), 400
    with get_connection() as connection:
        cursor = connection.execute(*database_query(
            "INSERT INTO businesses (name, email, password_hash, slug, email_verified) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (name, email, generate_password_hash(password), available_business_slug(name), False),
        ))
        business_id = cursor.fetchone()["id"]
    session["business_id"] = business_id
    business = get_business(business_id)
    delivered = send_verification_email(business)
    notice = "Account created. Check your email to verify your address." if delivered else "Account created. Use Settings to resend your verification email."
    return redirect(url_for("dashboard", notice=notice))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "GET":
        return render_template("auth.html", mode="login")
    email = request.form.get("email", "").strip().lower()
    business = get_business_by_email(email)
    if not business or not check_password_hash(business.password_hash, request.form.get("password", "")):
        return render_template("auth.html", mode="login", error="Email or password is incorrect."), 401
    session["business_id"] = business.id
    return redirect(url_for("dashboard"))


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("account_action.html", mode="forgot")
    email = request.form.get("email", "").strip().lower()
    business = get_business_by_email(email) if EMAIL_PATTERN.fullmatch(email) else None
    if business:
        token = account_token(business.email, "reset-password")
        link = request.url_root.rstrip("/") + url_for("reset_password", token=token)
        send_email(
            business.email,
            "Reset your Get Cleaning Quote password",
            f"Hi {business.name},\n\nReset your password:\n{link}\n\n"
            "This link expires in one hour. If you did not request it, you can ignore this message.",
        )
    return render_template("account_action.html", mode="forgot", sent=True)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token: str):
    email = token_email(token, "reset-password", 3600)
    business = get_business_by_email(email) if email else None
    if business is None:
        return render_template("account_action.html", mode="invalid"), 400
    if request.method == "GET":
        return render_template("account_action.html", mode="reset", token=token)
    password = request.form.get("password", "")
    if len(password) < 10:
        return render_template("account_action.html", mode="reset", token=token, error="Use at least 10 characters."), 400
    with get_connection() as connection:
        connection.execute(
            *database_query("UPDATE businesses SET password_hash = ? WHERE id = ?", (generate_password_hash(password), business.id))
        )
    session.clear()
    return redirect(url_for("login", reset="1"))


@app.get("/verify-email/<token>")
def verify_email(token: str):
    email = token_email(token, "verify-email", 86400)
    business = get_business_by_email(email) if email else None
    if business is None:
        return render_template("account_action.html", mode="invalid"), 400
    with get_connection() as connection:
        connection.execute(*database_query("UPDATE businesses SET email_verified = ? WHERE id = ?", (True, business.id)))
    return render_template("account_action.html", mode="verified")


@app.post("/resend-verification")
@limiter.limit("3 per hour")
def resend_verification():
    business_id = session.get("business_id")
    business = get_business(business_id) if business_id else None
    if business is None:
        return redirect(url_for("login"))
    if not business.email_verified:
        send_verification_email(business)
    return redirect(url_for("dashboard", notice="Verification email sent. Check your inbox."))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.post("/quote-requests")
@limiter.limit("10 per hour")
def create_quote_request():
    form = request.form
    try:
        business_id = int(form.get("business_id", ""))
        bedrooms = int(form.get("bedrooms", ""))
        bathrooms = int(form.get("bathrooms", ""))
    except (TypeError, ValueError):
        return "Please enter valid property details.", 400
    customer_name = form.get("customer_name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip()
    property_type = form.get("property_type", "")
    frequency = form.get("frequency", "")
    notes = form.get("notes", "").strip()
    consent = form.get("consent") == "yes"
    business = get_business(business_id)
    if (
        business is None
        or not customer_name
        or len(customer_name) > 120
        or not EMAIL_PATTERN.fullmatch(email)
        or len(email) > 254
        or not phone
        or len(phone) > 40
        or property_type not in ALLOWED_PROPERTY_TYPES
        or frequency not in ALLOWED_FREQUENCIES
        or not 0 <= bedrooms <= 20
        or not 1 <= bathrooms <= 20
        or len(notes) > 2000
        or not consent
    ):
        return "Please check the submitted quote details.", 400
    quote_request = QuoteRequest(
        id=0,
        business_id=business_id,
        customer_name=customer_name,
        email=email,
        phone=phone,
        property_type=property_type,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        frequency=frequency,
        notes=notes,
        created_at=datetime.now(timezone.utc).isoformat(),
        confirmation_token=secrets.token_urlsafe(32),
        consent_at=datetime.now(timezone.utc).isoformat(),
    )
    quote_request.estimated_price = estimate_price(
        business,
        quote_request.property_type,
        quote_request.bedrooms,
        quote_request.bathrooms,
        quote_request.frequency,
    )
    with get_connection() as connection:
        cursor = connection.execute(*database_query(
            """
            INSERT INTO quote_requests (
                business_id, customer_name, email, phone, property_type, bedrooms, bathrooms,
                frequency, notes, status, estimated_price, proposal, created_at, confirmation_token, consent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                quote_request.business_id,
                quote_request.customer_name,
                quote_request.email,
                quote_request.phone,
                quote_request.property_type,
                quote_request.bedrooms,
                quote_request.bathrooms,
                quote_request.frequency,
                quote_request.notes,
                quote_request.status,
                quote_request.estimated_price,
                quote_request.proposal,
                quote_request.created_at,
                quote_request.confirmation_token,
                quote_request.consent_at,
            ),
        ))
        quote_request.id = cursor.fetchone()["id"]
    if business:
        send_email(
            business.email,
            f"New quote request from {quote_request.customer_name}",
            f"{quote_request.customer_name} submitted a new cleaning quote request.\n\n"
            f"Property: {quote_request.property_type}\n"
            f"Bedrooms: {quote_request.bedrooms}\nBathrooms: {quote_request.bathrooms}\n"
            f"Frequency: {quote_request.frequency}\nEstimated price: ${quote_request.estimated_price}\n\n"
            f"Log in to review it: {request.url_root.rstrip('/')}{url_for('dashboard')}",
            reply_to=quote_request.email,
        )
        send_email(
            quote_request.email,
            f"Your quote request was received by {business.name}",
            f"Hi {quote_request.customer_name},\n\n"
            f"{business.name} received your cleaning quote request. Your starting estimate is "
            f"${quote_request.estimated_price}. They will review the details and follow up with a proposal.\n\n"
            f"This is a starting estimate, not a final or binding price.\n\n"
            f"Best,\n{business.name}",
            reply_to=business.email,
        )
    return redirect(url_for("request_received", token=quote_request.confirmation_token))


@app.get("/request-received/<token>")
def request_received(token: str):
    quote_request = get_quote_request_by_token(token)
    if quote_request is None:
        return "Quote request not found", 404
    return render_template("received.html", quote_request=quote_request)


@app.post("/request-received/<token>/accept")
@limiter.limit("10 per hour")
def accept_proposal(token: str):
    quote_request = get_quote_request_by_token(token)
    if quote_request is None or quote_request.status != "sent":
        return "Proposal not found or unavailable", 404
    with get_connection() as connection:
        connection.execute(*database_query("UPDATE quote_requests SET status = ? WHERE id = ?", ("accepted", quote_request.id)))
    return redirect(url_for("request_received", token=token))


@app.post("/request-received/<token>/decline")
@limiter.limit("10 per hour")
def decline_proposal(token: str):
    quote_request = get_quote_request_by_token(token)
    if quote_request is None or quote_request.status != "sent":
        return "Proposal not found or unavailable", 404
    with get_connection() as connection:
        connection.execute(*database_query("UPDATE quote_requests SET status = ? WHERE id = ?", ("declined", quote_request.id)))
    return redirect(url_for("request_received", token=token))


@app.get("/dashboard")
def dashboard():
    business_id = session.get("business_id")
    if not business_id:
        return redirect(url_for("login"))
    business = get_business(business_id)
    return render_template(
        "dashboard.html",
        requests=get_business_quote_requests(business_id),
        business=business,
        notice=request.args.get("notice"),
        error=request.args.get("error"),
    )


@app.route("/settings", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def settings():
    business_id = session.get("business_id")
    if not business_id:
        return redirect(url_for("login"))
    business = get_business(business_id)
    if business is None:
        session.clear()
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template(
            "settings.html",
            business=business,
            pricing=business_pricing(business),
            updated=request.args.get("updated") == "1",
        )

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    pricing_fields = {
        "home": (50, 1000),
        "apartment": (50, 1000),
        "office": (50, 2000),
        "airbnb": (50, 1000),
        "extra_bedroom": (0, 500),
        "extra_bathroom": (0, 500),
        "weekly_discount": (0, 50),
        "biweekly_discount": (0, 50),
        "monthly_discount": (0, 50),
    }
    pricing = {}
    current_pricing = business_pricing(business)
    try:
        for field, (minimum, maximum) in pricing_fields.items():
            submitted_value = request.form.get(field)
            value = current_pricing[field] if submitted_value is None else int(submitted_value)
            if not minimum <= value <= maximum:
                raise ValueError
            pricing[field] = value
    except (TypeError, ValueError):
        pricing = None
    error = None
    if not check_password_hash(business.password_hash, current_password):
        error = "Current password is incorrect."
    elif not name or len(name) > 120 or not EMAIL_PATTERN.fullmatch(email) or len(email) > 254:
        error = "Enter a valid business name and email address."
    elif new_password and len(new_password) < 10:
        error = "A new password must be at least 10 characters."
    elif pricing is None:
        error = "Enter valid prices and discounts within the displayed ranges."
    else:
        existing_business = get_business_by_email(email)
        if existing_business and existing_business.id != business.id:
            error = "That email address is already used by another account."
    if error:
        return render_template(
            "settings.html",
            business=business,
            pricing=pricing or business_pricing(business),
            error=error,
            form_name=name,
            form_email=email,
        ), 400

    password_hash = generate_password_hash(new_password) if new_password else business.password_hash
    email_verified = business.email_verified if email == business.email else False
    with get_connection() as connection:
        connection.execute(
            *database_query(
                "UPDATE businesses SET name = ?, email = ?, password_hash = ?, pricing_config = ?, email_verified = ? WHERE id = ?",
                (name, email, password_hash, json.dumps(pricing), email_verified, business.id),
            )
        )
    if not email_verified:
        send_verification_email(get_business(business.id))
    return redirect(url_for("settings", updated="1"))


@app.post("/settings/delete")
@limiter.limit("3 per hour")
def delete_account():
    business_id = session.get("business_id")
    business = get_business(business_id) if business_id else None
    if business is None:
        return redirect(url_for("login"))
    password = request.form.get("current_password", "")
    confirmation = request.form.get("confirmation", "").strip()
    if not check_password_hash(business.password_hash, password) or confirmation != "DELETE":
        return render_template(
            "settings.html",
            business=business,
            pricing=business_pricing(business),
            delete_error="Enter your current password and type DELETE exactly.",
        ), 400
    with get_connection() as connection:
        connection.execute(*database_query("DELETE FROM quote_requests WHERE business_id = ?", (business.id,)))
        connection.execute(*database_query("DELETE FROM businesses WHERE id = ?", (business.id,)))
    session.clear()
    return redirect(url_for("home", deleted="1"))


@app.post("/api/quote-requests/<int:request_id>/proposal")
def generate_proposal(request_id: int):
    quote_request = get_owned_quote_request(request_id)
    if quote_request is None:
        return jsonify({"error": "Quote request not found"}), 404
    business = get_business(quote_request.business_id)
    if business is None:
        return jsonify({"error": "Business not found"}), 404
    quote_request.proposal = (
        f"Hi {quote_request.customer_name},\n\n"
        f"Thanks for requesting a cleaning quote. Based on your {quote_request.property_type} "
        f"and {quote_request.frequency} service, we can help for an estimated ${quote_request.estimated_price}. "
        "This includes a careful clean of the agreed rooms and a quality check before we leave.\n\n"
        "Reply to confirm a convenient time, and we will send the final booking details.\n\n"
        f"Best,\n{business.name}"
    )
    with get_connection() as connection:
        connection.execute(*database_query(
            "UPDATE quote_requests SET proposal = ?, status = ? WHERE id = ?",
            (quote_request.proposal, "quoted", request_id),
        ))
    quote_request.status = "quoted"
    return jsonify({"proposal": quote_request.proposal, "status": quote_request.status})


@app.post("/quote-requests/<int:request_id>/proposal/save")
def save_proposal(request_id: int):
    quote_request = get_owned_quote_request(request_id)
    if quote_request is None:
        return "Quote request not found", 404
    proposal = request.form.get("proposal", "").strip()
    if not proposal or len(proposal) > MAX_PROPOSAL_LENGTH:
        return redirect(url_for("dashboard", error="Proposal must be between 1 and 10,000 characters."))
    with get_connection() as connection:
        connection.execute(
            *database_query("UPDATE quote_requests SET proposal = ?, status = ? WHERE id = ?", (proposal, "quoted", request_id))
        )
    return redirect(url_for("dashboard", notice="Proposal draft saved."))


@app.post("/quote-requests/<int:request_id>/proposal/send")
@limiter.limit("20 per hour")
def send_proposal(request_id: int):
    quote_request = get_owned_quote_request(request_id)
    if quote_request is None:
        return "Quote request not found", 404
    proposal = request.form.get("proposal", "").strip()
    if not proposal or len(proposal) > MAX_PROPOSAL_LENGTH:
        return redirect(url_for("dashboard", error="Proposal must be between 1 and 10,000 characters."))
    business = get_business(quote_request.business_id)
    if business is None:
        return "Business not found", 404
    delivered = send_email(
        quote_request.email,
        f"Your cleaning proposal from {business.name}",
        f"{proposal}\n\nReview and respond to this proposal:\n"
        f"{request.url_root.rstrip('/')}{url_for('request_received', token=quote_request.confirmation_token)}",
        reply_to=business.email,
    )
    if not delivered:
        return redirect(url_for("dashboard", error="Email could not be delivered. Check the email configuration and try again."))
    with get_connection() as connection:
        connection.execute(
            *database_query("UPDATE quote_requests SET proposal = ?, status = ? WHERE id = ?", (proposal, "sent", request_id))
        )
    return redirect(url_for("dashboard", notice="Proposal emailed to the customer."))


@app.get("/api/health")
def health():
    try:
        with get_connection() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        app.logger.exception("Database health check failed")
        return jsonify({"status": "error", "service": "Cleaning Quote Platform"}), 503
    return jsonify({"status": "ok", "service": "Cleaning Quote Platform"})


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https://images.unsplash.com data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(_error):
    return "The form expired or was invalid. Refresh the page and try again.", 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=app.config["DEBUG"])
