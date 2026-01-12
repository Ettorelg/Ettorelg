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
def get_user_shop_id(user_id: int) -> int | None:
    conn = psycopg2.connect(**build_db_config())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM negozi WHERE id_utente = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


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
                    COALESCE(img.url, '') as immagine_url
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
                WHERE id_negozio = %s AND visibile = TRUE
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

    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip()
    descrizione = (data.get("descrizione") or "").strip()
    prezzo_euro = data.get("prezzo_euro")
    disponibile = bool(data.get("disponibile", True))
    id_categoria = data.get("id_categoria")
    id_sottocategoria = data.get("id_sottocategoria")
    immagine_url = (data.get("immagine_url") or "").strip()

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    try:
        prezzo_euro = float(prezzo_euro)
    except Exception:
        return jsonify({"error": "prezzo non valido"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO prodotti (id_negozio, id_categoria, id_sottocategoria, nome, descrizione, prezzo_euro, disponibile, ordine)
                    VALUES (%s, %s, %s, %s, %s, %s, %s,
                        (SELECT COALESCE(MAX(ordine), 0) + 10 FROM prodotti WHERE id_negozio = %s)
                    )
                    RETURNING id
                """, (shop_id, id_categoria, id_sottocategoria, nome, descrizione, prezzo_euro, disponibile, shop_id))
                new_id = cur.fetchone()[0]

                if immagine_url:
                    cur.execute("""
                        INSERT INTO immagini_prodotti (id_prodotto, url, principale, ordine)
                        VALUES (%s, %s, TRUE, 0)
                    """, (new_id, immagine_url))

        return jsonify({"ok": True, "id": new_id})
    finally:
        conn.close()


@app.put("/api/prodotti/<int:prodotto_id>")
def api_prodotti_update(prodotto_id: int):
    if "user_id" not in session:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip()
    descrizione = (data.get("descrizione") or "").strip()
    prezzo_euro = data.get("prezzo_euro")
    disponibile = bool(data.get("disponibile", True))
    id_categoria = data.get("id_categoria")
    id_sottocategoria = data.get("id_sottocategoria")
    immagine_url = (data.get("immagine_url") or "").strip()

    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400

    try:
        prezzo_euro = float(prezzo_euro)
    except Exception:
        return jsonify({"error": "prezzo non valido"}), 400

    shop_id = get_user_shop_id(session["user_id"])
    if not shop_id:
        return jsonify({"error": "negozio non trovato"}), 400

    conn = psycopg2.connect(**build_db_config())
    try:
        with conn:
            with conn.cursor() as cur:
                # Assicura che il prodotto appartenga al negozio
                cur.execute("SELECT id FROM prodotti WHERE id = %s AND id_negozio = %s", (prodotto_id, shop_id))
                if not cur.fetchone():
                    return jsonify({"error": "not found"}), 404

                cur.execute("""
                    UPDATE prodotti
                    SET id_categoria=%s, id_sottocategoria=%s, nome=%s, descrizione=%s, prezzo_euro=%s, disponibile=%s
                    WHERE id=%s
                """, (id_categoria, id_sottocategoria, nome, descrizione, prezzo_euro, disponibile, prodotto_id))

                # immagine principale: upsert
                cur.execute("SELECT id FROM immagini_prodotti WHERE id_prodotto=%s AND principale=TRUE LIMIT 1", (prodotto_id,))
                img_row = cur.fetchone()
                if immagine_url:
                    if img_row:
                        cur.execute("UPDATE immagini_prodotti SET url=%s WHERE id=%s", (immagine_url, img_row[0]))
                    else:
                        cur.execute("""
                            INSERT INTO immagini_prodotti (id_prodotto, url, principale, ordine)
                            VALUES (%s, %s, TRUE, 0)
                        """, (prodotto_id, immagine_url))
                else:
                    # se svuota l'url, rimuovo l'immagine principale
                    if img_row:
                        cur.execute("DELETE FROM immagini_prodotti WHERE id=%s", (img_row[0],))

        return jsonify({"ok": True})
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

