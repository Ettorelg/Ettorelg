import os
from pathlib import Path

from werkzeug.utils import secure_filename
import uuid

import psycopg2
from flask import Flask, render_template, request, redirect, session

from db_config import build_db_config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")
UPLOAD_ROOT = os.path.join(app.root_path, "static", "uploads")
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def is_allowed_image(filename: str) -> bool:
    ext = os.path.splitext(filename.lower())[1]
    return ext in ALLOWED_IMAGE_EXT

def save_product_image(file_storage, shop_id: int) -> str:
    """
    Salva immagine su static/uploads/negozio_<id>/prodotti/<uuid>.<ext>
    Ritorna il path web: /static/uploads/...
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
    return f"/static/uploads/negozio_{shop_id}/prodotti/{new_name}"


def init_db() -> None:
    schema_path = Path(__file__).resolve().parent / "db" / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
    finally:
        conn.close()


# Esegui init schema solo se sei in ambiente con DB configurato
if os.getenv("DATABASE_URL") and os.getenv("AUTO_INIT_DB", "true").lower() == "true":
    init_db()


@app.route("/")
def index():
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        return render_template("login.html", error="Inserisci username e password.")

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, admin FROM utenti WHERE username = %s AND password = %s",
                (username, password),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return render_template("login.html", error="Username o password errati.")

    user_id, username_db, is_admin = row

    session["user_id"] = user_id
    session["username"] = username_db
    session["is_admin"] = bool(is_admin)

    if is_admin:
        return redirect("/dashboard_admin")
    else:
        return redirect("/dashboard_user")
        
@app.route("/dashboard_admin")
def dashboard_admin():
    if not session.get("is_admin"):
        return redirect("/login")

    return render_template("dashboard_admin.html", username=session.get("username"))



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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

