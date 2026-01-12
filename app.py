import os
from pathlib import Path

import psycopg2
from flask import Flask, render_template, request, redirect, session

from db_config import build_db_config

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")


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


@app.route("/dashboard_user/section/<section>")
def dashboard_user_section(section: str):
    if "user_id" not in session:
        abort(401)

    allowed = {
        "home",
        "prodotti",
        "categorie",
        "sottocategorie",
        "allergeni",
        "negozio",
        "orari",
        "qrcode",
        "anteprima",
        "licenze",
        "account",
    }
    if section not in allowed:
        abort(404)

    # Qui in futuro carichi dati da DB per ogni sezione
    # Esempio: if section == "prodotti": products = ...
    # return render_template("sections/prodotti.html", products=products)

    return render_template(f"sections/{section}.html", username=session.get("username", "utente"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

