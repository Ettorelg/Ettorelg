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

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        db = Database()
        result = db.execute_query(
            "SELECT id, username, admin FROM utenti WHERE username = %s AND password = %s",
            (username, password)
        )

        if result:
            user_id, username, is_admin = result[0]
            session["user_id"] = user_id
            session["username"] = username
            session["is_admin"] = is_admin
            db.close()
            return redirect("/dashboard_admin") if is_admin else redirect("/dashboard_user")
        else:
            db.close()
            return render_template("login.html", error="Username o password errati.")

    return render_template("login.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
