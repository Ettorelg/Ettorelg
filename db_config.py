import os
from urllib.parse import urlparse

DEFAULT_DATABASE_URL = (
    "postgresql://postgres:xBwFtlhJXquzSKZITlyUjtBfzGnqQIyt"
    "@postgres.railway.internal:5432/railway"
)


def build_db_config() -> dict:
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        parsed = urlparse(database_url)
        return {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/"),
            "user": parsed.username,
            "password": parsed.password,
        }

    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "menucloud"),
        "user": os.getenv("DB_USER", "menucloud"),
        "password": os.getenv("DB_PASSWORD", "menucloud"),
    }


DB_CONFIG = build_db_config()