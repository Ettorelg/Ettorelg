import io
import csv
import os
import re
import hmac
import html
import json
import secrets
import time
from datetime import date, datetime, timedelta
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024


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
LICENSE_PLANS = {
    "base": {"name": "Base", "price": "69.00", "product_limit": 50},
    "professional": {"name": "Professional", "price": "99.00", "product_limit": None},
}


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
    if not paypal_configured():
        raise RuntimeError("PayPal non è configurato: impossibile disdire l'abbonamento in sicurezza.")
    response = requests.post(
        f"{paypal_base_url()}/v1/billing/subscriptions/{subscription_id}/cancel",
        headers={"Authorization": f"Bearer {paypal_access_token()}", "Content-Type": "application/json"},
        json={"reason": reason}, timeout=20,
    )
    # PayPal può rispondere 422 se l'abbonamento era già terminato o cancellato.
    if response.status_code not in (200, 204, 422):
        raise RuntimeError("PayPal non ha confermato la disdetta dell'abbonamento.")


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
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return fallback


SUPPORTED_MENU_LANGUAGES = {
    "en": "English", "fr": "Français", "de": "Deutsch", "es": "Español"
}

MENU_UI = {
    "it": {"venue": "Il nostro locale", "contacts": "Contatti", "show": "VISUALIZZA IL MENU'", "back": "Torna alle informazioni", "hours": "Orari di apertura", "closed": "Chiuso", "empty": "Il menu sarà disponibile presto.", "categories": "Categorie del menu", "days": ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]},
    "en": {"venue": "Our venue", "contacts": "Contacts", "show": "VIEW MENU", "back": "Back to information", "hours": "Opening hours", "closed": "Closed", "empty": "The menu will be available soon.", "categories": "Menu categories", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]},
    "fr": {"venue": "Notre établissement", "contacts": "Contacts", "show": "VOIR LE MENU", "back": "Retour aux informations", "hours": "Horaires d'ouverture", "closed": "Fermé", "empty": "Le menu sera bientôt disponible.", "categories": "Catégories du menu", "days": ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]},
    "de": {"venue": "Unser Lokal", "contacts": "Kontakte", "show": "MENÜ ANZEIGEN", "back": "Zurück zu den Informationen", "hours": "Öffnungszeiten", "closed": "Geschlossen", "empty": "Das Menü ist bald verfügbar.", "categories": "Menükategorien", "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]},
    "es": {"venue": "Nuestro local", "contacts": "Contactos", "show": "VER EL MENÚ", "back": "Volver a la información", "hours": "Horario de apertura", "closed": "Cerrado", "empty": "El menú estará disponible pronto.", "categories": "Categorías del menú", "days": ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]},
}

for _code, (_cover, _print, _sold_out) in {
    "it": ("Coperto", "Stampa menu A4", "Esaurito"),
    "en": ("Cover charge", "Print A4 menu", "Sold out"),
    "fr": ("Couvert", "Imprimer le menu A4", "Épuisé"),
    "de": ("Gedeck", "A4-Menü drucken", "Ausverkauft"),
    "es": ("Cubierto", "Imprimir menú A4", "Agotado"),
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


@app.context_processor
def auth_provider_flags():
    return {"google_enabled": google_enabled(), "apple_enabled": apple_enabled()}


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
                cur.execute("ALTER TABLE negozi ADD COLUMN IF NOT EXISTS whatsapp TEXT")
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
def enforce_current_license():
    """Blocca anche le sessioni già aperte quando prova o licenza terminano."""
    user_id = session.get("user_id")
    if not user_id or session.get("is_admin"):
        return None
    public_endpoints = {
        "index", "login", "register", "auth_google", "auth_google_callback",
        "auth_apple", "auth_apple_callback", "logout",
        "privacy_policy", "terms_of_service", "uploaded_file", "static", "public_menu",
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


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_ROOT, filename)


@app.route("/")
def index():
    return redirect("/login")


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

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.password, u.admin, l.stato, l.data_scadenza
                FROM utenti u
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
                WHERE LOWER(u.email) = LOWER(%s)
            """, (email,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not verify_password(password, row[2]):
        return render_template("login.html", error="Email o password errati.", google_enabled=google_enabled())

    user_id, username_db, _, is_admin, status, expiry = row
    if not is_admin and not license_is_active(status, expiry):
        session.clear()
        session.update(pending_user_id=user_id, pending_username=username_db)
        return redirect(url_for("pagamento"))

    session.update(user_id=user_id, username=username_db, is_admin=bool(is_admin))
    return redirect("/dashboard_admin" if is_admin else "/dashboard_user")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))

    business_name = request.form.get("business_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")
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
                    "INSERT INTO utenti (username, email, password, admin) VALUES (%s, %s, %s, FALSE) RETURNING id",
                    (business_name, email, hash_password(password)),
                )
                user_id = cur.fetchone()[0]
                trial_end = date.today() + timedelta(days=APP_TRIAL_DAYS)
                cur.execute(
                    "INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza, piano) VALUES (%s, 'attiva', CURRENT_DATE, %s, 'professional')",
                    (user_id, trial_end),
                )
                cur.execute(
                    "INSERT INTO abbonamenti_paypal (id_utente, plan_id, stato, trial_fino, prossimo_addebito) VALUES (%s, %s, 'prova_locale', %s, %s)",
                    (user_id, paypal_plan_id("professional"), trial_end, trial_end),
                )
        session.clear()
        session.update(user_id=user_id, username=business_name, is_admin=False)
        return redirect(url_for("dashboard_user"))
    except psycopg2.IntegrityError:
        return render_template("register.html", error="Email o nome dell’attività già utilizzati.", google_enabled=google_enabled(), plans=LICENSE_PLANS, base_available=paypal_configured("base"))
    finally:
        conn.close()


@app.get("/auth/google")
def auth_google():
    if not google_enabled():
        return redirect(url_for("login"))
    callback = url_for("auth_google_callback", _external=True, _scheme="https")
    return google.authorize_redirect(callback)


@app.get("/auth/google/callback")
def auth_google_callback():
    if not google_enabled():
        return redirect(url_for("login"))
    token = google.authorize_access_token()
    profile = token.get("userinfo") or google.userinfo()
    email = (profile.get("email") or "").strip().lower()
    google_sub = profile.get("sub")
    if not email or not google_sub:
        return render_template("login.html", error="Google non ha restituito un indirizzo email valido.", google_enabled=True)

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, username, admin FROM utenti WHERE google_sub = %s OR LOWER(email) = LOWER(%s) ORDER BY google_sub = %s DESC LIMIT 1", (google_sub, email, google_sub))
                row = cur.fetchone()
                is_new_user = not bool(row)
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
                    cur.execute("INSERT INTO utenti (username, email, google_sub, password, admin, password_impostata) VALUES (%s, %s, %s, %s, FALSE, FALSE) RETURNING id", (username, email, google_sub, hash_password(os.urandom(32).hex())))
                    user_id = cur.fetchone()[0]
                    is_admin = False
                if is_new_user:
                    trial_end = date.today() + timedelta(days=APP_TRIAL_DAYS)
                    cur.execute("INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza, piano) VALUES (%s, 'attiva', CURRENT_DATE, %s, 'professional')", (user_id, trial_end))
                    cur.execute("INSERT INTO abbonamenti_paypal (id_utente, plan_id, stato, trial_fino, prossimo_addebito) VALUES (%s, %s, 'prova_locale', %s, %s)", (user_id, paypal_plan_id("professional"), trial_end, trial_end))
                else:
                    cur.execute("INSERT INTO licenze_utenti (id_utente, data_scadenza) VALUES (%s, %s) ON CONFLICT (id_utente) DO NOTHING", (user_id, annual_expiry()))
                cur.execute("SELECT stato, data_scadenza FROM licenze_utenti WHERE id_utente = %s", (user_id,))
                license_row = cur.fetchone()
        if not is_admin and (not license_row or not license_is_active(*license_row)):
            session.clear()
            session.update(pending_user_id=user_id, pending_username=username)
            return redirect(url_for("pagamento"))
        session.update(user_id=user_id, username=username, is_admin=bool(is_admin))
        return redirect("/dashboard_admin" if is_admin else "/dashboard_user")
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
                        INSERT INTO utenti (username,email,apple_sub,password,admin,password_impostata)
                        VALUES (%s,%s,%s,%s,FALSE,FALSE) RETURNING id
                        """,
                        (username, email, apple_sub, hash_password(os.urandom(32).hex())),
                    )
                    user_id = cur.fetchone()[0]
                    is_admin = False
                if is_new_user:
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
        if not is_admin and (not license_row or not license_is_active(*license_row)):
            session.clear()
            session.update(pending_user_id=user_id, pending_username=username)
            return redirect(url_for("pagamento"))
        session.clear()
        session.update(user_id=user_id, username=username, is_admin=bool(is_admin))
        return redirect("/dashboard_admin" if is_admin else "/dashboard_user")
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
    selected_plan = normalize_license_plan(session.get("renewal_plan") if selection_requested else row[8])
    plan_info = LICENSE_PLANS[selected_plan]
    return render_template(
        "pagamento.html", username=row[0], email=row[1], subscription_id=row[2],
        subscription_status=row[3], trial_until=row[4], next_billing=row[5],
        license_active=license_active, renewal_requested=renewal_requested, choice_requested=choice_requested, selection_requested=selection_requested,
        paypal_configured=paypal_configured(selected_plan), paypal_client_id=os.environ.get("PAYPAL_CLIENT_ID", ""),
        paypal_plan_id=paypal_plan_id(selected_plan), price=plan_info["price"], plan=selected_plan, plan_name=plan_info["name"],
        currency=PAYPAL_CURRENCY, trial_days=0,
    )


@app.post("/api/paypal/subscription/activate")
def paypal_subscription_activate():
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id or session.get("is_admin"):
        return jsonify({"error": "Sessione di registrazione non valida."}), 401
    payload = request.get_json(silent=True) or {}
    renewal_requested = bool(payload.get("renewal"))
    selection_requested = renewal_requested or bool(payload.get("choice"))
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(piano, 'professional') FROM licenze_utenti WHERE id_utente=%s", (user_id,))
            plan_row = cur.fetchone()
    finally:
        conn.close()
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
        return jsonify({"ok": True, "redirect": url_for("dashboard_user")})
    finally:
        conn.close()


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
                    next_date = parse_paypal_date(resource.get("billing_info", {}).get("next_billing_time"), date.today() + timedelta(days=PAYPAL_TRIAL_DAYS))
                    cur.execute("UPDATE abbonamenti_paypal SET stato='prova', trial_fino=COALESCE(trial_fino,%s), prossimo_addebito=%s, updated_at=NOW() WHERE id_utente=%s", (next_date, next_date, user_id))
                    cur.execute("UPDATE licenze_utenti SET stato='attiva', data_scadenza=%s, updated_at=NOW() WHERE id_utente=%s", (next_date, user_id))
                elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"):
                    cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
                elif event_type in ("BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.PAYMENT.FAILED"):
                    cur.execute("UPDATE abbonamenti_paypal SET stato='sospeso', updated_at=NOW() WHERE id_utente=%s", (user_id,))
                    cur.execute("UPDATE licenze_utenti SET stato='sospesa', updated_at=NOW() WHERE id_utente=%s", (user_id,))
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
            cur.execute("SELECT subscription_id FROM abbonamenti_paypal WHERE id_utente=%s", (user_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            return jsonify({"error": "Nessun abbonamento PayPal trovato."}), 404
        response = requests.post(
            f"{paypal_base_url()}/v1/billing/subscriptions/{row[0]}/cancel",
            headers={"Authorization": f"Bearer {paypal_access_token()}", "Content-Type": "application/json"},
            json={"reason": "Disdetta richiesta dal cliente"}, timeout=20,
        )
        if response.status_code not in (200, 204):
            return jsonify({"error": "PayPal non ha accettato la disdetta."}), 502
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE abbonamenti_paypal SET stato='cancellato', cancellato_il=NOW(), updated_at=NOW() WHERE id_utente=%s", (user_id,))
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
                "description": "14 giorni gratuiti, poi 69 EUR ogni anno",
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
                        "pricing_scheme": {"fixed_price": {"value": LICENSE_PLANS["base"]["price"], "currency_code": PAYPAL_CURRENCY}},
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
        source.raise_for_status()
        product_id = source.json()["product_id"]
        created = {}
        for plan, info in LICENSE_PLANS.items():
            response = requests.post(
                f"{paypal_base_url()}/v1/billing/plans",
                headers={**headers, "PayPal-Request-Id": f"alpha-menu-{plan}-annual-no-trial-{date.today().isoformat()}"},
                json={
                    "product_id": product_id,
                    "name": f"Alpha Menu {info['name']} annuale",
                    "description": f"{info['name']} · {info['price']} EUR ogni anno, senza periodo di prova PayPal",
                    "status": "ACTIVE",
                    "billing_cycles": [{
                        "frequency": {"interval_unit": "YEAR", "interval_count": 1},
                        "tenure_type": "REGULAR", "sequence": 1, "total_cycles": 0,
                        "pricing_scheme": {"fixed_price": {"value": info["price"], "currency_code": PAYPAL_CURRENCY}},
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
            "password_impostata": bool(row[3]), "admin": bool(row[4])
        }})
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
                       COALESCE(a.stato, '')
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
                              "in_prova": row[9] == "prova_locale"})
        return jsonify({"items": items, "current_user_id": session["user_id"]})
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
                cur.execute("INSERT INTO utenti (username, email, password, admin) VALUES (%s, %s, %s, %s) RETURNING id", (username, email, hash_password(password), is_admin))
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
                cur.execute("""
                    INSERT INTO licenze_utenti (id_utente, stato, data_scadenza, piano)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id_utente) DO UPDATE
                    SET stato=EXCLUDED.stato, data_scadenza=EXCLUDED.data_scadenza, piano=EXCLUDED.piano, updated_at=NOW()
                """, (user_id, status, expiry, plan))
                if is_trial:
                    cur.execute("""
                        INSERT INTO abbonamenti_paypal (id_utente, plan_id, stato, trial_fino, prossimo_addebito, updated_at)
                        VALUES (%s, %s, 'prova_locale', %s, %s, NOW())
                        ON CONFLICT (id_utente) DO UPDATE SET plan_id=EXCLUDED.plan_id, stato='prova_locale',
                            trial_fino=EXCLUDED.trial_fino, prossimo_addebito=EXCLUDED.prossimo_addebito, updated_at=NOW()
                    """, (user_id, paypal_plan_id("professional"), expiry, expiry))
        return jsonify({"ok": True})
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
        "menu_online", "qrcode", "anteprima", "lingue", "statistiche",
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

    return render_template(f"sections/{section}.html", username=session.get("username", "utente"))
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
        "whatsapp", "sito_web", "instagram_url", "google_maps_url", "colore_accento", "colore_sfondo", "costo_coperto",
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
                            whatsapp=%s, sito_web=%s, instagram_url=%s, google_maps_url=%s,
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
                            nazione, descrizione_breve, descrizione_estesa, whatsapp, sito_web, instagram_url, google_maps_url, colore_accento, colore_sfondo, costo_coperto, slug
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                       COALESCE(ordine_categorie_personalizzato, FALSE)
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
                    "sottocategoria_id": product[6], "sottocategoria": product[7], "immagine_url": product[8],
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
            cur.execute("SELECT codice FROM lingue_negozio WHERE id_negozio=%s ORDER BY codice", (shop["id"],))
            enabled_codes = [item[0] for item in cur.fetchall() if item[0] in SUPPORTED_MENU_LANGUAGES]
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
            hours = [{"nome": ui["days"][day], **saved_hours.get(day, {"aperto": False})} for day in range(7)]
            languages = [{"codice": "it", "nome": "Italiano"}] + [{"codice": code, "nome": SUPPORTED_MENU_LANGUAGES[code]} for code in enabled_codes]

        return render_template("public_menu.html", shop=shop, categories=categories, hours=hours, ui=ui, language=language, languages=languages)
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
        return jsonify({"error": "Il piano Base consente fino a 50 prodotti. Passa a Professional per inserirne altri."}), 403

    # multipart form fields
    nome = (request.form.get("nome") or "").strip().upper()
    descrizione = (request.form.get("descrizione") or "").strip()
    note = (request.form.get("note") or "").strip()
    allergeni_auto = detect_allergens(nome, descrizione)
    prezzo_euro = request.form.get("prezzo_euro")
    disponibile = (request.form.get("disponibile", "true").lower() == "true")
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
                    INSERT INTO prodotti (id_negozio, id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_euro, disponibile, ordine, etichette, allergeni_auto)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, (SELECT COALESCE(MAX(ordine), 0) + 10 FROM prodotti WHERE id_negozio = %s)),
                        %s, %s
                    )
                    RETURNING id
                """, (shop_id, id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_val, disponibile, ordine, shop_id, etichette, allergeni_auto))
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

        return jsonify({"ok": True, "id": new_id})
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
        return jsonify({"error": "Il piano Base consente fino a 50 prodotti. Passa a Professional per duplicarne altri."}), 403
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
        return jsonify({"error": "Hai raggiunto il limite di 50 prodotti del piano Base."}), 403
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
        return jsonify({"error": "Hai raggiunto il limite di 50 prodotti del piano Base."}), 403
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
                        ordine=COALESCE(%s, ordine), etichette=%s, allergeni_auto=%s
                    WHERE id=%s
                """, (id_categoria, id_sottocategoria, nome, descrizione, note, prezzo_val, disponibile, ordine, etichette, allergeni_auto, prodotto_id))

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
                    INSERT INTO sottocategorie (id_categoria, nome, ordine, visibile)
                    VALUES (%s, %s,
                        (SELECT COALESCE(MAX(ordine), 0) + 10 FROM sottocategorie WHERE id_categoria = %s),
                        %s
                    )
                    RETURNING id
                """, (id_categoria, nome, id_categoria, visibile))
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
    app.run(host="0.0.0.0", port=5000, debug=True)

