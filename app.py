import io
import csv
import os
import re
import hmac
import hashlib
import html
import json
import secrets
import smtplib
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from urllib.parse import urlencode
from pathlib import Path

from werkzeug.utils import secure_filename
import uuid

import psycopg2
import qrcode
import bcrypt
import requests
from authlib.integrations.flask_client import OAuth
from authlib.jose import jwt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import Flask, render_template, request, redirect, session, send_from_directory, send_file, url_for, abort, jsonify

from db_config import build_db_config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_REFRESH_EACH_REQUEST=True,
)


@app.before_request
def keep_authenticated_session_active():
    """Mantiene l'accesso nell'app installata, senza rendere persistenti i flussi anonimi."""
    if session.get("user_id"):
        session.permanent = True


@app.get("/manifest.webmanifest")
def pwa_manifest():
    response = send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/service-worker.js")
def pwa_service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response

@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def apple_enabled() -> bool:
    return all(os.environ.get(name) for name in (
        "APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"
    ))


def apple_client_secret() -> str | None:
    if not apple_enabled():
        return None
    now = int(time.time())
    private_key = os.environ["APPLE_PRIVATE_KEY"].replace("\\n", "\n")
    token = jwt.encode(
        {"alg": "ES256", "kid": os.environ["APPLE_KEY_ID"]},
        {
            "iss": os.environ["APPLE_TEAM_ID"],
            "iat": now,
            "exp": now + 15552000,
            "aud": "https://appleid.apple.com",
            "sub": os.environ["APPLE_CLIENT_ID"],
        },
        private_key,
    )
    return token.decode("utf-8") if isinstance(token, bytes) else token


def apple_state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.secret_key, salt="apple-sign-in")


oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

VOLUME_ROOT = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
UPLOAD_ROOT = os.environ.get("UPLOAD_DIR") or (os.path.join(VOLUME_ROOT, "uploads") if VOLUME_ROOT else os.path.join(app.root_path, "static", "uploads"))
UPLOAD_URL_PREFIX = os.environ.get("UPLOAD_URL_PREFIX", "/uploads").rstrip("/")
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
ALLERGEN_KEYWORDS = {
    "Glutine": ("farina", "frumento", "grano", "orzo", "segale", "avena", "farro", "pane", "pasta", "pizza"),
    "Crostacei": ("gamber", "scampo", "aragosta", "astice", "granchio"),
    "Uova": ("uovo", "uova", "maionese"),
    "Pesce": ("pesce", "tonno", "salmone", "acciuga", "acciughe", "merluzzo"),
    "Arachidi": ("arachide", "arachidi"),
    "Soia": ("soia", "tofu", "edamame"),
    "Latte": ("latte", "lattosio", "mozzarella", "formaggio", "burro", "panna", "yogurt"),
    "Frutta a guscio": ("mandorla", "nocciola", "noce", "noci", "pistacchio", "anacardo", "pecan", "macadamia"),
    "Sedano": ("sedano",),
    "Senape": ("senape",),
    "Semi di sesamo": ("sesamo",),
    "Solfiti": ("solfiti", "solfato", "vino"),
    "Lupini": ("lupino", "lupini"),
    "Molluschi": ("cozza", "cozze", "vongola", "vongole", "calamaro", "calamari", "polpo", "ostrica", "ostriche"),
}

def detect_allergens(product_name: str = "", ingredients: str = "") -> list[str]:
    """Rileva allergeni sia dal nome del prodotto sia dagli ingredienti."""
    text = f"{product_name or ''} {ingredients or ''}".lower()
    return [
        allergen for allergen, keywords in ALLERGEN_KEYWORDS.items()
        if any(re.search(r"(?<!\w)" + re.escape(keyword) + r"\w*", text) for keyword in keywords)
    ]


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def is_allowed_image(filename: str) -> bool:
    ext = os.path.splitext(filename.lower())[1]
    return ext in ALLOWED_IMAGE_EXT

def save_product_image(file_storage, shop_id: int) -> str:
    """
    Salva immagine nella cartella configurata (anche persistente) e restituisce il relativo URL pubblico.
    """
    if not file_storage or not file_storage.filename:
        return ""

    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Formato immagine non valido. Usa PNG/JPG/WEBP.")

    folder = os.path.join(UPLOAD_ROOT, f"negozio_{shop_id}", "prodotti")
    ensure_dir(folder)

    new_name = f"{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(folder, new_name)
    file_storage.save(abs_path)

    # path pubblico
    return f"{UPLOAD_URL_PREFIX}/negozio_{shop_id}/prodotti/{new_name}"


def save_shop_image(file_storage, shop_id: int, image_type: str) -> str:
    """Salva logo o copertina del negozio e restituisce il relativo URL pubblico."""
    if not file_storage or not file_storage.filename:
        raise ValueError("Seleziona un'immagine da caricare.")

    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ALLOWED_IMAGE_EXT or not (file_storage.mimetype or "").startswith("image/"):
        raise ValueError("Formato immagine non valido. Usa PNG, JPG o WEBP.")

    if image_type not in {"logo", "copertina"}:
        raise ValueError("Tipo di immagine non valido.")

    folder = os.path.join(UPLOAD_ROOT, f"negozio_{shop_id}", "branding")
    ensure_dir(folder)
    new_name = f"{image_type}_{uuid.uuid4().hex}{ext}"
    file_storage.save(os.path.join(folder, new_name))
    return f"{UPLOAD_URL_PREFIX}/negozio_{shop_id}/branding/{new_name}"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def request_fingerprint(email: str = "") -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    source = f"{forwarded or request.remote_addr or ''}|{email.strip().lower()}|{app.secret_key}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def login_is_limited(email: str) -> bool:
    fingerprint = request_fingerprint(email)
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM tentativi_login
                WHERE fingerprint=%s AND tentato_il > NOW() - INTERVAL '15 minutes'
            """, (fingerprint,))
            return cur.fetchone()[0] >= 10
    finally:
        conn.close()


def record_login_failure(email: str) -> None:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tentativi_login (fingerprint) VALUES (%s)", (request_fingerprint(email),))
                cur.execute("DELETE FROM tentativi_login WHERE tentato_il < NOW() - INTERVAL '24 hours'")
    finally:
        conn.close()


def clear_login_failures(email: str) -> None:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tentativi_login WHERE fingerprint=%s", (request_fingerprint(email),))
    finally:
        conn.close()


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False
    return hmac.compare_digest(password, stored)


def annual_expiry() -> date:
    return date.today() + timedelta(days=365)


def license_is_active(status, expiry) -> bool:
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    return status == "attiva" and bool(expiry) and expiry >= date.today()


def get_user_license_plan(user_id: int) -> str:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(piano, 'professional') FROM licenze_utenti WHERE id_utente=%s", (user_id,))
            row = cur.fetchone()
        return normalize_license_plan(row[0] if row else None)
    finally:
        conn.close()


def remaining_product_slots(user_id: int, shop_id: int) -> int | None:
    plan = get_user_license_plan(user_id)
    limit = LICENSE_PLANS[plan]["product_limit"]
    if limit is None:
        return None
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM prodotti WHERE id_negozio=%s", (shop_id,))
            used = cur.fetchone()[0]
        return max(0, limit - used)
    finally:
        conn.close()


PAYPAL_CURRENCY = "EUR"
PAYPAL_TRIAL_DAYS = 14
APP_TRIAL_DAYS = 14
LEGAL_TERMS_VERSION = "2026-09-09"
LEGAL_PRIVACY_VERSION = "2026-09-09"
LICENSE_PLANS = {
    "base": {"name": "Base", "price": "79.00", "product_limit": 100},
    "professional": {"name": "Professional", "price": "129.00", "product_limit": None},
}
VAT_RATE = Decimal("0.22")


def plan_price_with_vat(plan: str) -> str:
    """Prezzo PayPal lordo: il listino pubblico resta espresso al netto dell'IVA."""
    net = Decimal(LICENSE_PLANS[normalize_license_plan(plan)]["price"])
    return str((net * (Decimal("1.00") + VAT_RATE)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def record_registration_consents(cur, user_id: int, marketing: bool = False) -> None:
    """Registra separatamente i consensi necessari e quello marketing facoltativo."""
    user_agent = (request.headers.get("User-Agent") or "")[:500]
    rows = (
        (user_id, "termini", LEGAL_TERMS_VERSION, True, user_agent),
        (user_id, "privacy", LEGAL_PRIVACY_VERSION, True, user_agent),
        (user_id, "marketing", LEGAL_PRIVACY_VERSION, bool(marketing), user_agent),
    )
    cur.executemany(
        """
        INSERT INTO consensi_utenti (id_utente, tipo, versione, accettato, user_agent)
        VALUES (%s, %s, %s, %s, %s)
        """,
        rows,
    )


def registration_consent_from_form(provider: str = "manual") -> dict | None:
    if request.form.get("accept_legal") != "on":
        return None
    return {
        "legal": True,
        "marketing": request.form.get("accept_marketing") == "on",
        "terms_version": LEGAL_TERMS_VERSION,
        "privacy_version": LEGAL_PRIVACY_VERSION,
        "provider": provider,
        "created_at": int(time.time()),
    }


def oauth_registration_consent(provider: str) -> dict | None:
    consent = session.get("oauth_registration_consent")
    if not isinstance(consent, dict) or consent.get("provider") != provider:
        return None
    if int(time.time()) - int(consent.get("created_at", 0)) > 900:
        session.pop("oauth_registration_consent", None)
        return None
    return consent


def normalize_license_plan(value: str | None) -> str:
    return value if value in LICENSE_PLANS else "professional"


def paypal_plan_id(plan: str) -> str:
    """Restituisce il piano corrente; i piani annuali nel DB prevalgono sui vecchi ID d'ambiente."""
    plan = normalize_license_plan(plan)
    if os.environ.get("DATABASE_URL"):
        conn = psycopg2.connect(**build_db_config())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT valore FROM impostazioni_app WHERE chiave=%s", (f"paypal_plan_{plan}_id",))
                row = cur.fetchone()
            if row and row[0]:
                return row[0]
        except psycopg2.Error:
            pass
        finally:
            conn.close()
    if plan == "base":
        return os.environ.get("PAYPAL_PLAN_BASE_ID", "")
    return os.environ.get("PAYPAL_PLAN_PRO_ID") or os.environ.get("PAYPAL_PLAN_ID", "")


def paypal_configured(plan: str = "professional") -> bool:
    return bool(
        os.environ.get("PAYPAL_CLIENT_ID")
        and os.environ.get("PAYPAL_CLIENT_SECRET")
        and paypal_plan_id(plan)
    )


def paypal_base_url() -> str:
    mode = os.environ.get("PAYPAL_MODE", "sandbox").strip().lower()
    return "https://api-m.paypal.com" if mode == "live" else "https://api-m.sandbox.paypal.com"


def paypal_access_token() -> str:
    response = requests.post(
        f"{paypal_base_url()}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def paypal_get_subscription(subscription_id: str) -> dict:
    response = requests.get(
        f"{paypal_base_url()}/v1/billing/subscriptions/{subscription_id}",
        headers={"Authorization": f"Bearer {paypal_access_token()}", "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def paypal_cancel_subscription_by_id(subscription_id: str, reason: str) -> None:
    """Disattiva il rinnovo PayPal prima di revocare l'accesso locale."""
    if not subscription_id:
        return
    # Per la disdetta non serve l'ID di un piano: sono sufficienti le
    # credenziali API. Legare questo controllo a paypal_configured() bloccava
    # abbonamenti validi quando uno dei due piani commerciali non era salvato.
    if not os.environ.get("PAYPAL_CLIENT_ID") or not os.environ.get("PAYPAL_CLIENT_SECRET"):
        raise RuntimeError("Credenziali PayPal non configurate.")
    response = requests.post(
        f"{paypal_base_url()}/v1/billing/subscriptions/{subscription_id}/cancel",
        headers={"Authorization": f"Bearer {paypal_access_token()}", "Content-Type": "application/json"},
        json={"reason": reason}, timeout=20,
    )
    if response.status_code not in (200, 204):
        try:
            paypal_error = response.json().get("message") or response.json().get("name")
        except (ValueError, AttributeError):
            paypal_error = None
        detail = f"HTTP {response.status_code}" + (f" · {paypal_error}" if paypal_error else "")
        raise RuntimeError(f"PayPal non ha confermato la disdetta ({detail}).")


def paypal_verify_webhook(payload: dict) -> bool:
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return False
    verification = {
        "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO"),
        "cert_url": request.headers.get("PAYPAL-CERT-URL"),
        "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID"),
        "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG"),
        "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME"),
        "webhook_id": webhook_id,
        "webhook_event": payload,
    }
    response = requests.post(
        f"{paypal_base_url()}/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {paypal_access_token()}", "Content-Type": "application/json"},
        json=verification,
        timeout=20,
    )
    return response.ok and response.json().get("verification_status") == "SUCCESS"


def parse_paypal_date(value, fallback=None):
    if not value:
        return fallback
    try:
        # PayPal usa timestamp ISO 8601, normalmente con suffisso Z.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return fallback


def smtp_configured() -> bool:
    return all(os.environ.get(name) for name in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"))


def email_configured() -> bool:
    provider = os.environ.get("EMAIL_PROVIDER", "smtp").strip().lower()
    if provider == "resend":
        return all(os.environ.get(name, "").strip() for name in ("RESEND_API_KEY", "EMAIL_FROM"))
    return provider == "smtp" and smtp_configured()


def valid_email_address(value: str | None) -> bool:
    """Controllo minimo per gli indirizzi passati ai provider email."""
    return bool(value and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,63}", value.strip()))


def normalize_table_number_ranges(value: str | None) -> tuple[str, int] | None:
    """Accetta numeri/intervalli non sovrapposti, es. 1-30, 90-120."""
    compact = re.sub(r"\s+", "", value or "")
    if not compact or len(compact) > 160:
        return None
    intervals = []
    for part in compact.split(","):
        match = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > 10000:
            return None
        intervals.append((start, end))
    ordered = sorted(intervals)
    if any(next_start <= previous_end for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:])):
        return None
    quantity = sum(end - start + 1 for start, end in intervals)
    return ", ".join(f"{start}-{end}" if start != end else str(start) for start, end in intervals), quantity


def qr_quote_price(quantity: int, numbered: bool, nfc: bool) -> dict:
    """Sconti composti per blocchi: 10%, poi 9%, 8% e così via fino all'1%."""
    base_cents = 500 if numbered and nfc else 450 if numbered or nfc else 400
    discount_rates = [max(0, 10 - step) for step in range(quantity // 10)]
    unit_cents = base_cents
    for rate in discount_rates:
        if rate:
            # Arrotondamento commerciale al centesimo, identico a Math.round nel browser.
            unit_cents = (unit_cents * (100 - rate) + 50) // 100
    return {
        "base_cents": base_cents,
        "discount_rates": discount_rates,
        "discount_percent": round((1 - (unit_cents / base_cents)) * 100, 1),
        "unit_cents": unit_cents,
        "total_cents": unit_cents * quantity,
    }


def send_transactional_email(recipient: str, subject: str, body: str, reply_to: str | None = None) -> bool:
    """Invia email di servizio; gli errori non interrompono le operazioni del cliente."""
    if not email_configured() or not valid_email_address(recipient):
        return False
    subject = subject.replace("\r", " ").replace("\n", " ")[:180]
    if reply_to and valid_email_address(reply_to):
        reply_to = reply_to.replace("\r", "").replace("\n", "")[:254]
    else:
        reply_to = None
    if os.environ.get("EMAIL_PROVIDER", "smtp").strip().lower() == "resend":
        payload = {"from": os.environ["EMAIL_FROM"].strip(), "to": [recipient],
                   "subject": subject, "text": body}
        if reply_to:
            payload["reply_to"] = reply_to
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip()},
                json=payload, timeout=20,
            )
            if response.status_code == 200 and response.json().get("id"):
                app.logger.info("Email transazionale accettata da Resend")
                return True
            app.logger.error("Invio Resend non accettato (HTTP %s)", response.status_code)
            record_operational_error("email", f"Resend ha rifiutato l'invio: HTTP {response.status_code}")
        except (requests.RequestException, ValueError):
            # Non registrare credenziali, destinatari o link di recupero nei log.
            app.logger.error("Invio Resend fallito: rete o risposta non valida")
            record_operational_error("email", "Resend non raggiungibile o risposta non valida")
        return False
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:180]
    if reply_to:
        message["Reply-To"] = reply_to.replace("\r", "").replace("\n", "")[:254]
    message.set_content(body)
    host = os.environ["SMTP_HOST"]
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
                server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("Invio email transazionale non riuscito")
        record_operational_error("email", "Invio SMTP non riuscito")
        return False


def record_operational_error(area: str, message: str, user_id: int | None = None, severity: str = "errore") -> None:
    """Registra un errore consultabile senza salvare token, password o payload sensibili."""
    try:
        conn = psycopg2.connect(**build_db_config())
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO errori_operativi (area,gravita,messaggio,id_utente) VALUES (%s,%s,%s,%s)",
                                (area[:40], severity[:16], str(message)[:500], user_id))
        finally:
            conn.close()
    except Exception:
        app.logger.error("Impossibile registrare errore operativo nell'area %s", area)


def record_commercial_event(event: str, user_id: int | None = None, source: str | None = None) -> None:
    try:
        conn = psycopg2.connect(**build_db_config())
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO eventi_commerciali (evento,id_utente,provenienza) VALUES (%s,%s,%s)",
                                (event[:40], user_id, (source or "diretto")[:120]))
        finally:
            conn.close()
    except Exception:
        app.logger.warning("Evento commerciale non registrato: %s", event)


def commercial_source() -> str:
    return (request.args.get("utm_source") or request.headers.get("Referer") or "diretto")[:120]


def maybe_send_license_expiry_email(user_id: int) -> None:
    """Invia al massimo un promemoria per ciascuna soglia: 14, 7, 3 e 1 giorno."""
    if not email_configured():
        return
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.username, u.email, l.data_scadenza,
                       COALESCE(a.stato, '')='prova_locale'
                FROM utenti u
                JOIN licenze_utenti l ON l.id_utente=u.id
                LEFT JOIN abbonamenti_paypal a ON a.id_utente=u.id
                WHERE u.id=%s AND l.stato='attiva'
            """, (user_id,))
            row = cur.fetchone()
            if not row or not row[1] or not row[2]:
                return
            days = (row[2] - date.today()).days
            threshold = next((value for value in (1, 3, 7, 14) if days <= value), None) if days >= 0 else None
            if threshold is None:
                return
            notice_type = "trial" if row[3] else "licenza"
            reference = f"{row[2].isoformat()}:{threshold}"
            cur.execute("SELECT 1 FROM notifiche_email WHERE id_utente=%s AND tipo=%s AND riferimento=%s", (user_id, notice_type, reference))
            if cur.fetchone():
                return
        label = "La prova gratuita" if row[3] else "La licenza Alpha Menu"
        action = "Scegli il piano Base o Professional" if row[3] else "Controlla il rinnovo dalla dashboard"
        sent = send_transactional_email(
            row[1],
            f"{label} scade tra {days} giorn{'o' if days == 1 else 'i'}",
            f"Ciao {row[0]},\n\n{label.lower()} scade il {row[2].strftime('%d/%m/%Y')}.\n{action}: https://menu.alphasystemsrl.it/dashboard_user#licenze\n\nAlpha Menu – Alpha System S.r.l.",
        )
        if sent:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO notifiche_email (id_utente,tipo,riferimento) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (user_id, notice_type, reference))
    finally:
        conn.close()


def trigger_license_expiry_email(user_id: int) -> None:
    try:
        maybe_send_license_expiry_email(user_id)
    except Exception:
        app.logger.exception("Controllo promemoria licenza non riuscito")


_last_daily_reminder_check: date | None = None


def process_daily_license_reminders() -> None:
    """Esegue una sola scansione al giorno, anche con più processi applicativi."""
    global _last_daily_reminder_check
    today = date.today()
    if not email_configured():
        return
    conn = psycopg2.connect(**build_db_config())
    completed = False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (7281451,))
                if not cur.fetchone()[0]:
                    return
                cur.execute("SELECT valore FROM impostazioni_app WHERE chiave='ultima_scansione_promemoria'")
                previous = cur.fetchone()
                if previous and previous[0] == today.isoformat():
                    completed = True
                    return
                cur.execute("""
                    SELECT id_utente
                    FROM licenze_utenti
                    WHERE stato='attiva'
                      AND data_scadenza BETWEEN CURRENT_DATE AND CURRENT_DATE + 14
                    ORDER BY data_scadenza, id_utente
                """)
                user_ids = [row[0] for row in cur.fetchall()]
                for user_id in user_ids:
                    trigger_license_expiry_email(user_id)
                cur.execute("""
                    INSERT INTO impostazioni_app (chiave,valore,updated_at)
                    VALUES ('ultima_scansione_promemoria',%s,NOW())
                    ON CONFLICT (chiave) DO UPDATE SET valore=EXCLUDED.valore,updated_at=NOW()
                """, (today.isoformat(),))
                completed = True
    finally:
        conn.close()
        if completed:
            _last_daily_reminder_check = today
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return fallback


SUPPORTED_MENU_LANGUAGES = {
    "en": "English", "fr": "Français", "de": "Deutsch", "es": "Español",
    "pt": "Português", "nl": "Nederlands", "pl": "Polski", "ro": "Română", "zh": "中文"
}

MENU_UI = {
    "it": {"venue": "Il nostro locale", "contacts": "Contatti", "show": "VISUALIZZA IL MENU'", "back": "Torna alle informazioni", "hours": "Orari di apertura", "closed": "Chiuso", "empty": "Il menu sarà disponibile presto.", "categories": "Categorie del menu", "days": ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]},
    "en": {"venue": "Our venue", "contacts": "Contacts", "show": "VIEW MENU", "back": "Back to information", "hours": "Opening hours", "closed": "Closed", "empty": "The menu will be available soon.", "categories": "Menu categories", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
    "fr": {"venue": "Notre établissement", "contacts": "Contacts", "show": "VOIR LE MENU", "back": "Retour aux informations", "hours": "Horaires d'ouverture", "closed": "Fermé", "empty": "Le menu sera bientôt disponible.", "categories": "Catégories du menu", "days": ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]},
    "de": {"venue": "Unser Lokal", "contacts": "Kontakte", "show": "MENÜ ANZEIGEN", "back": "Zurück zu den Informationen", "hours": "Öffnungszeiten", "closed": "Geschlossen", "empty": "Das Menü ist bald verfügbar.", "categories": "Menükategorien", "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]},
    "es": {"venue": "Nuestro local", "contacts": "Contactos", "show": "VER EL MENÚ", "back": "Volver a la información", "hours": "Horario de apertura", "closed": "Cerrado", "empty": "El menú estará disponible pronto.", "categories": "Categorías del menú", "days": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]},
    "pt": {"venue": "O nosso espaço", "contacts": "Contactos", "show": "VER MENU", "back": "Voltar às informações", "hours": "Horário de funcionamento", "closed": "Fechado", "empty": "O menu estará disponível em breve.", "categories": "Categorias do menu", "days": ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"]},
    "nl": {"venue": "Onze zaak", "contacts": "Contact", "show": "BEKIJK MENU", "back": "Terug naar informatie", "hours": "Openingstijden", "closed": "Gesloten", "empty": "Het menu is binnenkort beschikbaar.", "categories": "Menucategorieën", "days": ["Maandag", "Dinsdag", "Woensdag", "Donderdag", "Vrijdag", "Zaterdag", "Zondag"]},
    "pl": {"venue": "Nasz lokal", "contacts": "Kontakt", "show": "ZOBACZ MENU", "back": "Powrót do informacji", "hours": "Godziny otwarcia", "closed": "Zamknięte", "empty": "Menu będzie dostępne wkrótce.", "categories": "Kategorie menu", "days": ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]},
    "ro": {"venue": "Localul nostru", "contacts": "Contacte", "show": "VEZI MENIUL", "back": "Înapoi la informații", "hours": "Program", "closed": "Închis", "empty": "Meniul va fi disponibil în curând.", "categories": "Categorii meniu", "days": ["Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă", "Duminică"]},
    "zh": {"venue": "我们的餐厅", "contacts": "联系方式", "show": "查看菜单", "back": "返回信息", "hours": "营业时间", "closed": "休息", "empty": "菜单即将上线。", "categories": "菜单分类", "days": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]},
}

for _code, (_call, _whatsapp, _book, _booking_message) in {
    "it": ("Chiama", "Scrivi su WhatsApp", "Prenota", "Ciao, vorrei prenotare un tavolo."),
    "en": ("Call", "Message on WhatsApp", "Book", "Hello, I would like to book a table."),
    "fr": ("Appeler", "Écrire sur WhatsApp", "Réserver", "Bonjour, je voudrais réserver une table."),
    "de": ("Anrufen", "Über WhatsApp schreiben", "Reservieren", "Hallo, ich möchte einen Tisch reservieren."),
    "es": ("Llamar", "Escribir por WhatsApp", "Reservar", "Hola, me gustaría reservar una mesa."),
    "pt": ("Ligar", "Escrever no WhatsApp", "Reservar", "Olá, gostaria de reservar uma mesa."),
    "nl": ("Bellen", "WhatsApp sturen", "Reserveren", "Hallo, ik wil graag een tafel reserveren."),
    "pl": ("Zadzwoń", "Napisz na WhatsApp", "Zarezerwuj", "Dzień dobry, chcę zarezerwować stolik."),
    "ro": ("Sună", "Scrie pe WhatsApp", "Rezervă", "Bună ziua, aș dori să rezerv o masă."),
    "zh": ("致电", "WhatsApp 联系", "预订", "您好，我想预订一张桌子。"),
}.items():
    MENU_UI[_code]["call"] = _call
    MENU_UI[_code]["whatsapp"] = _whatsapp
    MENU_UI[_code]["book"] = _book
    MENU_UI[_code]["booking_message"] = _booking_message

for _code, (_cover, _print, _sold_out) in {
    "it": ("Coperto", "Stampa menu A4", "Esaurito"),
    "en": ("Cover charge", "Print A4 menu", "Sold out"),
    "fr": ("Couvert", "Imprimer le menu A4", "Épuisé"),
    "de": ("Gedeck", "A4-Menü drucken", "Ausverkauft"),
    "es": ("Cubierto", "Imprimir menú A4", "Agotado"),
    "pt": ("Couvert", "Imprimir menu A4", "Esgotado"),
    "nl": ("Couvert", "A4-menu afdrukken", "Uitverkocht"),
    "pl": ("Opłata za nakrycie", "Drukuj menu A4", "Wyprzedane"),
    "ro": ("Taxă de masă", "Tipărește meniul A4", "Indisponibil"),
    "zh": ("餐位费", "打印 A4 菜单", "售罄"),
}.items():
    MENU_UI[_code]["cover"] = _cover
    MENU_UI[_code]["print"] = _print
    MENU_UI[_code]["sold_out"] = _sold_out


def google_translate_texts(texts: list[str], target: str) -> list[str]:
    api_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if not api_key:
        raise RuntimeError("Configura GOOGLE_TRANSLATE_API_KEY su Railway.")
    if not texts:
        return []
    response = requests.post(
        "https://translation.googleapis.com/language/translate/v2",
        params={"key": api_key},
        json={"q": texts, "source": "it", "target": target, "format": "text"},
        timeout=30,
    )
    if not response.ok:
        detail = response.json().get("error", {}).get("message", "Errore Google Translate.")
        raise RuntimeError(detail)
    rows = response.json().get("data", {}).get("translations", [])
    return [html.unescape(row.get("translatedText", "")) for row in rows]


def google_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def translate_new_product(shop_id: int, product_id: int, fields: list[tuple[str, str]]) -> int:
    """Traduce un nuovo prodotto nelle lingue già abilitate per il negozio."""
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT codice FROM lingue_negozio WHERE id_negozio=%s ORDER BY codice", (shop_id,))
                languages = [row[0] for row in cur.fetchall() if row[0] in SUPPORTED_MENU_LANGUAGES]
                entries = [(field, value) for field, value in fields if value]
                total = 0
                for language in languages:
                    translated = google_translate_texts([value for _, value in entries], language)
                    for (field, original), translated_text in zip(entries, translated):
                        if original.isupper():
                            translated_text = translated_text.upper()
                        cur.execute("""
                            INSERT INTO traduzioni_menu (id_negozio,tipo,id_entita,campo,lingua,testo,testo_originale)
                            VALUES (%s,'prodotto',%s,%s,%s,%s,%s)
                            ON CONFLICT (id_negozio,tipo,id_entita,campo,lingua)
                            DO UPDATE SET testo=EXCLUDED.testo,testo_originale=EXCLUDED.testo_originale,updated_at=NOW()
                        """, (shop_id, product_id, field, language, translated_text, original))
                        total += 1
                return total
    finally:
        conn.close()


@app.context_processor
def auth_provider_flags():
    return {"google_enabled": google_enabled(), "apple_enabled": apple_enabled(), "csrf_token": csrf_token}


def init_db() -> None:
    schema_path = Path(__file__).resolve().parent / "db" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
                # Tabella di collegamento tra l'account legacy e il suo negozio.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS negozi (
                        id SERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL UNIQUE,
                        nome TEXT NOT NULL,
                        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS etichette TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS allergeni_auto TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS indirizzo TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS citta TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS cap TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS provincia TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS telefono TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS nazione TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS descrizione_breve TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS descrizione_estesa TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS logo_url TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS copertina_url TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS colore_accento TEXT NOT NULL DEFAULT '#9d3e27'")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS colore_sfondo TEXT NOT NULL DEFAULT '#f7f3ed'")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS costo_coperto NUMERIC(10,2) NOT NULL DEFAULT 0")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS orari_negozio (
                        id SERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        giorno SMALLINT NOT NULL CHECK (giorno BETWEEN 0 AND 6),
                        aperto BOOLEAN NOT NULL DEFAULT FALSE,
                        apertura TIME,
                        chiusura TIME,
                        apertura_2 TIME,
                        chiusura_2 TIME,
                        UNIQUE (id_negozio, giorno)
                    )
                """)
                cur.execute("ALTER TABLE orari_negozio ADD COLUMN IF NOT EXISTS apertura_2 TIME")
                cur.execute("ALTER TABLE orari_negozio ADD COLUMN IF NOT EXISTS chiusura_2 TIME")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS email TEXT")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS google_sub TEXT")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS apple_sub TEXT")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS password_impostata BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS email_verificata BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("ALTER TABLE utenti ADD COLUMN IF NOT EXISTS guida_iniziale_vista BOOLEAN NOT NULL DEFAULT TRUE")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS lingue_negozio (
                        id SERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        codice TEXT NOT NULL,
                        UNIQUE (id_negozio, codice)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS traduzioni_menu (
                        id SERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        tipo TEXT NOT NULL,
                        id_entita INTEGER NOT NULL,
                        campo TEXT NOT NULL,
                        lingua TEXT NOT NULL,
                        testo TEXT NOT NULL,
                        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                        UNIQUE (id_negozio, tipo, id_entita, campo, lingua)
                    )
                """)
                cur.execute("ALTER TABLE traduzioni_menu ADD COLUMN IF NOT EXISTS testo_originale TEXT NOT NULL DEFAULT ''")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS menu_visite (
                        id BIGSERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        lingua TEXT NOT NULL DEFAULT 'it',
                        visited_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS menu_visite_negozio_data ON menu_visite (id_negozio, visited_at DESC)")
                cur.execute("ALTER TABLE menu_visite ADD COLUMN IF NOT EXISTS sorgente TEXT NOT NULL DEFAULT 'diretto'")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS categoria_aperture (
                        id BIGSERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        id_categoria INTEGER NOT NULL REFERENCES categorie(id) ON DELETE CASCADE,
                        lingua TEXT NOT NULL DEFAULT 'it',
                        opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS categoria_aperture_negozio_data ON categoria_aperture (id_negozio, opened_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS prodotto_aperture (
                        id BIGSERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        id_prodotto INTEGER NOT NULL REFERENCES prodotti(id) ON DELETE CASCADE,
                        lingua TEXT NOT NULL DEFAULT 'it',
                        opened_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS prodotto_aperture_negozio_data ON prodotto_aperture (id_negozio, opened_at DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS prodotto_aperture_prodotto ON prodotto_aperture (id_prodotto)")

                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS utenti_email_unique ON utenti (LOWER(email)) WHERE email IS NOT NULL")
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS utenti_google_sub_unique ON utenti (google_sub) WHERE google_sub IS NOT NULL")
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS utenti_apple_sub_unique ON utenti (apple_sub) WHERE apple_sub IS NOT NULL")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tentativi_login (
                        id BIGSERIAL PRIMARY KEY,
                        fingerprint TEXT NOT NULL,
                        tentato_il TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS tentativi_login_fingerprint_data ON tentativi_login (fingerprint,tentato_il DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reset_password (
                        id BIGSERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                        token_hash TEXT NOT NULL UNIQUE,
                        scade_il TIMESTAMP WITH TIME ZONE NOT NULL,
                        usato_il TIMESTAMP WITH TIME ZONE
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS reset_password_utente ON reset_password (id_utente,scade_il DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS verifiche_email (
                        id BIGSERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                        token_hash TEXT NOT NULL UNIQUE,
                        scade_il TIMESTAMP WITH TIME ZONE NOT NULL,
                        usato_il TIMESTAMP WITH TIME ZONE
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS verifiche_email_utente ON verifiche_email (id_utente,scade_il DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS errori_operativi (
                        id BIGSERIAL PRIMARY KEY, area TEXT NOT NULL, gravita TEXT NOT NULL DEFAULT 'errore',
                        messaggio TEXT NOT NULL, id_utente INTEGER REFERENCES utenti(id) ON DELETE SET NULL,
                        risolto BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS errori_operativi_data ON errori_operativi (created_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS eventi_commerciali (
                        id BIGSERIAL PRIMARY KEY, evento TEXT NOT NULL,
                        id_utente INTEGER REFERENCES utenti(id) ON DELETE SET NULL,
                        provenienza TEXT NOT NULL DEFAULT 'diretto', created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS eventi_commerciali_evento_data ON eventi_commerciali (evento,created_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS dati_fatturazione (
                        id_utente INTEGER PRIMARY KEY REFERENCES utenti(id) ON DELETE CASCADE,
                        ragione_sociale TEXT NOT NULL, partita_iva TEXT NOT NULL DEFAULT '',
                        codice_fiscale TEXT NOT NULL DEFAULT '', indirizzo TEXT NOT NULL,
                        cap TEXT NOT NULL, citta TEXT NOT NULL, provincia TEXT NOT NULL,
                        nazione TEXT NOT NULL DEFAULT 'Italia', codice_sdi TEXT NOT NULL DEFAULT '',
                        pec TEXT NOT NULL DEFAULT '', email_amministrativa TEXT NOT NULL,
                        telefono TEXT NOT NULL DEFAULT '', updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS consensi_utenti (
                        id BIGSERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                        tipo TEXT NOT NULL CHECK (tipo IN ('termini', 'privacy', 'marketing')),
                        versione TEXT NOT NULL,
                        accettato BOOLEAN NOT NULL,
                        accepted_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
                        user_agent TEXT NOT NULL DEFAULT ''
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS consensi_utenti_utente_data ON consensi_utenti (id_utente, accepted_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS richieste_privacy (
                        id BIGSERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                        tipo TEXT NOT NULL CHECK (tipo IN ('accesso', 'portabilita', 'rettifica', 'cancellazione', 'limitazione', 'opposizione')),
                        dettagli TEXT NOT NULL DEFAULT '',
                        stato TEXT NOT NULL DEFAULT 'ricevuta' CHECK (stato IN ('ricevuta', 'in_lavorazione', 'completata', 'rifiutata')),
                        note_admin TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        completed_at TIMESTAMP WITH TIME ZONE
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS richieste_privacy_stato_data ON richieste_privacy (stato,created_at DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS licenze_utenti (
                        id SERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL UNIQUE REFERENCES utenti(id) ON DELETE CASCADE,
                        stato TEXT NOT NULL DEFAULT 'attiva' CHECK (stato IN ('attiva', 'sospesa')),
                        data_inizio DATE NOT NULL DEFAULT CURRENT_DATE,
                        data_scadenza DATE NOT NULL DEFAULT (CURRENT_DATE + 365),
                        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("ALTER TABLE licenze_utenti ADD COLUMN IF NOT EXISTS piano TEXT NOT NULL DEFAULT 'professional'")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS ordine_categorie_personalizzato BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE categorie ADD COLUMN IF NOT EXISTS ordine_prodotti_personalizzato BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE categorie ADD COLUMN IF NOT EXISTS visibile_da DATE")
                cur.execute("ALTER TABLE categorie ADD COLUMN IF NOT EXISTS visibile_fino DATE")
                cur.execute("ALTER TABLE categorie ADD COLUMN IF NOT EXISTS ora_inizio TIME")
                cur.execute("ALTER TABLE categorie ADD COLUMN IF NOT EXISTS ora_fine TIME")
                cur.execute("ALTER TABLE sottocategorie ADD COLUMN IF NOT EXISTS visibile_da DATE")
                cur.execute("ALTER TABLE sottocategorie ADD COLUMN IF NOT EXISTS visibile_fino DATE")
                cur.execute("ALTER TABLE sottocategorie ADD COLUMN IF NOT EXISTS ora_inizio TIME")
                cur.execute("ALTER TABLE sottocategorie ADD COLUMN IF NOT EXISTS ora_fine TIME")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS promozione BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS titolo_promozione TEXT")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS promozione_da DATE")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS promozione_fino DATE")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS visibile_da DATE")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS visibile_fino DATE")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS ora_inizio TIME")
                cur.execute("ALTER TABLE prodotti ADD COLUMN IF NOT EXISTS ora_fine TIME")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS whatsapp TEXT")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS prenotazione_url TEXT")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS ordini_attivi BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS limite_ordini_giorno INTEGER NOT NULL DEFAULT 0")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ordini_menu (
                        id BIGSERIAL PRIMARY KEY,
                        id_negozio INTEGER NOT NULL REFERENCES negozi(id) ON DELETE CASCADE,
                        nome_cliente VARCHAR(120) NOT NULL,
                        telefono_cliente VARCHAR(40) NOT NULL,
                        riferimento VARCHAR(80) NOT NULL DEFAULT '',
                        note VARCHAR(500) NOT NULL DEFAULT '',
                        stato VARCHAR(20) NOT NULL DEFAULT 'da_evadere'
                          CHECK (stato IN ('da_evadere','in_lavorazione','evaso','annullato')),
                        totale NUMERIC(10,2) NOT NULL CHECK (totale >= 0),
                        data_richiesta DATE,
                        origine VARCHAR(20) NOT NULL DEFAULT 'cliente',
                        google_sub_cliente TEXT,
                        email_cliente TEXT,
                        creato_il TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        aggiornato_il TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ordini_menu_negozio_data ON ordini_menu (id_negozio, creato_il DESC)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS righe_ordini_menu (
                        id BIGSERIAL PRIMARY KEY,
                        id_ordine BIGINT NOT NULL REFERENCES ordini_menu(id) ON DELETE CASCADE,
                        id_prodotto INTEGER REFERENCES prodotti(id) ON DELETE SET NULL,
                        nome_prodotto VARCHAR(200) NOT NULL,
                        quantita NUMERIC(10,3) NOT NULL CHECK (quantita > 0 AND quantita <= 999),
                        prezzo_unitario NUMERIC(10,2) NOT NULL CHECK (prezzo_unitario >= 0),
                        totale_riga NUMERIC(10,2) NOT NULL CHECK (totale_riga >= 0)
                    )
                """)
                cur.execute("ALTER TABLE righe_ordini_menu ALTER COLUMN quantita TYPE NUMERIC(10,3)")
                cur.execute("ALTER TABLE righe_ordini_menu DROP CONSTRAINT IF EXISTS righe_ordini_menu_quantita_check")
                cur.execute("ALTER TABLE righe_ordini_menu ADD CONSTRAINT righe_ordini_menu_quantita_check CHECK (quantita > 0 AND quantita <= 999)")
                cur.execute("ALTER TABLE ordini_menu ADD COLUMN IF NOT EXISTS data_richiesta DATE")
                cur.execute("ALTER TABLE ordini_menu ADD COLUMN IF NOT EXISTS origine VARCHAR(20) NOT NULL DEFAULT 'cliente'")
                cur.execute("ALTER TABLE ordini_menu ADD COLUMN IF NOT EXISTS google_sub_cliente TEXT")
                cur.execute("ALTER TABLE ordini_menu ADD COLUMN IF NOT EXISTS email_cliente TEXT")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS sito_web TEXT")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS instagram_url TEXT")
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS google_maps_url TEXT")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS varianti_prodotti (
                        id SERIAL PRIMARY KEY,
                        id_prodotto INTEGER NOT NULL REFERENCES prodotti(id) ON DELETE CASCADE,
                        nome TEXT NOT NULL,
                        prezzo_extra NUMERIC(10,2) NOT NULL DEFAULT 0,
                        disponibile BOOLEAN NOT NULL DEFAULT TRUE,
                        ordine INTEGER NOT NULL DEFAULT 0
                    )
                """)

                cur.execute("ALTER TABLE licenze_utenti ADD COLUMN IF NOT EXISTS piano_programmato TEXT")
                cur.execute("ALTER TABLE licenze_utenti ADD COLUMN IF NOT EXISTS cambio_piano_il DATE")
                cur.execute("""
                    INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza)
                    SELECT id, 'attiva', CURRENT_DATE, CURRENT_DATE + 365
                    FROM utenti
                    WHERE NOT EXISTS (SELECT 1 FROM licenze_utenti)
                    ON CONFLICT (id_utente) DO NOTHING
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS abbonamenti_paypal (
                        id SERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL UNIQUE REFERENCES utenti(id) ON DELETE CASCADE,
                        subscription_id TEXT UNIQUE,
                        plan_id TEXT,
                        stato TEXT NOT NULL DEFAULT 'in_attesa',
                        trial_fino DATE,
                        prossimo_addebito DATE,
                        ultimo_pagamento TIMESTAMP WITH TIME ZONE,
                        cancellato_il TIMESTAMP WITH TIME ZONE,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS eventi_paypal (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        ricevuto_il TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS notifiche_email (
                        id BIGSERIAL PRIMARY KEY,
                        id_utente INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                        tipo TEXT NOT NULL,
                        riferimento TEXT NOT NULL,
                        inviata_il TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                        UNIQUE (id_utente, tipo, riferimento)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS impostazioni_app (
                        chiave TEXT PRIMARY KEY,
                        valore TEXT NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
                    )
                """)
    finally:
        conn.close()


# Esegui init schema solo se sei in ambiente con DB configurato
if os.getenv("DATABASE_URL") and os.getenv("AUTO_INIT_DB", "true").lower() == "true":
    init_db()


@app.before_request
def run_daily_reminder_check():
    # Il webhook PayPal deve rispondere rapidamente e non viene rallentato dalla scansione email.
    global _last_daily_reminder_check
    today = date.today()
    if request.endpoint != "paypal_webhook" and _last_daily_reminder_check != today and email_configured():
        _last_daily_reminder_check = today

        def background_check():
            try:
                process_daily_license_reminders()
            except Exception:
                app.logger.exception("Scansione giornaliera promemoria non riuscita")

        threading.Thread(target=background_check, name="license-reminders", daemon=True).start()


@app.before_request
def enforce_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.endpoint in ("paypal_webhook", "auth_apple_callback", "track_category_open", "track_product_open"):
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Sessione di sicurezza scaduta. Ricarica la pagina e riprova."}), 403
        return "Sessione di sicurezza scaduta. Torna alla pagina precedente e riprova.", 403


@app.before_request
def enforce_current_license():
    """Blocca anche le sessioni già aperte quando prova o licenza terminano."""
    user_id = session.get("user_id")
    if not user_id or session.get("is_admin"):
        return None
    public_endpoints = {
        "index", "free_trial", "login", "register", "verify_email", "register_google", "register_apple", "forgot_password", "reset_password", "auth_google", "auth_google_callback",
        "auth_apple", "auth_apple_callback", "auth_google_order", "logout",
        "privacy_policy", "terms_of_service", "billing_data", "uploaded_file", "static", "public_menu",
        "paypal_webhook", "pagamento", "paypal_subscription_activate",
        "paypal_subscription_cancel", "paypal_subscription_current",
    }
    if request.endpoint in public_endpoints:
        return None
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stato, data_scadenza FROM licenze_utenti WHERE id_utente=%s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row and license_is_active(*row):
        return None
    username = session.get("username", "")
    session.clear()
    session.update(pending_user_id=user_id, pending_username=username)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Licenza non attiva.", "payment_url": url_for("pagamento")}), 402
    return redirect(url_for("pagamento"))


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if getattr(error, "code", None):
        return error
    path = request.path.lower()
    area = "importazione" if "import" in path else "caricamento" if "upload" in path or "immagin" in path else "applicazione"
    record_operational_error(area, f"Errore interno su {request.method} {request.path}", session.get("user_id"), "critico")
    app.logger.exception("Errore applicativo non gestito")
    if request.path.startswith("/api/"):
        return jsonify({"error": "Errore interno. L'amministratore è stato avvisato nel pannello di monitoraggio."}), 500
    return "Errore interno. Riprova tra qualche minuto.", 500


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_ROOT, filename)


@app.route("/")
def index():
    source = commercial_source()
    session["commercial_source"] = source
    record_commercial_event("visita_landing", source=source)
    return render_template("index.html")


@app.get("/prova-gratuita")
def free_trial():
    source = commercial_source()
    session["commercial_source"] = source
    record_commercial_event("visita_prova", source=source)
    return render_template("free_trial.html")


@app.get("/privacy")
def privacy_policy():
    return render_template("privacy.html")


@app.get("/terms")
def terms_of_service():
    return render_template("terms.html")


@app.get("/cookie")
def cookie_policy():
    return render_template("cookie.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", google_enabled=google_enabled())

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        return render_template("login.html", error="Inserisci email e password.", google_enabled=google_enabled())
    if login_is_limited(email):
        return render_template("login.html", error="Troppi tentativi di accesso. Attendi 15 minuti e riprova.", google_enabled=google_enabled()), 429

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.password, u.admin, l.stato, l.data_scadenza, u.email_verificata
                FROM utenti u
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
                WHERE LOWER(u.email) = LOWER(%s)
            """, (email,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not verify_password(password, row[2]):
        record_login_failure(email)
        return render_template("login.html", error="Email o password errati.", google_enabled=google_enabled())

    user_id, username_db, _, is_admin, status, expiry, email_verified = row
    clear_login_failures(email)
    if not email_verified:
        return render_template("email_verification_pending.html", email=email)
    if not is_admin and not license_is_active(status, expiry):
        session.clear()
        session.update(pending_user_id=user_id, pending_username=username_db)
        return redirect(url_for("pagamento"))

    session.update(user_id=user_id, username=username_db, is_admin=bool(is_admin))
    if not is_admin:
        trigger_license_expiry_email(user_id)
    return redirect("/dashboard_admin" if is_admin else url_for("dashboard_choice"))


@app.route("/password-dimenticata", methods=["GET", "POST"])
def forgot_password():
    message = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        # Limite separato dal login per evitare invii ripetuti di email di recupero.
        recovery_key = "password-recovery:" + email
        if login_is_limited(recovery_key):
            return render_template("forgot_password.html", message="Troppe richieste. Attendi 15 minuti e riprova."), 429
        record_login_failure(recovery_key)
        conn = psycopg2.connect(**build_db_config())
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,username,email FROM utenti WHERE LOWER(email)=LOWER(%s)", (email,))
                    row = cur.fetchone()
                    if row and row[2]:
                        raw_token = secrets.token_urlsafe(40)
                        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                        cur.execute("UPDATE reset_password SET usato_il=NOW() WHERE id_utente=%s AND usato_il IS NULL", (row[0],))
                        cur.execute("INSERT INTO reset_password (id_utente,token_hash,scade_il) VALUES (%s,%s,NOW()+INTERVAL '1 hour')", (row[0], token_hash))
                        reset_url = "https://menu.alphasystemsrl.it" + url_for("reset_password", token=raw_token)
                        send_transactional_email(
                            row[2], "Reimposta la password di Alpha Menu",
                            f"Ciao {row[1]},\n\nusa questo collegamento entro un'ora per scegliere una nuova password:\n{reset_url}\n\nSe non hai richiesto tu il recupero, ignora questa email.\n\nAlpha Menu – Alpha System S.r.l.",
                        )
            message = "Se l’indirizzo è associato a un account, riceverai a breve le istruzioni."
        finally:
            conn.close()
    return render_template("forgot_password.html", message=message)


@app.route("/reimposta-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id,u.id,u.email FROM reset_password r
                JOIN utenti u ON u.id=r.id_utente
                WHERE r.token_hash=%s AND r.usato_il IS NULL AND r.scade_il>NOW()
            """, (token_hash,))
            row = cur.fetchone()
        if not row:
            return render_template("reset_password.html", invalid=True), 400
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("password_confirm", "")
            if len(password) < 8:
                return render_template("reset_password.html", error="La password deve avere almeno 8 caratteri.")
            if len(password.encode("utf-8")) > 72:
                return render_template("reset_password.html", error="La password è troppo lunga: usa al massimo 72 byte UTF-8.")
            if password != confirm:
                return render_template("reset_password.html", error="Le password non coincidono.")
            with conn:
                with conn.cursor() as cur:
                    # Consuma il token atomicamente: due richieste simultanee non
                    # possono entrambe modificare la password con lo stesso link.
                    cur.execute("UPDATE reset_password SET usato_il=NOW() WHERE id=%s AND usato_il IS NULL AND scade_il>NOW() RETURNING id", (row[0],))
                    if not cur.fetchone():
                        return render_template("reset_password.html", invalid=True), 400
                    cur.execute("UPDATE utenti SET password=%s,password_impostata=TRUE WHERE id=%s", (hash_password(password), row[1]))
                    cur.execute("UPDATE reset_password SET usato_il=NOW() WHERE id_utente=%s AND usato_il IS NULL", (row[1],))
                    cur.execute("DELETE FROM tentativi_login WHERE fingerprint=%s", (request_fingerprint(row[2]),))
            return render_template("reset_password.html", success=True)
        return render_template("reset_password.html")
    finally:
        conn.close()


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))

    business_name = request.form.get("business_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")
    consent = registration_consent_from_form()
    if not consent:
        return render_template("register.html", error="Per creare l’account devi accettare i Termini di servizio e l’Informativa privacy.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    if not business_name or not email or not password:
        return render_template("register.html", error="Compila tutti i campi.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    if password != confirm:
        return render_template("register.html", error="Le password non coincidono.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    if len(password) < 8:
        return render_template("register.html", error="La password deve avere almeno 8 caratteri.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO utenti (username, email, password, admin, email_verificata, guida_iniziale_vista) VALUES (%s, %s, %s, FALSE, FALSE, FALSE) RETURNING id",
                    (business_name, email, hash_password(password)),
                )
                user_id = cur.fetchone()[0]
                record_registration_consents(cur, user_id, consent["marketing"])
                raw_token = secrets.token_urlsafe(40)
                token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                cur.execute("INSERT INTO verifiche_email (id_utente,token_hash,scade_il) VALUES (%s,%s,NOW()+INTERVAL '24 hours')", (user_id, token_hash))
                verify_url = request.url_root.rstrip("/") + url_for("verify_email", token=raw_token)
                if not send_transactional_email(
                    email,
                    "Conferma la tua email per iniziare la prova Alpha Menu",
                    f"Ciao {business_name},\n\nconferma il tuo indirizzo email entro 24 ore:\n{verify_url}\n\nI 14 giorni gratuiti inizieranno soltanto dopo la conferma.\n\nAlpha Menu – Alpha System S.r.l.",
                ):
                    raise RuntimeError("Invio email non riuscito")
        record_commercial_event("registrazione", user_id, session.get("commercial_source") or commercial_source())
        return render_template("email_verification_pending.html", email=email)
    except psycopg2.IntegrityError:
        return render_template("register.html", error="Email o nome dell’attività già utilizzati.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    except RuntimeError:
        return render_template("register.html", error="Non è stato possibile inviare l’email di conferma. Riprova tra qualche minuto.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base")), 502
    finally:
        conn.close()


@app.get("/verifica-email/<token>")
def verify_email(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT v.id,u.id,u.username,u.email_verificata
                    FROM verifiche_email v JOIN utenti u ON u.id=v.id_utente
                    WHERE v.token_hash=%s AND v.usato_il IS NULL AND v.scade_il>NOW()
                    FOR UPDATE
                """, (token_hash,))
                row = cur.fetchone()
                if not row:
                    return render_template("email_verification_pending.html", invalid=True), 400
                verification_id, user_id, username, already_verified = row
                if not already_verified:
                    trial_end = date.today() + timedelta(days=APP_TRIAL_DAYS)
                    cur.execute("UPDATE utenti SET email_verificata=TRUE WHERE id=%s", (user_id,))
                    cur.execute("""
                        INSERT INTO licenze_utenti (id_utente,stato,data_inizio,data_scadenza,piano)
                        VALUES (%s,'attiva',CURRENT_DATE,%s,'professional')
                        ON CONFLICT (id_utente) DO NOTHING
                    """, (user_id, trial_end))
                    cur.execute("""
                        INSERT INTO abbonamenti_paypal (id_utente,plan_id,stato,trial_fino,prossimo_addebito)
                        VALUES (%s,%s,'prova_locale',%s,%s)
                        ON CONFLICT (id_utente) DO NOTHING
                    """, (user_id, paypal_plan_id("professional"), trial_end, trial_end))
                cur.execute("UPDATE verifiche_email SET usato_il=NOW() WHERE id=%s", (verification_id,))
        source = session.get("commercial_source")
        session.clear()
        session.update(user_id=user_id, username=username, is_admin=False)
        record_commercial_event("trial_avviato", user_id, source)
        return render_template("email_verification_pending.html", verified=True)
    finally:
        conn.close()


@app.post("/register/google")
def register_google():
    consent = registration_consent_from_form("google")
    if not consent:
        return render_template("register.html", error="Per registrarti con Google devi accettare i Termini di servizio e l’Informativa privacy.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    session["oauth_registration_consent"] = consent
    return redirect(url_for("auth_google"))


@app.post("/register/apple")
def register_apple():
    consent = registration_consent_from_form("apple")
    if not consent:
        return render_template("register.html", error="Per registrarti con Apple devi accettare i Termini di servizio e l’Informativa privacy.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    session["oauth_registration_consent"] = consent
    return redirect(url_for("auth_apple"))


@app.get("/auth/google")
def auth_google():
    if not google_enabled():
        return redirect(url_for("login"))
    session.pop("customer_order_slug", None)
    callback = url_for("auth_google_callback", _external=True, _scheme="https")
    return google.authorize_redirect(callback)


@app.get("/auth/google/ordine/<slug>")
def auth_google_order(slug: str):
    if not google_enabled():
        return redirect(url_for("public_menu", slug=slug))
    session["customer_order_slug"] = slug
    callback = url_for("auth_google_callback", _external=True, _scheme="https")
    return google.authorize_redirect(callback)


@app.get("/auth/google/callback")
def auth_google_callback():
    if not google_enabled():
        return redirect(url_for("login"))
    token = google.authorize_access_token()
    profile = token.get("userinfo") or google.userinfo()
    customer_slug = session.pop("customer_order_slug", None)
    if customer_slug:
        if not profile.get("sub") or not profile.get("email") or not profile.get("email_verified"):
            return redirect(url_for("public_menu", slug=customer_slug, accesso="non_riuscito"))
        session["customer_google"] = {
            "sub": str(profile["sub"]),
            "email": str(profile["email"]).strip().lower(),
            "name": str(profile.get("name") or "")[:120],
        }
        return redirect(url_for("public_menu", slug=customer_slug) + "#orderPanel")
    email = (profile.get("email") or "").strip().lower()
    google_sub = profile.get("sub")
    if not email or not google_sub:
        return render_template("login.html", error="Google non ha restituito un indirizzo email valido.", google_enabled=True)

    link_user_id = session.pop("google_link_user_id", None)
    if link_user_id:
        conn = psycopg2.connect(**build_db_config())
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM utenti WHERE google_sub=%s AND id<>%s", (google_sub, link_user_id))
                    if cur.fetchone():
                        session["account_google_message"] = "Questo account Google è già collegato a un altro cliente."
                    else:
                        cur.execute("UPDATE utenti SET google_sub=%s WHERE id=%s", (google_sub, link_user_id))
                        session["account_google_message"] = f"Account Google {email} collegato correttamente."
            return redirect(url_for("dashboard_user") + "#account")
        finally:
            conn.close()

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, username, admin FROM utenti WHERE google_sub = %s OR LOWER(email) = LOWER(%s) ORDER BY google_sub = %s DESC LIMIT 1", (google_sub, email, google_sub))
                row = cur.fetchone()
                is_new_user = not bool(row)
                registration_consent = oauth_registration_consent("google")
                if is_new_user and not registration_consent:
                    return render_template("register.html", error="Prima di creare un nuovo account con Google, accetta i Termini e la Privacy dalla pagina di registrazione.", google_enabled=True, plans=LICENSE_PLANS, base_available=paypal_configured("base"))
                if row:
                    user_id, username, is_admin = row
                    cur.execute("UPDATE utenti SET google_sub = %s, email = %s WHERE id = %s", (google_sub, email, user_id))
                else:
                    base = (profile.get("name") or email.split("@")[0])[:70]
                    username = base
                    suffix = 1
                    while True:
                        cur.execute("SELECT 1 FROM utenti WHERE LOWER(username) = LOWER(%s)", (username,))
                        if not cur.fetchone():
                            break
                        suffix += 1
                        username = f"{base[:65]}-{suffix}"
                    cur.execute("INSERT INTO utenti (username, email, google_sub, password, admin, password_impostata, guida_iniziale_vista) VALUES (%s, %s, %s, %s, FALSE, FALSE, FALSE) RETURNING id", (username, email, google_sub, hash_password(os.urandom(32).hex())))
                    user_id = cur.fetchone()[0]
                    is_admin = False
                if is_new_user:
                    record_registration_consents(cur, user_id, registration_consent.get("marketing", False))
                    trial_end = date.today() + timedelta(days=APP_TRIAL_DAYS)
                    cur.execute("INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza, piano) VALUES (%s, 'attiva', CURRENT_DATE, %s, 'professional')", (user_id, trial_end))
                    cur.execute("INSERT INTO abbonamenti_paypal (id_utente, plan_id, stato, trial_fino, prossimo_addebito) VALUES (%s, %s, 'prova_locale', %s, %s)", (user_id, paypal_plan_id("professional"), trial_end, trial_end))
                else:
                    cur.execute("INSERT INTO licenze_utenti (id_utente, data_scadenza) VALUES (%s, %s) ON CONFLICT (id_utente) DO NOTHING", (user_id, annual_expiry()))
                cur.execute("SELECT stato, data_scadenza FROM licenze_utenti WHERE id_utente = %s", (user_id,))
                license_row = cur.fetchone()
        session.pop("oauth_registration_consent", None)
        if not is_admin and (not license_row or not license_is_active(*license_row)):
            session.clear()
            session.update(pending_user_id=user_id, pending_username=username)
            return redirect(url_for("pagamento"))
        session.update(user_id=user_id, username=username, is_admin=bool(is_admin))
        if not is_admin:
            trigger_license_expiry_email(user_id)
        return redirect("/dashboard_admin" if is_admin else url_for("dashboard_choice"))
    finally:
        conn.close()


@app.get("/auth/apple")
def auth_apple():
    if not apple_enabled():
        return redirect(url_for("login"))
    nonce = secrets.token_urlsafe(24)
    state = apple_state_serializer().dumps({"nonce": nonce})
    callback = url_for("auth_apple_callback", _external=True, _scheme="https")
    params = {
        "client_id": os.environ["APPLE_CLIENT_ID"],
        "redirect_uri": callback,
        "response_type": "code",
        "response_mode": "form_post",
        "scope": "name email",
        "state": state,
        "nonce": nonce,
    }
    return redirect("https://appleid.apple.com/auth/authorize?" + urlencode(params))


@app.route("/auth/apple/callback", methods=["POST"])
def auth_apple_callback():
    if not apple_enabled():
        return redirect(url_for("login"))
    try:
        state_data = apple_state_serializer().loads(request.form.get("state", ""), max_age=600)
    except (BadSignature, SignatureExpired):
        return render_template("login.html", error="La richiesta Apple è scaduta o non è valida. Riprova.")
    if request.form.get("error"):
        return render_template("login.html", error="Apple non ha autorizzato l'accesso.")
    code = (request.form.get("code") or "").strip()
    if not code:
        return render_template("login.html", error="Apple non ha restituito il codice di accesso.")

    callback = url_for("auth_apple_callback", _external=True, _scheme="https")
    try:
        token_response = requests.post(
            "https://appleid.apple.com/auth/token",
            data={
                "client_id": os.environ["APPLE_CLIENT_ID"],
                "client_secret": apple_client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": callback,
            },
            timeout=20,
        )
        token_response.raise_for_status()
        id_token = token_response.json()["id_token"]
        jwks = requests.get("https://appleid.apple.com/auth/keys", timeout=20).json()
        claims = jwt.decode(id_token, jwks)
        claims.validate(leeway=10)
    except (requests.RequestException, KeyError, ValueError):
        return render_template("login.html", error="Non è stato possibile verificare l'accesso Apple.")

    audience = claims.get("aud")
    audience_ok = os.environ["APPLE_CLIENT_ID"] in (audience if isinstance(audience, list) else [audience])
    if claims.get("iss") != "https://appleid.apple.com" or not audience_ok or claims.get("nonce") != state_data.get("nonce"):
        return render_template("login.html", error="Il token Apple non è valido.")

    apple_sub = (claims.get("sub") or "").strip()
    email = (claims.get("email") or "").strip().lower()
    if not apple_sub:
        return render_template("login.html", error="Apple non ha restituito un identificativo valido.")
    apple_user = {}
    try:
        apple_user = json.loads(request.form.get("user") or "{}")
    except (TypeError, ValueError):
        apple_user = {}
    name_data = apple_user.get("name") or {}
    display_name = " ".join(filter(None, (
        (name_data.get("firstName") or "").strip(),
        (name_data.get("lastName") or "").strip(),
    ))).strip()
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, username, admin FROM utenti
                    WHERE apple_sub=%s OR (%s <> '' AND LOWER(email)=LOWER(%s))
                    ORDER BY apple_sub=%s DESC LIMIT 1
                    """,
                    (apple_sub, email, email, apple_sub),
                )
                row = cur.fetchone()
                is_new_user = not bool(row)
                registration_consent = oauth_registration_consent("apple")
                if is_new_user and not registration_consent:
                    return render_template("register.html", error="Prima di creare un nuovo account con Apple, accetta i Termini e la Privacy dalla pagina di registrazione.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
                if row:
                    user_id, username, is_admin = row
                    cur.execute(
                        "UPDATE utenti SET apple_sub=%s, email=COALESCE(NULLIF(%s,''),email) WHERE id=%s",
                        (apple_sub, email, user_id),
                    )
                else:
                    if not email:
                        return render_template("login.html", error="Apple non ha condiviso un indirizzo email per creare l'account.")
                    base = (display_name or email.split("@")[0])[:70]
                    username = base
                    suffix = 1
                    while True:
                        cur.execute("SELECT 1 FROM utenti WHERE LOWER(username)=LOWER(%s)", (username,))
                        if not cur.fetchone():
                            break
                        suffix += 1
                        username = f"{base[:65]}-{suffix}"
                    cur.execute(
                        """
                        INSERT INTO utenti (username,email,apple_sub,password,admin,password_impostata,guida_iniziale_vista)
                        VALUES (%s,%s,%s,%s,FALSE,FALSE,FALSE) RETURNING id
                        """,
                        (username, email, apple_sub, hash_password(os.urandom(32).hex())),
                    )
                    user_id = cur.fetchone()[0]
                    is_admin = False
                if is_new_user:
                    record_registration_consents(cur, user_id, registration_consent.get("marketing", False))
                    trial_end = date.today() + timedelta(days=APP_TRIAL_DAYS)
                    cur.execute(
                        """
                        INSERT INTO licenze_utenti (id_utente,stato,data_inizio,data_scadenza,piano)
                        VALUES (%s,'attiva',CURRENT_DATE,%s,'professional')
                        """,
                        (user_id, trial_end),
                    )
                    cur.execute(
                        "INSERT INTO abbonamenti_paypal (id_utente,plan_id,stato,trial_fino,prossimo_addebito) VALUES (%s,%s,'prova_locale',%s,%s)",
                        (user_id, paypal_plan_id("professional"), trial_end, trial_end),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO licenze_utenti (id_utente,data_scadenza)
                        VALUES (%s,%s) ON CONFLICT (id_utente) DO NOTHING
                        """,
                        (user_id, annual_expiry()),
                    )
                cur.execute("SELECT stato,data_scadenza FROM licenze_utenti WHERE id_utente=%s", (user_id,))
                license_row = cur.fetchone()
        session.pop("oauth_registration_consent", None)
        if not is_admin and (not license_row or not license_is_active(*license_row)):
            session.clear()
            session.update(pending_user_id=user_id, pending_username=username)
            return redirect(url_for("pagamento"))
        session.clear()
        session.update(user_id=user_id, username=username, is_admin=bool(is_admin))
        if not is_admin:
            trigger_license_expiry_email(user_id)
        return redirect("/dashboard_admin" if is_admin else url_for("dashboard_choice"))
    finally:
        conn.close()


@app.get("/pagamento")
def pagamento():
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id or session.get("is_admin"):
        return redirect(url_for("login"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.username, u.email, a.subscription_id, a.stato, a.trial_fino,
                       a.prossimo_addebito, l.stato, l.data_scadenza, COALESCE(l.piano, 'professional')
                FROM utenti u
                LEFT JOIN abbonamenti_paypal a ON a.id_utente = u.id
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
                WHERE u.id = %s
            """, (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        session.clear()
        return redirect(url_for("login"))
    license_active = license_is_active(row[6], row[7])
    renewal_requested = request.args.get("rinnovo") == "1"
    choice_requested = request.args.get("scelta") == "1"
    selection_requested = renewal_requested or choice_requested
    recurring_active = bool(row[2]) and row[3] in ("attivo", "ACTIVE", "prova")
    selected_plan = normalize_license_plan(session.get("renewal_plan") if selection_requested else row[8])
    plan_info = LICENSE_PLANS[selected_plan]
    requires_payment = (not license_active or selection_requested) and not (renewal_requested and recurring_active)
    if requires_payment and not billing_data_complete(billing_data_for_user(user_id)):
        destination = "pagamento_rinnovo" if renewal_requested else "pagamento_scelta" if choice_requested else "pagamento"
        return redirect(url_for("billing_data", return_to=destination))
    return render_template(
        "pagamento.html", username=row[0], email=row[1], subscription_id=row[2],
        subscription_status=row[3], trial_until=row[4], next_billing=row[5],
        license_active=license_active, renewal_requested=renewal_requested, choice_requested=choice_requested, selection_requested=selection_requested,
        recurring_active=recurring_active,
        paypal_configured=paypal_configured(selected_plan), paypal_client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
        paypal_plan_id=paypal_plan_id(selected_plan), price=plan_info["price"], price_with_vat=plan_price_with_vat(selected_plan), plan=selected_plan, plan_name=plan_info["name"],
        currency=PAYPAL_CURRENCY, trial_days=0,
    )


@app.post("/api/paypal/subscription/activate")
def paypal_subscription_activate():
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id or session.get("is_admin"):
        return jsonify({"error": "Sessione di registrazione non valida."}), 401
    if not billing_data_complete(billing_data_for_user(user_id)):
        return jsonify({"error": "Completa i dati di fatturazione prima di procedere al pagamento."}), 400
    payload = request.get_json(silent=True) or {}
    renewal_requested = bool(payload.get("renewal"))
    selection_requested = renewal_requested or bool(payload.get("choice"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(l.piano, 'professional'), a.subscription_id, a.stato
                FROM licenze_utenti l
                LEFT JOIN abbonamenti_paypal a ON a.id_utente=l.id_utente
                WHERE l.id_utente=%s
            """, (user_id,))
            plan_row = cur.fetchone()
    finally:
        conn.close()

    if renewal_requested and plan_row and plan_row[1] and plan_row[2] in ("attivo", "ACTIVE", "prova"):
        return jsonify({"error": "Il rinnovo automatico PayPal è già attivo: non è stato creato un secondo abbonamento."}), 409
    selected_plan = normalize_license_plan(session.get("renewal_plan") if selection_requested else (plan_row[0] if plan_row else None))
    expected_plan_id = paypal_plan_id(selected_plan)
    if not paypal_configured(selected_plan):
        return jsonify({"error": "Il piano PayPal selezionato non è ancora configurato."}), 503
    subscription_id = (payload.get("subscription_id") or "").strip()
    if not subscription_id:
        return jsonify({"error": "Identificativo abbonamento mancante."}), 400
    try:
        details = paypal_get_subscription(subscription_id)
    except requests.RequestException:
        return jsonify({"error": "Non è stato possibile verificare l'abbonamento con PayPal."}), 502
    if details.get("status") != "ACTIVE":
        return jsonify({"error": "L'abbonamento PayPal non risulta attivo."}), 409
    if details.get("plan_id") != expected_plan_id:
        return jsonify({"error": "Il piano PayPal non corrisponde all'offerta selezionata."}), 400
    if str(details.get("custom_id", "")) != str(user_id):
        return jsonify({"error": "L'abbonamento non appartiene a questo account."}), 403

    next_billing = parse_paypal_date(details.get("billing_info", {}).get("next_billing_time"), date.today() + timedelta(days=365))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data_scadenza FROM licenze_utenti WHERE id_utente=%s FOR UPDATE", (user_id,))
                license_row = cur.fetchone()
                current_expiry = license_row[0] if license_row else None
                expiry = next_billing
                if renewal_requested and current_expiry and current_expiry >= date.today():
                    expiry = current_expiry + timedelta(days=365)
                cur.execute("""
                    INSERT INTO abbonamenti_paypal (id_utente, subscription_id, plan_id, stato, trial_fino, prossimo_addebito, updated_at)
                    VALUES (%s, %s, %s, 'attivo', NULL, %s, NOW())
                    ON CONFLICT (id_utente) DO UPDATE SET subscription_id=EXCLUDED.subscription_id,
                        plan_id=EXCLUDED.plan_id, stato='attivo', trial_fino=NULL,
                        prossimo_addebito=EXCLUDED.prossimo_addebito, updated_at=NOW()
                """, (user_id, subscription_id, details.get("plan_id"), next_billing))
                cur.execute("UPDATE licenze_utenti SET stato='attiva', piano=%s, data_inizio=CURRENT_DATE, data_scadenza=%s, updated_at=NOW() WHERE id_utente=%s", (selected_plan, expiry, user_id))
                cur.execute("SELECT username FROM utenti WHERE id=%s", (user_id,))
                username = cur.fetchone()[0]
        session.clear()
        session.update(user_id=user_id, username=username, is_admin=False)
        record_commercial_event(f"conversione_{selected_plan}", user_id)
        return jsonify({"ok": True, "redirect": url_for("dashboard_user")})
    finally:
        conn.close()


@app.get("/account/collega-google")
def account_link_google():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if not google_enabled():
        session["account_google_message"] = "Accesso Google non configurato."
        return redirect(url_for("dashboard_user") + "#account")
    session["google_link_user_id"] = session["user_id"]
    callback = url_for("auth_google_callback", _external=True, _scheme="https")
    return google.authorize_redirect(callback)


@app.post("/api/paypal/pending-plan")
def paypal_pending_plan():
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id or session.get("is_admin"):
        return jsonify({"error": "Sessione di registrazione non valida."}), 401
    raw_plan = ((request.get_json(silent=True) or {}).get("piano") or "").strip().lower()
    if raw_plan not in LICENSE_PLANS:
        return jsonify({"error": "Piano non valido."}), 400
    plan = normalize_license_plan(raw_plan)
    if not paypal_configured(plan):
        return jsonify({"error": "Il piano selezionato non è disponibile."}), 503
    selection_requested = bool((request.get_json(silent=True) or {}).get("rinnovo") or (request.get_json(silent=True) or {}).get("scelta"))
    if selection_requested:
        # Nel rinnovo o nella scelta post-prova il piano diventa effettivo solo dopo PayPal.
        session["renewal_plan"] = plan
        return jsonify({"ok": True, "plan": plan})
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE licenze_utenti
                    SET piano=%s, updated_at=NOW()
                    WHERE id_utente=%s
                      AND (stato='sospesa' OR data_scadenza < CURRENT_DATE)
                    RETURNING id_utente
                    """,
                    (plan, user_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Il piano può essere scelto al termine della prova gratuita."}), 409
                cur.execute("UPDATE abbonamenti_paypal SET plan_id=%s, updated_at=NOW() WHERE id_utente=%s", (paypal_plan_id(plan), user_id))
        return jsonify({"ok": True, "plan": plan})
    finally:
        conn.close()


@app.post("/api/paypal/webhook")
def paypal_webhook():
    payload = request.get_json(silent=True)
    if not payload or not paypal_verify_webhook(payload):
        return jsonify({"error": "Firma webhook non valida."}), 400
    event_id = payload.get("id")
    event_type = payload.get("event_type", "")
    resource = payload.get("resource") or {}
    subscription_id = resource.get("billing_agreement_id") or resource.get("id")
    if not event_id:
        return jsonify({"error": "Evento PayPal senza identificativo."}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO eventi_paypal (event_id, event_type) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_id", (event_id, event_type))
                if not cur.fetchone():
                    return jsonify({"ok": True, "duplicate": True})
                if not subscription_id:
                    return jsonify({"ok": True, "ignored": True})
                cur.execute("SELECT id_utente FROM abbonamenti_paypal WHERE subscription_id=%s", (subscription_id,))
                owner = cur.fetchone()
                if not owner:
                    return jsonify({"ok": True, "ignored": True})
                user_id = owner[0]
                if event_type == "PAYMENT.SALE.COMPLETED":
                    try:
                        details = paypal_get_subscription(subscription_id)
                        next_date = parse_paypal_date(details.get("billing_info", {}).get("next_billing_time"), annual_expiry())
                    except requests.RequestException:
                        next_date = annual_expiry()
                    cur.execute("UPDATE abbonamenti_paypal SET stato='attivo', prossimo_addebito=%s, ultimo_pagamento=NOW(), updated_at=NOW() WHERE id_utente=%s", (next_date, user_id))
                    cur.execute("UPDATE licenze_utenti SET stato='attiva', data_scadenza=%s, updated_at=NOW() WHERE id_utente=%s", (next_date, user_id))
                    cur.execute("""
                        UPDATE licenze_utenti
                        SET piano=piano_programmato, piano_programmato=NULL,
                            cambio_piano_il=NULL, updated_at=NOW()
                        WHERE id_utente=%s AND piano_programmato IS NOT NULL
                          AND cambio_piano_il IS NOT NULL AND cambio_piano_il <= CURRENT_DATE
                    """, (user_id,))
                elif event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
                    next_date = parse_paypal_date(resource.get("billing_info", {}).get("next_billing_time"), annual_expiry())
                    cur.execute("UPDATE abbonamenti_paypal SET stato='attivo', trial_fino=NULL, prossimo_addebito=%s, updated_at=NOW() WHERE id_utente=%s", (next_date, user_id))
                    cur.execute("UPDATE licenze_utenti SET stato='attiva', data_scadenza=%s, updated_at=NOW() WHERE id_utente=%s", (next_date, user_id))
                elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
                    cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
                elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
                    cur.execute("UPDATE abbonamenti_paypal SET stato='scaduto', cancellato_il=COALESCE(cancellato_il,NOW()), updated_at=NOW() WHERE id_utente=%s", (user_id,))
                    cur.execute("UPDATE licenze_utenti SET stato='sospesa', updated_at=NOW() WHERE id_utente=%s AND data_scadenza < CURRENT_DATE", (user_id,))
                elif event_type in ("BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.PAYMENT.FAILED"):
                    cur.execute("UPDATE abbonamenti_paypal SET stato='pagamento_fallito', updated_at=NOW() WHERE id_utente=%s", (user_id,))
                    cur.execute("UPDATE licenze_utenti SET stato='sospesa', updated_at=NOW() WHERE id_utente=%s AND data_scadenza < CURRENT_DATE", (user_id,))
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/paypal/subscription/cancel")
def paypal_subscription_cancel():
    user_id = session.get("user_id")
    if not user_id or session.get("is_admin"):
        return jsonify({"error": "Accesso richiesto."}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.subscription_id, u.username, u.email, l.data_scadenza
                FROM abbonamenti_paypal a
                JOIN utenti u ON u.id=a.id_utente
                LEFT JOIN licenze_utenti l ON l.id_utente=a.id_utente
                WHERE a.id_utente=%s
            """, (user_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            return jsonify({"error": "Nessun abbonamento PayPal trovato."}), 404
        try:
            paypal_cancel_subscription_by_id(row[0], "Disdetta richiesta dal cliente")
        except (requests.RequestException, RuntimeError):
            return jsonify({"error": "PayPal non ha accettato la disdetta. Riprova o contatta l’assistenza."}), 502
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
        expiry_text = row[3].strftime("%d/%m/%Y") if row[3] else "la scadenza indicata in dashboard"
        send_transactional_email(
            row[2],
            "Conferma disattivazione rinnovo Alpha Menu",
            f"Ciao {row[1]},\n\nabbiamo disattivato il rinnovo automatico PayPal. Non saranno effettuati altri rinnovi automatici. Il servizio resta disponibile fino al {expiry_text}.\n\nAlpha Menu – Alpha System S.r.l.",
        )
        return jsonify({"ok": True, "message": "Rinnovo automatico disattivato. La licenza resta valida fino alla scadenza."})
    finally:
        conn.close()


@app.get("/api/paypal/subscription")
def paypal_subscription_current():
    if not session.get("user_id") or session.get("is_admin"):
        return jsonify({"error": "Accesso richiesto."}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stato, trial_fino, prossimo_addebito, cancellato_il, subscription_id FROM abbonamenti_paypal WHERE id_utente=%s", (session["user_id"],))
            row = cur.fetchone()
        if not row:
            return jsonify({"item": None})
        return jsonify({"item": {
            "stato": row[0], "trial_fino": row[1].isoformat() if row[1] else None,
            "prossimo_addebito": row[2].isoformat() if row[2] else None,
            "cancellato_il": row[3].isoformat() if row[3] else None,
            "puo_disdire": bool(row[4]) and row[0] not in ("cancellato", "scaduto"),
        }})
    finally:
        conn.close()


@app.get("/cambio-piano")
def paypal_change_plan_page():
    user_id = session.get("user_id")
    if not user_id or session.get("is_admin"):
        return redirect(url_for("login"))
    target_plan = normalize_license_plan(request.args.get("plan"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(l.piano, 'professional'), a.subscription_id,
                       a.prossimo_addebito, a.stato
                FROM licenze_utenti l
                LEFT JOIN abbonamenti_paypal a ON a.id_utente=l.id_utente
                WHERE l.id_utente=%s
            """, (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[1]:
        return redirect(url_for("dashboard_user") + "#licenze")
    current_plan = normalize_license_plan(row[0])
    if target_plan == current_plan:
        return redirect(url_for("dashboard_user") + "#licenze")
    if not paypal_configured(target_plan):
        return render_template("cambio_piano.html", error="Il piano selezionato non è ancora configurato su PayPal.")
    return render_template(
        "cambio_piano.html", error=None, current_plan=current_plan,
        current_name=LICENSE_PLANS[current_plan]["name"], target_plan=target_plan,
        target_name=LICENSE_PLANS[target_plan]["name"],
        target_price=LICENSE_PLANS[target_plan]["price"],
        target_plan_id=paypal_plan_id(target_plan), subscription_id=row[1],
        next_billing=row[2], paypal_client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
        currency=PAYPAL_CURRENCY,
    )


@app.post("/api/paypal/subscription/change/confirm")
def paypal_change_plan_confirm():
    user_id = session.get("user_id")
    if not user_id or session.get("is_admin"):
        return jsonify({"error": "Accesso richiesto."}), 401
    target_plan = normalize_license_plan((request.get_json(silent=True) or {}).get("piano"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(l.piano, 'professional'), a.subscription_id,
                       a.prossimo_addebito, l.data_scadenza
                FROM licenze_utenti l JOIN abbonamenti_paypal a ON a.id_utente=l.id_utente
                WHERE l.id_utente=%s
            """, (user_id,))
            row = cur.fetchone()
        if not row or not row[1]:
            return jsonify({"error": "Abbonamento PayPal non trovato."}), 404
        current_plan = normalize_license_plan(row[0])
        if current_plan == target_plan:
            return jsonify({"ok": True, "message": "Il piano è già attivo."})
        try:
            details = paypal_get_subscription(row[1])
        except requests.RequestException:
            return jsonify({"error": "Non è stato possibile verificare il cambio con PayPal."}), 502
        if details.get("status") != "ACTIVE" or details.get("plan_id") != paypal_plan_id(target_plan):
            return jsonify({"error": "PayPal non ha ancora confermato il nuovo piano."}), 409
        change_date = row[2] or row[3] or date.today()
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE abbonamenti_paypal SET plan_id=%s, updated_at=NOW() WHERE id_utente=%s", (details.get("plan_id"), user_id))
                if target_plan == "professional":
                    cur.execute("UPDATE licenze_utenti SET piano='professional', piano_programmato=NULL, cambio_piano_il=NULL, updated_at=NOW() WHERE id_utente=%s", (user_id,))
                    message = "Upgrade a Professional completato. Le nuove funzioni sono già disponibili."
                else:
                    cur.execute("UPDATE licenze_utenti SET piano_programmato='base', cambio_piano_il=%s, updated_at=NOW() WHERE id_utente=%s", (change_date, user_id))
                    message = f"Downgrade programmato: il piano Base partirà dal {change_date.isoformat()}."
        return jsonify({"ok": True, "message": message, "redirect": url_for("dashboard_user") + "#licenze"})
    finally:
        conn.close()


@app.route("/dashboard_admin")
def dashboard_admin():
    if not session.get("is_admin"):
        return redirect("/login")
    return render_template("dashboard_admin.html", username=session.get("username"))


def require_admin():
    if "user_id" not in session or not session.get("is_admin"):
        return jsonify({"error": "Accesso amministratore richiesto."}), 403
    return None


@app.get("/api/admin/paypal/piano-base")
def api_admin_paypal_base_plan_status():
    denied = require_admin()
    if denied:
        return denied
    return jsonify({"configured": bool(paypal_plan_id("base"))})


@app.post("/api/admin/paypal/piano-base")
def api_admin_create_paypal_base_plan():
    denied = require_admin()
    if denied:
        return denied
    existing = paypal_plan_id("base")
    if not os.environ.get("PAYPAL_CLIENT_ID") or not os.environ.get("PAYPAL_CLIENT_SECRET"):
        return jsonify({"error": "Credenziali PayPal mancanti su Railway."}), 503
    try:
        token = paypal_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        professional_id = paypal_plan_id("professional")
        professional_response = requests.get(f"{paypal_base_url()}/v1/billing/plans/{professional_id}", headers=headers, timeout=25)
        professional_response.raise_for_status()
        product_id = professional_response.json()["product_id"]
        if existing:
            existing_response = requests.get(f"{paypal_base_url()}/v1/billing/plans/{existing}", headers=headers, timeout=25)
            if existing_response.ok and existing_response.json().get("product_id") == product_id:
                return jsonify({"ok": True, "plan_id": existing, "existing": True})
        plan_headers = dict(headers)
        plan_headers["PayPal-Request-Id"] = f"alpha-menu-base-compatible-{date.today().isoformat()}"
        plan_response = requests.post(
            f"{paypal_base_url()}/v1/billing/plans",
            headers=plan_headers,
            json={
                "product_id": product_id,
                "name": "Alpha Menu Base annuale",
                "description": f"Piano Base Alpha Menu, 79 EUR + IVA ({plan_price_with_vat('base')} EUR) ogni anno",
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": "DAY", "interval_count": 14},
                        "tenure_type": "TRIAL",
                        "sequence": 1,
                        "total_cycles": 1,
                        "pricing_scheme": {"fixed_price": {"value": "0", "currency_code": PAYPAL_CURRENCY}},
                    },
                    {
                        "frequency": {"interval_unit": "YEAR", "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 2,
                        "total_cycles": 0,
                        "pricing_scheme": {"fixed_price": {"value": plan_price_with_vat("base"), "currency_code": PAYPAL_CURRENCY}},
                    },
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "setup_fee": {"value": "0", "currency_code": PAYPAL_CURRENCY},
                    "setup_fee_failure_action": "CONTINUE",
                    "payment_failure_threshold": 3,
                },
            },
            timeout=25,
        )
        plan_response.raise_for_status()
        plan_data = plan_response.json()
        plan_id = plan_data["id"]
    except (requests.RequestException, KeyError) as error:
        detail = "PayPal non ha creato il piano Base."
        if isinstance(error, requests.HTTPError) and error.response is not None:
            try:
                detail = error.response.json().get("message") or detail
            except ValueError:
                pass
        return jsonify({"error": detail}), 502

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO impostazioni_app (chiave, valore)
                    VALUES ('paypal_plan_base_id', %s)
                    ON CONFLICT (chiave) DO UPDATE SET valore=EXCLUDED.valore, updated_at=NOW()
                """, (plan_id,))
        if existing and existing != plan_id:
            try:
                requests.post(
                    f"{paypal_base_url()}/v1/billing/plans/{existing}/deactivate",
                    headers={"Authorization": f"Bearer {paypal_access_token()}", "Content-Type": "application/json"}, timeout=20,
                )
            except requests.RequestException:
                pass
        return jsonify({"ok": True, "plan_id": plan_id, "status": plan_data.get("status"), "replaced": bool(existing)})
    finally:
        conn.close()


@app.post("/api/admin/paypal/piani-annuali")
def api_admin_create_annual_paypal_plans():
    """Crea i due piani annuali senza trial; non modifica gli abbonamenti già attivi."""
    denied = require_admin()
    if denied:
        return denied
    if not os.environ.get("PAYPAL_CLIENT_ID") or not os.environ.get("PAYPAL_CLIENT_SECRET"):
        return jsonify({"error": "Credenziali PayPal mancanti su Railway."}), 503
    try:
        token = paypal_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        source_id = os.environ.get("PAYPAL_PLAN_PRO_ID") or os.environ.get("PAYPAL_PLAN_ID") or paypal_plan_id("professional")
        source = requests.get(f"{paypal_base_url()}/v1/billing/plans/{source_id}", headers=headers, timeout=25)
        if source.ok:
            product_id = source.json()["product_id"]
        else:
            product = requests.post(
                f"{paypal_base_url()}/v1/catalogs/products",
                headers={**headers, "PayPal-Request-Id": f"alpha-menu-product-{os.environ.get('PAYPAL_MODE', 'sandbox').lower()}"},
                json={
                    "name": "Alpha Menu",
                    "description": "Servizio annuale per la creazione e gestione di menu digitali",
                    "type": "SERVICE",
                    "category": "SOFTWARE",
                },
                timeout=25,
            )
            product.raise_for_status()
            product_id = product.json()["id"]
        created = {}
        for plan, info in LICENSE_PLANS.items():
            response = requests.post(
                f"{paypal_base_url()}/v1/billing/plans",
                headers={**headers, "PayPal-Request-Id": f"alpha-menu-{plan}-annual-no-trial-{date.today().isoformat()}"},
                json={
                    "product_id": product_id,
                    "name": f"Alpha Menu {info['name']} annuale",
                    "description": f"{info['name']} · {info['price']} EUR + IVA ({plan_price_with_vat(plan)} EUR) ogni anno, senza periodo di prova PayPal",
                    "status": "ACTIVE",
                    "billing_cycles": [{
                        "frequency": {"interval_unit": "YEAR", "interval_count": 1},
                        "tenure_type": "REGULAR", "sequence": 1, "total_cycles": 0,
                        "pricing_scheme": {"fixed_price": {"value": plan_price_with_vat(plan), "currency_code": PAYPAL_CURRENCY}},
                    }],
                    "payment_preferences": {"auto_bill_outstanding": True, "setup_fee": {"value": "0", "currency_code": PAYPAL_CURRENCY}, "setup_fee_failure_action": "CONTINUE", "payment_failure_threshold": 3},
                }, timeout=25,
            )
            response.raise_for_status()
            created[plan] = response.json()["id"]
    except (requests.RequestException, KeyError) as error:
        detail = "PayPal non ha creato i piani annuali."
        if isinstance(error, requests.HTTPError) and error.response is not None:
            try: detail = error.response.json().get("message") or detail
            except ValueError: pass
        return jsonify({"error": detail}), 502
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                for plan, plan_id in created.items():
                    cur.execute("""INSERT INTO impostazioni_app (chiave, valore) VALUES (%s, %s)
                        ON CONFLICT (chiave) DO UPDATE SET valore=EXCLUDED.valore, updated_at=NOW()""", (f"paypal_plan_{plan}_id", plan_id))
        return jsonify({"ok": True, "plans": created})
    finally:
        conn.close()


@app.get("/api/licenza")
def api_license_current():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.stato, l.data_inizio, l.data_scadenza, COALESCE(l.piano, 'professional'),
                       l.piano_programmato, l.cambio_piano_il, COALESCE(a.stato, '')
                FROM licenze_utenti l
                LEFT JOIN abbonamenti_paypal a ON a.id_utente=l.id_utente
                WHERE l.id_utente = %s
            """, (session["user_id"],))
            row = cur.fetchone()
        if not row:
            return jsonify({"item": None})
        remaining = (row[2] - date.today()).days
        return jsonify({"item": {"stato": row[0], "data_inizio": row[1].isoformat(), "data_scadenza": row[2].isoformat(), "giorni_rimanenti": remaining, "piano": normalize_license_plan(row[3]), "nome_piano": LICENSE_PLANS[normalize_license_plan(row[3])]["name"], "piano_programmato": normalize_license_plan(row[4]) if row[4] else None, "cambio_piano_il": row[5].isoformat() if row[5] else None, "in_prova": row[6] == "prova_locale"}})
    finally:
        conn.close()


@app.get("/api/account")
def api_account_get():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT username, COALESCE(email, ''), google_sub IS NOT NULL,
                       COALESCE(password_impostata, TRUE), admin
                FROM utenti WHERE id = %s
            """, (session["user_id"],))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Account non trovato."}), 404
        return jsonify({"item": {
            "username": row[0], "email": row[1], "google_collegato": bool(row[2]),
            "password_impostata": bool(row[3]), "admin": bool(row[4]),
            "google_disponibile": google_enabled(), "google_messaggio": session.pop("account_google_message", None)
        }})
    finally:
        conn.close()


def billing_data_for_user(user_id: int) -> dict | None:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ragione_sociale,partita_iva,codice_fiscale,indirizzo,cap,citta,provincia,
                       nazione,codice_sdi,pec,email_amministrativa,telefono
                FROM dati_fatturazione WHERE id_utente=%s
            """, (user_id,))
            row = cur.fetchone()
        if not row:
            return None
        keys = ("ragione_sociale","partita_iva","codice_fiscale","indirizzo","cap","citta","provincia","nazione","codice_sdi","pec","email_amministrativa","telefono")
        return dict(zip(keys, row))
    finally:
        conn.close()


def billing_data_complete(data: dict | None) -> bool:
    if not data:
        return False
    required = ("ragione_sociale", "indirizzo", "cap", "citta", "provincia", "nazione", "email_amministrativa")
    return (all((data.get(field) or "").strip() for field in required)
            and bool((data.get("partita_iva") or "").strip() or (data.get("codice_fiscale") or "").strip())
            and bool((data.get("codice_sdi") or "").strip() or (data.get("pec") or "").strip()))


@app.route("/dati-fatturazione", methods=["GET", "POST"])
def billing_data():
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id or session.get("is_admin"):
        return redirect(url_for("login"))
    existing = billing_data_for_user(user_id) or {}
    return_to = request.values.get("return_to") or "account"
    if return_to not in ("account", "pagamento", "pagamento_rinnovo", "pagamento_scelta"):
        return_to = "account"
    if request.method == "GET":
        return render_template("billing_data.html", item=existing, return_to=return_to)
    fields = ("ragione_sociale","partita_iva","codice_fiscale","indirizzo","cap","citta","provincia","nazione","codice_sdi","pec","email_amministrativa","telefono")
    data = {field: (request.form.get(field) or "").strip() for field in fields}
    data["partita_iva"] = re.sub(r"\s+", "", data["partita_iva"]).upper()
    data["codice_fiscale"] = re.sub(r"\s+", "", data["codice_fiscale"]).upper()
    data["codice_sdi"] = re.sub(r"\s+", "", data["codice_sdi"]).upper()
    data["pec"] = data["pec"].lower()
    data["email_amministrativa"] = data["email_amministrativa"].lower()
    if not billing_data_complete(data):
        return render_template("billing_data.html", item=data, return_to=return_to, error="Compila i campi obbligatori, almeno Partita IVA o Codice fiscale e almeno Codice SDI o PEC."), 400
    if not valid_email_address(data["email_amministrativa"]) or (data["pec"] and not valid_email_address(data["pec"])):
        return render_template("billing_data.html", item=data, return_to=return_to, error="Controlla l’indirizzo email amministrativo e la PEC."), 400
    if data["nazione"].lower() == "italia" and data["partita_iva"] and not re.fullmatch(r"\d{11}", data["partita_iva"]):
        return render_template("billing_data.html", item=data, return_to=return_to, error="La Partita IVA italiana deve contenere 11 cifre."), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO dati_fatturazione
                      (id_utente,ragione_sociale,partita_iva,codice_fiscale,indirizzo,cap,citta,provincia,nazione,codice_sdi,pec,email_amministrativa,telefono)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id_utente) DO UPDATE SET ragione_sociale=EXCLUDED.ragione_sociale,
                      partita_iva=EXCLUDED.partita_iva,codice_fiscale=EXCLUDED.codice_fiscale,indirizzo=EXCLUDED.indirizzo,
                      cap=EXCLUDED.cap,citta=EXCLUDED.citta,provincia=EXCLUDED.provincia,nazione=EXCLUDED.nazione,
                      codice_sdi=EXCLUDED.codice_sdi,pec=EXCLUDED.pec,email_amministrativa=EXCLUDED.email_amministrativa,
                      telefono=EXCLUDED.telefono,updated_at=NOW()
                """, (user_id, *(data[field] for field in fields)))
    finally:
        conn.close()
    if return_to == "pagamento_rinnovo":
        return redirect(url_for("pagamento", rinnovo=1))
    if return_to == "pagamento_scelta":
        return redirect(url_for("pagamento", scelta=1))
    if return_to == "pagamento":
        return redirect(url_for("pagamento"))
    return redirect(url_for("dashboard_user") + "#account")


@app.route("/api/privacy/richieste", methods=["GET", "POST"])
def api_privacy_requests():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user_id = session["user_id"]
    conn = psycopg2.connect(**build_db_config())
    try:
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            request_type = (data.get("tipo") or "").strip().lower()
            details = (data.get("dettagli") or "").strip()
            allowed = {"accesso", "portabilita", "rettifica", "cancellazione", "limitazione", "opposizione"}
            if request_type not in allowed:
                return jsonify({"error": "Seleziona un tipo di richiesta valido."}), 400
            if len(details) > 2000:
                return jsonify({"error": "I dettagli non possono superare 2.000 caratteri."}), 400
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT username,COALESCE(email,'') FROM utenti WHERE id=%s", (user_id,))
                    account = cur.fetchone()
                    cur.execute("""
                        INSERT INTO richieste_privacy (id_utente,tipo,dettagli)
                        VALUES (%s,%s,%s) RETURNING id,created_at
                    """, (user_id, request_type, details))
                    created = cur.fetchone()
            reference = f"PRIV-{created[0]}"
            if account and account[1]:
                send_transactional_email(account[1], f"Richiesta privacy ricevuta · {reference}",
                    f"Ciao {account[0]},\n\nabbiamo ricevuto la tua richiesta {request_type}.\nRiferimento: {reference}.\nTi aggiorneremo dopo la verifica dell'identità e della richiesta.")
            send_transactional_email(os.getenv("PRIVACY_EMAIL", "alphasystemsrl@gmail.com"),
                f"Nuova richiesta privacy · {reference}",
                f"Cliente: {account[0] if account else user_id}\nTipo: {request_type}\nDettagli: {details or '—'}")
            return jsonify({"ok": True, "riferimento": reference}), 201
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id,tipo,dettagli,stato,note_admin,created_at,updated_at
                FROM richieste_privacy WHERE id_utente=%s ORDER BY created_at DESC
            """, (user_id,))
            items = [{"id": r[0], "riferimento": f"PRIV-{r[0]}", "tipo": r[1], "dettagli": r[2],
                      "stato": r[3], "note": r[4], "data": r[5].isoformat(), "aggiornata": r[6].isoformat()}
                     for r in cur.fetchall()]
        return jsonify({"items": items})
    finally:
        conn.close()


@app.get("/api/privacy/esporta")
def api_privacy_export():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user_id = session["user_id"]
    conn = psycopg2.connect(**build_db_config())
    try:
        result = {"esportato_il": datetime.now().astimezone().isoformat(), "account": {}, "dati_fatturazione": billing_data_for_user(user_id), "licenza": None, "consensi": [], "richieste_privacy": [], "negozi": []}
        with conn.cursor() as cur:
            cur.execute("SELECT id,username,email,admin FROM utenti WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Account non trovato."}), 404
            result["account"] = {"id": row[0], "username": row[1], "email": row[2], "admin": row[3]}
            cur.execute("SELECT stato,data_inizio,data_scadenza,piano,piano_programmato,cambio_piano_il FROM licenze_utenti WHERE id_utente=%s", (user_id,))
            license_row = cur.fetchone()
            if license_row:
                result["licenza"] = dict(zip(("stato","data_inizio","data_scadenza","piano","piano_programmato","cambio_piano_il"), license_row))
            cur.execute("SELECT tipo,versione,accettato,accepted_at FROM consensi_utenti WHERE id_utente=%s ORDER BY accepted_at", (user_id,))
            result["consensi"] = [dict(zip(("tipo","versione","accettato","data"), x)) for x in cur.fetchall()]
            cur.execute("SELECT id,tipo,dettagli,stato,note_admin,created_at,updated_at FROM richieste_privacy WHERE id_utente=%s ORDER BY created_at", (user_id,))
            result["richieste_privacy"] = [dict(zip(("id","tipo","dettagli","stato","note","data","aggiornata"), x)) for x in cur.fetchall()]
            cur.execute("SELECT id,nome,slug,indirizzo,citta,cap,provincia,email,telefono,nazione,descrizione_breve,descrizione_estesa FROM negozi WHERE id_utente=%s", (user_id,))
            for shop in cur.fetchall():
                item = dict(zip(("id","nome","slug","indirizzo","citta","cap","provincia","email","telefono","nazione","descrizione_breve","descrizione_estesa"), shop))
                cur.execute("SELECT id,nome FROM categorie WHERE id_negozio=%s ORDER BY nome", (shop[0],))
                item["categorie"] = [{"id": x[0], "nome": x[1]} for x in cur.fetchall()]
                cur.execute("SELECT id,nome,descrizione,prezzo,note,disponibile FROM prodotti WHERE id_negozio=%s ORDER BY nome", (shop[0],))
                item["prodotti"] = [{"id": x[0], "nome": x[1], "ingredienti": x[2], "prezzo": str(x[3]), "note": x[4], "disponibile": x[5]} for x in cur.fetchall()]
                result["negozi"].append(item)
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        return send_file(io.BytesIO(payload), mimetype="application/json", as_attachment=True,
                         download_name=f"alpha-menu-dati-{date.today().isoformat()}.json")
    finally:
        conn.close()


@app.route("/api/guida-iniziale", methods=["GET", "POST"])
def api_initial_guide():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user_id = session["user_id"]
    conn = psycopg2.connect(**build_db_config())
    try:
        if request.method == "POST":
            with conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE utenti SET guida_iniziale_vista=TRUE WHERE id=%s", (user_id,))
            return jsonify({"ok": True})
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(guida_iniziale_vista,TRUE) FROM utenti WHERE id=%s", (user_id,))
            user_row = cur.fetchone()
            cur.execute("""
                SELECT id, nome, indirizzo, citta, cap, provincia, descrizione_breve, descrizione_estesa,
                       COALESCE(logo_url,''), COALESCE(copertina_url,'')
                FROM negozi WHERE id_utente=%s
            """, (user_id,))
            shop = cur.fetchone()
            shop_id = shop[0] if shop else None
            categories = products = languages = hours = 0
            if shop_id:
                cur.execute("SELECT COUNT(*) FROM categorie WHERE id_negozio=%s", (shop_id,))
                categories = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM prodotti WHERE id_negozio=%s", (shop_id,))
                products = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM lingue_negozio WHERE id_negozio=%s", (shop_id,))
                languages = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM orari_negozio WHERE id_negozio=%s AND aperto=TRUE", (shop_id,))
                hours = cur.fetchone()[0]
        essential_shop = bool(shop and all(shop[index] for index in range(1, 8)))
        branding = bool(shop and shop[8] and shop[9])
        steps = {
            "locale": essential_shop,
            "immagine_orari": branding and hours > 0,
            "categorie": categories > 0,
            "prodotti": products > 0,
            "lingue": languages > 0,
            "pubblicazione": products > 0 and categories > 0,
        }
        return jsonify({
            "mostra_automaticamente": bool(user_row and not user_row[0]),
            "passaggi": steps,
            "completati": sum(1 for value in steps.values() if value),
            "totale": len(steps),
            "conteggi": {"categorie": categories, "prodotti": products, "lingue": languages},
        })
    finally:
        conn.close()


@app.put("/api/account")
def api_account_update():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    current_password = data.get("password_attuale") or ""
    new_password = data.get("nuova_password") or ""
    confirm_password = data.get("conferma_password") or ""

    if not username or not email:
        return jsonify({"error": "Username ed email sono obbligatori."}), 400
    if len(username) > 80 or len(email) > 254:
        return jsonify({"error": "Username o email troppo lunghi."}), 400
    if new_password:
        if len(new_password) < 8:
            return jsonify({"error": "La nuova password deve avere almeno 8 caratteri."}), 400
        if new_password != confirm_password:
            return jsonify({"error": "Le nuove password non coincidono."}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT password, COALESCE(password_impostata, TRUE) FROM utenti WHERE id = %s FOR UPDATE",
                    (session["user_id"],),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Account non trovato."}), 404
                stored_password, password_set = row
                if new_password and password_set and not verify_password(current_password, stored_password):
                    return jsonify({"error": "La password attuale non è corretta."}), 400

                if new_password:
                    cur.execute("""
                        UPDATE utenti
                        SET username=%s, email=%s, password=%s, password_impostata=TRUE
                        WHERE id=%s
                    """, (username, email, hash_password(new_password), session["user_id"]))
                else:
                    cur.execute(
                        "UPDATE utenti SET username=%s, email=%s WHERE id=%s",
                        (username, email, session["user_id"]),
                    )
        session["username"] = username
        return jsonify({"ok": True, "message": "Account aggiornato correttamente."})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username o email già utilizzati da un altro account."}), 409
    finally:
        conn.close()


@app.get("/api/lingue")
def api_languages_get():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    if get_user_license_plan(session["user_id"]) != "professional":
        return jsonify({"error": "Le lingue aggiuntive richiedono la licenza Professional."}), 403
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"items": [], "disponibili": SUPPORTED_MENU_LANGUAGES, "api_configurata": bool(os.environ.get("GOOGLE_TRANSLATE_API_KEY"))})
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT codice FROM lingue_negozio WHERE id_negozio=%s ORDER BY codice", (shop_id,))
            enabled = [row[0] for row in cur.fetchall()]
        return jsonify({"items": enabled, "disponibili": SUPPORTED_MENU_LANGUAGES, "api_configurata": bool(os.environ.get("GOOGLE_TRANSLATE_API_KEY"))})
    finally:
        conn.close()


@app.put("/api/lingue")
def api_languages_save():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    if get_user_license_plan(session["user_id"]) != "professional":
        return jsonify({"error": "Le lingue aggiuntive richiedono la licenza Professional."}), 403
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "Salva prima le informazioni del negozio."}), 400
    data = request.get_json(silent=True) or {}
    languages = list(dict.fromkeys(data.get("lingue") or []))
    if any(code not in SUPPORTED_MENU_LANGUAGES for code in languages):
        return jsonify({"error": "Una o più lingue non sono supportate."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM lingue_negozio WHERE id_negozio=%s", (shop_id,))
                for code in languages:
                    cur.execute("INSERT INTO lingue_negozio (id_negozio, codice) VALUES (%s, %s)", (shop_id, code))
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/traduzioni/genera")
def api_translations_generate():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    if get_user_license_plan(session["user_id"]) != "professional":
        return jsonify({"error": "La traduzione automatica richiede la licenza Professional."}), 403
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "Salva prima le informazioni del negozio."}), 400
    requested = (request.get_json(silent=True) or {}).get("lingue") or []
    languages = [code for code in dict.fromkeys(requested) if code in SUPPORTED_MENU_LANGUAGES]
    if not languages:
        return jsonify({"error": "Seleziona almeno una lingua."}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                entries = []
                cur.execute("SELECT id, descrizione_breve, descrizione_estesa FROM negozi WHERE id=%s", (shop_id,))
                shop = cur.fetchone()
                if shop:
                    for field, value in (("descrizione_breve", shop[1]), ("descrizione_estesa", shop[2])):
                        if value:
                            entries.append(("negozio", shop[0], field, value))

                cur.execute("SELECT id, nome FROM categorie WHERE id_negozio=%s", (shop_id,))
                entries += [("categoria", row[0], "nome", row[1]) for row in cur.fetchall() if row[1]]
                cur.execute("SELECT sc.id, sc.nome FROM sottocategorie sc JOIN categorie c ON c.id = sc.id_categoria WHERE c.id_negozio=%s", (shop_id,))
                entries += [("sottocategoria", row[0], "nome", row[1]) for row in cur.fetchall() if row[1]]
                cur.execute("SELECT id, nome, descrizione, note, etichette, allergeni_auto FROM prodotti WHERE id_negozio=%s", (shop_id,))
                for row in cur.fetchall():
                    for field, value in (("nome", row[1]), ("descrizione", row[2]), ("note", row[3])):
                        if value:
                            entries.append(("prodotto", row[0], field, value))
                    for index, value in enumerate(row[4] or []):
                        if value:
                            entries.append(("prodotto", row[0], f"etichetta_{index}", value))
                    for index, value in enumerate(row[5] or []):
                        if value:
                            entries.append(("prodotto", row[0], f"allergene_{index}", value))

                total = 0
                unchanged = 0
                for language in languages:
                    cur.execute("""
                        SELECT tipo, id_entita, campo, testo_originale
                        FROM traduzioni_menu
                        WHERE id_negozio=%s AND lingua=%s
                    """, (shop_id, language))
                    originals = {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}
                    pending = [
                        entry for entry in entries
                        if originals.get((entry[0], entry[1], entry[2])) != entry[3]
                    ]
                    unchanged += len(entries) - len(pending)
                    source_texts = [entry[3] for entry in pending]
                    translated = []
                    for offset in range(0, len(source_texts), 100):
                        translated.extend(google_translate_texts(source_texts[offset:offset + 100], language))
                    for entry, translated_text in zip(pending, translated):
                        if entry[3].isupper():
                            translated_text = translated_text.upper()
                        cur.execute("""
                            INSERT INTO traduzioni_menu
                                (id_negozio, tipo, id_entita, campo, lingua, testo, testo_originale)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (id_negozio, tipo, id_entita, campo, lingua)
                            DO UPDATE SET
                                testo=EXCLUDED.testo,
                                testo_originale=EXCLUDED.testo_originale,
                                updated_at=NOW()
                        """, (shop_id, entry[0], entry[1], entry[2], language, translated_text, entry[3]))
                        total += 1
                    cur.execute("INSERT INTO lingue_negozio (id_negozio, codice) VALUES (%s,%s) ON CONFLICT DO NOTHING", (shop_id, language))
        return jsonify({"ok": True, "traduzioni": total, "inalterate": unchanged})
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 502
    finally:
        conn.close()


@app.get("/api/admin/utenti")
def api_admin_users_list():
    denied = require_admin()
    if denied:
        return denied
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, COALESCE(u.email, ''), u.admin, COALESCE(n.nome, ''),
                       COALESCE(l.stato, 'sospesa'), l.data_inizio, l.data_scadenza, COALESCE(l.piano, 'professional'),
                       COALESCE(a.stato, ''), COALESCE(a.subscription_id, ''),
                       EXISTS(SELECT 1 FROM dati_fatturazione df WHERE df.id_utente=u.id)
                FROM utenti u
                LEFT JOIN negozi n ON n.id_utente = u.id
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
                LEFT JOIN abbonamenti_paypal a ON a.id_utente = u.id
                ORDER BY u.id
            """)
            items = []
            for row in cur.fetchall():
                expiry = row[7]
                days = (expiry - date.today()).days if expiry else None
                items.append({"id": row[0], "username": row[1], "email": row[2], "admin": bool(row[3]),
                              "negozio": row[4], "stato_licenza": row[5],
                              "data_inizio": row[6].isoformat() if row[6] else None,
                              "data_scadenza": expiry.isoformat() if expiry else None,
                              "giorni_rimanenti": days,
                              "in_scadenza": days is not None and 0 <= days <= 30,
                              "piano": normalize_license_plan(row[8]),
                              "in_prova": row[9] == "prova_locale",
                              "fonte_pagamento": row[9],
                              "paypal_collegato": bool(row[10]), "fatturazione_presente": bool(row[11])})
        return jsonify({"items": items, "current_user_id": session["user_id"]})
    finally:
        conn.close()


@app.get("/api/admin/utenti/<int:user_id>/dati-fatturazione")
def api_admin_user_billing(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    data = billing_data_for_user(user_id)
    if not data:
        return jsonify({"error": "Il cliente non ha ancora inserito i dati di fatturazione."}), 404
    return jsonify({"item": data, "completi": billing_data_complete(data)})


@app.get("/api/admin/privacy/richieste")
def api_admin_privacy_requests():
    denied = require_admin()
    if denied:
        return denied
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.id,r.tipo,r.dettagli,r.stato,r.note_admin,r.created_at,r.updated_at,
                       u.id,u.username,COALESCE(u.email,'')
                FROM richieste_privacy r JOIN utenti u ON u.id=r.id_utente
                ORDER BY CASE r.stato WHEN 'ricevuta' THEN 0 WHEN 'in_lavorazione' THEN 1 ELSE 2 END,r.created_at DESC
            """)
            items = [{"id": r[0], "riferimento": f"PRIV-{r[0]}", "tipo": r[1], "dettagli": r[2],
                      "stato": r[3], "note": r[4], "data": r[5].isoformat(), "aggiornata": r[6].isoformat(),
                      "id_utente": r[7], "cliente": r[8], "email": r[9]} for r in cur.fetchall()]
        return jsonify({"items": items})
    finally:
        conn.close()


@app.patch("/api/admin/privacy/richieste/<int:request_id>")
def api_admin_privacy_request_update(request_id: int):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    status = (data.get("stato") or "").strip()
    notes = (data.get("note") or "").strip()
    if status not in {"ricevuta", "in_lavorazione", "completata", "rifiutata"}:
        return jsonify({"error": "Stato non valido."}), 400
    if len(notes) > 2000:
        return jsonify({"error": "Le note non possono superare 2.000 caratteri."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE richieste_privacy SET stato=%s,note_admin=%s,updated_at=NOW(),
                      completed_at=CASE WHEN %s IN ('completata','rifiutata') THEN NOW() ELSE NULL END
                    WHERE id=%s RETURNING id_utente
                """, (status, notes, status, request_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Richiesta non trovata."}), 404
                cur.execute("SELECT username,COALESCE(email,'') FROM utenti WHERE id=%s", (row[0],))
                account = cur.fetchone()
        if account and account[1]:
            send_transactional_email(account[1], f"Aggiornamento richiesta privacy · PRIV-{request_id}",
                f"Ciao {account[0]},\n\nla tua richiesta PRIV-{request_id} è ora: {status.replace('_', ' ')}.\n\n{notes}")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/admin/monitoraggio")
def api_admin_monitoring():
    denied = require_admin()
    if denied:
        return denied
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT evento,COUNT(*) FROM eventi_commerciali
                WHERE created_at>=NOW()-INTERVAL '30 days' GROUP BY evento
            """)
            metrics = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("""
                SELECT provenienza,COUNT(*) FROM eventi_commerciali
                WHERE created_at>=NOW()-INTERVAL '30 days' AND evento IN ('registrazione','trial_avviato')
                GROUP BY provenienza ORDER BY COUNT(*) DESC LIMIT 10
            """)
            sources = [{"nome": row[0], "totale": row[1]} for row in cur.fetchall()]
            cur.execute("""
                SELECT area,gravita,messaggio,created_at FROM errori_operativi
                WHERE risolto=FALSE ORDER BY created_at DESC LIMIT 30
            """)
            errors = [{"area": row[0], "gravita": row[1], "messaggio": row[2], "data": row[3].isoformat()} for row in cur.fetchall()]
        return jsonify({"metriche_30_giorni": metrics, "provenienze": sources, "errori_aperti": errors})
    finally:
        conn.close()


def admin_paypal_subscription_row(user_id: int):
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.username, a.subscription_id, COALESCE(a.plan_id, ''), COALESCE(a.stato, ''),
                       a.prossimo_addebito, a.cancellato_il
                FROM utenti u
                LEFT JOIN abbonamenti_paypal a ON a.id_utente=u.id
                WHERE u.id=%s
            """, (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def paypal_admin_snapshot(user_id: int) -> tuple[dict | None, tuple | None]:
    row = admin_paypal_subscription_row(user_id)
    if not row or not row[1]:
        return None, row
    details = paypal_get_subscription(row[1])
    return details, row


@app.get("/api/admin/paypal/abbonamenti/<int:user_id>")
def api_admin_paypal_subscription_status(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    try:
        details, row = paypal_admin_snapshot(user_id)
    except requests.RequestException as error:
        status_code = error.response.status_code if getattr(error, "response", None) is not None else None
        record_operational_error("paypal", f"Lettura abbonamento fallita HTTP {status_code or 'rete'}", user_id)
        return jsonify({"error": f"PayPal non ha restituito lo stato dell'abbonamento{f' (HTTP {status_code})' if status_code else ''}."}), 502
    if not row:
        return jsonify({"error": "Utente non trovato."}), 404
    local = {
        "subscription_id": row[1] or "",
        "plan_id": row[2],
        "stato": row[3] or "nessun abbonamento",
        "prossimo_addebito": row[4].isoformat() if row[4] else None,
        "cancellato_il": row[5].isoformat() if row[5] else None,
    }
    remote = None
    if details:
        next_billing = details.get("billing_info", {}).get("next_billing_time")
        next_billing_date = parse_paypal_date(next_billing)
        remote = {
            "stato": details.get("status", "SCONOSCIUTO"),
            "plan_id": details.get("plan_id", ""),
            "prossimo_addebito": next_billing_date.isoformat() if next_billing_date else None,
        }
    return jsonify({"ok": True, "cliente": row[0], "locale": local, "paypal": remote})


@app.post("/api/admin/paypal/abbonamenti/<int:user_id>/sincronizza")
def api_admin_paypal_subscription_sync(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    try:
        details, row = paypal_admin_snapshot(user_id)
    except requests.RequestException as error:
        status_code = error.response.status_code if getattr(error, "response", None) is not None else None
        record_operational_error("paypal", f"Sincronizzazione fallita HTTP {status_code or 'rete'}", user_id)
        return jsonify({"error": f"Sincronizzazione PayPal non riuscita{f' (HTTP {status_code})' if status_code else ''}."}), 502
    if not row:
        return jsonify({"error": "Utente non trovato."}), 404
    if not details:
        return jsonify({"error": "Questo cliente non ha un abbonamento PayPal collegato."}), 404
    remote_status = (details.get("status") or "").upper()
    local_status = {
        "ACTIVE": "attivo", "APPROVAL_PENDING": "in_attesa", "APPROVED": "in_attesa",
        "SUSPENDED": "sospeso", "CANCELLED": "cancellato", "EXPIRED": "scaduto",
    }.get(remote_status, remote_status.lower() or "sconosciuto")
    next_billing = parse_paypal_date(details.get("billing_info", {}).get("next_billing_time"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE abbonamenti_paypal
                    SET plan_id=%s, stato=%s, prossimo_addebito=%s,
                        cancellato_il=CASE WHEN %s IN ('cancellato','scaduto') THEN COALESCE(cancellato_il,NOW()) ELSE NULL END,
                        updated_at=NOW()
                    WHERE id_utente=%s
                """, (details.get("plan_id"), local_status, next_billing, local_status, user_id))
                if remote_status == "ACTIVE":
                    cur.execute("""
                        UPDATE licenze_utenti SET stato='attiva',
                            data_scadenza=COALESCE(%s, data_scadenza), updated_at=NOW()
                        WHERE id_utente=%s
                    """, (next_billing, user_id))
        return jsonify({"ok": True, "stato": remote_status, "prossimo_addebito": next_billing.isoformat() if next_billing else None})
    finally:
        conn.close()


@app.post("/api/admin/paypal/abbonamenti/<int:user_id>/disdici")
def api_admin_paypal_subscription_cancel(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    row = admin_paypal_subscription_row(user_id)
    if not row:
        return jsonify({"error": "Utente non trovato."}), 404
    if not row[1]:
        return jsonify({"error": "Questo cliente non ha un abbonamento PayPal collegato."}), 404
    try:
        details = paypal_get_subscription(row[1])
        remote_status = (details.get("status") or "").upper()
        already_closed = remote_status in ("CANCELLED", "EXPIRED")
        if not already_closed:
            paypal_cancel_subscription_by_id(row[1], "Disdetta richiesta dal Superadmin")
    except (requests.RequestException, RuntimeError) as error:
        app.logger.warning("Disdetta PayPal Superadmin non riuscita per utente %s: %s", user_id, error)
        record_operational_error("paypal", f"Disdetta non confermata: {error}", user_id, "critico")
        return jsonify({"error": f"Disdetta non confermata: {error} Nessuna modifica locale è stata effettuata."}), 502
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
        message = "L'abbonamento risultava già terminato su PayPal. Stato locale sincronizzato." if already_closed else "Rinnovo PayPal disdetto. La licenza resta valida fino alla scadenza corrente."
        return jsonify({"ok": True, "message": message})
    finally:
        conn.close()


@app.post("/api/admin/paypal/abbonamenti/<int:user_id>/scollega-test")
def api_admin_paypal_subscription_unlink_test(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    row = admin_paypal_subscription_row(user_id)
    if not row:
        return jsonify({"error": "Utente non trovato."}), 404
    subscription_id = row[1]
    if not subscription_id:
        return jsonify({"error": "Questo cliente non ha un abbonamento PayPal collegato."}), 404
    try:
        paypal_get_subscription(subscription_id)
    except requests.HTTPError as error:
        status_code = error.response.status_code if error.response is not None else None
        if status_code != 404:
            return jsonify({"error": f"Impossibile verificare l'abbonamento su PayPal (HTTP {status_code or 'errore di rete'}). Collegamento non rimosso."}), 502
    except (requests.RequestException, RuntimeError):
        return jsonify({"error": "Impossibile verificare l'abbonamento su PayPal. Collegamento non rimosso."}), 502
    else:
        return jsonify({"error": "L'abbonamento esiste nell'ambiente PayPal attivo. Devi disdirlo prima di scollegarlo."}), 409

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE abbonamenti_paypal
                    SET subscription_id=NULL, stato='manuale', cancellato_il=NOW(), updated_at=NOW()
                    WHERE id_utente=%s
                """, (user_id,))
        return jsonify({"ok": True, "message": "Abbonamento di test scollegato. La licenza locale non è stata modificata."})
    finally:
        conn.close()


@app.post("/api/admin/utenti")
def api_admin_users_create():
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower() or None
    password = data.get("password") or ""
    is_admin = bool(data.get("admin"))
    plan = normalize_license_plan(data.get("piano"))
    if not username or not password:
        return jsonify({"error": "Username e password sono obbligatori."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO utenti (username, email, password, admin, guida_iniziale_vista) VALUES (%s, %s, %s, %s, %s) RETURNING id", (username, email, hash_password(password), is_admin, is_admin))
                user_id = cur.fetchone()[0]
                cur.execute("INSERT INTO licenze_utenti (id_utente, data_scadenza, piano) VALUES (%s, %s, %s)", (user_id, annual_expiry(), plan))
        return jsonify({"ok": True, "id": user_id}), 201
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username o email già utilizzati."}), 409
    finally:
        conn.close()


@app.put("/api/admin/utenti/<int:user_id>")
def api_admin_users_update(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower() or None
    password = data.get("password") or ""
    is_admin = bool(data.get("admin"))
    if not username:
        return jsonify({"error": "Lo username è obbligatorio."}), 400
    if user_id == session["user_id"] and not is_admin:
        return jsonify({"error": "Non puoi rimuovere il ruolo amministratore dal tuo account."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                if password:
                    cur.execute("UPDATE utenti SET username=%s, email=%s, password=%s, admin=%s WHERE id=%s", (username, email, hash_password(password), is_admin, user_id))
                else:
                    cur.execute("UPDATE utenti SET username=%s, email=%s, admin=%s WHERE id=%s", (username, email, is_admin, user_id))
                if not cur.rowcount:
                    return jsonify({"error": "Utente non trovato."}), 404
        if user_id == session["user_id"]:
            session["username"] = username
        return jsonify({"ok": True})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Username o email già utilizzati."}), 409
    finally:
        conn.close()


@app.put("/api/admin/licenze/<int:user_id>")
def api_admin_license_update(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    status = data.get("stato")
    expiry_raw = data.get("data_scadenza")
    raw_plan = (data.get("piano") or "").strip().lower()
    confirm_paypal_cancel = data.get("conferma_annullamento_paypal") is True
    force_manual = data.get("forza_gestione_manuale") is True
    if raw_plan not in {"trial", *LICENSE_PLANS}:
        return jsonify({"error": "Piano licenza non valido."}), 400
    is_trial = raw_plan == "trial"
    plan = "professional" if is_trial else normalize_license_plan(raw_plan)
    if status not in {"attiva", "sospesa"}:
        return jsonify({"error": "Stato licenza non valido."}), 400
    try:
        expiry = date.fromisoformat(expiry_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "Data di scadenza non valida."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT subscription_id, stato FROM abbonamenti_paypal WHERE id_utente=%s FOR UPDATE", (user_id,))
                subscription = cur.fetchone()
                converted_from_trial = bool(subscription and subscription[1] == "prova_locale" and not is_trial)
                active_paypal_subscription = bool(subscription and subscription[0] and subscription[1] not in {"cancellato", "scaduto"})
                if active_paypal_subscription and not confirm_paypal_cancel:
                    return jsonify({
                        "error": "Questo cliente ha un abbonamento PayPal attivo. Conferma per annullarlo e passare alla gestione manuale.",
                        "codice": "conferma_annullamento_paypal",
                    }), 409
                if active_paypal_subscription and not force_manual:
                    try:
                        paypal_cancel_subscription_by_id(subscription[0], "Passaggio a licenza gestita manualmente dall'amministratore")
                    except (requests.RequestException, RuntimeError) as error:
                        app.logger.warning("Disdetta PayPal non confermata per utente %s: %s", user_id, error)
                        return jsonify({
                            "error": "PayPal non ha confermato la disdetta. Verifica e annulla l'abbonamento dal pannello PayPal; poi puoi forzare il passaggio alla gestione manuale.",
                            "codice": "disdetta_paypal_non_confermata",
                        }), 409
                if converted_from_trial:
                    status = "attiva"
                    expiry = annual_expiry()
                cur.execute("""
                    INSERT INTO licenze_utenti (id_utente, stato, data_scadenza, piano)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id_utente) DO UPDATE
                    SET stato=EXCLUDED.stato, data_scadenza=EXCLUDED.data_scadenza, piano=EXCLUDED.piano, updated_at=NOW()
                """, (user_id, status, expiry, plan))
                if converted_from_trial:
                    cur.execute("UPDATE licenze_utenti SET data_inizio=CURRENT_DATE WHERE id_utente=%s", (user_id,))
                if is_trial:
                    cur.execute("""
                        INSERT INTO abbonamenti_paypal (id_utente, plan_id, stato, trial_fino, prossimo_addebito, updated_at)
                        VALUES (%s, %s, 'prova_locale', %s, %s, NOW())
                        ON CONFLICT (id_utente) DO UPDATE SET subscription_id=NULL, plan_id=EXCLUDED.plan_id, stato='prova_locale',
                            trial_fino=EXCLUDED.trial_fino, prossimo_addebito=EXCLUDED.prossimo_addebito, cancellato_il=NULL, updated_at=NOW()
                    """, (user_id, paypal_plan_id("professional"), expiry, expiry))
                else:
                    # Bonifico e contanti: licenza attiva senza rinnovo PayPal.
                    cur.execute("""
                        INSERT INTO abbonamenti_paypal
                            (id_utente, subscription_id, plan_id, stato, trial_fino, prossimo_addebito, ultimo_pagamento, cancellato_il, updated_at)
                        VALUES (%s, NULL, NULL, 'manuale', NULL, NULL, NOW(), NULL, NOW())
                        ON CONFLICT (id_utente) DO UPDATE
                        SET subscription_id=NULL, plan_id=NULL, stato='manuale', trial_fino=NULL,
                            prossimo_addebito=NULL, ultimo_pagamento=NOW(), cancellato_il=NULL, updated_at=NOW()
                    """, (user_id,))
        return jsonify({
            "ok": True,
            "piano": plan,
            "data_scadenza": expiry.isoformat(),
            "conversione_trial": converted_from_trial,
            "gestione_forzata": bool(active_paypal_subscription and force_manual),
        })
    finally:
        conn.close()


@app.post("/api/admin/licenze/<int:user_id>/rinnova")
def api_admin_license_renew(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza)
                    VALUES (%s, 'attiva', CURRENT_DATE, CURRENT_DATE + 365)
                    ON CONFLICT (id_utente) DO UPDATE
                    SET stato='attiva',
                        data_scadenza=GREATEST(CURRENT_DATE, licenze_utenti.data_scadenza) + 365,
                        updated_at=NOW()
                    RETURNING data_scadenza
                """, (user_id,))
                expiry = cur.fetchone()[0]
        return jsonify({"ok": True, "data_scadenza": expiry.isoformat()})
    finally:
        conn.close()


@app.delete("/api/admin/licenze/<int:user_id>")
def api_admin_license_delete(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM utenti WHERE id=%s", (user_id,))
                if not cur.fetchone():
                    return jsonify({"error": "Utente non trovato."}), 404
                cur.execute("SELECT subscription_id FROM abbonamenti_paypal WHERE id_utente=%s FOR UPDATE", (user_id,))
                subscription = cur.fetchone()
                if subscription and subscription[0]:
                    try:
                        paypal_cancel_subscription_by_id(subscription[0], "Licenza revocata dall'amministratore")
                    except RuntimeError as error:
                        return jsonify({"error": str(error)}), 502
                cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
                cur.execute("DELETE FROM licenze_utenti WHERE id_utente=%s", (user_id,))
                if not cur.rowcount:
                    return jsonify({"error": "Licenza non trovata."}), 404
        return jsonify({"ok": True, "message": "Licenza eliminata e rinnovo automatico disattivato."})
    finally:
        conn.close()


@app.delete("/api/admin/utenti/<int:user_id>")
def api_admin_user_delete(user_id: int):
    denied = require_admin()
    if denied:
        return denied
    if user_id == session.get("user_id"):
        return jsonify({"error": "Non puoi eliminare l'account amministratore con cui hai effettuato l'accesso."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM utenti WHERE id=%s FOR UPDATE", (user_id,))
                user = cur.fetchone()
                if not user:
                    return jsonify({"error": "Utente non trovato."}), 404
                cur.execute("SELECT subscription_id FROM abbonamenti_paypal WHERE id_utente=%s", (user_id,))
                subscription = cur.fetchone()
                if subscription and subscription[0]:
                    try:
                        paypal_cancel_subscription_by_id(subscription[0], "Account eliminato dall'amministratore")
                    except RuntimeError as error:
                        return jsonify({"error": str(error)}), 502
                # I dati del negozio vengono rimossi per primi: le relative FK eliminano
                # categorie, prodotti, orari, lingue, traduzioni e statistiche collegate.
                cur.execute("DELETE FROM negozi WHERE id_utente=%s", (user_id,))
                cur.execute("DELETE FROM utenti WHERE id=%s", (user_id,))
        return jsonify({"ok": True, "message": f"Utente {user[0]} e dati collegati eliminati."})
    except psycopg2.IntegrityError:
        return jsonify({"error": "Impossibile eliminare l'utente: esistono dati collegati non rimovibili automaticamente."}), 409
    finally:
        conn.close()


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.get("/dashboard/scelta")
def dashboard_choice():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("is_admin"):
        return redirect("/dashboard_admin")
    return render_template("dashboard_choice.html", username=session.get("username", "utente"))

@app.route("/dashboard_user")
def dashboard_user():
    if "user_id" not in session:
        return redirect("/login")

    # sezione iniziale
    license_plan = get_user_license_plan(session["user_id"])
    return render_template(
        "dashboard_user.html",
        username=session.get("username", "utente"),
        active_section="home",
        shop_configured=get_user_shop_id(session["user_id"]) is not None,
        license_plan=license_plan,
    )

@app.route("/dashboard_user/section/<section>")
def dashboard_user_section(section: str):
    if "user_id" not in session:
        abort(401)

    allowed = {
        "home",
        "attivita",
        "menu_online",
        "ordini",
        "prodotti",
        "categorie",
        "sottocategorie",
        "allergeni",
        "negozio",
        "orari",
        "qrcode",
        "anteprima",
        "licenze",
        "lingue",
        "statistiche",
        "account",
    }
    if section not in allowed:
        abort(404)

    if section in {"lingue", "statistiche"} and get_user_license_plan(session["user_id"]) != "professional":
        return (
            '<div class="error"><b>Funzione disponibile con la licenza Professional.</b>'
            '<br>Puoi chiedere all’amministratore il passaggio al piano Professional.</div>',
            403,
        )

    shop_required_sections = {
        "prodotti", "categorie", "sottocategorie", "allergeni",
        "menu_online", "ordini", "qrcode", "anteprima", "lingue", "statistiche",
    }
    if section in shop_required_sections and not get_user_shop_id(session["user_id"]):
        return (
            '<div class="error"><b>Completa prima l’anagrafica del negozio.</b>'
            '<br>Salva i dati nella sezione “Negozio e orari” per abilitare questa funzione.</div>',
            409,
        )

    # Qui in futuro carichi dati da DB per ogni sezione
    # Esempio: if section == "prodotti": products = ...
    # return render_template("sections/prodotti.html", products=products)

    return render_template(
        f"sections/{section}.html",
        username=session.get("username", "utente"),
        license_plan=get_user_license_plan(session["user_id"]),
    )
def get_user_shop_id(user_id: int) -> int | None:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM negozi WHERE id_utente = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


@app.route("/api/negozio", methods=["GET", "POST"])
def api_negozio():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    user_id = session["user_id"]
    fields = (
        "nome", "indirizzo", "citta", "cap", "provincia", "email",
        "telefono", "nazione", "descrizione_breve", "descrizione_estesa",
        "whatsapp", "prenotazione_url", "sito_web", "instagram_url", "google_maps_url", "colore_accento", "colore_sfondo", "costo_coperto",
    )
    required_fields = ("nome", "indirizzo", "citta", "cap", "provincia", "descrizione_breve", "descrizione_estesa")
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                if request.method == "GET":
                    cur.execute(
                        "SELECT id, " + ", ".join(fields) + ", logo_url, copertina_url FROM negozi WHERE id_utente = %s",
                        (user_id,),
                    )
                    row = cur.fetchone()
                    item = (
                        {
                            "id": row[0],
                            **dict(zip(fields, row[1:1 + len(fields)])),
                            "logo_url": row[1 + len(fields)] or "",
                            "copertina_url": row[2 + len(fields)] or "",
                        }
                        if row else None
                    )
                    return jsonify({"item": item})

                data = request.get_json(silent=True) or {}
                values = {field: (data.get(field) or "").strip() for field in fields}
                if values["prenotazione_url"] and not re.match(r"^https?://", values["prenotazione_url"], re.IGNORECASE):
                    return jsonify({"error": "Il link prenotazioni deve iniziare con http:// o https://.", "fields": ["prenotazione_url"]}), 400
                values["colore_accento"] = values["colore_accento"] if re.fullmatch(r"#[0-9a-fA-F]{6}", values["colore_accento"]) else "#9d3e27"
                values["colore_sfondo"] = values["colore_sfondo"] if re.fullmatch(r"#[0-9a-fA-F]{6}", values["colore_sfondo"]) else "#f7f3ed"
                cover_value = values["costo_coperto"].replace(",", ".") or "0"
                if not re.fullmatch(r"\d{1,7}(?:\.\d{1,2})?", cover_value):
                    return jsonify({"error": "Il costo del coperto non è valido.", "fields": ["costo_coperto"]}), 400
                values["costo_coperto"] = f"{float(cover_value):.2f}"
                missing = [field for field in required_fields if not values[field]]
                if missing:
                    return jsonify({"error": "campi obbligatori mancanti", "fields": missing}), 400

                cur.execute("SELECT id FROM negozi WHERE id_utente = %s", (user_id,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE negozi
                        SET nome=%s, indirizzo=%s, citta=%s, cap=%s, provincia=%s,
                            email=%s, telefono=%s, nazione=%s, descrizione_breve=%s, descrizione_estesa=%s,
                            whatsapp=%s, prenotazione_url=%s, sito_web=%s, instagram_url=%s, google_maps_url=%s,
                            colore_accento=%s, colore_sfondo=%s, costo_coperto=%s
                        WHERE id = %s
                        """,
                        [values[field] for field in fields] + [row[0]],
                    )
                    shop_id = row[0]
                else:
                    slug_base = re.sub(r"[^a-z0-9]+", "-", values["nome"].lower()).strip("-") or "negozio"
                    slug = f"{slug_base}-{user_id}"
                    cur.execute(
                        """
                        INSERT INTO negozi (
                            id_utente, nome, indirizzo, citta, cap, provincia, email, telefono,
                            nazione, descrizione_breve, descrizione_estesa, whatsapp, prenotazione_url, sito_web, instagram_url, google_maps_url, colore_accento, colore_sfondo, costo_coperto, slug
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        [user_id] + [values[field] for field in fields] + [slug],
                    )
                    shop_id = cur.fetchone()[0]
                # Il nome visualizzato dell'account coincide sempre con l'attività.
                cur.execute("UPDATE utenti SET username=%s WHERE id=%s", (values["nome"], user_id))
                session["username"] = values["nome"]

        return jsonify({"ok": True, "id": shop_id, "item": values})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante il salvataggio del negozio.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500
    finally:
        conn.close()


@app.post("/api/negozio/immagini")
def api_negozio_immagini():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "Salva prima le informazioni del punto vendita."}), 400

    uploads = {
        "logo": request.files.get("logo"),
        "copertina": request.files.get("copertina"),
    }
    selected = {kind: file for kind, file in uploads.items() if file and file.filename}
    if not selected:
        return jsonify({"error": "Seleziona almeno un'immagine da caricare."}), 400

    try:
        urls = {kind: save_shop_image(file, shop_id, kind) for kind, file in selected.items()}
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                for kind, url in urls.items():
                    column = "logo_url" if kind == "logo" else "copertina_url"
                    cur.execute(f"UPDATE negozi SET {column} = %s WHERE id = %s", (url, shop_id))
    finally:
        conn.close()

    return jsonify({"ok": True, **urls})


@app.route("/api/orari", methods=["GET", "POST"])
def api_orari():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    defaults = [
        {
            "giorno": day, "aperto": False, "apertura": "09:00", "chiusura": "13:00",
            "secondo_turno": False, "apertura_2": "14:00", "chiusura_2": "18:00",
        }
        for day in range(7)
    ]
    if not shop_id:
        if request.method == "GET":
            return jsonify({"items": defaults})
        return jsonify({"error": "Salva prima le informazioni del punto vendita."}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        if request.method == "GET":
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT giorno, aperto, apertura, chiusura, apertura_2, chiusura_2
                    FROM orari_negozio WHERE id_negozio = %s ORDER BY giorno
                    """,
                    (shop_id,),
                )
                saved = {
                    row[0]: {
                        "giorno": row[0],
                        "aperto": bool(row[1]),
                        "apertura": row[2].strftime("%H:%M") if row[2] else "09:00",
                        "chiusura": row[3].strftime("%H:%M") if row[3] else "13:00",
                        "secondo_turno": bool(row[4] and row[5]),
                        "apertura_2": row[4].strftime("%H:%M") if row[4] else "14:00",
                        "chiusura_2": row[5].strftime("%H:%M") if row[5] else "18:00",
                    }
                    for row in cur.fetchall()
                }
            return jsonify({"items": [saved.get(day, defaults[day]) for day in range(7)]})

        data = request.get_json(silent=True) or {}
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 7:
            return jsonify({"error": "Invia gli orari per tutti i sette giorni."}), 400

        normalized = []
        seen_days = set()
        time_pattern = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
        for item in items:
            try:
                day = int(item.get("giorno"))
            except (TypeError, ValueError, AttributeError):
                return jsonify({"error": "Giorno non valido."}), 400
            if day not in range(7) or day in seen_days:
                return jsonify({"error": "Giorni mancanti o duplicati."}), 400
            seen_days.add(day)
            is_open = bool(item.get("aperto"))
            has_second_shift = is_open and bool(item.get("secondo_turno"))
            opening = (item.get("apertura") or "").strip()
            closing = (item.get("chiusura") or "").strip()
            opening_2 = (item.get("apertura_2") or "").strip()
            closing_2 = (item.get("chiusura_2") or "").strip()

            if is_open and (not time_pattern.match(opening) or not time_pattern.match(closing)):
                return jsonify({"error": "Inserisci orari validi per il primo turno."}), 400
            if is_open and opening == closing:
                return jsonify({"error": "Apertura e chiusura del primo turno devono essere diverse."}), 400
            if has_second_shift and (not time_pattern.match(opening_2) or not time_pattern.match(closing_2)):
                return jsonify({"error": "Inserisci orari validi per il secondo turno."}), 400
            if has_second_shift and opening_2 == closing_2:
                return jsonify({"error": "Apertura e chiusura del secondo turno devono essere diverse."}), 400

            normalized.append((
                day, is_open, opening if is_open else None, closing if is_open else None,
                opening_2 if has_second_shift else None, closing_2 if has_second_shift else None,
            ))

        with conn:
            with conn.cursor() as cur:
                for day, is_open, opening, closing, opening_2, closing_2 in normalized:
                    cur.execute(
                        """
                        INSERT INTO orari_negozio
                            (id_negozio, giorno, aperto, apertura, chiusura, apertura_2, chiusura_2)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id_negozio, giorno) DO UPDATE SET
                            aperto = EXCLUDED.aperto,
                            apertura = EXCLUDED.apertura,
                            chiusura = EXCLUDED.chiusura,
                            apertura_2 = EXCLUDED.apertura_2,
                            chiusura_2 = EXCLUDED.chiusura_2
                        """,
                        (shop_id, day, is_open, opening, closing, opening_2, closing_2),
                    )
        return jsonify({"ok": True})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante il salvataggio degli orari.",
            "detail": error.diag.message_primary or "Errore database non specificato.",
        }), 500
    finally:
        conn.close()


@app.get("/api/statistiche")
def api_statistiche():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    if get_user_license_plan(session["user_id"]) != "professional":
        return jsonify({"error": "Le statistiche richiedono la licenza Professional."}), 403
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE visited_at >= NOW() - INTERVAL '7 days'),
                       COUNT(*) FILTER (WHERE visited_at >= NOW() - INTERVAL '30 days'),
                       COUNT(*) FILTER (WHERE visited_at >= NOW() - INTERVAL '30 days' AND sorgente = 'qr')
                FROM menu_visite WHERE id_negozio=%s
            """, (shop_id,))
            total, last7, last30, qr30 = cur.fetchone()
            cur.execute("""
                SELECT lingua, COUNT(*) FROM menu_visite
                WHERE id_negozio=%s AND visited_at >= NOW() - INTERVAL '30 days'
                GROUP BY lingua ORDER BY COUNT(*) DESC
            """, (shop_id,))
            languages = [{"lingua": row[0], "visite": row[1]} for row in cur.fetchall()]
            cur.execute("""
                SELECT TO_CHAR(DATE(visited_at), 'YYYY-MM-DD'), COUNT(*),
                       COUNT(*) FILTER (WHERE sorgente = 'qr')
                FROM menu_visite
                WHERE id_negozio=%s AND visited_at >= CURRENT_DATE - INTERVAL '29 days'
                GROUP BY DATE(visited_at) ORDER BY DATE(visited_at)
            """, (shop_id,))
            days = [{"data": row[0], "visite": row[1], "qr": row[2]} for row in cur.fetchall()]
            cur.execute("""
                SELECT p.nome, COUNT(*) AS aperture
                FROM prodotto_aperture pa
                JOIN prodotti p ON p.id = pa.id_prodotto
                WHERE pa.id_negozio = %s AND pa.opened_at >= NOW() - INTERVAL '30 days'
                GROUP BY p.id, p.nome
                ORDER BY aperture DESC, LOWER(p.nome) ASC
                LIMIT 10
            """, (shop_id,))
            top_products = [{"nome": row[0], "aperture": row[1]} for row in cur.fetchall()]
            cur.execute("""
                SELECT c.nome, COUNT(*) AS aperture
                FROM categoria_aperture ca
                JOIN categorie c ON c.id = ca.id_categoria
                WHERE ca.id_negozio = %s AND ca.opened_at >= NOW() - INTERVAL '30 days'
                GROUP BY c.id, c.nome
                ORDER BY aperture DESC, LOWER(c.nome) ASC
                LIMIT 10
            """, (shop_id,))
            top_categories = [{"nome": row[0], "aperture": row[1]} for row in cur.fetchall()]
        return jsonify({
            "totale": total, "ultimi_7_giorni": last7, "ultimi_30_giorni": last30,
            "scansioni_qr_30_giorni": qr30, "lingue": languages, "giorni": days,
            "articoli_piu_aperti": top_products, "categorie_piu_aperte": top_categories,
        })
    finally:
        conn.close()


@app.route("/api/ordini/configurazione", methods=["GET", "PUT"])
def api_ordini_configurazione():
    if "user_id" not in session:
        return jsonify({"error": "Accesso richiesto."}), 401
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "Configura prima il negozio."}), 409
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                if request.method == "PUT":
                    payload = request.get_json(silent=True) or {}
                    enabled = payload.get("attivi")
                    limit = payload.get("limite_giornaliero")
                    if enabled is not None and not isinstance(enabled, bool):
                        return jsonify({"error": "Impostazione non valida."}), 400
                    if limit is not None and (type(limit) is not int or not 0 <= limit <= 10000):
                        return jsonify({"error": "Limite giornaliero non valido."}), 400
                    if enabled is None and limit is None:
                        return jsonify({"error": "Nessuna impostazione indicata."}), 400
                    cur.execute("UPDATE negozi SET ordini_attivi=COALESCE(%s,ordini_attivi),limite_ordini_giorno=COALESCE(%s,limite_ordini_giorno) WHERE id=%s", (enabled, limit, shop_id))
                cur.execute("SELECT ordini_attivi,limite_ordini_giorno FROM negozi WHERE id=%s", (shop_id,))
                row = cur.fetchone()
                return jsonify({"attivi": bool(row[0]), "limite_giornaliero": row[1]})
    finally:
        conn.close()


@app.get("/api/menu/<slug>/ordini/disponibilita")
def api_disponibilita_ordini(slug: str):
    try:
        requested = date.fromisoformat(request.args.get("data", ""))
    except ValueError:
        return jsonify({"error": "Data non valida."}), 400
    today = datetime.now(ZoneInfo("Europe/Rome")).date()
    if not today <= requested <= today + timedelta(days=365):
        return jsonify({"error": "Scegli una data entro i prossimi 365 giorni."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,ordini_attivi,limite_ordini_giorno FROM negozi WHERE slug=%s", (slug,))
            shop = cur.fetchone()
            if not shop or not shop[1]:
                return jsonify({"error": "Gli ordini online non sono disponibili."}), 403
            cur.execute("SELECT COUNT(*) FROM ordini_menu WHERE id_negozio=%s AND data_richiesta=%s AND stato<>'annullato'", (shop[0], requested))
            used = cur.fetchone()[0]
            return jsonify({"disponibile": shop[2] == 0 or used < shop[2], "posti_rimanenti": None if shop[2] == 0 else max(0, shop[2] - used), "limite_giornaliero": shop[2]})
    finally:
        conn.close()


@app.post("/api/ordini/manuale")
@app.post("/api/menu/<slug>/ordini")
def api_crea_ordine_menu(slug: str | None = None):
    manual = slug is None
    shop_id_manual = None
    if manual:
        if "user_id" not in session:
            return jsonify({"error": "Accesso richiesto."}), 401
        shop_id_manual = get_user_shop_id(session["user_id"])
        if not shop_id_manual:
            return jsonify({"error": "Configura prima il negozio."}), 409
    data = request.get_json(silent=True) or {}
    name = str(data.get("nome") or "").strip()
    phone = str(data.get("telefono") or "").strip()
    reference = str(data.get("riferimento") or "").strip()
    notes = str(data.get("note") or "").strip()
    items = data.get("prodotti")
    try:
        requested = date.fromisoformat(str(data.get("data_richiesta") or ""))
    except ValueError:
        return jsonify({"error": "Scegli una data valida per l'ordine."}), 400
    today = datetime.now(ZoneInfo("Europe/Rome")).date()
    if not today <= requested <= today + timedelta(days=365):
        return jsonify({"error": "Scegli una data entro i prossimi 365 giorni."}), 400
    if not (2 <= len(name) <= 120) or not (6 <= len(phone) <= 40) or not re.fullmatch(r"[+\d ()-]+", phone):
        return jsonify({"error": "Inserisci nome e telefono validi."}), 400
    if len(reference) > 80 or len(notes) > 500 or not isinstance(items, list) or not 1 <= len(items) <= 50:
        return jsonify({"error": "Controlla prodotti, riferimento e note."}), 400
    quantities = {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            return jsonify({"error": "Prodotto o quantità non validi."}), 400
        product_id = item["id"]
        try:
            quantity = Decimal(str(item.get("quantita")).replace(",", "."))
        except (ValueError, ArithmeticError):
            return jsonify({"error": "Prodotto o quantità non validi."}), 400
        if product_id <= 0 or not quantity.is_finite() or not Decimal("0.001") <= quantity <= Decimal("999") or quantity.as_tuple().exponent < -3 or product_id in quantities:
            return jsonify({"error": "Prodotto o quantità non validi."}), 400
        quantities[product_id] = quantity
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                if manual:
                    cur.execute("SELECT id,ordini_attivi,limite_ordini_giorno FROM negozi WHERE id=%s FOR UPDATE", (shop_id_manual,))
                else:
                    cur.execute("SELECT id,ordini_attivi,limite_ordini_giorno FROM negozi WHERE slug=%s FOR UPDATE", (slug,))
                shop = cur.fetchone()
                if not shop or (not manual and not shop[1]):
                    return jsonify({"error": "Gli ordini online non sono disponibili per questo locale."}), 403
                shop_id = shop[0]
                cur.execute("SELECT COUNT(*) FROM ordini_menu WHERE id_negozio=%s AND data_richiesta=%s AND stato<>'annullato'", (shop_id, requested))
                used = cur.fetchone()[0]
                if shop[2] and used >= shop[2]:
                    return jsonify({"error": "La data selezionata è completa. Scegline un'altra."}), 409
                if not manual:
                    cur.execute("""
                        SELECT COUNT(*) FROM ordini_menu
                        WHERE id_negozio=%s AND telefono_cliente=%s AND creato_il >= NOW() - INTERVAL '10 minutes'
                    """, (shop_id, phone))
                    if cur.fetchone()[0] >= 3:
                        return jsonify({"error": "Hai inviato troppi ordini. Riprova tra qualche minuto."}), 429
                cur.execute("""
                    SELECT p.id,p.nome,p.prezzo_euro
                    FROM prodotti p
                    JOIN categorie c ON c.id=p.id_categoria AND c.visibile=TRUE
                    LEFT JOIN sottocategorie sc ON sc.id=p.id_sottocategoria
                    WHERE p.id_negozio=%s AND p.id=ANY(%s) AND p.disponibile=TRUE
                      AND (p.visibile_da IS NULL OR p.visibile_da <= CURRENT_DATE)
                      AND (p.visibile_fino IS NULL OR p.visibile_fino >= CURRENT_DATE)
                      AND (p.ora_inizio IS NULL OR p.ora_inizio <= CURRENT_TIME)
                      AND (p.ora_fine IS NULL OR p.ora_fine >= CURRENT_TIME)
                      AND (c.visibile_da IS NULL OR c.visibile_da <= CURRENT_DATE)
                      AND (c.visibile_fino IS NULL OR c.visibile_fino >= CURRENT_DATE)
                      AND (c.ora_inizio IS NULL OR c.ora_inizio <= CURRENT_TIME)
                      AND (c.ora_fine IS NULL OR c.ora_fine >= CURRENT_TIME)
                      AND (sc.id IS NULL OR (sc.visibile=TRUE
                        AND (sc.visibile_da IS NULL OR sc.visibile_da <= CURRENT_DATE)
                        AND (sc.visibile_fino IS NULL OR sc.visibile_fino >= CURRENT_DATE)
                        AND (sc.ora_inizio IS NULL OR sc.ora_inizio <= CURRENT_TIME)
                        AND (sc.ora_fine IS NULL OR sc.ora_fine >= CURRENT_TIME)))
                """, (shop_id, list(quantities)))
                products = {row[0]: row for row in cur.fetchall()}
                if len(products) != len(quantities):
                    return jsonify({"error": "Un prodotto non è più disponibile. Aggiorna il menu e riprova."}), 409
                line_totals = {product_id: (row[2] * quantities[product_id]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for product_id, row in products.items()}
                total = sum(line_totals.values(), Decimal("0.00"))
                customer_google = session.get("customer_google") if not manual else None
                cur.execute("""
                    INSERT INTO ordini_menu (id_negozio,nome_cliente,telefono_cliente,riferimento,note,totale,data_richiesta,origine,google_sub_cliente,email_cliente)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (shop_id, name, phone, reference, notes, total, requested, "titolare" if manual else "cliente", customer_google.get("sub") if customer_google else None, customer_google.get("email") if customer_google else None))
                order_id = cur.fetchone()[0]
                for product_id, quantity in quantities.items():
                    row = products[product_id]
                    cur.execute("""
                        INSERT INTO righe_ordini_menu
                            (id_ordine,id_prodotto,nome_prodotto,quantita,prezzo_unitario,totale_riga)
                        VALUES (%s,%s,%s,%s,%s,%s)
                    """, (order_id, product_id, row[1], quantity, row[2], line_totals[product_id]))
        return jsonify({"ok": True, "ordine_id": order_id, "totale": str(total), "messaggio": "Ordine registrato." if manual else "Ordine inviato. Il locale deve ancora confermarlo."}), 201
    finally:
        conn.close()


@app.get("/api/ordini")
def api_elenco_ordini():
    if "user_id" not in session:
        return jsonify({"error": "Accesso richiesto."}), 401
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "Configura prima il negozio."}), 409
    period = request.args.get("periodo", "giorno")
    if period not in {"giorno", "settimana", "mese", "anno"}:
        return jsonify({"error": "Periodo non valido."}), 400
    try:
        selected = date.fromisoformat(request.args.get("data", date.today().isoformat()))
    except ValueError:
        return jsonify({"error": "Data non valida."}), 400
    if period == "giorno":
        start, end = selected, selected + timedelta(days=1)
    elif period == "settimana":
        start = selected - timedelta(days=selected.weekday())
        end = start + timedelta(days=7)
    elif period == "mese":
        start = selected.replace(day=1)
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    else:
        start, end = date(selected.year, 1, 1), date(selected.year + 1, 1, 1)
    try:
        page = int(request.args.get("pagina", "1"))
    except (TypeError, ValueError):
        return jsonify({"error": "Pagina non valida."}), 400
    if not 1 <= page <= 10000:
        return jsonify({"error": "Pagina non valida."}), 400
    page_size = 100
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT stato, COUNT(*), COALESCE(SUM(totale),0)
                FROM ordini_menu WHERE id_negozio=%s
                  AND COALESCE(data_richiesta,(creato_il AT TIME ZONE 'Europe/Rome')::date) >= %s
                  AND COALESCE(data_richiesta,(creato_il AT TIME ZONE 'Europe/Rome')::date) < %s
                GROUP BY stato
            """, (shop_id, start, end))
            summary = {row[0]: {"numero": row[1], "totale": str(row[2])} for row in cur.fetchall()}
            cur.execute("""
                SELECT id,nome_cliente,telefono_cliente,riferimento,note,stato,totale,
                       TO_CHAR(creato_il AT TIME ZONE 'Europe/Rome','DD/MM/YYYY HH24:MI'),
                       COALESCE(data_richiesta,(creato_il AT TIME ZONE 'Europe/Rome')::date),origine,email_cliente
                FROM ordini_menu WHERE id_negozio=%s
                  AND COALESCE(data_richiesta,(creato_il AT TIME ZONE 'Europe/Rome')::date) >= %s
                  AND COALESCE(data_richiesta,(creato_il AT TIME ZONE 'Europe/Rome')::date) < %s
                ORDER BY CASE WHEN stato IN ('da_evadere','in_lavorazione') THEN 0 ELSE 1 END,
                         data_richiesta ASC NULLS LAST, creato_il DESC LIMIT %s OFFSET %s
            """, (shop_id, start, end, page_size, (page - 1) * page_size))
            orders = [dict(zip(("id","nome","telefono","riferimento","note","stato","totale","creato_il","data_richiesta","origine","email_cliente"), row)) for row in cur.fetchall()]
            for order in orders:
                order["totale"] = str(order["totale"])
                order["data_richiesta"] = order["data_richiesta"].isoformat()
            if orders:
                cur.execute("""
                    SELECT id_ordine,nome_prodotto,quantita,prezzo_unitario,totale_riga
                    FROM righe_ordini_menu WHERE id_ordine=ANY(%s) ORDER BY id
                """, ([order["id"] for order in orders],))
                lines = {}
                for order_id, name, quantity, price, line_total in cur.fetchall():
                    lines.setdefault(order_id, []).append({"nome": name, "quantita": str(quantity), "prezzo": str(price), "totale": str(line_total)})
                for order in orders:
                    order["prodotti"] = lines.get(order["id"], [])
        return jsonify({"ordini": orders, "riepilogo": summary, "periodo": period, "da": start.isoformat(), "a": (end - timedelta(days=1)).isoformat(), "pagina": page, "dimensione_pagina": page_size, "totale_ordini": sum(item["numero"] for item in summary.values())})
    finally:
        conn.close()


@app.patch("/api/ordini/<int:order_id>")
def api_aggiorna_ordine(order_id: int):
    if "user_id" not in session:
        return jsonify({"error": "Accesso richiesto."}), 401
    shop_id = get_user_shop_id(session["user_id"])
    status = (request.get_json(silent=True) or {}).get("stato")
    if status not in {"da_evadere", "in_lavorazione", "evaso", "annullato"}:
        return jsonify({"error": "Stato non valido."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ordini_menu SET stato=%s,aggiornato_il=NOW()
                    WHERE id=%s AND id_negozio=%s RETURNING id
                """, (status, order_id, shop_id))
                if not cur.fetchone():
                    return jsonify({"error": "Ordine non trovato."}), 404
        return jsonify({"ok": True, "stato": status})
    finally:
        conn.close()


@app.get("/menu/<slug>")
def public_menu(slug: str):
    requested_language = (request.args.get("lang") or "it").lower()
    visit_source = "qr" if (request.args.get("src") or "").lower() == "qr" else "diretto"
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nome, indirizzo, citta, cap, provincia, email, telefono, nazione,
                       descrizione_breve, descrizione_estesa, slug, logo_url, copertina_url,
                       colore_accento, colore_sfondo, costo_coperto,
                       COALESCE(ordine_categorie_personalizzato, FALSE), COALESCE(whatsapp, ''),
                       COALESCE(prenotazione_url, ''),
                       COALESCE((SELECT piano FROM licenze_utenti WHERE id_utente = negozi.id_utente LIMIT 1), 'professional'),
                       COALESCE(ordini_attivi, FALSE)
                FROM negozi WHERE slug = %s
                """,
                (slug,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            shop = {
                "id": row[0], "nome": row[1], "indirizzo": row[2] or "",
                "citta": row[3] or "", "cap": row[4] or "", "provincia": row[5] or "",
                "email": row[6] or "", "telefono": row[7] or "", "nazione": row[8] or "",
                "descrizione_breve": row[9] or "", "descrizione_estesa": row[10] or "",
                "slug": row[11], "logo_url": row[12] or "", "copertina_url": row[13] or "",
                "colore_accento": row[14] or "#9d3e27", "colore_sfondo": row[15] or "#f7f3ed",
                "costo_coperto": f"{float(row[16] or 0):.2f}".replace(".", ","),
                "ordine_categorie_personalizzato": bool(row[17]),
                "whatsapp": row[18] or "",
                "prenotazione_url": row[19] or "",
                "piano": normalize_license_plan(row[20]),
                "ordini_attivi": bool(row[21]),
            }
            cur.execute(
                "INSERT INTO menu_visite (id_negozio, lingua, sorgente) VALUES (%s, %s, %s)",
                (shop["id"], requested_language if requested_language in SUPPORTED_MENU_LANGUAGES else "it", visit_source),
            )
            conn.commit()

            cur.execute(
                """
                SELECT id, nome FROM categorie
                WHERE id_negozio = %s AND visibile = TRUE
                  AND (visibile_da IS NULL OR visibile_da <= CURRENT_DATE)
                  AND (visibile_fino IS NULL OR visibile_fino >= CURRENT_DATE)
                  AND (ora_inizio IS NULL OR ora_inizio <= CURRENT_TIME)
                  AND (ora_fine IS NULL OR ora_fine >= CURRENT_TIME)
                ORDER BY CASE WHEN %s THEN ordine ELSE 0 END ASC, LOWER(nome) ASC
                """,
                (shop["id"], shop["ordine_categorie_personalizzato"]),
            )
            categories = [{"id": item[0], "nome": item[1], "prodotti": []} for item in cur.fetchall()]
            category_map = {category["id"]: category for category in categories}

            cur.execute(
                """
                SELECT p.id, p.nome, COALESCE(p.descrizione, ''), COALESCE(p.note, ''),
                       p.prezzo_euro, p.id_categoria, COALESCE(sc.id, 0), COALESCE(sc.nome, ''),
                       COALESCE(img.url, ''), COALESCE(p.etichette, ARRAY[]::TEXT[]),
                       COALESCE(p.allergeni_auto, ARRAY[]::TEXT[]), p.disponibile
                FROM prodotti p
                JOIN categorie c ON c.id = p.id_categoria AND c.visibile = TRUE
                LEFT JOIN sottocategorie sc ON sc.id = p.id_sottocategoria
                LEFT JOIN LATERAL (
                    SELECT url FROM immagini_prodotti
                    WHERE id_prodotto = p.id AND principale = TRUE
                    ORDER BY ordine ASC, id ASC LIMIT 1
                ) img ON TRUE
                WHERE p.id_negozio = %s
                  AND (p.visibile_da IS NULL OR p.visibile_da <= CURRENT_DATE)
                  AND (p.visibile_fino IS NULL OR p.visibile_fino >= CURRENT_DATE)
                  AND (p.ora_inizio IS NULL OR p.ora_inizio <= CURRENT_TIME)
                  AND (p.ora_fine IS NULL OR p.ora_fine >= CURRENT_TIME)
                  AND (sc.id IS NULL OR (
                    sc.visibile = TRUE
                    AND (sc.visibile_da IS NULL OR sc.visibile_da <= CURRENT_DATE)
                    AND (sc.visibile_fino IS NULL OR sc.visibile_fino >= CURRENT_DATE)
                    AND (sc.ora_inizio IS NULL OR sc.ora_inizio <= CURRENT_TIME)
                    AND (sc.ora_fine IS NULL OR sc.ora_fine >= CURRENT_TIME)
                  ))
                ORDER BY CASE WHEN %s THEN c.ordine ELSE 0 END ASC,
                         COALESCE(sc.ordine, 0) ASC,
                         CASE WHEN c.ordine_prodotti_personalizzato THEN p.ordine ELSE 0 END ASC,
                         LOWER(p.nome) ASC
                """,
                (shop["id"], shop["ordine_categorie_personalizzato"]),
            )
            allergen_updates = []
            for product in cur.fetchall():
                category = category_map.get(product[5])
                if not category:
                    continue
                detected_allergens = detect_allergens(product[1], product[2])
                if detected_allergens != (product[10] or []):
                    allergen_updates.append((detected_allergens, product[0]))
                category["prodotti"].append({
                    "id": product[0], "nome": product[1], "descrizione": product[2],
                    "note": product[3], "prezzo": f"{product[4]:.2f}".replace(".", ","),
                    "sottocategoria_id": product[6], "sottocategoria": product[7],
                    "immagine_url": product[8] if shop["piano"] == "professional" else "",
                    "etichette": product[9] or [], "allergeni": detected_allergens, "disponibile": bool(product[11]),
                })
            for allergens, product_id in allergen_updates:
                cur.execute("UPDATE prodotti SET allergeni_auto=%s WHERE id=%s AND id_negozio=%s", (allergens, product_id, shop["id"]))
            if allergen_updates:
                conn.commit()
            categories = [category for category in categories if category["prodotti"]]

            cur.execute(
                """
                SELECT giorno, aperto, apertura, chiusura, apertura_2, chiusura_2
                FROM orari_negozio WHERE id_negozio = %s ORDER BY giorno
                """,
                (shop["id"],),
            )
            saved_hours = {
                item[0]: {
                    "giorno": item[0], "aperto": bool(item[1]),
                    "apertura": item[2].strftime("%H:%M") if item[2] else "",
                    "chiusura": item[3].strftime("%H:%M") if item[3] else "",
                    "apertura_2": item[4].strftime("%H:%M") if item[4] else "",
                    "chiusura_2": item[5].strftime("%H:%M") if item[5] else "",
                }
                for item in cur.fetchall()
            }
            if shop["piano"] == "professional":
                cur.execute("SELECT codice FROM lingue_negozio WHERE id_negozio=%s ORDER BY codice", (shop["id"],))
                enabled_codes = [item[0] for item in cur.fetchall() if item[0] in SUPPORTED_MENU_LANGUAGES]
            else:
                enabled_codes = []
            language = requested_language if requested_language in enabled_codes else "it"
            translations = {}
            if language != "it":
                cur.execute("SELECT tipo, id_entita, campo, testo FROM traduzioni_menu WHERE id_negozio=%s AND lingua=%s", (shop["id"], language))
                translations = {(item[0], item[1], item[2]): item[3] for item in cur.fetchall()}
                shop["descrizione_breve"] = translations.get(("negozio", shop["id"], "descrizione_breve"), shop["descrizione_breve"])
                shop["descrizione_estesa"] = translations.get(("negozio", shop["id"], "descrizione_estesa"), shop["descrizione_estesa"])
                for category in categories:
                    category["nome"] = translations.get(("categoria", category["id"], "nome"), category["nome"])
                    for product in category["prodotti"]:
                        for field in ("nome", "descrizione", "note"):
                            product[field] = translations.get(("prodotto", product["id"], field), product[field])
                        product["sottocategoria"] = translations.get(("sottocategoria", product["sottocategoria_id"], "nome"), product["sottocategoria"])
                        product["etichette"] = [translations.get(("prodotto", product["id"], f"etichetta_{i}"), value) for i, value in enumerate(product["etichette"])]
                        product["allergeni"] = [translations.get(("prodotto", product["id"], f"allergene_{i}"), value) for i, value in enumerate(product["allergeni"])]

            ui = MENU_UI.get(language, MENU_UI["it"])
            shop["whatsapp_digits"] = re.sub(r"\D", "", shop["whatsapp"] or shop["telefono"])
            hours = [{"nome": ui["days"][day], **saved_hours.get(day, {"aperto": False})} for day in range(7)]
            languages = [{"codice": "it", "nome": "Italiano"}] + [{"codice": code, "nome": SUPPORTED_MENU_LANGUAGES[code]} for code in enabled_codes]

        return render_template("public_menu.html", shop=shop, categories=categories, hours=hours, ui=ui, language=language, languages=languages, customer_google=session.get("customer_google"))
    finally:
        conn.close()


@app.post("/menu/<slug>/categorie/<int:category_id>/apri")
def track_category_open(slug: str, category_id: int):
    """Registra in forma aggregata l'apertura di una categoria."""
    requested_language = (request.args.get("lang") or "it").lower()
    language = requested_language if requested_language in SUPPORTED_MENU_LANGUAGES else "it"
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT n.id FROM negozi n
                    JOIN categorie c ON c.id_negozio = n.id
                    WHERE n.slug = %s AND c.id = %s AND c.visibile = TRUE
                """, (slug, category_id))
                row = cur.fetchone()
                if not row:
                    abort(404)
                cur.execute(
                    "INSERT INTO categoria_aperture (id_negozio, id_categoria, lingua) VALUES (%s, %s, %s)",
                    (row[0], category_id, language),
                )
        return ("", 204)
    finally:
        conn.close()


@app.post("/menu/<slug>/prodotti/<int:product_id>/apri")
def track_product_open(slug: str, product_id: int):
    """Registra l'apertura di un articolo del menu pubblico."""
    requested_language = (request.args.get("lang") or "it").lower()
    language = requested_language if requested_language in SUPPORTED_MENU_LANGUAGES else "it"
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT n.id
                    FROM negozi n
                    JOIN prodotti p ON p.id_negozio = n.id
                    JOIN categorie c ON c.id = p.id_categoria AND c.visibile = TRUE
                    LEFT JOIN sottocategorie sc ON sc.id = p.id_sottocategoria
                    WHERE n.slug = %s AND p.id = %s AND p.disponibile = TRUE
                      AND (sc.id IS NULL OR sc.visibile = TRUE)
                """, (slug, product_id))
                row = cur.fetchone()
                if not row:
                    abort(404)
                cur.execute(
                    "INSERT INTO prodotto_aperture (id_negozio, id_prodotto, lingua) VALUES (%s, %s, %s)",
                    (row[0], product_id, language),
                )
        return ("", 204)
    finally:
        conn.close()


@app.get("/menu/<slug>/qrcode.png")
def public_menu_qrcode(slug: str):
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM negozi WHERE slug = %s", (slug,))
            if not cur.fetchone():
                abort(404)
    finally:
        conn.close()

    menu_url = url_for("public_menu", slug=slug, src="qr", _external=True, _scheme="https")
    image = qrcode.make(menu_url)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"menu-{slug}.png")


@app.get("/api/menu-pubblico")
def api_menu_pubblico():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM negozi WHERE id_utente = %s", (session["user_id"],))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Salva prima le informazioni del punto vendita."}), 404
            slug = row[0]
    finally:
        conn.close()
    return jsonify({
        "slug": slug,
        "menu_url": url_for("public_menu", slug=slug, _external=True, _scheme="https"),
        "qr_url": url_for("public_menu_qrcode", slug=slug, _external=True, _scheme="https"),
    })


@app.post("/api/richieste-qr")
def api_richieste_qr():
    if not session.get("user_id") or session.get("is_admin"):
        return jsonify({"error": "Accesso richiesto."}), 401
    data = request.get_json(silent=True) or {}
    prodotto = (data.get("prodotto") or "").strip()
    numero_tavolo = (data.get("numero_tavolo") or "").strip()
    numeri_tavolo = (data.get("numeri_tavolo") or "").strip()
    nfc = (data.get("nfc") or "").strip()
    try:
        quantity = int(data.get("quantita", 0))
    except (TypeError, ValueError):
        quantity = 0
    ranges = normalize_table_number_ranges(numeri_tavolo) if numero_tavolo == "Sì" else None
    if prodotto != "Supporto QR da tavolo stampato in 3D" or numero_tavolo not in {"Sì", "No"} or nfc not in {"Sì", "No"} or not 1 <= quantity <= 10000 or (numero_tavolo == "Sì" and not ranges):
        return jsonify({"error": "Dati del preventivo non validi."}), 400
    if ranges:
        numeri_tavolo, quantity = ranges
    quote = qr_quote_price(quantity, numero_tavolo == "Sì", nfc == "Sì")
    recipient = os.environ.get("QR_ORDER_RECIPIENT", "").strip()
    if not recipient or not email_configured():
        return jsonify({"error": "Il servizio richieste non è ancora configurato. Contatta Alpha System."}), 503
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.username, COALESCE(u.email,''), n.nome, n.slug
                FROM utenti u JOIN negozi n ON n.id_utente=u.id
                WHERE u.id=%s
            """, (session["user_id"],))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Completa prima i dati del negozio."}), 409
    menu_url = url_for("public_menu", slug=row[3], _external=True, _scheme="https")
    sent = send_transactional_email(
        recipient,
        f"Richiesta preventivo QR – {row[2]}",
        f"Nuova richiesta Alpha Menu\n\nCliente: {row[0]}\nAttività: {row[2]}\nEmail: {row[1] or 'non indicata'}\nProdotto: {prodotto}\nNumero tavolo sul retro: {numero_tavolo}\nNumeri tavolo: {numeri_tavolo if ranges else 'non previsto'}\nNFC integrato: {nfc}\nQuantità: {quantity}\nPrezzo base unitario: € {quote['base_cents'] / 100:.2f}\nSconti composti: {' + '.join(f'{rate}%' for rate in quote['discount_rates'] if rate) or 'nessuno'} ({quote['discount_percent']:.1f}%)\nPrezzo unitario scontato: € {quote['unit_cents'] / 100:.2f}\nTotale indicativo IVA esclusa: € {quote['total_cents'] / 100:.2f}\nMenu: {menu_url}",
        reply_to=row[1] or None,
    )
    if not sent:
        return jsonify({"error": "Invio non riuscito. Riprova tra qualche minuto."}), 502
    return jsonify({"ok": True, "message": "Richiesta inviata ad Alpha System. Ti contatteremo per il preventivo."})


@app.get("/api/prodotti")
def api_prodotti_list():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"items": []})

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id, p.nome, p.descrizione, p.prezzo_euro, p.disponibile,
                    p.id_categoria, c.nome as categoria_nome,
                    p.id_sottocategoria, sc.nome as sottocategoria_nome,
                    COALESCE(img.url, '') as immagine_url,
                    p.ordine, COALESCE(p.etichette, ARRAY[]::TEXT[]) as etichette,
                    COALESCE(p.note, '') as note,
                    COALESCE(p.allergeni_auto, ARRAY[]::TEXT[]) as allergeni_auto
                FROM prodotti p
                LEFT JOIN categorie c ON c.id = p.id_categoria
                LEFT JOIN sottocategorie sc ON sc.id = p.id_sottocategoria
                LEFT JOIN LATERAL (
                    SELECT url
                    FROM immagini_prodotti
                    WHERE id_prodotto = p.id AND principale = TRUE
                    ORDER BY ordine ASC, id ASC
                    LIMIT 1
                ) img ON true
                WHERE p.id_negozio = %s
                ORDER BY p.ordine ASC, p.id DESC
            """, (shop_id,))
            rows = cur.fetchall()

        items = []
        allergen_updates = []
        for r in rows:
            detected_allergens = detect_allergens(r[1], r[2])
            if detected_allergens != (r[13] or []):
                allergen_updates.append((detected_allergens, r[0]))
            items.append({
                "id": r[0],
                "nome": r[1],
                "descrizione": r[2] or "",
                "prezzo_euro": str(r[3]),
                "disponibile": bool(r[4]),
                "id_categoria": r[5],
                "categoria_nome": r[6] or "",
                "id_sottocategoria": r[7],
                "sottocategoria_nome": r[8] or "",
                "immagine_url": r[9] or "",
                "ordine": r[10],
                "etichette": r[11] or [],
                "note": r[12] or "",
                "allergeni_auto": detected_allergens,
            })
        # Aggiorna anche i prodotti già presenti, non solo quelli creati/modificati dopo la novità.
        if allergen_updates:
            with conn:
                with conn.cursor() as cur:
                    for allergens, product_id in allergen_updates:
                        cur.execute("UPDATE prodotti SET allergeni_auto=%s WHERE id=%s AND id_negozio=%s", (allergens, product_id, shop_id))
        return jsonify({"items": items})
    finally:
        conn.close()


@app.get("/api/categorie")
def api_categorie_list():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"items": []})

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome
                FROM categorie
                WHERE id_negozio = %s
                ORDER BY ordine ASC, nome ASC
            """, (shop_id,))
            cats = [{"id": r[0], "nome": r[1]} for r in cur.fetchall()]
        return jsonify({"items": cats})
    finally:
        conn.close()



@app.get("/api/sottocategorie")
def api_sottocategorie_list():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    categoria_id = request.args.get("categoria_id", type=int)
    if not categoria_id:
        return jsonify({"items": []})

    # Nota: per sicurezza, potresti verificare che la categoria appartenga al negozio dell'utente.
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome
                FROM sottocategorie
                WHERE id_categoria = %s AND visibile = TRUE
                ORDER BY ordine ASC, nome ASC
            """, (categoria_id,))
            items = [{"id": r[0], "nome": r[1]} for r in cur.fetchall()]
        return jsonify({"items": items})
    finally:
        conn.close()


@app.post("/api/prodotti")
def api_prodotti_create():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400
    if remaining_product_slots(session["user_id"], shop_id) == 0:
        return jsonify({"error": "Il piano Base consente fino a 100 prodotti. Passa a Professional per inserirne altri."}), 403

    # multipart form fields
    nome = (request.form.get("nome") or "").strip().upper()
    descrizione = (request.form.get("descrizione") or "").strip()
    note = (request.form.get("note") or "").strip()
    allergeni_auto = detect_allergens(nome, descrizione)
    prezzo_euro = request.form.get("prezzo_euro")
    disponibile = (request.form.get("disponibile", "true").lower() == "true")
    visibile_da = request.form.get("visibile_da") or None
    visibile_fino = request.form.get("visibile_fino") or None
    ora_inizio = request.form.get("ora_inizio") or None
    ora_fine = request.form.get("ora_fine") or None
    id_categoria = request.form.get("id_categoria") or None
    id_sottocategoria = request.form.get("id_sottocategoria") or None
    ordine = request.form.get("ordine") or None
    etichette = [tag.strip() for tag in (request.form.get("etichette") or "").split(",") if tag.strip()]

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    try:
        prezzo_val = float(prezzo_euro)
    except Exception:
        return jsonify({"error": "prezzo non valido"}), 400

    # cast id categoria
    try:
        id_categoria = int(id_categoria) if id_categoria not in (None, "", "null") else None
    except Exception:
        return jsonify({"error": "categoria non valida"}), 400

    try:
        id_sottocategoria = int(id_sottocategoria) if id_sottocategoria not in (None, "", "null") else None
    except Exception:
        return jsonify({"error": "sottocategoria non valida"}), 400
    try:
        ordine = int(ordine) if ordine not in (None, "", "null") else None
        if ordine is not None and ordine < 0:
            raise ValueError
    except Exception:
        return jsonify({"error": "ordine non valido"}), 400
    if not id_categoria:
        return jsonify({"error": "categoria obbligatoria"}), 400

    # file
    image_file = request.files.get("immagine")
    image_path = ""
    if image_file and image_file.filename:
        if get_user_license_plan(session["user_id"]) == "base":
            return jsonify({"error": "Le foto dei prodotti richiedono la licenza Professional."}), 403
        if not is_allowed_image(image_file.filename):
            return jsonify({"error": "Formato immagine non valido (png/jpg/webp)"}), 400
        try:
            image_path = save_product_image(image_file, shop_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO prodotti (id_negozio, id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_euro, disponibile, visibile_da, visibile_fino, ora_inizio, ora_fine, ordine, etichette, allergeni_auto)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, (SELECT COALESCE(MAX(ordine), 0) + 10 FROM prodotti WHERE id_negozio = %s)),
                        %s, %s
                    )
                    RETURNING id
                """, (shop_id, id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_val, disponibile, visibile_da, visibile_fino, ora_inizio, ora_fine, ordine, shop_id, etichette, allergeni_auto))
                new_id = cur.fetchone()[0]

                # Senza un ordine manuale, mantieni l'ordine alfabetico nella categoria.
                if ordine is None:
                    cur.execute("""
                        SELECT id
                        FROM prodotti
                        WHERE id_negozio = %s AND id_categoria = %s
                        ORDER BY LOWER(nome) ASC, id ASC
                    """, (shop_id, id_categoria))
                    for index, row in enumerate(cur.fetchall(), start=1):
                        cur.execute(
                            "UPDATE prodotti SET ordine = %s WHERE id = %s",
                            (index * 10, row[0]),
                        )

                if image_path:
                    cur.execute("""
                        INSERT INTO immagini_prodotti (id_prodotto, url, principale, ordine)
                        VALUES (%s, %s, TRUE, 0)
                    """, (new_id, image_path))

        translation_started = False
        if get_user_license_plan(session["user_id"]) == "professional":
            translation_fields = [("nome", nome), ("descrizione", descrizione), ("note", note)]
            translation_fields += [(f"etichetta_{i}", value) for i, value in enumerate(etichette)]
            translation_fields += [(f"allergene_{i}", value) for i, value in enumerate(allergeni_auto)]
            user_id = session["user_id"]
            def background_translation():
                try:
                    translate_new_product(shop_id, new_id, translation_fields)
                except (RuntimeError, requests.RequestException, psycopg2.Error) as error:
                    record_operational_error("traduzione", str(error), user_id)
            threading.Thread(target=background_translation, name=f"translate-product-{new_id}", daemon=True).start()
            translation_started = True
        return jsonify({"ok": True, "id": new_id, "traduzione_avviata": translation_started})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante il salvataggio del prodotto.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500

    finally:
        conn.close()


@app.post("/api/prodotti/<int:prodotto_id>/duplica")
def api_prodotto_duplica(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400
    if remaining_product_slots(session["user_id"], shop_id) == 0:
        return jsonify({"error": "Il piano Base consente fino a 100 prodotti. Passa a Professional per duplicarne altri."}), 403
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO prodotti (id_negozio, id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_euro, disponibile, ordine, etichette, allergeni_auto)
                    SELECT id_negozio, id_categoria, id_sottocategoria, LEFT(nome || ' COPIA', 100), descrizione, note,
                           prezzo_euro, disponibile, COALESCE((SELECT MAX(ordine) + 10 FROM prodotti WHERE id_negozio=%s), 10), etichette, allergeni_auto
                    FROM prodotti WHERE id=%s AND id_negozio=%s RETURNING id
                """, (shop_id, prodotto_id, shop_id))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "prodotto non trovato"}), 404
                new_id = row[0]
                cur.execute("""
                    INSERT INTO immagini_prodotti (id_prodotto, url, principale, ordine)
                    SELECT %s, url, principale, ordine FROM immagini_prodotti WHERE id_prodotto=%s
                """, (new_id, prodotto_id))
        return jsonify({"ok": True, "id": new_id})
    finally:
        conn.close()


@app.patch("/api/prodotti/<int:prodotto_id>/disponibilita")
def api_prodotto_disponibilita(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    data = request.get_json(silent=True) or {}
    disponibile = bool(data.get("disponibile"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE prodotti SET disponibile=%s WHERE id=%s AND id_negozio=%s", (disponibile, prodotto_id, shop_id))
                if not cur.rowcount:
                    return jsonify({"error": "prodotto non trovato"}), 404
        return jsonify({"ok": True, "disponibile": disponibile})
    finally:
        conn.close()


@app.post("/api/prodotti/importa-csv")
def api_prodotti_importa_csv():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    uploaded = request.files.get("file")
    if not shop_id or not uploaded:
        return jsonify({"error": "File CSV mancante"}), 400
    remaining_slots = remaining_product_slots(session["user_id"], shop_id)
    if remaining_slots == 0:
        return jsonify({"error": "Hai raggiunto il limite di 100 prodotti del piano Base."}), 403
    try:
        content = uploaded.read().decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(content[:2048], delimiters=",;")
        rows = csv.DictReader(io.StringIO(content), dialect=dialect)
    except Exception:
        return jsonify({"error": "CSV non valido"}), 400
    imported = 0
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                for raw in rows:
                    if remaining_slots is not None and imported >= remaining_slots:
                        break
                    data = {(key or "").strip().lower(): (value or "").strip() for key, value in raw.items()}
                    name = data.get("nome", "").upper()
                    category_name = data.get("categoria", "").upper()
                    if not name or not category_name:
                        continue
                    try:
                        price = float(data.get("prezzo", data.get("prezzo_euro", "0")).replace(",", "."))
                    except ValueError:
                        continue
                    cur.execute("SELECT id FROM categorie WHERE id_negozio=%s AND UPPER(nome)=%s", (shop_id, category_name))
                    category = cur.fetchone()
                    if category:
                        category_id = category[0]
                    else:
                        cur.execute("INSERT INTO categorie (id_negozio, nome, visibile, ordine) VALUES (%s,%s,TRUE,(SELECT COALESCE(MAX(ordine),0)+10 FROM categorie WHERE id_negozio=%s)) RETURNING id", (shop_id, category_name, shop_id))
                        category_id = cur.fetchone()[0]
                    description = data.get("descrizione", data.get("ingredienti", ""))
                    available = data.get("disponibile", "si").lower() not in {"no", "false", "0"}
                    cur.execute("""
                        INSERT INTO prodotti (id_negozio,id_categoria,nome,descrizione,note,prezzo_euro,disponibile,ordine,allergeni_auto)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,(SELECT COALESCE(MAX(ordine),0)+10 FROM prodotti WHERE id_negozio=%s),%s)
                    """, (shop_id, category_id, name, description, data.get("note", ""), price, available, shop_id, detect_allergens(name, description)))
                    imported += 1
        return jsonify({"ok": True, "importati": imported, "limite_raggiunto": remaining_slots is not None and imported >= remaining_slots})
    finally:
        conn.close()


def parse_imported_menu_text(raw_text: str) -> list[dict]:
    """Estrae prodotti da testo OCR/PDF senza salvare nulla."""
    lines = [re.sub(r"\s+", " ", line).strip(" \t•·") for line in (raw_text or "").splitlines()]
    lines = [line for line in lines if len(line) > 1][:4000]
    price_line = re.compile(r"^(.*?)(?:\s*[.·…]{2,}\s*|\s+)(?:€\s*)?(\d{1,3}(?:[.,]\d{2}))\s*€?$")
    only_price = re.compile(r"^(?:€\s*)?(\d{1,3}(?:[.,]\d{2}))\s*€?$")
    items = []
    category = "MENU IMPORTATO"
    skip_next = False
    for index, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        next_price = only_price.match(next_line)
        if next_price and len(line) <= 100:
            items.append({"categoria": category, "nome": line[:100], "descrizione": "", "prezzo": next_price.group(1).replace(",", ".")})
            skip_next = True
            continue
        match = price_line.match(line)
        if match and match.group(1).strip():
            items.append({"categoria": category, "nome": match.group(1).strip(" .-")[:100], "descrizione": "", "prezzo": match.group(2).replace(",", ".")})
            continue
        letters = [char for char in line if char.isalpha()]
        uppercase_ratio = sum(char.isupper() for char in letters) / max(1, len(letters))
        looks_like_category = len(line) <= 55 and (line.endswith(":") or uppercase_ratio >= 0.82)
        if looks_like_category:
            category = line.rstrip(":").strip()[:100]
        elif items and len(line) <= 300:
            current = items[-1]
            current["descrizione"] = (current["descrizione"] + " " + line).strip()[:500]
    return [item for item in items if item["nome"] and float(item["prezzo"]) >= 0][:250]


@app.post("/api/prodotti/importa-documento/anteprima")
def api_prodotti_importa_documento_anteprima():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    text = str((request.get_json(silent=True) or {}).get("testo") or "")
    if len(text.strip()) < 4:
        return jsonify({"error": "Non è stato possibile estrarre testo dal documento."}), 400
    items = parse_imported_menu_text(text[:200000])
    if not items:
        return jsonify({"error": "Nessun prodotto con prezzo riconosciuto. Controlla che nomi e prezzi siano leggibili."}), 400
    return jsonify({"items": items, "riconosciuti": len(items)})


@app.post("/api/prodotti/importa-documento")
def api_prodotti_importa_documento():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Nessun prodotto da importare."}), 400
    remaining_slots = remaining_product_slots(session["user_id"], shop_id)
    if remaining_slots == 0:
        return jsonify({"error": "Hai raggiunto il limite di 100 prodotti del piano Base."}), 403
    imported = 0
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                category_cache = {}
                for raw in items[:250]:
                    if remaining_slots is not None and imported >= remaining_slots:
                        break
                    name = str(raw.get("nome") or "").strip().upper()[:100]
                    category_name = str(raw.get("categoria") or "MENU IMPORTATO").strip().upper()[:100]
                    description = str(raw.get("descrizione") or "").strip()[:500]
                    try:
                        price = float(str(raw.get("prezzo") or "0").replace(",", "."))
                    except ValueError:
                        continue
                    if not name or price < 0:
                        continue
                    cache_key = category_name.casefold()
                    category_id = category_cache.get(cache_key)
                    if not category_id:
                        cur.execute("SELECT id FROM categorie WHERE id_negozio=%s AND LOWER(nome)=LOWER(%s)", (shop_id, category_name))
                        row = cur.fetchone()
                        if row:
                            category_id = row[0]
                        else:
                            cur.execute("""
                                INSERT INTO categorie (id_negozio, nome, visibile, ordine)
                                VALUES (%s,%s,TRUE,(SELECT COALESCE(MAX(ordine),0)+10 FROM categorie WHERE id_negozio=%s))
                                RETURNING id
                            """, (shop_id, category_name, shop_id))
                            category_id = cur.fetchone()[0]
                        category_cache[cache_key] = category_id
                    cur.execute("""
                        INSERT INTO prodotti (id_negozio,id_categoria,nome,descrizione,note,prezzo_euro,disponibile,ordine,allergeni_auto)
                        VALUES (%s,%s,%s,%s,'',%s,TRUE,(SELECT COALESCE(MAX(ordine),0)+10 FROM prodotti WHERE id_negozio=%s),%s)
                    """, (shop_id, category_id, name, description, price, shop_id, detect_allergens(name, description)))
                    imported += 1
        return jsonify({"ok": True, "importati": imported, "limite_raggiunto": remaining_slots is not None and imported >= remaining_slots})
    finally:
        conn.close()


@app.put("/api/prodotti/<int:prodotto_id>")
def api_prodotti_update(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    # multipart fields
    nome = (request.form.get("nome") or "").strip().upper()
    descrizione = (request.form.get("descrizione") or "").strip()
    note = (request.form.get("note") or "").strip()
    allergeni_auto = detect_allergens(nome, descrizione)
    prezzo_euro = request.form.get("prezzo_euro")
    disponibile = (request.form.get("disponibile", "true").lower() == "true")
    visibile_da = request.form.get("visibile_da") or None
    visibile_fino = request.form.get("visibile_fino") or None
    ora_inizio = request.form.get("ora_inizio") or None
    ora_fine = request.form.get("ora_fine") or None
    id_categoria = request.form.get("id_categoria") or None
    id_sottocategoria = request.form.get("id_sottocategoria") or None
    ordine = request.form.get("ordine") or None
    etichette = [tag.strip() for tag in (request.form.get("etichette") or "").split(",") if tag.strip()]
    remove_image = (request.form.get("remove_image", "false").lower() == "true")

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    try:
        prezzo_val = float(prezzo_euro)
    except Exception:
        return jsonify({"error": "prezzo non valido"}), 400

    try:
        id_categoria = int(id_categoria) if id_categoria not in (None, "", "null") else None
    except Exception:
        return jsonify({"error": "categoria non valida"}), 400

    try:
        id_sottocategoria = int(id_sottocategoria) if id_sottocategoria not in (None, "", "null") else None
    except Exception:
        return jsonify({"error": "sottocategoria non valida"}), 400
    try:
        ordine = int(ordine) if ordine not in (None, "", "null") else None
        if ordine is not None and ordine < 0:
            raise ValueError
    except Exception:
        return jsonify({"error": "ordine non valido"}), 400
    if not id_categoria:
        return jsonify({"error": "categoria obbligatoria"}), 400

    image_file = request.files.get("immagine")
    new_image_path = ""
    if image_file and image_file.filename:
        if get_user_license_plan(user_id) == "base":
            return jsonify({"error": "Le foto dei prodotti richiedono la licenza Professional."}), 403
        if not is_allowed_image(image_file.filename):
            return jsonify({"error": "Formato immagine non valido (png/jpg/webp)"}), 400
        try:
            new_image_path = save_product_image(image_file, shop_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                # verifica ownership
                cur.execute("SELECT id FROM prodotti WHERE id=%s AND id_negozio=%s", (prodotto_id, shop_id))
                if not cur.fetchone():
                    return jsonify({"error": "not found"}), 404

                cur.execute("""
                    UPDATE prodotti
                    SET id_categoria=%s, id_sottocategoria=%s, nome=%s, descrizione=%s, note=%s, prezzo_euro=%s, disponibile=%s,
                        visibile_da=%s, visibile_fino=%s, ora_inizio=%s, ora_fine=%s, ordine=COALESCE(%s, ordine), etichette=%s, allergeni_auto=%s
                    WHERE id=%s
                """, (id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_val, disponibile, visibile_da, visibile_fino, ora_inizio, ora_fine, ordine, etichette, allergeni_auto, prodotto_id))

                # immagine principale: gestisci remove / sostituzione
                cur.execute("SELECT id, url FROM immagini_prodotti WHERE id_prodotto=%s AND principale=TRUE LIMIT 1", (prodotto_id,))
                img_row = cur.fetchone()

                if remove_image:
                    if img_row:
                        cur.execute("DELETE FROM immagini_prodotti WHERE id=%s", (img_row[0],))
                    # opzionale: potresti anche cancellare il file fisico qui (se vuoi)
                elif new_image_path:
                    if img_row:
                        cur.execute("UPDATE immagini_prodotti SET url=%s WHERE id=%s", (new_image_path, img_row[0]))
                    else:
                        cur.execute("""
                            INSERT INTO immagini_prodotti (id_prodotto, url, principale, ordine)
                            VALUES (%s, %s, TRUE, 0)
                        """, (prodotto_id, new_image_path))

        return jsonify({"ok": True})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante il salvataggio del prodotto.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500

    finally:
        conn.close()


@app.post("/api/prodotti/posizioni")
def api_prodotti_posizioni():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        id_categoria = int(data.get("id_categoria"))
    except (TypeError, ValueError):
        return jsonify({"error": "seleziona una categoria"}), 400
    positions = data.get("posizioni")
    if not isinstance(positions, list) or not positions:
        return jsonify({"error": "posizioni non valide"}), 400

    parsed = []
    try:
        for item in positions:
            product_id = int(item["id"])
            posizione = int(item["posizione"])
            if posizione < 0:
                raise ValueError
            parsed.append((product_id, posizione))
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "posizioni non valide"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM prodotti WHERE id_negozio = %s AND id_categoria = %s",
                    (shop_id, id_categoria),
                )
                valid_ids = {row[0] for row in cur.fetchall()}
                if {item[0] for item in parsed} != valid_ids:
                    return jsonify({"error": "prodotti non validi per la categoria"}), 400

                for product_id, posizione in parsed:
                    cur.execute(
                        "UPDATE prodotti SET ordine = %s WHERE id = %s AND id_negozio = %s",
                        (posizione, product_id, shop_id),
                    )
                cur.execute(
                    "UPDATE categorie SET ordine_prodotti_personalizzato = TRUE WHERE id = %s AND id_negozio = %s",
                    (id_categoria, shop_id),
                )
        return jsonify({"ok": True, "updated": len(parsed)})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante l'ordinamento personalizzato.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500
    finally:
        conn.close()


@app.post("/api/prodotti/ordina")
def api_prodotti_ordina():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        id_categoria = int(data.get("id_categoria"))
    except (TypeError, ValueError):
        return jsonify({"error": "seleziona una categoria"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id
                    FROM prodotti
                    WHERE id_negozio = %s AND id_categoria = %s
                    ORDER BY LOWER(nome) ASC, id ASC
                    """,
                    (shop_id, id_categoria),
                )
                product_ids = [row[0] for row in cur.fetchall()]
                for index, product_id in enumerate(product_ids, start=1):
                    cur.execute(
                        "UPDATE prodotti SET ordine = %s WHERE id = %s AND id_negozio = %s",
                        (index * 10, product_id, shop_id),
                    )
                cur.execute(
                    "UPDATE categorie SET ordine_prodotti_personalizzato = FALSE WHERE id = %s AND id_negozio = %s",
                    (id_categoria, shop_id),
                )
        return jsonify({"ok": True, "updated": len(product_ids)})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante l'ordinamento dei prodotti.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500
    finally:
        conn.close()


@app.post("/api/prodotti/disponibilita")
def api_prodotti_bulk_disponibilita():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    disponibile = data.get("disponibile")
    id_categoria = data.get("id_categoria")
    id_sottocategoria = data.get("id_sottocategoria")

    if disponibile not in (True, False):
        return jsonify({"error": "stato disponibilità non valido"}), 400
    try:
        id_categoria = int(id_categoria) if id_categoria not in (None, "", "null") else None
        id_sottocategoria = int(id_sottocategoria) if id_sottocategoria not in (None, "", "null") else None
    except (TypeError, ValueError):
        return jsonify({"error": "categoria o sottocategoria non valida"}), 400
    if not id_categoria and not id_sottocategoria:
        return jsonify({"error": "seleziona una categoria o sottocategoria"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                where = ["id_negozio = %s"]
                params = [shop_id]
                if id_categoria:
                    where.append("id_categoria = %s")
                    params.append(id_categoria)
                if id_sottocategoria:
                    where.append("id_sottocategoria = %s")
                    params.append(id_sottocategoria)

                cur.execute(
                    "UPDATE prodotti SET disponibile = %s WHERE " + " AND ".join(where),
                    [disponibile] + params,
                )
                updated = cur.rowcount
        return jsonify({"ok": True, "updated": updated})
    except psycopg2.Error as error:
        return jsonify({
            "error": "Errore database durante l'aggiornamento dei prodotti.",
            "detail": error.diag.message_primary or "Errore database non specificato."
        }), 500
    finally:
        conn.close()


@app.delete("/api/prodotti/<int:prodotto_id>")
def api_prodotti_delete(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM prodotti WHERE id=%s AND id_negozio=%s", (prodotto_id, shop_id))
        return jsonify({"ok": True})
    finally:
        conn.close()
from flask import jsonify

# ---------- CATEGORIE ----------

@app.post("/api/categorie")
def api_categorie_create():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized", "detail": "sessione assente"}), 401

    data = request.get_json(silent=True)
    print("DEBUG /api/categorie JSON:", data)

    if not data:
        return jsonify({"error": "bad_request", "detail": "JSON mancante o non valido"}), 400

    nome = (data.get("nome") or "").strip().upper()
    visibile = bool(data.get("visibile", True))
    ordine = data.get("ordine")
    visibile_da = data.get("visibile_da") or None
    visibile_fino = data.get("visibile_fino") or None
    ora_inizio = data.get("ora_inizio") or None
    ora_fine = data.get("ora_fine") or None

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    ordine_int = None
    if ordine is not None and ordine != "":
        try:
            ordine_int = int(ordine)
        except Exception:
            return jsonify({"error": "ordine non valido"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    print("DEBUG shop_id:", shop_id)

    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                if ordine_int is None:
                    cur.execute("""
                        INSERT INTO categorie (id_negozio, nome, ordine, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine)
                        VALUES (%s, %s,
                            (SELECT COALESCE(MAX(ordine), 0) + 10 FROM categorie WHERE id_negozio = %s),
                            %s, %s, %s, %s, %s
                        )
                        RETURNING id
                    """, (shop_id, nome, shop_id, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine))
                else:
                    cur.execute("""
                        INSERT INTO categorie (id_negozio, nome, ordine, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (shop_id, nome, ordine_int, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine))

                new_id = cur.fetchone()[0]
                if ordine_int is not None:
                    cur.execute("UPDATE negozi SET ordine_categorie_personalizzato = TRUE WHERE id = %s", (shop_id,))
                print("DEBUG inserted categoria id:", new_id)

        return jsonify({"ok": True, "id": new_id})
    except Exception as e:
        conn.rollback()
        print("DEBUG INSERT ERROR:", repr(e))
        return jsonify({"error": "db_error", "detail": str(e)}), 500
    finally:
        conn.close()




@app.put("/api/categorie/<int:categoria_id>")
def api_categorie_update(categoria_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip().upper()
    visibile = bool(data.get("visibile", True))
    ordine = data.get("ordine")
    visibile_da = data.get("visibile_da") or None
    visibile_fino = data.get("visibile_fino") or None
    ora_inizio = data.get("ora_inizio") or None
    ora_fine = data.get("ora_fine") or None

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM categorie WHERE id=%s AND id_negozio=%s", (categoria_id, shop_id))
                if not cur.fetchone():
                    return jsonify({"error": "not found"}), 404

                if ordine is None or ordine == "":
                    cur.execute("""
                        UPDATE categorie
                        SET nome=%s, visibile=%s, visibile_da=%s, visibile_fino=%s, ora_inizio=%s, ora_fine=%s
                        WHERE id=%s
                    """, (nome, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine, categoria_id))
                else:
                    try:
                        ordine_int = int(ordine)
                    except Exception:
                        return jsonify({"error": "ordine non valido"}), 400
                    cur.execute("""
                        UPDATE categorie
                        SET nome=%s, visibile=%s, ordine=%s, visibile_da=%s, visibile_fino=%s, ora_inizio=%s, ora_fine=%s
                        WHERE id=%s
                    """, (nome, visibile, ordine_int, visibile_da, visibile_fino, ora_inizio, ora_fine, categoria_id))
                    cur.execute("UPDATE negozi SET ordine_categorie_personalizzato = TRUE WHERE id = %s", (shop_id,))

        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/categorie/<int:categoria_id>")
def api_categorie_delete(categoria_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM categorie WHERE id=%s AND id_negozio=%s",
                    (categoria_id, shop_id),
                )
                if not cur.fetchone():
                    return jsonify({"error": "categoria non trovata"}), 404

                cur.execute(
                    "SELECT id FROM prodotti WHERE id_negozio=%s AND id_categoria=%s",
                    (shop_id, categoria_id),
                )
                product_ids = [row[0] for row in cur.fetchall()]
                cur.execute(
                    "SELECT id FROM sottocategorie WHERE id_categoria=%s",
                    (categoria_id,),
                )
                subcategory_ids = [row[0] for row in cur.fetchall()]

                cur.execute(
                    "DELETE FROM prodotti WHERE id_negozio=%s AND id_categoria=%s",
                    (shop_id, categoria_id),
                )
                deleted_products = cur.rowcount
                cur.execute(
                    "DELETE FROM sottocategorie WHERE id_categoria=%s",
                    (categoria_id,),
                )
                deleted_subcategories = cur.rowcount

                if product_ids:
                    cur.execute(
                        "DELETE FROM traduzioni_menu WHERE id_negozio=%s AND tipo='prodotto' AND id_entita = ANY(%s)",
                        (shop_id, product_ids),
                    )
                if subcategory_ids:
                    cur.execute(
                        "DELETE FROM traduzioni_menu WHERE id_negozio=%s AND tipo='sottocategoria' AND id_entita = ANY(%s)",
                        (shop_id, subcategory_ids),
                    )
                cur.execute(
                    "DELETE FROM traduzioni_menu WHERE id_negozio=%s AND tipo='categoria' AND id_entita=%s",
                    (shop_id, categoria_id),
                )
                cur.execute(
                    "DELETE FROM categorie WHERE id=%s AND id_negozio=%s",
                    (categoria_id, shop_id),
                )
        return jsonify({
            "ok": True,
            "prodotti_eliminati": deleted_products,
            "sottocategorie_eliminate": deleted_subcategories,
        })
    finally:
        conn.close()


@app.post("/api/categorie/posizioni")
def api_categorie_posizioni():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    positions = (request.get_json(silent=True) or {}).get("posizioni") or []
    try:
        ids = [int(item["id"]) for item in positions]
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "posizioni non valide"}), 400
    if not shop_id or not ids or len(ids) != len(set(ids)):
        return jsonify({"error": "posizioni non valide"}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM categorie WHERE id_negozio=%s", (shop_id,))
                valid_ids = {row[0] for row in cur.fetchall()}
                if set(ids) != valid_ids:
                    return jsonify({"error": "L’elenco deve contenere tutte le categorie."}), 400
                for index, category_id in enumerate(ids, 1):
                    cur.execute("UPDATE categorie SET ordine=%s WHERE id=%s AND id_negozio=%s", (index * 10, category_id, shop_id))
                cur.execute("UPDATE negozi SET ordine_categorie_personalizzato=TRUE WHERE id=%s", (shop_id,))
        return jsonify({"ok": True, "updated": len(ids)})
    finally:
        conn.close()


@app.get("/api/categorie_full")
def api_categorie_full():
    """Categorie del negozio con visibile + ordine (per gestione)."""
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"items": []})

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, nome, ordine, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine
                FROM categorie
                WHERE id_negozio = %s
                ORDER BY ordine ASC, nome ASC
            """, (shop_id,))
            items = [{
                "id": r[0],
                "nome": r[1],
                "ordine": int(r[2]) if r[2] is not None else 0,
                "visibile": bool(r[3]),
                "visibile_da": r[4].isoformat() if r[4] else "",
                "visibile_fino": r[5].isoformat() if r[5] else "",
                "ora_inizio": r[6].strftime("%H:%M") if r[6] else "",
                "ora_fine": r[7].strftime("%H:%M") if r[7] else "",
            } for r in cur.fetchall()]
        return jsonify({"items": items})
    finally:
        conn.close()


# ---------- SOTTOCATEGORIE ----------

def categoria_belongs_to_shop(categoria_id: int, shop_id: int) -> bool:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM categorie WHERE id=%s AND id_negozio=%s", (categoria_id, shop_id))
            return cur.fetchone() is not None
    finally:
        conn.close()


@app.post("/api/sottocategorie/posizioni")
def api_sottocategorie_posizioni():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    shop_id = get_user_shop_id(session["user_id"])
    data = request.get_json(silent=True) or {}
    try:
        category_id = int(data.get("id_categoria"))
        ids = [int(item["id"]) for item in (data.get("posizioni") or [])]
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "posizioni non valide"}), 400
    if not shop_id or not ids or len(ids) != len(set(ids)):
        return jsonify({"error": "posizioni non valide"}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT sc.id FROM sottocategorie sc
                    JOIN categorie c ON c.id=sc.id_categoria
                    WHERE c.id_negozio=%s AND c.id=%s
                """, (shop_id, category_id))
                valid_ids = {row[0] for row in cur.fetchall()}
                if set(ids) != valid_ids:
                    return jsonify({"error": "L’elenco deve contenere tutte le sottocategorie della categoria."}), 400
                for index, subcategory_id in enumerate(ids, 1):
                    cur.execute("UPDATE sottocategorie SET ordine=%s WHERE id=%s AND id_categoria=%s", (index * 10, subcategory_id, category_id))
        return jsonify({"ok": True, "updated": len(ids)})
    finally:
        conn.close()


@app.get("/api/sottocategorie_full")
def api_sottocategorie_full():
    """Tutte le sottocategorie del negozio (per gestione)."""
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"items": []})

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sc.id, sc.id_categoria, c.nome as categoria_nome,
                       sc.nome, sc.ordine, sc.visibile
                FROM sottocategorie sc
                JOIN categorie c ON c.id = sc.id_categoria
                WHERE c.id_negozio = %s
                ORDER BY c.ordine ASC, sc.ordine ASC, sc.nome ASC
            """, (shop_id,))
            items = [{
                "id": r[0],
                "id_categoria": r[1],
                "categoria_nome": r[2] or "",
                "nome": r[3],
                "ordine": int(r[4]) if r[4] is not None else 0,
                "visibile": bool(r[5]),
            } for r in cur.fetchall()]
        return jsonify({"items": items})
    finally:
        conn.close()


@app.post("/api/sottocategorie")
def api_sottocategorie_create():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip().upper()
    visibile = bool(data.get("visibile", True))
    visibile_da = data.get("visibile_da") or None
    visibile_fino = data.get("visibile_fino") or None
    ora_inizio = data.get("ora_inizio") or None
    ora_fine = data.get("ora_fine") or None
    id_categoria = data.get("id_categoria")

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400
    try:
        id_categoria = int(id_categoria)
    except Exception:
        return jsonify({"error": "categoria non valida"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    if not categoria_belongs_to_shop(id_categoria, shop_id):
        return jsonify({"error": "categoria non appartiene al negozio"}), 403

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sottocategorie (id_categoria, nome, ordine, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine)
                    VALUES (%s, %s,
                        (SELECT COALESCE(MAX(ordine), 0) + 10 FROM sottocategorie WHERE id_categoria = %s),
                        %s, %s, %s, %s, %s
                    )
                    RETURNING id
                """, (id_categoria, nome, id_categoria, visibile, visibile_da, visibile_fino, ora_inizio, ora_fine))
                new_id = cur.fetchone()[0]
        return jsonify({"ok": True, "id": new_id})
    finally:
        conn.close()


@app.put("/api/sottocategorie/<int:sottocategoria_id>")
def api_sottocategorie_update(sottocategoria_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip().upper()
    visibile = bool(data.get("visibile", True))
    ordine = data.get("ordine")
    id_categoria = data.get("id_categoria")

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400
    try:
        id_categoria = int(id_categoria)
    except Exception:
        return jsonify({"error": "categoria non valida"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400
    if not categoria_belongs_to_shop(id_categoria, shop_id):
        return jsonify({"error": "categoria non appartiene al negozio"}), 403

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                # verifica che la sottocategoria appartenga al negozio (join su categorie)
                cur.execute("""
                    SELECT sc.id
                    FROM sottocategorie sc
                    JOIN categorie c ON c.id = sc.id_categoria
                    WHERE sc.id = %s AND c.id_negozio = %s
                """, (sottocategoria_id, shop_id))
                if not cur.fetchone():
                    return jsonify({"error": "not found"}), 404

                ordine_int = None
                if ordine is not None and ordine != "":
                    try:
                        ordine_int = int(ordine)
                    except Exception:
                        return jsonify({"error": "ordine non valido"}), 400

                if ordine_int is None:
                    cur.execute("""
                        UPDATE sottocategorie
                        SET id_categoria=%s, nome=%s, visibile=%s
                        WHERE id=%s
                    """, (id_categoria, nome, visibile, sottocategoria_id))
                else:
                    cur.execute("""
                        UPDATE sottocategorie
                        SET id_categoria=%s, nome=%s, visibile=%s, ordine=%s
                        WHERE id=%s
                    """, (id_categoria, nome, visibile, ordine_int, sottocategoria_id))

        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/sottocategorie/<int:sottocategoria_id>")
def api_sottocategorie_delete(sottocategoria_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM sottocategorie
                    WHERE id = %s
                    AND id IN (
                        SELECT sc.id
                        FROM sottocategorie sc
                        JOIN categorie c ON c.id = sc.id_categoria
                        WHERE sc.id = %s AND c.id_negozio = %s
                    )
                """, (sottocategoria_id, sottocategoria_id, shop_id))
        return jsonify({"ok": True})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")


