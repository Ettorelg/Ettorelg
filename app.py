SELECT sc.id, sc.nome FROM sottocategorie sc JOIN categorie c ON c.id = sc.id_categoria WHERE c.id_negozio=%simport io
import os
import re
import hmac
import html
from datetime import date, datetime, timedelta
from pathlib import Path

from werkzeug.utils import secure_filename
import uuid

import psycopg2
import qrcode
import bcrypt
import requests
from authlib.integrations.flask_client import OAuth
from flask import Flask, render_template, request, redirect, session, send_from_directory, send_file, url_for, abort

from db_config import build_db_config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
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

def detect_allergens(ingredients: str) -> list[str]:
    text = (ingredients or "").lower()
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
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS utenti_email_unique ON utenti (LOWER(email)) WHERE email IS NOT NULL")
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS utenti_google_sub_unique ON utenti (google_sub) WHERE google_sub IS NOT NULL")
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
                cur.execute("""
                    INSERT INTO licenze_utenti (id_utente, stato, data_inizio, data_scadenza)
                    SELECT id, 'attiva', CURRENT_DATE, CURRENT_DATE + 365
                    FROM utenti
                    ON CONFLICT (id_utente) DO NOTHING
                """)
    finally:
        conn.close()


# Esegui init schema solo se sei in ambiente con DB configurato
if os.getenv("DATABASE_URL") and os.getenv("AUTO_INIT_DB", "true").lower() == "true":
    init_db()


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


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", google_enabled=google_enabled())

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not username or not password:
        return render_template("login.html", error="Inserisci username e password.", google_enabled=google_enabled())

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.password, u.admin, l.stato, l.data_scadenza
                FROM utenti u
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
                WHERE LOWER(u.username) = LOWER(%s)
            """, (username,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not verify_password(password, row[2]):
        return render_template("login.html", error="Username o password errati.", google_enabled=google_enabled())

    user_id, username_db, _, is_admin, status, expiry = row
    if not is_admin and not license_is_active(status, expiry):
        return render_template("login.html", error="La licenza è scaduta o sospesa. Contatta l'amministratore.", google_enabled=google_enabled())

    session.update(user_id=user_id, username=username_db, is_admin=bool(is_admin))
    return redirect("/dashboard_admin" if is_admin else "/dashboard_user")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", google_enabled=google_enabled())

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")
    if not username or not email or not password:
        return render_template("register.html", error="Compila tutti i campi.", google_enabled=google_enabled())
    if password != confirm:
        return render_template("register.html", error="Le password non coincidono.", google_enabled=google_enabled())
    if len(password) < 8:
        return render_template("register.html", error="La password deve avere almeno 8 caratteri.", google_enabled=google_enabled())

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO utenti (username, email, password, admin) VALUES (%s, %s, %s, FALSE) RETURNING id",
                    (username, email, hash_password(password)),
                )
                user_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO licenze_utenti (id_utente, data_scadenza) VALUES (%s, %s)",
                    (user_id, annual_expiry()),
                )
        session.update(user_id=user_id, username=username, is_admin=False)
        return redirect("/dashboard_user")
    except psycopg2.IntegrityError:
        return render_template("register.html", error="Username o email già utilizzati.", google_enabled=google_enabled())
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
                cur.execute("INSERT INTO licenze_utenti (id_utente, data_scadenza) VALUES (%s, %s) ON CONFLICT (id_utente) DO NOTHING", (user_id, annual_expiry()))
                cur.execute("SELECT stato, data_scadenza FROM licenze_utenti WHERE id_utente = %s", (user_id,))
                license_row = cur.fetchone()
        if not is_admin and (not license_row or not license_is_active(*license_row)):
            return render_template("login.html", error="La licenza è scaduta o sospesa. Contatta l'amministratore.", google_enabled=True)
        session.update(user_id=user_id, username=username, is_admin=bool(is_admin))
        return redirect("/dashboard_admin" if is_admin else "/dashboard_user")
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


@app.get("/api/licenza")
def api_license_current():
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT stato, data_inizio, data_scadenza FROM licenze_utenti WHERE id_utente = %s", (session["user_id"],))
            row = cur.fetchone()
        if not row:
            return jsonify({"item": None})
        remaining = (row[2] - date.today()).days
        return jsonify({"item": {"stato": row[0], "data_inizio": row[1].isoformat(), "data_scadenza": row[2].isoformat(), "giorni_rimanenti": remaining}})
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
                cur.execute("SELECT id, nome FROM sottocategorie WHERE id_negozio=%s", (shop_id,))
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

                source_texts = [entry[3] for entry in entries]
                total = 0
                for language in languages:
                    translated = []
                    for offset in range(0, len(source_texts), 100):
                        translated.extend(google_translate_texts(source_texts[offset:offset + 100], language))
                    for entry, translated_text in zip(entries, translated):
                        if entry[3].isupper():
                            translated_text = translated_text.upper()
                        cur.execute("""
                            INSERT INTO traduzioni_menu (id_negozio, tipo, id_entita, campo, lingua, testo)
                            VALUES (%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (id_negozio, tipo, id_entita, campo, lingua)
                            DO UPDATE SET testo=EXCLUDED.testo, updated_at=NOW()
                        """, (shop_id, entry[0], entry[1], entry[2], language, translated_text))
                        total += 1
                    cur.execute("INSERT INTO lingue_negozio (id_negozio, codice) VALUES (%s,%s) ON CONFLICT DO NOTHING", (shop_id, language))
        return jsonify({"ok": True, "traduzioni": total})
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
                       COALESCE(l.stato, 'sospesa'), l.data_inizio, l.data_scadenza
                FROM utenti u
                LEFT JOIN negozi n ON n.id_utente = u.id
                LEFT JOIN licenze_utenti l ON l.id_utente = u.id
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
                              "in_scadenza": days is not None and 0 <= days <= 30})
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
    if not username or not password:
        return jsonify({"error": "Username e password sono obbligatori."}), 400
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO utenti (username, email, password, admin) VALUES (%s, %s, %s, %s) RETURNING id", (username, email, hash_password(password), is_admin))
                user_id = cur.fetchone()[0]
                cur.execute("INSERT INTO licenze_utenti (id_utente, data_scadenza) VALUES (%s, %s)", (user_id, annual_expiry()))
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
                    INSERT INTO licenze_utenti (id_utente, stato, data_scadenza)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id_utente) DO UPDATE
                    SET stato=EXCLUDED.stato, data_scadenza=EXCLUDED.data_scadenza, updated_at=NOW()
                """, (user_id, status, expiry))
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


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/dashboard_user")
def dashboard_user():
    if "user_id" not in session:
        return redirect("/login")

    # sezione iniziale
    return render_template(
        "dashboard_user.html",
        username=session.get("username", "utente"),
        active_section="home"
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
        "account",
    }
    if section not in allowed:
        abort(404)

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
                            email=%s, telefono=%s, nazione=%s, descrizione_breve=%s, descrizione_estesa=%s
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
                            nazione, descrizione_breve, descrizione_estesa, slug
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        [user_id] + [values[field] for field in fields] + [slug],
                    )
                    shop_id = cur.fetchone()[0]

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


@app.get("/menu/<slug>")
def public_menu(slug: str):
    requested_language = (request.args.get("lang") or "it").lower()
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nome, indirizzo, citta, cap, provincia, email, telefono, nazione,
                       descrizione_breve, descrizione_estesa, slug, logo_url, copertina_url
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
            }

            cur.execute(
                """
                SELECT id, nome FROM categorie
                WHERE id_negozio = %s AND visibile = TRUE
                ORDER BY ordine ASC, nome ASC
                """,
                (shop["id"],),
            )
            categories = [{"id": item[0], "nome": item[1], "prodotti": []} for item in cur.fetchall()]
            category_map = {category["id"]: category for category in categories}

            cur.execute(
                """
                SELECT p.id, p.nome, COALESCE(p.descrizione, ''), COALESCE(p.note, ''),
                       p.prezzo_euro, p.id_categoria, COALESCE(sc.id, 0), COALESCE(sc.nome, ''),
                       COALESCE(img.url, ''), COALESCE(p.etichette, ARRAY[]::TEXT[]),
                       COALESCE(p.allergeni_auto, ARRAY[]::TEXT[])
                FROM prodotti p
                JOIN categorie c ON c.id = p.id_categoria AND c.visibile = TRUE
                LEFT JOIN sottocategorie sc ON sc.id = p.id_sottocategoria
                LEFT JOIN LATERAL (
                    SELECT url FROM immagini_prodotti
                    WHERE id_prodotto = p.id AND principale = TRUE
                    ORDER BY ordine ASC, id ASC LIMIT 1
                ) img ON TRUE
                WHERE p.id_negozio = %s AND p.disponibile = TRUE
                  AND (sc.id IS NULL OR sc.visibile = TRUE)
                ORDER BY c.ordine ASC, COALESCE(sc.ordine, 0) ASC, p.ordine ASC, LOWER(p.nome) ASC
                """,
                (shop["id"],),
            )
            for product in cur.fetchall():
                category = category_map.get(product[5])
                if not category:
                    continue
                category["prodotti"].append({
                    "id": product[0], "nome": product[1], "descrizione": product[2],
                    "note": product[3], "prezzo": f"{product[4]:.2f}".replace(".", ","),
                    "sottocategoria_id": product[6], "sottocategoria": product[7], "immagine_url": product[8],
                    "etichette": product[9] or [], "allergeni": product[10] or [],
                })
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

    menu_url = url_for("public_menu", slug=slug, _external=True, _scheme="https")
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
        for r in rows:
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
                "allergeni_auto": r[13] or [],
            })
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

    # multipart form fields
    nome = (request.form.get("nome") or "").strip()
    descrizione = (request.form.get("descrizione") or "").strip()
    note = (request.form.get("note") or "").strip()
    allergeni_auto = detect_allergens(descrizione)
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


@app.put("/api/prodotti/<int:prodotto_id>")
def api_prodotti_update(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    # multipart fields
    nome = (request.form.get("nome") or "").strip()
    descrizione = (request.form.get("descrizione") or "").strip()
    note = (request.form.get("note") or "").strip()
    allergeni_auto = detect_allergens(descrizione)
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

    nome = (data.get("nome") or "").strip()
    visibile = bool(data.get("visibile", True))
    ordine = data.get("ordine")

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
                        INSERT INTO categorie (id_negozio, nome, ordine, visibile)
                        VALUES (%s, %s,
                            (SELECT COALESCE(MAX(ordine), 0) + 10 FROM categorie WHERE id_negozio = %s),
                            %s
                        )
                        RETURNING id
                    """, (shop_id, nome, shop_id, visibile))
                else:
                    cur.execute("""
                        INSERT INTO categorie (id_negozio, nome, ordine, visibile)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                    """, (shop_id, nome, ordine_int, visibile))

                new_id = cur.fetchone()[0]
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
    nome = (data.get("nome") or "").strip()
    visibile = bool(data.get("visibile", True))
    ordine = data.get("ordine")

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
                        SET nome=%s, visibile=%s
                        WHERE id=%s
                    """, (nome, visibile, categoria_id))
                else:
                    try:
                        ordine_int = int(ordine)
                    except Exception:
                        return jsonify({"error": "ordine non valido"}), 400
                    cur.execute("""
                        UPDATE categorie
                        SET nome=%s, visibile=%s, ordine=%s
                        WHERE id=%s
                    """, (nome, visibile, ordine_int, categoria_id))

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
                cur.execute("DELETE FROM categorie WHERE id=%s AND id_negozio=%s", (categoria_id, shop_id))
        return jsonify({"ok": True})
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
                SELECT id, nome, ordine, visibile
                FROM categorie
                WHERE id_negozio = %s
                ORDER BY ordine ASC, nome ASC
            """, (shop_id,))
            items = [{
                "id": r[0],
                "nome": r[1],
                "ordine": int(r[2]) if r[2] is not None else 0,
                "visibile": bool(r[3]),
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
    nome = (data.get("nome") or "").strip()
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
    nome = (data.get("nome") or "").strip()
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

