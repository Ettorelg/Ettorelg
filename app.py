import os
from pathlib import Path

import psycopg2
from flask import Flask, render_template

from db_config import build_db_config

app = Flask(__name__)


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


if os.getenv("DATABASE_URL") and os.getenv("AUTO_INIT_DB", "true").lower() == "true":
    init_db()


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/dashboard_user")
def dashboard_user():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    db = Database()

    # Recupera le licenze dell'utente
    licenze = db.execute_query(
        "SELECT tipo, data_scadenza FROM licenze WHERE id_utente = %s", (user_id,)
    )

    # Controlla se le licenze specifiche sono attive
    eliminacode_attiva = any(licenza[0] == "eliminacode" for licenza in licenze)
    prenotazioni_attiva = any(licenza[0] == "prenotazioni" for licenza in licenze)

    db.close()
    return render_template(
        "dashboard_user.html",
        username=session["username"],
        licenze=licenze,
        eliminacode_attiva=eliminacode_attiva,
        prenotazioni_attiva=prenotazioni_attiva
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
