import os

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "menucloud"),
    "user": os.getenv("DB_USER", "menucloud"),
    "password": os.getenv("DB_PASSWORD", "menucloud"),
}
