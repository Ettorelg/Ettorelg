import bcrypt
import psycopg2

from db_config import DB_CONFIG


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    print("▶ Avvio seed database demo...")

    allergens = [
        ("GLUTEN", "Cereali contenenti glutine"),
        ("CROSTACEI", "Crostacei"),
        ("UOVA", "Uova"),
        ("PESCE", "Pesce"),
        ("ARACHIDI", "Arachidi"),
        ("SOIA", "Soia"),
        ("LATTE", "Latte"),
        ("FRUTTA_GUSCIO", "Frutta a guscio"),
        ("SEDANO", "Sedano"),
        ("SENAPE", "Senape"),
        ("SESAMO", "Semi di sesamo"),
        ("SOLFITI", "Anidride solforosa e solfiti"),
        ("LUPINI", "Lupini"),
        ("MOLLUSCHI", "Molluschi"),
    ]

    for code, name in allergens:
        cur.execute(
            """
            INSERT INTO allergens(code, name)
            VALUES (%s, %s)
            ON CONFLICT (code) DO NOTHING
            """,
            (code, name),
        )

    cur.execute(
        """
        INSERT INTO restaurants
        (name, slug, city, province, short_desc, long_desc, is_active)
        VALUES
        (%s, %s, %s, %s, %s, %s, true)
        ON CONFLICT (slug) DO NOTHING
        RETURNING id
        """,
        (
            "Ristorante Demo Alpha",
            "ristorante-demo-alpha",
            "Trapani",
            "TP",
            "Menu digitale con QR",
            "Ristorante demo per test e presentazioni",
        ),
    )

    row = cur.fetchone()
    if row:
        restaurant_id = row[0]
    else:
        cur.execute("SELECT id FROM restaurants WHERE slug=%s", ("ristorante-demo-alpha",))
        restaurant_id = cur.fetchone()[0]

    admin_email = "admin@alphasystem.it"
    admin_password = hash_password("Admin123!")

    cur.execute(
        """
        INSERT INTO users(email, password_hash, role, is_active)
        VALUES (%s, %s, 'admin', true)
        ON CONFLICT (email) DO NOTHING
        RETURNING id
        """,
        (admin_email, admin_password),
    )

    row = cur.fetchone()
    if row:
        user_id = row[0]
    else:
        cur.execute("SELECT id FROM users WHERE email=%s", (admin_email,))
        user_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO user_restaurants(user_id, restaurant_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (user_id, restaurant_id),
    )

    categories = [
        ("Antipasti", None, 10),
        ("Primi", None, 20),
        ("Secondi", None, 30),
        ("Dolci", None, 40),
        ("Bevande", None, 50),
    ]

    cat_ids = {}

    for name, parent, order in categories:
        cur.execute(
            """
            INSERT INTO categories
            (restaurant_id, name, parent_id, sort_order, is_active)
            VALUES (%s, %s, %s, %s, true)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (restaurant_id, name, parent, order),
        )

        row = cur.fetchone()
        if row:
            cat_ids[name] = row[0]
        else:
            cur.execute(
                """
                SELECT id FROM categories
                WHERE restaurant_id=%s AND name=%s AND parent_id IS NULL
                """,
                (restaurant_id, name),
            )
            cat_ids[name] = cur.fetchone()[0]

    subcategories = [
        ("Acqua e Bibite", "Bevande", 10),
        ("Birre", "Bevande", 20),
        ("Vini", "Bevande", 30),
    ]

    for name, parent_name, order in subcategories:
        cur.execute(
            """
            INSERT INTO categories
            (restaurant_id, name, parent_id, sort_order, is_active)
            VALUES (%s, %s, %s, %s, true)
            ON CONFLICT DO NOTHING
            """,
            (restaurant_id, name, cat_ids[parent_name], order),
        )

    products = [
        ("Bruschette al pomodoro", "Antipasti", "Pane tostato e pomodoro", 600),
        ("Pasta al pomodoro", "Primi", "Pasta con salsa di pomodoro", 900),
        ("Acqua naturale 0.5L", "Acqua e Bibite", None, 150),
    ]

    product_ids = {}

    for name, cat, desc, price in products:
        cur.execute(
            """
            INSERT INTO products
            (restaurant_id, category_id, name, description, price_cents, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING id
            """,
            (restaurant_id, cat_ids.get(cat) or cat_ids["Bevande"], name, desc, price),
        )

        product_ids[name] = cur.fetchone()[0]

    cur.execute("SELECT id FROM allergens WHERE code='GLUTEN'")
    gluten_id = cur.fetchone()[0]

    cur.execute(
        """
        INSERT INTO product_allergens(product_id, allergen_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (product_ids["Bruschette al pomodoro"], gluten_id),
    )

    for day in range(0, 6):
        cur.execute(
            """
            INSERT INTO opening_hours
            (restaurant_id, weekday, open_time, close_time, is_closed)
            VALUES (%s, %s, '12:00', '15:00', false)
            ON CONFLICT (restaurant_id, weekday) DO UPDATE
            SET open_time='12:00', close_time='15:00', is_closed=false
            """,
            (restaurant_id, day),
        )

    cur.execute(
        """
        INSERT INTO opening_hours
        (restaurant_id, weekday, is_closed)
        VALUES (%s, 6, true)
        ON CONFLICT (restaurant_id, weekday) DO UPDATE
        SET is_closed=true
        """,
        (restaurant_id,),
    )

    conn.commit()
    cur.close()
    conn.close()

    print("✅ Seed completato con successo")


if __name__ == "__main__":
    main()
