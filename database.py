import os
import re
import psycopg
import json
from datetime import datetime, date, time as dt_time, timedelta, timezone
from decimal import Decimal
import random
from pathlib import Path
import hashlib
import hmac
import secrets


ACTIVE_ORDER_LIMIT = 2
CART_RESERVE_MINUTES = 3

ORDER_TRANSITIONS = {
    "new": {"cooking", "canceled"},
    "cooking": {"ready"},
    "ready": {"issued", "expired"},
    "issued": set(),
    "expired": set(),
    "canceled": set(),
}

PAYMENT_TRANSITIONS = {
    "unpaid": {"pending", "paid", "refund_required"},
    "pending": {"unpaid", "paid", "refund_required"},
    "paid": {"refunded", "refund_required"},
    "refund_required": {"refunded"},
    "refunded": set(),
}



MSK_TZ = timezone(timedelta(hours=3))


def hash_operator_password(password):
    iterations = 260000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"

def verify_operator_password(stored, password):
    try:
        scheme, iterations, salt_hex, digest_hex = (stored or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)).hex()
        return hmac.compare_digest(actual, digest_hex)
    except Exception:
        return False

class HybridRow(dict):
    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor):
    if cursor.description is None:
        return tuple

    names = [column.name for column in cursor.description]

    def make_row(values):
        return HybridRow(names, values)

    return make_row

def translate_sql(query):
    if not isinstance(query, str):
        return query
    q = query
    stripped = q.strip().upper()
    if stripped.startswith("PRAGMA "):
        return "SELECT 1"
    q = q.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    q = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", q, flags=re.IGNORECASE)
    if "INSERT OR IGNORE" in query.upper():
        q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    q = q.replace("GROUP_CONCAT(DISTINCT menu.name)", "STRING_AGG(DISTINCT menu.name, ',')")
    q = q.replace("datetime('now','-30 days')", "(CURRENT_TIMESTAMP - INTERVAL '30 days')")
    q = q.replace("datetime(created_at,'+3 hours')", "(created_at + INTERVAL '3 hours')")
    q = q.replace("strftime('%H', (created_at + INTERVAL '3 hours'))", "TO_CHAR((created_at + INTERVAL '3 hours'), 'HH24')")
    q = q.replace("(julianday(accepted_at)-julianday(created_at))*1440", "EXTRACT(EPOCH FROM (accepted_at-created_at))/60.0")
    q = q.replace("(julianday(ready_at)-julianday(accepted_at))*1440", "EXTRACT(EPOCH FROM (ready_at-accepted_at))/60.0")
    q = q.replace("(julianday(issued_at)-julianday(ready_at))*1440", "EXTRACT(EPOCH FROM (issued_at-ready_at))/60.0")
    q = q.replace("?", "%s")
    return q


class PostgresCursor(psycopg.Cursor):
    def execute(self, query, params=None, *, prepare=None, binary=None):
        return super().execute(translate_sql(query), params, prepare=prepare, binary=binary)

    def executemany(self, query, params_seq, *, returning=False):
        return super().executemany(translate_sql(query), params_seq, returning=returning)

    @property
    def lastrowid(self):
        with self.connection.cursor() as cur:
            cur.execute("SELECT LASTVAL()")
            row = cur.fetchone()
            return row[0] if row else None


class Database:
    def __init__(self, *args, **kwargs):
        self.host = os.getenv("DB_HOST", "postgres")
        self.port = int(os.getenv("DB_PORT", "5432"))
        self.name = os.getenv("DB_NAME", "vgmu_bufet")
        self.user = os.getenv("DB_USER", "vgmu")
        self.password = os.getenv("DB_PASSWORD", "")
        self.init_db()

    def conn(self):
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.name,
            user=self.user,
            password=self.password,
            row_factory=hybrid_row_factory,
            cursor_factory=PostgresCursor,
            connect_timeout=10,
        )

    def column_exists(self, cur, table, column):
        cur.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
        """, (table, column))
        return bool(cur.fetchone()[0])

    def add_column(self, cur, table, column, definition):
        if not self.column_exists(cur, table, column):
            cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

    def init_db(self):
        with self.conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS branches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    closed INTEGER DEFAULT 0,
                    closed_reason TEXT
                )
            """)

            try:
                cur.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS closed INTEGER DEFAULT 0")
            except Exception:
                pass

            try:
                cur.execute("ALTER TABLE branches ADD COLUMN IF NOT EXISTS closed_reason TEXT")
            except Exception:
                pass

            cur.execute("""
                CREATE TABLE IF NOT EXISTS kitchens(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    login TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    branch_id INTEGER NOT NULL,
                    FOREIGN KEY(branch_id) REFERENCES branches(id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS menu(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    icon TEXT,
                    image TEXT,
                    description TEXT,
                    available INTEGER DEFAULT 1,
                    is_special INTEGER DEFAULT 0,
                    old_price INTEGER DEFAULT 0,
                    discount_price INTEGER DEFAULT 0,
                    writeoff_enabled INTEGER DEFAULT 0,
                    writeoff_hours INTEGER DEFAULT 0
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS menu_stock(
                    menu_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    quantity INTEGER DEFAULT 0,
                    PRIMARY KEY(menu_id, branch_id),
                    FOREIGN KEY(menu_id) REFERENCES menu(id),
                    FOREIGN KEY(branch_id) REFERENCES branches(id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    items TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    status TEXT DEFAULT 'new',
                    pickup_code TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    accepted_at TIMESTAMP,
                    ready_at TIMESTAMP,
                    issued_at TIMESTAMP,
                    expires_at TIMESTAMP,
                    expired_at TIMESTAMP,
                    canceled_at TIMESTAMP,
                    stock_reserved INTEGER DEFAULT 0,
                    pickup_time TIMESTAMP,
                    payment_status TEXT DEFAULT 'unpaid',
                    payment_method TEXT DEFAULT 'cash',
                    paid_at TIMESTAMP,
                    payment_hold_expires_at TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(branch_id) REFERENCES branches(id)
                )
            """)

            for sql in [
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status TEXT DEFAULT 'unpaid'",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT 'cash'",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_hold_expires_at TIMESTAMP"
            ]:
                try:
                    cur.execute(sql)
                except Exception:
                    pass

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payment_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    provider_order_id TEXT,
                    amount INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending',
                    payment_url TEXT,
                    raw_response TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid_at TIMESTAMP,
                    refund_id TEXT,
                    refund_status TEXT,
                    refunded_at TIMESTAMP,
                    refund_raw_response TEXT,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                )
            """)

            for sql in [
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_id TEXT",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_status TEXT",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMP",
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_raw_response TEXT"
            ]:
                try:
                    cur.execute(sql)
                except Exception:
                    pass

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cart_reservations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token TEXT NOT NULL,
                    user_id INTEGER,
                    branch_id INTEGER NOT NULL,
                    items TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS combos(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    icon TEXT,
                    image TEXT,
                    description TEXT,
                    available INTEGER DEFAULT 1
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS combo_items(
                    combo_id INTEGER NOT NULL,
                    menu_id INTEGER NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    PRIMARY KEY(combo_id, menu_id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS branch_schedule(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id INTEGER NOT NULL,
                    weekday INTEGER NOT NULL,
                    open_time TEXT DEFAULT '08:00',
                    close_time TEXT DEFAULT '20:00',
                    enabled INTEGER DEFAULT 1,
                    UNIQUE(branch_id, weekday)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ratings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    menu_id INTEGER NOT NULL,
                    stars INTEGER NOT NULL,
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(order_id, menu_id)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS writeoff_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_id INTEGER NOT NULL,
                    branch_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS action_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_type TEXT NOT NULL,
                    actor_id INTEGER,
                    action TEXT NOT NULL,
                    order_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS incidents(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT UNIQUE NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    order_id INTEGER,
                    details TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            for table, col, definition in [
                ("menu", "image", "TEXT"),
                ("menu", "available", "INTEGER DEFAULT 1"),
                ("menu", "is_special", "INTEGER DEFAULT 0"),
                ("menu", "old_price", "INTEGER DEFAULT 0"),
                ("menu", "discount_price", "INTEGER DEFAULT 0"),
                ("menu", "writeoff_enabled", "INTEGER DEFAULT 0"),
                ("menu", "writeoff_hours", "INTEGER DEFAULT 0"),
                ("orders", "branch_id", "INTEGER DEFAULT 1"),
                ("orders", "pickup_time", "TIMESTAMP"),
                ("orders", "accepted_at", "TIMESTAMP"),
                ("orders", "ready_at", "TIMESTAMP"),
                ("orders", "issued_at", "TIMESTAMP"),
                ("orders", "expires_at", "TIMESTAMP"),
                ("orders", "expired_at", "TIMESTAMP"),
                ("orders", "canceled_at", "TIMESTAMP"),
                ("orders", "stock_reserved", "INTEGER DEFAULT 0"),
            ]:
                self.add_column(cur, table, col, definition)

            cur.execute("SELECT COUNT(*) FROM branches")
            if cur.fetchone()[0] == 0:
                cur.executemany("INSERT INTO branches(name) VALUES(?)", [
                    ("Буфет в подвале",),
                    ("Буфет на 1-ом этаже",),
                    ("Буфет на 2-ом этаже",),
                ])

            cur.execute("SELECT id FROM branches")
            schedule_branch_ids = [r["id"] for r in cur.fetchall()]

            for branch_id in schedule_branch_ids:
                for weekday in range(7):
                    cur.execute("""
                        INSERT OR IGNORE INTO branch_schedule(branch_id, weekday, open_time, close_time, enabled)
                        VALUES(?,?,?,?,?)
                    """, (branch_id, weekday, "08:00", "20:00", 1))

            cur.execute("SELECT COUNT(*) FROM kitchens")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO kitchens(login,password,branch_id) VALUES(?,?,?)",
                    [
                        ("podval", "1234", 1),
                        ("floor1", "1234", 2),
                        ("floor2", "1234", 3),
                    ]
                )

            # Security migration: old versions stored operator passwords as plaintext.
            for op_row in cur.execute("SELECT id,password FROM kitchens").fetchall():
                stored = op_row["password"] or ""
                if not stored.startswith("pbkdf2_sha256$"):
                    cur.execute("UPDATE kitchens SET password=? WHERE id=?", (hash_operator_password(stored), op_row["id"]))

            cur.execute("SELECT COUNT(*) FROM menu")
            if cur.fetchone()[0] == 0:
                cur.executemany("""
                    INSERT INTO menu
                    (name, price, icon, image, description, available, is_special, old_price, discount_price)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, [
                    ("Сэндвич с тунцом", 250, "", "default_sandwich.webp", "Тунец, овощи, соус", 1, 0, 0, 0),
                    ("Кофе американо", 120, "", "default_coffee.webp", "Свежесваренный кофе", 1, 0, 0, 0),
                    ("Пицца", 180, "", "default_pizza.webp", "Сырная пицца", 1, 0, 0, 0),
                    ("Блюдо дня", 149, "", "default_sandwich.webp", "Курица, сыр, салат", 1, 1, 250, 149),
                    ("Хот-дог", 160, "", "default_burger.webp", "Сосиска, булочка, соус", 1, 0, 0, 0),
                    ("Салат", 140, "", "default_salad.webp", "Овощной салат", 1, 0, 0, 0),
                ])

            # V56_SAFE_DEFAULT_IMAGES
            # Назначаем реальные дефолтные фото старым позициям, если у них пустое image.
            default_food_images = [
                ("%коф%", "default_coffee.webp"),
                ("%американо%", "default_coffee.webp"),
                ("%капуч%", "default_coffee.webp"),
                ("%чай%", "default_tea.webp"),
                ("%пицц%", "default_pizza.webp"),
                ("%салат%", "default_salad.webp"),
                ("%хот-дог%", "default_burger.webp"),
                ("%бургер%", "default_burger.webp"),
                ("%сок%", "default_juice.webp"),
                ("%напит%", "default_juice.webp"),
                ("%торт%", "default_cake.webp"),
                ("%пирож%", "default_pastry.webp"),
                ("%булоч%", "default_pastry.webp"),
                ("%печ%", "default_pastry.webp"),
                ("%сэндв%", "default_sandwich.webp"),
                ("%бутер%", "default_sandwich.webp"),
                ("%блюдо дня%", "default_sandwich.webp"),
            ]

            for pattern, image_name in default_food_images:
                cur.execute("""
                    UPDATE menu
                    SET image=?
                    WHERE (image IS NULL OR image='')
                      AND lower(name) LIKE ?
                """, (image_name, pattern))

            cur.execute("""
                UPDATE menu
                SET image='default_sandwich.webp'
                WHERE image IS NULL OR image=''
            """)

            cur.execute("SELECT id FROM menu")
            menu_ids = [r["id"] for r in cur.fetchall()]

            cur.execute("SELECT id FROM branches")
            branch_ids = [r["id"] for r in cur.fetchall()]

            for mid in menu_ids:
                for bid in branch_ids:
                    cur.execute(
                        "INSERT OR IGNORE INTO menu_stock(menu_id,branch_id,quantity) VALUES(?,?,?)",
                        (mid, bid, 10)
                    )

            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_branch_status ON orders(branch_id,status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_expires ON orders(expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_menu_branch ON menu_stock(menu_id,branch_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_action_log_order ON action_log(order_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reserve_token ON cart_reservations(token)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reserve_expires ON cart_reservations(expires_at)")

            # Убираем дубли расписания из старых версий, чтобы снятые галочки не возвращались.
            cur.execute("""
                DELETE FROM branch_schedule
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM branch_schedule
                    GROUP BY branch_id, weekday
                )
            """)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_branch_weekday_unique ON branch_schedule(branch_id, weekday)")

            conn.commit()

    def log_action(self, cur, actor_type, actor_id, action, order_id=None, details=None):
        cur.execute("""
            INSERT INTO action_log(actor_type, actor_id, action, order_id, details)
            VALUES(?,?,?,?,?)
        """, (actor_type, actor_id, action, order_id, details))

    def get_action_log(self, order_id=None, limit=200):
        with self.conn() as conn:
            if order_id:
                rows = conn.execute("""
                    SELECT * FROM action_log
                    WHERE order_id=?
                    ORDER BY id DESC
                    LIMIT ?
                """, (order_id, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM action_log
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,)).fetchall()

            return [dict(r) for r in rows]


    def _assert_order_transition(self, current_status, new_status, payment_status=None):
        current_status = str(current_status or "")
        new_status = str(new_status or "")
        if current_status == new_status:
            return True
        allowed = ORDER_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise Exception(f"Недопустимый переход заказа: {current_status} → {new_status}")
        if new_status in ("cooking", "issued") and payment_status == "pending":
            raise Exception("Нельзя продолжить заказ, пока онлайн-оплата ожидает подтверждения")
        return True

    def _assert_payment_transition(self, current_status, new_status, order_status=None):
        current_status = str(current_status or "unpaid")
        new_status = str(new_status or "")
        if current_status == new_status:
            return True
        allowed = PAYMENT_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise Exception(f"Недопустимый переход оплаты: {current_status} → {new_status}")
        if new_status == "paid" and order_status in ("canceled", "expired", "issued"):
            raise Exception("Закрытый заказ нельзя пометить оплаченным")
        return True

    def record_incident(self, fingerprint, category, message, severity="warning", order_id=None, details=""):
        with self.conn() as conn:
            conn.execute("""
                INSERT INTO incidents(fingerprint,severity,category,message,order_id,details,status,updated_at)
                VALUES(?,?,?,?,?,?,'open',CURRENT_TIMESTAMP)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    severity=excluded.severity,
                    category=excluded.category,
                    message=excluded.message,
                    order_id=excluded.order_id,
                    details=excluded.details,
                    status='open',
                    updated_at=CURRENT_TIMESTAMP
            """, (fingerprint, severity, category, message, order_id, details))
            conn.commit()

    def resolve_incident(self, incident_id):
        with self.conn() as conn:
            conn.execute("UPDATE incidents SET status='resolved', updated_at=CURRENT_TIMESTAMP WHERE id=?", (incident_id,))
            conn.commit()

    def get_incidents(self, status="open", limit=200):
        with self.conn() as conn:
            if status:
                rows = conn.execute("""
                    SELECT * FROM incidents
                    WHERE status=?
                    ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id DESC
                    LIMIT ?
                """, (status, int(limit))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def scan_state_incidents(self):
        found = []
        with self.conn() as conn:
            cur = conn.cursor()
            checks = [
                ("pending_closed", "critical", "payment", """
                    SELECT id,status,payment_status FROM orders
                    WHERE payment_status='pending' AND status IN ('issued','expired','canceled')
                """, "Закрытый заказ всё ещё ждёт онлайн-оплату"),
                ("ready_without_code", "critical", "order", """
                    SELECT id,status,payment_status FROM orders
                    WHERE status='ready' AND (pickup_code IS NULL OR TRIM(pickup_code)='')
                """, "Готовый заказ не имеет кода выдачи"),
                ("issued_without_time", "warning", "order", """
                    SELECT id,status,payment_status FROM orders
                    WHERE status='issued' AND issued_at IS NULL
                """, "Выданный заказ не имеет времени выдачи"),
                ("paid_without_time", "warning", "payment", """
                    SELECT id,status,payment_status FROM orders
                    WHERE payment_status='paid' AND paid_at IS NULL
                """, "Оплаченный заказ не имеет времени оплаты"),
                ("closed_reserved", "critical", "stock", """
                    SELECT id,status,payment_status FROM orders
                    WHERE status IN ('expired','canceled') AND stock_reserved=1
                """, "Закрытый заказ всё ещё удерживает товар"),
            ]
            for key, severity, category, sql, message in checks:
                for row in cur.execute(sql).fetchall():
                    found.append((f"{key}:order:{row['id']}", severity, category, message, row["id"], f"status={row['status']}, payment={row['payment_status']}"))

            for row in cur.execute("""
                SELECT menu_id,branch_id,quantity FROM menu_stock WHERE quantity < 0
            """).fetchall():
                found.append((f"negative_stock:{row['menu_id']}:{row['branch_id']}", "critical", "stock", "Отрицательный остаток товара", None, f"menu_id={row['menu_id']}, branch_id={row['branch_id']}, quantity={row['quantity']}"))

        for fingerprint, severity, category, message, order_id, details in found:
            self.record_incident(fingerprint, category, message, severity, order_id, details)
        return len(found)

    def resolve_incident_by_fingerprint(self, fingerprint):
        with self.conn() as conn:
            conn.execute("UPDATE incidents SET status='resolved', updated_at=CURRENT_TIMESTAMP WHERE fingerprint=?", (fingerprint,))
            conn.commit()

    def get_pending_order_ids(self, limit=20):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT id FROM orders
                WHERE payment_status='pending' AND status IN ('new','cooking','ready')
                ORDER BY id ASC
                LIMIT ?
            """, (int(limit),)).fetchall()
            return [int(r["id"]) for r in rows]

    def get_health_snapshot(self):
        with self.conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1").fetchone()
            result = {
                "orders_active": int(cur.execute("SELECT COUNT(*) AS cnt FROM orders WHERE status IN ('new','cooking','ready')").fetchone()["cnt"] or 0),
                "payments_pending": int(cur.execute("SELECT COUNT(*) AS cnt FROM orders WHERE payment_status='pending'").fetchone()["cnt"] or 0),
                "refund_required": int(cur.execute("SELECT COUNT(*) AS cnt FROM orders WHERE payment_status='refund_required'").fetchone()["cnt"] or 0),
                "incidents_open": int(cur.execute("SELECT COUNT(*) AS cnt FROM incidents WHERE status='open'").fetchone()["cnt"] or 0),
                "negative_stock": int(cur.execute("SELECT COUNT(*) AS cnt FROM menu_stock WHERE quantity<0").fetchone()["cnt"] or 0),
                "cart_reservations": int(cur.execute("SELECT COUNT(*) AS cnt FROM cart_reservations").fetchone()["cnt"] or 0),
            }
            last_action = cur.execute("SELECT created_at,action FROM action_log ORDER BY id DESC LIMIT 1").fetchone()
            result["last_action_at"] = last_action["created_at"] if last_action else None
            result["last_action"] = last_action["action"] if last_action else None
            return result

    def cleanup_expired_reservations(self, cur):
        now = datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("DELETE FROM cart_reservations WHERE expires_at < ?", (now,))

    def get_reserved_map(self, cur, branch_id, exclude_token=None):
        self.cleanup_expired_reservations(cur)

        rows = cur.execute("""
            SELECT token, items
            FROM cart_reservations
            WHERE branch_id=? AND (? IS NULL OR token != ?)
        """, (branch_id, exclude_token, exclude_token)).fetchall()

        reserved = {}

        for row in rows:
            try:
                items = json.loads(row["items"])
            except Exception:
                items = []

            for item in items:
                menu_id = int(item["id"])
                reserved[menu_id] = reserved.get(menu_id, 0) + int(item["quantity"])

        return reserved

    def clear_reservation(self, token):
        with self.conn() as conn:
            conn.execute("DELETE FROM cart_reservations WHERE token=?", (token,))
            conn.commit()

    def reserve_cart(self, token, user_id, branch_id, items):
        if not token:
            raise Exception("Нет токена корзины")

        if not items:
            self.clear_reservation(token)
            return None

        expires_at = datetime.now(MSK_TZ) + timedelta(minutes=CART_RESERVE_MINUTES)

        with self.conn() as conn:
            cur = conn.cursor()
            self.validate_items_against_stock(cur, int(branch_id), items, exclude_token=token)

            cur.execute("DELETE FROM cart_reservations WHERE token=?", (token,))

            cur.execute("""
                INSERT INTO cart_reservations(token,user_id,branch_id,items,expires_at)
                VALUES(?,?,?,?,?)
            """, (
                token,
                user_id,
                int(branch_id),
                json.dumps(items, ensure_ascii=False),
                expires_at.strftime("%Y-%m-%d %H:%M:%S")
            ))

            conn.commit()

        return expires_at.strftime("%Y-%m-%d %H:%M:%S")

    def get_branches(self):
        with self.conn() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM branches ORDER BY id").fetchall()
            ]

    def add_branch(self, name):
        name = (name or "").strip()

        if not name:
            raise Exception("Введите название буфета")

        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO branches(name, closed, closed_reason)
                VALUES(?,?,?)
            """, (name, 0, ""))

            branch_id = cur.lastrowid

            # Создаём нулевые остатки для всех существующих позиций меню.
            menu_rows = cur.execute("SELECT id FROM menu").fetchall()

            for row in menu_rows:
                cur.execute("""
                    INSERT OR IGNORE INTO menu_stock(menu_id, branch_id, quantity)
                    VALUES(?,?,0)
                """, (row["id"], branch_id))

            # Создаём расписание по умолчанию: Пн-Пт 08:00-20:00, Сб-Вс выходной.
            for weekday in range(7):
                cur.execute("""
                    INSERT INTO branch_schedule(branch_id, weekday, open_time, close_time, enabled)
                    VALUES(?,?,?,?,?)
                """, (branch_id, weekday, "08:00", "20:00", 1 if weekday < 5 else 0))

            self.log_action(cur, "admin", None, "Добавлен новый буфет", None, f"branch_id={branch_id}, name={name}")
            conn.commit()

            return branch_id

    def set_branch_closed(self, branch_id, closed, reason=""):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                UPDATE branches
                SET closed=?, closed_reason=?
                WHERE id=?
            """, (1 if closed else 0, reason or "", branch_id))

            self.log_action(
                cur,
                "admin",
                None,
                "Изменён статус буфета",
                None,
                f"branch_id={branch_id}, closed={closed}, reason={reason}"
            )

            conn.commit()

    def create_user(self, phone):
        with self.conn() as conn:
            cur = conn.cursor()
            name = "User" + phone[-6:]

            cur.execute(
                "INSERT INTO users(phone,name) VALUES(?,?)",
                (phone, name)
            )

            conn.commit()
            return cur.lastrowid

    def get_user(self, uid):
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=?",
                (uid,)
            ).fetchone()

            return dict(row) if row else None

    def get_user_by_phone(self, phone):
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE phone=?",
                (phone,)
            ).fetchone()

            return dict(row) if row else None

    def get_kitchen(self, login, password):
        login = (login or "").strip()
        password = password or ""
        with self.conn() as conn:
            row = conn.execute("""
                SELECT kitchens.*, branches.name AS branch_name
                FROM kitchens
                JOIN branches ON branches.id=kitchens.branch_id
                WHERE login=?
            """, (login,)).fetchone()

            if not row:
                return None

            data = dict(row)
            stored = data.get("password") or ""
            valid = False
            try:
                if stored.startswith("pbkdf2_sha256$"):
                    valid = verify_operator_password(stored, password)
                else:
                    valid = hmac.compare_digest(stored, password)
            except Exception:
                valid = False

            if not valid:
                return None

            # Opportunistic migration if a legacy DB somehow still has plaintext.
            if not stored.startswith("pbkdf2_sha256$"):
                conn.execute("UPDATE kitchens SET password=? WHERE id=?", (hash_operator_password(password), data["id"]))
                conn.commit()

            data.pop("password", None)
            return data

    def get_kitchen_by_id(self, kid):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT kitchens.*, branches.name AS branch_name
                FROM kitchens
                JOIN branches ON branches.id=kitchens.branch_id
                WHERE kitchens.id=?
            """, (kid,)).fetchone()

            if not row:
                return None
            data = dict(row)
            data.pop("password", None)
            return data

    def get_operators(self):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT
                    kitchens.id,
                    kitchens.login,
                    kitchens.branch_id,
                    branches.name AS branch_name
                FROM kitchens
                JOIN branches ON branches.id=kitchens.branch_id
                ORDER BY kitchens.id DESC
            """).fetchall()

            return [dict(r) for r in rows]

    def add_operator(self, login, password, branch_id):
        login = (login or "").strip()
        password = (password or "").strip()

        if not login:
            raise Exception("Введите логин оператора")

        if not password:
            raise Exception("Введите пароль оператора")

        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO kitchens(login,password,branch_id)
                VALUES(?,?,?)
            """, (login, hash_operator_password(password), int(branch_id)))

            operator_id = cur.lastrowid

            self.log_action(cur, "admin", None, "Добавлен оператор", None, f"operator_id={operator_id}, login={login}, branch_id={branch_id}")
            conn.commit()

            return operator_id

    def update_operator(self, operator_id, password=None, branch_id=None):
        with self.conn() as conn:
            cur = conn.cursor()

            if password:
                cur.execute("""
                    UPDATE kitchens
                    SET password=?
                    WHERE id=?
                """, (hash_operator_password((password or "").strip()), operator_id))

            if branch_id:
                cur.execute("""
                    UPDATE kitchens
                    SET branch_id=?
                    WHERE id=?
                """, (int(branch_id), operator_id))

            self.log_action(cur, "admin", None, "Изменён оператор", None, f"operator_id={operator_id}")
            conn.commit()

    def delete_operator(self, operator_id):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("DELETE FROM kitchens WHERE id=?", (operator_id,))
            self.log_action(cur, "admin", None, "Удалён оператор", None, f"operator_id={operator_id}")
            conn.commit()

    def get_menu_for_branch(self, branch_id, token=None):
        branch_id = int(branch_id)

        with self.conn() as conn:
            cur = conn.cursor()
            reserved_other = self.get_reserved_map(cur, branch_id, exclude_token=token)

            rows = cur.execute("""
                SELECT
                    menu.*,
                    ROUND(AVG(ratings.stars), 1) AS avg_stars,
                    COUNT(ratings.id) AS rating_count
                FROM menu
                LEFT JOIN ratings ON ratings.menu_id=menu.id
                WHERE menu.available=1
                GROUP BY menu.id
                ORDER BY menu.is_special DESC, menu.id DESC
            """).fetchall()

            result = []

            for row in rows:
                item = dict(row)

                item["avg_stars"] = float(item["avg_stars"]) if item.get("avg_stars") is not None else None
                item["rating_count"] = int(item.get("rating_count") or 0)

                stock_rows = cur.execute("""
                    SELECT
                        branches.id,
                        branches.name,
                        COALESCE(menu_stock.quantity, 0) AS quantity
                    FROM branches
                    LEFT JOIN menu_stock
                        ON menu_stock.branch_id=branches.id
                        AND menu_stock.menu_id=?
                    ORDER BY branches.id
                """, (item["id"],)).fetchall()

                all_stock = []

                for stock_row in stock_rows:
                    stock = dict(stock_row)

                    if int(stock["id"]) == branch_id:
                        stock["reserved"] = int(reserved_other.get(int(item["id"]), 0))
                        stock["quantity_available"] = max(0, int(stock["quantity"]) - stock["reserved"])
                    else:
                        stock["reserved"] = 0
                        stock["quantity_available"] = int(stock["quantity"])

                    all_stock.append(stock)

                selected = next((s for s in all_stock if int(s["id"]) == branch_id), None)

                item["quantity"] = selected["quantity_available"] if selected else 0
                item["raw_quantity"] = selected["quantity"] if selected else 0
                item["reserved_by_others"] = selected["reserved"] if selected else 0
                item["all_stock"] = all_stock
                item["total_stock"] = sum(int(s["quantity_available"]) for s in all_stock)

                result.append(item)

            return result


    def get_all_menu_with_stock(self):
        with self.conn() as conn:
            items = [
                dict(r)
                for r in conn.execute("SELECT * FROM menu ORDER BY id DESC").fetchall()
            ]

            for item in items:
                rows = conn.execute("""
                    SELECT
                        branches.id,
                        branches.name,
                        COALESCE(menu_stock.quantity,0) AS quantity
                    FROM branches
                    LEFT JOIN menu_stock
                        ON menu_stock.branch_id=branches.id
                        AND menu_stock.menu_id=?
                    ORDER BY branches.id
                """, (item["id"],)).fetchall()

                item["stock"] = [dict(r) for r in rows]

            return items

    def add_menu_item(self, name, price, icon, description, image, is_special, old_price, discount_price, writeoff_enabled=0, writeoff_hours=0):
        with self.conn() as conn:
            cur = conn.cursor()

            if is_special:
                cur.execute("UPDATE menu SET is_special=0")

            cur.execute("""
                INSERT INTO menu
                (name,price,icon,image,description,available,is_special,old_price,discount_price,writeoff_enabled,writeoff_hours)
                VALUES(?,?,?,?,?,1,?,?,?,?,?)
            """, (
                name,
                price,
                icon,
                image,
                description,
                is_special,
                old_price,
                discount_price,
                1 if writeoff_enabled else 0,
                int(writeoff_hours or 0)
            ))

            item_id = cur.lastrowid

            for branch in self.get_branches():
                cur.execute("""
                    INSERT OR IGNORE INTO menu_stock(menu_id,branch_id,quantity)
                    VALUES(?,?,0)
                """, (item_id, branch["id"]))

            self.log_action(cur, "admin", None, "Добавлена позиция меню", None, name)
            conn.commit()
            return item_id


    def update_menu_item(self, item_id, name, price, icon, description, available, is_special, old_price, discount_price, writeoff_enabled=0, writeoff_hours=0):
        with self.conn() as conn:
            cur = conn.cursor()

            if is_special:
                cur.execute("UPDATE menu SET is_special=0 WHERE id!=?", (item_id,))

            cur.execute("""
                UPDATE menu
                SET
                    name=?,
                    price=?,
                    icon=?,
                    description=?,
                    available=?,
                    is_special=?,
                    old_price=?,
                    discount_price=?,
                    writeoff_enabled=?,
                    writeoff_hours=?
                WHERE id=?
            """, (
                name,
                price,
                icon,
                description,
                available,
                is_special,
                old_price,
                discount_price,
                1 if writeoff_enabled else 0,
                int(writeoff_hours or 0),
                item_id
            ))

            self.log_action(cur, "admin", None, "Изменена позиция меню", None, f"menu_id={item_id}")
            conn.commit()


    def set_available(self, item_id, available):
        with self.conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE menu SET available=? WHERE id=?",
                (available, item_id)
            )
            self.log_action(cur, "admin", None, "Изменена видимость позиции меню", None, f"menu_id={item_id}, available={available}")
            conn.commit()

    def delete_menu_item(self, item_id):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("DELETE FROM menu_stock WHERE menu_id=?", (item_id,))
            cur.execute("DELETE FROM combo_items WHERE menu_id=?", (item_id,))
            cur.execute("DELETE FROM menu WHERE id=?", (item_id,))

            self.log_action(cur, "admin", None, "Позиция меню удалена навсегда", None, f"menu_id={item_id}")
            conn.commit()

    def set_menu_writeoff(self, item_id, enabled):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute(
                "UPDATE menu SET writeoff_enabled=?, writeoff_hours=0 WHERE id=?",
                (1 if enabled else 0, item_id)
            )

            self.log_action(
                cur,
                "admin",
                None,
                "Изменено автосписание позиции",
                None,
                f"menu_id={item_id}, enabled={enabled}"
            )

            conn.commit()

    def set_stock(self, menu_id, branch_id, quantity):
        with self.conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO menu_stock(menu_id,branch_id,quantity)
                VALUES(?,?,?)
                ON CONFLICT(menu_id,branch_id)
                DO UPDATE SET quantity=excluded.quantity
            """, (menu_id, branch_id, max(0, quantity)))
            self.log_action(cur, "admin", None, "Изменён остаток", None, f"menu_id={menu_id}, branch_id={branch_id}, quantity={quantity}")
            conn.commit()

    def count_active_orders_for_user(self, cur, user_id):
        row = cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM orders
            WHERE user_id=? AND status IN ('new','cooking','ready')
        """, (user_id,)).fetchone()
        return int(row["cnt"] or 0)

    def validate_items_against_stock(self, cur, branch_id, items, exclude_token=None):
        reserved_other = self.get_reserved_map(cur, branch_id, exclude_token=exclude_token)

        checked = []
        total = 0

        for item in items:
            qty = int(item.get("quantity", 0))

            if qty <= 0:
                raise Exception("Некорректное количество")

            # Комбо-набор
            if item.get("is_combo") or item.get("combo_id"):
                combo_id = int(item.get("combo_id") or item.get("id"))

                combo = cur.execute("""
                    SELECT *
                    FROM combos
                    WHERE id=? AND available=1
                """, (combo_id,)).fetchone()

                if not combo:
                    raise Exception("Комбо убрано из меню")

                parts = cur.execute("""
                    SELECT
                        menu.id,
                        menu.name,
                        menu.icon,
                        menu.available,
                        combo_items.quantity AS need_qty,
                        COALESCE(menu_stock.quantity,0) AS stock_qty
                    FROM combo_items
                    JOIN menu ON menu.id=combo_items.menu_id
                    LEFT JOIN menu_stock
                        ON menu_stock.menu_id=menu.id
                        AND menu_stock.branch_id=?
                    WHERE combo_items.combo_id=?
                """, (branch_id, combo_id)).fetchall()

                if not parts:
                    raise Exception(f"Комбо «{combo['name']}» пустое")

                component_items = []

                for part in parts:
                    need = int(part["need_qty"]) * qty
                    available = int(part["stock_qty"]) - int(reserved_other.get(int(part["id"]), 0))

                    if part["available"] != 1:
                        raise Exception(f"Позиция «{part['name']}» из комбо убрана из меню")

                    if available < need:
                        raise Exception(f"Для комбо «{combo['name']}» недостаточно позиции «{part['name']}». Доступно: {available}")

                    component_items.append({
                        "id": int(part["id"]),
                        "name": part["name"],
                        "icon": part["icon"] or "🥪",
                        "quantity": need
                    })

                checked.append({
                    "combo_id": combo_id,
                    "is_combo": True,
                    "name": combo["name"],
                    "price": int(combo["price"]),
                    "icon": combo["icon"] or "🍱",
                    "quantity": qty,
                    "components": component_items
                })

                total += int(combo["price"]) * qty
                continue

            # Обычная позиция меню
            mid = int(item["id"])

            row = cur.execute("""
                SELECT menu.*, COALESCE(menu_stock.quantity,0) AS quantity
                FROM menu
                LEFT JOIN menu_stock
                    ON menu_stock.menu_id=menu.id
                    AND menu_stock.branch_id=?
                WHERE menu.id=?
            """, (branch_id, mid)).fetchone()

            if not row or row["available"] != 1:
                raise Exception("Позиция убрана из меню")

            available = int(row["quantity"]) - int(reserved_other.get(mid, 0))

            if available < qty:
                total_in_all = cur.execute("""
                    SELECT SUM(quantity) AS total
                    FROM menu_stock
                    WHERE menu_id=?
                """, (mid,)).fetchone()["total"] or 0

                if total_in_all > 0:
                    raise Exception(
                        f"Позиция «{row['name']}» закончилась или забронирована в выбранном буфете, попробуйте другой буфет"
                    )

                raise Exception(f"Позиция «{row['name']}» закончилась во всех буфетах")

            price = row["discount_price"] if row["is_special"] and row["discount_price"] else row["price"]

            checked.append({
                "id": row["id"],
                "name": row["name"],
                "price": int(price),
                "icon": row["icon"] or "🥪",
                "quantity": qty
            })

            total += int(price) * qty

        return checked, total


    def create_order(self, user_id, branch_id, items, token=None, pickup_time=None):
        if not items:
            raise Exception("Корзина пуста")

        now = datetime.now(MSK_TZ).replace(tzinfo=None)
        branch_id = int(branch_id)
        pickup_time_db = None

        if pickup_time:
            try:
                pickup_dt = datetime.strptime(pickup_time, "%Y-%m-%dT%H:%M")
            except Exception:
                raise Exception("Некорректное время предзаказа")

            if pickup_dt.date() != now.date():
                raise Exception("Предзаказ доступен только на текущий день")

            if pickup_dt < now:
                raise Exception("Нельзя выбрать прошедшее время")

            if not self.branch_is_open(branch_id, pickup_dt):
                raise Exception("В выбранное время буфет не работает")

            pickup_time_db = pickup_dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            if not self.branch_is_open(branch_id, now):
                raise Exception("Буфет сейчас закрыт. Выберите другой буфет или сделайте предзаказ на рабочее время.")

        with self.conn() as conn:
            cur = conn.cursor()

            active_count = self.count_active_orders_for_user(cur, user_id)

            if active_count >= ACTIVE_ORDER_LIMIT:
                raise Exception(f"У вас уже есть {ACTIVE_ORDER_LIMIT} активных заказа. Заберите или отмените один из них.")

            checked, total = self.validate_items_against_stock(
                cur,
                branch_id,
                items,
                exclude_token=token
            )

            cur.execute("""
                INSERT INTO orders(user_id,branch_id,items,total,status,stock_reserved,pickup_time,payment_status,payment_method)
                VALUES(?,?,?,?, 'new', 0, ?, 'unpaid', 'cash')
            """, (
                user_id,
                branch_id,
                json.dumps(checked, ensure_ascii=False),
                total,
                pickup_time_db
            ))

            order_id = cur.lastrowid

            if token:
                cur.execute("DELETE FROM cart_reservations WHERE token=?", (token,))

            self.log_action(
                cur,
                "user",
                user_id,
                "Создан заказ",
                order_id,
                f"branch_id={branch_id}, total={total}, pickup_time={pickup_time_db}"
            )

            conn.commit()
            return order_id

    def cancel_order(self, order_id, user_id):
        now = datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")

        with self.conn() as conn:
            cur = conn.cursor()

            order = cur.execute("""
                SELECT * FROM orders
                WHERE id=? AND user_id=?
            """, (order_id, user_id)).fetchone()

            if not order:
                raise Exception("Заказ не найден")

            if order["status"] != "new":
                raise Exception("Можно отменить только заказ со статусом «Заказ принят», пока повар его не принял")

            self._assert_order_transition(order["status"], "canceled", order["payment_status"])

            if order["stock_reserved"]:
                self._restore_order_stock(cur, order)

            cur.execute("""
                UPDATE orders
                SET status='canceled', canceled_at=?, payment_hold_expires_at=NULL
                WHERE id=?
            """, (now, order_id))

            self.log_action(cur, "user", user_id, "Отменён заказ", order_id)

            conn.commit()
            return order["branch_id"]

    def _restore_order_stock(self, cur, order):
        if not order["stock_reserved"]:
            return False

        items = json.loads(order["items"])
        for item in items:
            stock_items = item.get("components") if item.get("is_combo") else [item]
            for stock_item in stock_items:
                cur.execute("""
                    UPDATE menu_stock
                    SET quantity=quantity+?
                    WHERE menu_id=? AND branch_id=?
                """, (stock_item["quantity"], stock_item["id"], order["branch_id"]))

        cur.execute("UPDATE orders SET stock_reserved=0 WHERE id=?", (order["id"],))
        return True

    def start_payment_hold(self, order_id, minutes=10):
        minutes = max(3, min(60, int(minutes or 10)))
        expires_at = datetime.now(MSK_TZ).replace(tzinfo=None) + timedelta(minutes=minutes)

        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")
            if order["status"] not in ("new", "cooking", "ready"):
                raise Exception("Этот заказ уже закрыт")
            if order["payment_status"] == "paid":
                return None

            # На время перехода в платёжный шлюз реальные остатки списываются в резерв.
            self.reserve_stock(cur, order)
            cur.execute("""
                UPDATE orders
                SET payment_hold_expires_at=?
                WHERE id=?
            """, (expires_at.strftime("%Y-%m-%d %H:%M:%S"), order_id))
            self.log_action(cur, "system", None, "Товар зарезервирован на время оплаты", order_id, f"minutes={minutes}")
            conn.commit()
        return expires_at.strftime("%Y-%m-%d %H:%M:%S")

    def release_payment_hold(self, order_id, reason="Оплата не началась"):
        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                return False
            # После принятия оператором или успешной оплаты запас уже относится к заказу и не освобождается.
            if order["status"] != "new" or order["payment_status"] == "paid":
                cur.execute("UPDATE orders SET payment_hold_expires_at=NULL WHERE id=?", (order_id,))
                conn.commit()
                return False
            restored = self._restore_order_stock(cur, order)
            cur.execute("""
                UPDATE orders
                SET payment_hold_expires_at=NULL,
                    payment_status=CASE WHEN payment_status='pending' THEN 'unpaid' ELSE payment_status END,
                    payment_method=CASE WHEN payment_status='pending' THEN 'cash' ELSE payment_method END
                WHERE id=?
            """, (order_id,))
            cur.execute("UPDATE payments SET status='expired' WHERE order_id=? AND status='pending'", (order_id,))
            self.log_action(cur, "system", None, "Платёжная бронь снята", order_id, reason)
            conn.commit()
            return restored

    def expire_payment_holds(self):
        now = datetime.now(MSK_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        changed = []
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT id, branch_id FROM orders
                WHERE status='new'
                  AND payment_status='pending'
                  AND payment_hold_expires_at IS NOT NULL
                  AND payment_hold_expires_at < ?
            """, (now,)).fetchall()
        for row in rows:
            if self.release_payment_hold(row["id"], "Истекло время ожидания онлайн-оплаты"):
                changed.append(row["branch_id"])
        return list(set(changed))

    def get_latest_payment_for_order(self, order_id):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT * FROM payments
                WHERE order_id=?
                ORDER BY id DESC LIMIT 1
            """, (order_id,)).fetchone()
            return dict(row) if row else None

    def get_payment_attempt_count(self, order_id):
        with self.conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM payments WHERE order_id=?", (order_id,)).fetchone()
            return int(row["cnt"] or 0)

    def mark_payment_canceled(self, order_id, provider_order_id=None):
        with self.conn() as conn:
            cur = conn.cursor()
            if provider_order_id:
                cur.execute("UPDATE payments SET status='canceled' WHERE order_id=? AND provider_order_id=?", (order_id, provider_order_id))
            else:
                cur.execute("UPDATE payments SET status='canceled' WHERE order_id=? AND status='pending'", (order_id,))
            conn.commit()
        self.release_payment_hold(order_id, "Платёж отменён платёжной системой")

    def get_order(self, order_id):
        with self.conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id=?",
                (order_id,)
            ).fetchone()

            return dict(row) if row else None

    def reserve_stock(self, cur, order):
        if order["stock_reserved"]:
            return

        items = json.loads(order["items"])

        # Проверка остатков
        for item in items:
            stock_items = item.get("components") if item.get("is_combo") else [item]

            for stock_item in stock_items:
                row = cur.execute("""
                    SELECT menu.name, menu.available, COALESCE(menu_stock.quantity,0) AS quantity
                    FROM menu
                    LEFT JOIN menu_stock
                        ON menu_stock.menu_id=menu.id
                        AND menu_stock.branch_id=?
                    WHERE menu.id=?
                """, (order["branch_id"], stock_item["id"])).fetchone()

                if not row or row["available"] != 1:
                    raise Exception(f"Позиция «{stock_item['name']}» убрана из меню")

                if row["quantity"] < stock_item["quantity"]:
                    raise Exception(
                        f"Недостаточно «{stock_item['name']}» в этом буфете. Доступно: {row['quantity']}"
                    )

        # Списание остатков
        for item in items:
            stock_items = item.get("components") if item.get("is_combo") else [item]

            for stock_item in stock_items:
                cur.execute("""
                    UPDATE menu_stock
                    SET quantity=quantity-?
                    WHERE menu_id=? AND branch_id=? AND quantity>=?
                """, (
                    stock_item["quantity"],
                    stock_item["id"],
                    order["branch_id"],
                    stock_item["quantity"]
                ))
                if cur.rowcount != 1:
                    raise Exception(
                        f"Позиция «{stock_item.get('name','товар')}» только что закончилась. Обновите меню и попробуйте снова."
                    )

        cur.execute(
            "UPDATE orders SET stock_reserved=1 WHERE id=?",
            (order["id"],)
        )


    def _generate_unique_pickup_code(self, cur, branch_id):
        # 4 digits are convenient for manual issue, but must be unique among active ready orders.
        for _ in range(200):
            code = str(random.randint(1000, 9999))
            exists = cur.execute("""
                SELECT 1 FROM orders
                WHERE branch_id=? AND status='ready' AND pickup_code=?
                LIMIT 1
            """, (branch_id, code)).fetchone()
            if not exists:
                return code
        raise Exception("Не удалось сгенерировать уникальный код выдачи")

    def next_order_status(self, order_id, branch_id, kitchen_id=None):
        now = datetime.now(MSK_TZ)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        with self.conn() as conn:
            cur = conn.cursor()

            order = cur.execute("""
                SELECT *
                FROM orders
                WHERE id=? AND branch_id=?
            """, (order_id, branch_id)).fetchone()

            if not order:
                raise Exception("Заказ не найден в вашем буфете")

            if order["status"] == "new":
                if order["payment_status"] == "pending":
                    raise Exception("Ожидается онлайн-оплата. Принять заказ можно после оплаты или окончания платёжной брони.")
                self._assert_order_transition(order["status"], "cooking", order["payment_status"])

                self.reserve_stock(cur, order)

                cur.execute("""
                    UPDATE orders
                    SET status='cooking', accepted_at=?
                    WHERE id=?
                """, (now_str, order_id))

                self.log_action(cur, "kitchen", kitchen_id, "Повар принял заказ", order_id)

            elif order["status"] == "cooking":
                self._assert_order_transition(order["status"], "ready", order["payment_status"])
                if order["pickup_time"]:
                    pickup_dt = datetime.strptime(order["pickup_time"], "%Y-%m-%d %H:%M:%S")
                    expires_at = pickup_dt + timedelta(minutes=30)
                else:
                    expires_at = now + timedelta(minutes=30)

                cur.execute("""
                    UPDATE orders
                    SET
                        status='ready',
                        pickup_code=?,
                        ready_at=?,
                        expires_at=?
                    WHERE id=?
                """, (
                    self._generate_unique_pickup_code(cur, branch_id),
                    now_str,
                    expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                    order_id
                ))

                self.log_action(cur, "kitchen", kitchen_id, "Заказ готов к выдаче", order_id)

            elif order["status"] == "ready":
                self._assert_order_transition(order["status"], "issued", order["payment_status"])
                cur.execute("""
                    UPDATE orders
                    SET status='issued', issued_at=?
                    WHERE id=?
                """, (now_str, order_id))

                self.log_action(cur, "kitchen", kitchen_id, "Заказ выдан", order_id)

            else:
                raise Exception("Заказ уже закрыт")

            conn.commit()

    def expire_old_orders(self):
        now = datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        changed_branch_ids = set()

        with self.conn() as conn:
            cur = conn.cursor()

            rows = cur.execute("""
                SELECT *
                FROM orders
                WHERE
                    status='ready'
                    AND expires_at IS NOT NULL
                    AND expires_at < ?
            """, (now,)).fetchall()

            for order in rows:
                self._assert_order_transition(order["status"], "expired", order["payment_status"])
                items = json.loads(order["items"])

                if order["stock_reserved"]:
                    for item in items:
                        stock_items = item.get("components") if item.get("is_combo") else [item]

                        for stock_item in stock_items:
                            cur.execute("""
                                UPDATE menu_stock
                                SET quantity=quantity+?
                                WHERE menu_id=? AND branch_id=?
                            """, (
                                stock_item["quantity"],
                                stock_item["id"],
                                order["branch_id"]
                            ))

                cur.execute("""
                    UPDATE orders
                    SET
                        status='expired',
                        expired_at=?,
                        stock_reserved=0
                    WHERE id=?
                """, (now, order["id"]))

                self.log_action(cur, "system", None, "Заказ просрочен, позиции возвращены на витрину", order["id"])

                changed_branch_ids.add(order["branch_id"])

            conn.commit()

        return list(changed_branch_ids)

    def get_active_orders(self, branch_id):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT
                    orders.*,
                    users.name,
                    users.phone,
                    branches.name AS branch_name
                FROM orders
                JOIN users ON users.id=orders.user_id
                JOIN branches ON branches.id=orders.branch_id
                WHERE
                    orders.branch_id=?
                    AND orders.status NOT IN ('issued', 'expired', 'canceled')
                ORDER BY orders.id ASC
            """, (branch_id,)).fetchall()

            result = []

            for r in rows:
                order = dict(r)
                order["items"] = json.loads(order["items"])
                result.append(order)

            return result

    def get_payment_settings(self):
        defaults = {
            "enabled": "0",
            "provider": "demo",
            "api_url": "https://securepayments.sberbank.ru/payment/rest",
            "test_api_url": "https://3dsec.sberbank.ru/payment/rest",
            "username": "",
            "password": "",
            "return_url": "",
            "fail_url": "",
            "callback_url": "",
            "demo_payment_url": "",
            "yookassa_api_url": "https://api.yookassa.ru/v3",
            "yookassa_shop_id": "",
            "yookassa_secret_key": "",
            "yookassa_payment_method": "auto",
            "yookassa_test_mode": "0",
            "payment_hold_minutes": "10",
            "fiscal_enabled": "0",
            "onec_enabled": "0",
            "onec_url": "",
            "onec_token": ""
        }

        data = dict(defaults)

        # Private local bootstrap config: useful for a test shop without hardcoding
        # credentials into Python source. Values saved in the admin panel (DB) win.
        try:
            local_config_path = Path(self.path).resolve().parent / "local_payment_config.json"
            if local_config_path.exists():
                local_data = json.loads(local_config_path.read_text(encoding="utf-8"))
                for key in defaults:
                    if key in local_data and local_data[key] is not None:
                        data[key] = str(local_data[key])
        except Exception:
            pass

        with self.conn() as conn:
            rows = conn.execute("SELECT key,value FROM payment_settings").fetchall()

            for row in rows:
                data[row["key"]] = row["value"]

            return data

    def save_payment_settings(self, data):
        allowed = [
            "enabled", "provider", "api_url", "test_api_url", "username", "password",
            "return_url", "fail_url", "callback_url", "demo_payment_url",
            "yookassa_api_url", "yookassa_shop_id", "yookassa_secret_key", "yookassa_payment_method", "yookassa_test_mode", "payment_hold_minutes",
            "fiscal_enabled", "onec_enabled", "onec_url", "onec_token"
        ]

        with self.conn() as conn:
            cur = conn.cursor()

            for key in allowed:
                if key in data:
                    cur.execute("""
                        INSERT INTO payment_settings(key,value)
                        VALUES(?,?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """, (key, str(data.get(key) or "")))

            self.log_action(cur, "admin", None, "Изменены настройки оплаты", None, "payment_settings")
            conn.commit()

    def create_payment_record(self, order_id, provider, provider_order_id, amount, payment_url, raw_response):
        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT status,payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")
            self._assert_payment_transition(order["payment_status"], "pending", order["status"])

            cur.execute("""
                INSERT INTO payments(order_id,provider,provider_order_id,amount,status,payment_url,raw_response)
                VALUES(?,?,?,?,?,?,?)
            """, (
                order_id,
                provider,
                provider_order_id,
                int(amount),
                "pending",
                payment_url,
                json.dumps(raw_response or {}, ensure_ascii=False)
            ))

            payment_id = cur.lastrowid

            cur.execute("""
                UPDATE orders
                SET payment_status='pending', payment_method=?
                WHERE id=?
            """, (provider, order_id))

            self.log_action(cur, "user", None, "Создан платёж", order_id, f"provider={provider}, payment_id={payment_id}")
            conn.commit()

            return payment_id

    def get_order_by_provider_order_id(self, provider_order_id):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT orders.*
                FROM payments
                JOIN orders ON orders.id=payments.order_id
                WHERE payments.provider_order_id=?
                ORDER BY payments.id DESC
                LIMIT 1
            """, (provider_order_id,)).fetchone()

            return dict(row) if row else None

    def finalize_paid_order(self, order_id, provider_order_id=None):
        """Атомарно подтверждает оплату и гарантирует резерв товара.
        Если 10-минутная платёжная бронь уже истекла, пытается зарезервировать товар заново.
        Исключение означает: деньги могли прийти, но товара уже нет — нужен возврат.
        """
        now = datetime.now(MSK_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")
            if order["payment_status"] == "paid":
                return True
            self._assert_payment_transition(order["payment_status"], "paid", order["status"])

            if not order["stock_reserved"]:
                self.reserve_stock(cur, order)

            cur.execute("""
                UPDATE orders
                SET payment_status='paid', paid_at=?, payment_hold_expires_at=NULL
                WHERE id=?
            """, (now, order_id))
            if provider_order_id:
                cur.execute("""
                    UPDATE payments SET status='paid', paid_at=?
                    WHERE order_id=? AND provider_order_id=?
                """, (now, order_id, provider_order_id))
            else:
                cur.execute("UPDATE payments SET status='paid', paid_at=? WHERE order_id=?", (now, order_id))
            self.log_action(cur, "system", None, "Заказ оплачен и товар подтверждён", order_id, f"provider_order_id={provider_order_id or ''}")
            conn.commit()
        return True

    def mark_refund_required(self, order_id, provider_order_id=None, reason=""):
        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT status,payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")
            self._assert_payment_transition(order["payment_status"], "refund_required", order["status"])
            cur.execute("UPDATE orders SET payment_status='refund_required', payment_hold_expires_at=NULL WHERE id=?", (order_id,))
            if provider_order_id:
                cur.execute("UPDATE payments SET status='refund_required' WHERE order_id=? AND provider_order_id=?", (order_id, provider_order_id))
            self.log_action(cur, "system", None, "Требуется возврат платежа", order_id, reason)
            conn.commit()

    def mark_order_refunded(self, order_id, provider_order_id, refund_id, refund_status, raw_response, cancel_order=False):
        now = datetime.now(MSK_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")

            self._assert_payment_transition(order["payment_status"], "refunded", order["status"])

            # Автоматическое восстановление остатков безопасно только пока оператор не принял заказ.
            if cancel_order and order["status"] == "new" and order["stock_reserved"]:
                self._restore_order_stock(cur, order)

            cur.execute("""
                UPDATE orders
                SET payment_status='refunded', payment_hold_expires_at=NULL,
                    status=CASE WHEN ?=1 AND status='new' THEN 'canceled' ELSE status END,
                    canceled_at=CASE WHEN ?=1 AND status='new' THEN ? ELSE canceled_at END
                WHERE id=?
            """, (1 if cancel_order else 0, 1 if cancel_order else 0, now, order_id))
            cur.execute("""
                UPDATE payments
                SET status='refunded', refund_id=?, refund_status=?, refunded_at=?, refund_raw_response=?
                WHERE order_id=? AND provider_order_id=?
            """, (refund_id, refund_status, now, json.dumps(raw_response or {}, ensure_ascii=False), order_id, provider_order_id))
            self.log_action(cur, "admin" if not cancel_order else "system", None, "Платёж возвращён", order_id, f"refund_id={refund_id}, status={refund_status}")
            conn.commit()

    def get_refundable_payment(self, order_id):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT payments.*, orders.status AS order_status, orders.payment_status AS order_payment_status, orders.total
                FROM payments JOIN orders ON orders.id=payments.order_id
                WHERE payments.order_id=? AND payments.status='paid'
                ORDER BY payments.id DESC LIMIT 1
            """, (order_id,)).fetchone()
            return dict(row) if row else None

    def mark_order_paid(self, order_id, provider_order_id=None):
        now = datetime.now(MSK_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

        with self.conn() as conn:
            cur = conn.cursor()
            order = cur.execute("SELECT status,payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise Exception("Заказ не найден")
            self._assert_payment_transition(order["payment_status"], "paid", order["status"])

            cur.execute("""
                UPDATE orders
                SET payment_status='paid', paid_at=?, payment_hold_expires_at=NULL
                WHERE id=?
            """, (now, order_id))

            if provider_order_id:
                cur.execute("""
                    UPDATE payments
                    SET status='paid', paid_at=?
                    WHERE order_id=? AND provider_order_id=?
                """, (now, order_id, provider_order_id))
            else:
                cur.execute("""
                    UPDATE payments
                    SET status='paid', paid_at=?
                    WHERE order_id=?
                """, (now, order_id))

            self.log_action(cur, "system", None, "Заказ оплачен", order_id, f"provider_order_id={provider_order_id or ''}")
            conn.commit()

    def get_payment_log(self):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT
                    payments.*,
                    orders.total,
                    orders.payment_status,
                    orders.status AS order_status,
                    users.phone,
                    branches.name AS branch_name
                FROM payments
                JOIN orders ON orders.id=payments.order_id
                JOIN users ON users.id=orders.user_id
                JOIN branches ON branches.id=orders.branch_id
                ORDER BY payments.id DESC
                LIMIT 200
            """).fetchall()

            return [dict(r) for r in rows]

    def get_user_orders(self, user_id):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT orders.*, branches.name AS branch_name
                FROM orders
                JOIN branches ON branches.id=orders.branch_id
                WHERE user_id=?
                ORDER BY id DESC
            """, (user_id,)).fetchall()

            result = []

            for r in rows:
                order = dict(r)
                order["items"] = json.loads(order["items"])

                rating_row = conn.execute("""
                    SELECT COUNT(*) AS cnt, MAX(stars) AS stars, MAX(comment) AS comment
                    FROM ratings
                    WHERE order_id=? AND user_id=?
                """, (order["id"], user_id)).fetchone()

                order["rated"] = (rating_row["cnt"] or 0) > 0
                order["rating_stars"] = rating_row["stars"]
                order["rating_comment"] = rating_row["comment"]

                result.append(order)

            return result

    def get_all_users(self, search=""):
        with self.conn() as conn:
            params = []
            where = ""

            if search:
                where = "WHERE users.name LIKE ? OR users.phone LIKE ?"
                params = [f"%{search}%", f"%{search}%"]

            rows = conn.execute(f"""
                SELECT
                    users.*,
                    COUNT(orders.id) AS orders_count,
                    COALESCE(SUM(orders.total),0) AS total_spent
                FROM users
                LEFT JOIN orders ON orders.user_id=users.id
                {where}
                GROUP BY users.id
                ORDER BY users.id DESC
            """, params).fetchall()

            return [dict(r) for r in rows]

    def get_all_orders(self, status="", branch_id="", search=""):
        with self.conn() as conn:
            where_parts = []
            params = []

            if status:
                where_parts.append("orders.status=?")
                params.append(status)

            if branch_id:
                where_parts.append("orders.branch_id=?")
                params.append(int(branch_id))

            if search:
                where_parts.append("(users.name LIKE ? OR users.phone LIKE ? OR orders.id LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

            where = ""
            if where_parts:
                where = "WHERE " + " AND ".join(where_parts)

            rows = conn.execute(f"""
                SELECT
                    orders.*,
                    users.name,
                    users.phone,
                    branches.name AS branch_name
                FROM orders
                JOIN users ON users.id=orders.user_id
                JOIN branches ON branches.id=orders.branch_id
                {where}
                ORDER BY orders.id DESC
            """, params).fetchall()

            result = []

            for r in rows:
                order = dict(r)
                order["items"] = json.loads(order["items"])

                rating_row = conn.execute("""
                    SELECT COUNT(*) AS cnt, MAX(stars) AS stars, MAX(comment) AS comment
                    FROM ratings
                    WHERE order_id=?
                """, (order["id"],)).fetchone()

                order["rated"] = (rating_row["cnt"] or 0) > 0
                order["rating_stars"] = rating_row["stars"]
                order["rating_comment"] = rating_row["comment"]

                result.append(order)

            return result



    def daily_writeoff(self):
        with self.conn() as conn:
            cur = conn.cursor()

            rows = cur.execute("""
                SELECT id, name
                FROM menu
                WHERE writeoff_enabled=1
            """).fetchall()

            for row in rows:
                stock_rows = cur.execute("""
                    SELECT branch_id, quantity
                    FROM menu_stock
                    WHERE menu_id=? AND quantity>0
                """, (row["id"],)).fetchall()

                for stock in stock_rows:
                    cur.execute("""
                        INSERT INTO writeoff_log(menu_id, branch_id, quantity)
                        VALUES(?,?,?)
                    """, (row["id"], stock["branch_id"], stock["quantity"]))

                cur.execute("""
                    UPDATE menu_stock
                    SET quantity=0
                    WHERE menu_id=?
                """, (row["id"],))

                cur.execute("""
                    UPDATE menu
                    SET available=0
                    WHERE id=?
                """, (row["id"],))

                self.log_action(
                    cur,
                    "system",
                    None,
                    "Автосписание и закрытие позиции",
                    None,
                    f'menu_id={row["id"]}, name={row["name"]}'
                )

            conn.commit()

    def auto_close_empty_positions(self):
        with self.conn() as conn:
            cur = conn.cursor()

            rows = cur.execute("""
                SELECT menu.id, menu.name, COALESCE(SUM(menu_stock.quantity),0) AS total_qty
                FROM menu
                LEFT JOIN menu_stock ON menu_stock.menu_id=menu.id
                WHERE menu.available=1
                GROUP BY menu.id
                HAVING COALESCE(SUM(menu_stock.quantity),0) <= 0
            """).fetchall()

            for row in rows:
                cur.execute("UPDATE menu SET available=0 WHERE id=?", (row["id"],))
                self.log_action(cur, "system", None, "Позиция автоматически закрыта — остаток 0", None, f'menu_id={row["id"]}, name={row["name"]}')

            conn.commit()

            return len(rows)

    def branch_is_open(self, branch_id, when=None):
        when = when or datetime.now(MSK_TZ)
        weekday = when.weekday()
        now_time = when.strftime("%H:%M")

        with self.conn() as conn:
            branch = conn.execute("""
                SELECT closed
                FROM branches
                WHERE id=?
            """, (branch_id,)).fetchone()

            if branch and branch["closed"] == 1:
                return False

            row = conn.execute("""
                SELECT *
                FROM branch_schedule
                WHERE branch_id=? AND weekday=?
                ORDER BY id DESC
                LIMIT 1
            """, (branch_id, weekday)).fetchone()

            if not row:
                return True

            if row["enabled"] != 1:
                return False

            return row["open_time"] <= now_time <= row["close_time"]

    def get_branch_schedule(self):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT
                    branches.id AS branch_id,
                    branches.name AS branch_name,
                    days.weekday AS weekday,
                    COALESCE(bs.open_time, '08:00') AS open_time,
                    COALESCE(bs.close_time, '20:00') AS close_time,
                    CAST(COALESCE(bs.enabled, 1) AS INTEGER) AS enabled
                FROM branches
                CROSS JOIN (
                    SELECT 0 AS weekday UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6
                ) AS days
                LEFT JOIN branch_schedule AS bs
                    ON bs.id = (
                        SELECT id
                        FROM branch_schedule
                        WHERE branch_id=branches.id AND weekday=days.weekday
                        ORDER BY id DESC
                        LIMIT 1
                    )
                ORDER BY branches.id, days.weekday
            """).fetchall()

            return [dict(r) for r in rows]

    def update_branch_schedule(self, branch_id, weekday, open_time, close_time, enabled):
        with self.conn() as conn:
            cur = conn.cursor()

            # В старых версиях таблица могла создаться без UNIQUE(branch_id, weekday),
            # поэтому не используем ON CONFLICT: чистим дубли и вставляем одну актуальную строку.
            cur.execute("""
                DELETE FROM branch_schedule
                WHERE branch_id=? AND weekday=?
            """, (branch_id, weekday))

            cur.execute("""
                INSERT INTO branch_schedule(branch_id, weekday, open_time, close_time, enabled)
                VALUES(?,?,?,?,?)
            """, (branch_id, weekday, open_time, close_time, 1 if enabled else 0))

            self.log_action(cur, "admin", None, "Изменено расписание буфета", None, f"branch_id={branch_id}, weekday={weekday}, enabled={enabled}")
            conn.commit()

    def update_branch_schedule_bulk(self, schedules):
        with self.conn() as conn:
            cur = conn.cursor()

            for item in schedules:
                branch_id = int(item["branch_id"])
                weekday = int(item["weekday"])
                open_time = item.get("open_time", "08:00")
                close_time = item.get("close_time", "20:00")
                enabled = 1 if item.get("enabled") in [True, 1, "1", "true", "True"] else 0

                cur.execute("""
                    DELETE FROM branch_schedule
                    WHERE branch_id=? AND weekday=?
                """, (branch_id, weekday))

                cur.execute("""
                    INSERT INTO branch_schedule(branch_id, weekday, open_time, close_time, enabled)
                    VALUES(?,?,?,?,?)
                """, (branch_id, weekday, open_time, close_time, enabled))

            self.log_action(cur, "admin", None, "Массовое изменение расписания буфета", None, f"count={len(schedules)}")
            conn.commit()

    def add_combo(self, name, price, icon, description, image, items):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO combos(name, price, icon, image, description, available)
                VALUES(?,?,?,?,?,1)
            """, (name, price, icon, image, description))

            combo_id = cur.lastrowid

            for item in items:
                cur.execute("""
                    INSERT INTO combo_items(combo_id, menu_id, quantity)
                    VALUES(?,?,?)
                """, (combo_id, int(item["menu_id"]), int(item["quantity"])))

            self.log_action(cur, "admin", None, "Добавлено комбо", None, name)
            conn.commit()
            return combo_id

    def get_combos(self):
        with self.conn() as conn:
            combos = [dict(r) for r in conn.execute("""
                SELECT *
                FROM combos
                WHERE available=1
                ORDER BY id DESC
            """).fetchall()]

            for combo in combos:
                rows = conn.execute("""
                    SELECT menu.id, menu.name, menu.icon, menu.image, menu.description, menu.price, combo_items.quantity
                    FROM combo_items
                    JOIN menu ON menu.id=combo_items.menu_id
                    WHERE combo_items.combo_id=?
                """, (combo["id"],)).fetchall()

                combo["items"] = [dict(r) for r in rows]

            return combos

    def get_all_combos(self):
        with self.conn() as conn:
            combos = [dict(r) for r in conn.execute("""
                SELECT *
                FROM combos
                ORDER BY id DESC
            """).fetchall()]

            for combo in combos:
                rows = conn.execute("""
                    SELECT menu.id, menu.name, menu.icon, menu.image, menu.description, menu.price, combo_items.quantity
                    FROM combo_items
                    JOIN menu ON menu.id=combo_items.menu_id
                    WHERE combo_items.combo_id=?
                """, (combo["id"],)).fetchall()

                combo["items"] = [dict(r) for r in rows]

            return combos

    def delete_combo(self, combo_id):
        with self.conn() as conn:
            cur = conn.cursor()

            cur.execute("DELETE FROM combo_items WHERE combo_id=?", (combo_id,))
            cur.execute("DELETE FROM combos WHERE id=?", (combo_id,))

            self.log_action(cur, "admin", None, "Комбо удалено навсегда", None, f"combo_id={combo_id}")
            conn.commit()

    def set_combo_available(self, combo_id, available):
        with self.conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE combos SET available=? WHERE id=?", (available, combo_id))
            self.log_action(cur, "admin", None, "Изменена видимость комбо", None, f"combo_id={combo_id}, available={available}")
            conn.commit()

    def get_operator_menu(self, branch_id):
        # Оператор видит только свой буфет и не может менять цену/описание.
        return self.get_menu_for_branch(int(branch_id))

    def operator_mark_sold_out(self, menu_id, branch_id, kitchen_id=None):
        with self.conn() as conn:
            cur = conn.cursor()
            row = cur.execute("""
                SELECT menu.name, COALESCE(menu_stock.quantity,0) AS quantity
                FROM menu
                LEFT JOIN menu_stock ON menu_stock.menu_id=menu.id AND menu_stock.branch_id=?
                WHERE menu.id=?
            """, (int(branch_id), int(menu_id))).fetchone()
            if not row:
                raise Exception("Позиция меню не найдена")
            cur.execute("""
                INSERT INTO menu_stock(menu_id,branch_id,quantity)
                VALUES(?,?,0)
                ON CONFLICT(menu_id,branch_id) DO UPDATE SET quantity=0
            """, (int(menu_id), int(branch_id)))
            self.log_action(cur, "kitchen", kitchen_id, "Оператор поставил позицию в стоп-лист", None,
                            f"menu_id={menu_id}, branch_id={branch_id}, previous_quantity={row['quantity']}")
            conn.commit()
            return {"name": row["name"], "previous_quantity": int(row["quantity"] or 0)}

    def find_order_by_pickup_code_any(self, branch_id, code):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT orders.*, users.name, users.phone, branches.name AS branch_name
                FROM orders
                JOIN users ON users.id=orders.user_id
                JOIN branches ON branches.id=orders.branch_id
                WHERE orders.branch_id=? AND orders.pickup_code=?
                ORDER BY orders.id DESC
                LIMIT 1
            """, (int(branch_id), str(code))).fetchone()
            if not row:
                return None
            order = dict(row)
            order["items"] = json.loads(order["items"])
            return order

    def find_ready_order_by_code(self, branch_id, code):
        with self.conn() as conn:
            row = conn.execute("""
                SELECT orders.*, users.name, users.phone, branches.name AS branch_name
                FROM orders
                JOIN users ON users.id=orders.user_id
                JOIN branches ON branches.id=orders.branch_id
                WHERE orders.branch_id=? AND orders.status='ready' AND orders.pickup_code=?
            """, (branch_id, str(code))).fetchone()

            if not row:
                return None

            order = dict(row)
            order["items"] = json.loads(order["items"])
            return order

    def issue_order_by_code(self, branch_id, code, kitchen_id=None):
        now = datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")

        with self.conn() as conn:
            cur = conn.cursor()

            order = cur.execute("""
                SELECT *
                FROM orders
                WHERE branch_id=? AND status='ready' AND pickup_code=?
            """, (branch_id, str(code))).fetchone()

            if not order:
                raise Exception("Готовый заказ с таким кодом не найден")

            self._assert_order_transition(order["status"], "issued", order["payment_status"])

            cur.execute("""
                UPDATE orders
                SET status='issued', issued_at=?
                WHERE id=?
            """, (now, order["id"]))

            self.log_action(cur, "kitchen", kitchen_id, "Заказ выдан по QR/коду", order["id"])
            conn.commit()
            return order["id"]

    def rate_order_items(self, order_id, user_id, stars, comment=""):
        if stars < 1 or stars > 5:
            raise Exception("Оценка должна быть от 1 до 5")

        comment = (comment or "").strip()
        if len(comment) > 500:
            raise Exception("Комментарий слишком длинный — максимум 500 символов")

        with self.conn() as conn:
            cur = conn.cursor()

            order = cur.execute("""
                SELECT *
                FROM orders
                WHERE id=? AND user_id=? AND status='issued'
            """, (order_id, user_id)).fetchone()

            if not order:
                raise Exception("Оценивать можно только выданный заказ")

            existing = cur.execute("""
                SELECT COUNT(*) AS cnt
                FROM ratings
                WHERE order_id=? AND user_id=?
            """, (order_id, user_id)).fetchone()

            if existing["cnt"] and existing["cnt"] > 0:
                raise Exception("Этот заказ уже оценён")

            items = json.loads(order["items"])
            rating_menu_ids = set()

            for item in items:
                if item.get("is_combo"):
                    for component in item.get("components") or []:
                        if component.get("id"):
                            rating_menu_ids.add(int(component["id"]))
                elif item.get("id"):
                    rating_menu_ids.add(int(item["id"]))

            if not rating_menu_ids:
                raise Exception("Не удалось определить позиции заказа для оценки")

            for menu_id in sorted(rating_menu_ids):
                cur.execute("""
                    INSERT INTO ratings(order_id,user_id,menu_id,stars,comment)
                    VALUES(?,?,?,?,?)
                """, (order_id, user_id, menu_id, stars, comment))

            self.log_action(cur, "user", user_id, "Пользователь оценил заказ", order_id, f"stars={stars}")
            conn.commit()

    def get_rating_stats(self):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT menu.name, menu.icon, COUNT(ratings.id) AS rating_count, ROUND(AVG(ratings.stars),2) AS avg_stars
                FROM menu
                LEFT JOIN ratings ON ratings.menu_id=menu.id
                GROUP BY menu.id
                HAVING rating_count>0
                ORDER BY avg_stars DESC, rating_count DESC
            """).fetchall()

            return [dict(r) for r in rows]

    def get_writeoff_stats(self):
        with self.conn() as conn:
            rows = conn.execute("""
                SELECT menu.name, menu.icon, branches.name AS branch_name, SUM(writeoff_log.quantity) AS quantity
                FROM writeoff_log
                JOIN menu ON menu.id=writeoff_log.menu_id
                JOIN branches ON branches.id=writeoff_log.branch_id
                GROUP BY writeoff_log.menu_id, writeoff_log.branch_id
                ORDER BY quantity DESC
            """).fetchall()

            return [dict(r) for r in rows]

    def get_analytics(self):
        with self.conn() as conn:
            cur = conn.cursor()

            summary = dict(cur.execute("""
                SELECT
                    SUM(CASE WHEN status='issued' THEN 1 ELSE 0 END) AS orders_total,
                    COUNT(*) AS orders_all,
                    SUM(CASE WHEN status='canceled' THEN 1 ELSE 0 END) AS canceled_total,
                    SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_total,
                    COALESCE(SUM(CASE WHEN status='issued' THEN total ELSE 0 END),0) AS revenue_total,
                    COALESCE(AVG(CASE WHEN status='issued' THEN total END),0) AS avg_check,
                    SUM(CASE WHEN payment_status='paid' THEN 1 ELSE 0 END) AS paid_orders,
                    COALESCE(SUM(CASE WHEN payment_status='paid' THEN total ELSE 0 END),0) AS online_revenue
                FROM orders
            """).fetchone())

            all_count = int(summary.get("orders_all") or 0)
            issued_count = int(summary.get("orders_total") or 0)
            summary["completion_rate"] = round((issued_count / all_count * 100), 1) if all_count else 0

            by_branch = [
                dict(r) for r in cur.execute("""
                    SELECT
                        branches.name,
                        COUNT(orders.id) AS orders_count,
                        COALESCE(SUM(orders.total),0) AS revenue
                    FROM branches
                    LEFT JOIN orders
                        ON orders.branch_id=branches.id
                        AND orders.status='issued'
                    GROUP BY branches.id
                    ORDER BY revenue DESC
                """).fetchall()
            ]

            by_status = [dict(r) for r in cur.execute("""
                SELECT status, COUNT(*) AS count
                FROM orders
                GROUP BY status
                ORDER BY count DESC
            """).fetchall()]

            payment_statuses = [dict(r) for r in cur.execute("""
                SELECT COALESCE(payment_status,'unpaid') AS status, COUNT(*) AS count
                FROM orders
                GROUP BY COALESCE(payment_status,'unpaid')
                ORDER BY count DESC
            """).fetchall()]

            rows = cur.execute("SELECT items FROM orders WHERE status='issued'").fetchall()
            popular = {}
            for row in rows:
                for item in json.loads(row["items"]):
                    name = item.get("name") or "Без названия"
                    if name not in popular:
                        popular[name] = {"name": name, "quantity": 0, "revenue": 0}
                    qty = int(item.get("quantity") or 0)
                    price = int(item.get("price") or 0)
                    popular[name]["quantity"] += qty
                    popular[name]["revenue"] += price * qty

            popular_items = sorted(popular.values(), key=lambda item: item["quantity"], reverse=True)[:20]

            rating_summary = dict(cur.execute("""
                SELECT
                    COUNT(*) AS feedback_count,
                    COALESCE(ROUND(AVG(stars),2),0) AS avg_rating,
                    SUM(CASE WHEN LENGTH(TRIM(COALESCE(comment,'')))>0 THEN 1 ELSE 0 END) AS comments_count
                FROM (
                    SELECT order_id, MAX(stars) AS stars, MAX(comment) AS comment
                    FROM ratings
                    GROUP BY order_id
                )
            """).fetchone())

            rating_distribution = [dict(r) for r in cur.execute("""
                SELECT stars, COUNT(*) AS count
                FROM (SELECT order_id, MAX(stars) AS stars FROM ratings GROUP BY order_id)
                GROUP BY stars
                ORDER BY stars DESC
            """).fetchall()]

            recent_feedback = [dict(r) for r in cur.execute("""
                SELECT
                    ratings.order_id,
                    MAX(ratings.stars) AS stars,
                    MAX(ratings.comment) AS comment,
                    MIN(ratings.created_at) AS created_at,
                    branches.name AS branch_name,
                    GROUP_CONCAT(DISTINCT menu.name) AS items
                FROM ratings
                JOIN orders ON orders.id=ratings.order_id
                JOIN branches ON branches.id=orders.branch_id
                JOIN menu ON menu.id=ratings.menu_id
                GROUP BY ratings.order_id
                HAVING LENGTH(TRIM(COALESCE(MAX(ratings.comment),'')))>0
                ORDER BY MIN(ratings.created_at) DESC
                LIMIT 20
            """).fetchall()]

            daily = [dict(r) for r in cur.execute("""
                SELECT date(issued_at) AS day, COUNT(*) AS orders_count, COALESCE(SUM(total),0) AS revenue
                FROM orders
                WHERE status='issued' AND issued_at IS NOT NULL
                GROUP BY date(issued_at)
                ORDER BY day DESC
                LIMIT 7
            """).fetchall()]
            daily.reverse()

            now_local = datetime.now(MSK_TZ).replace(tzinfo=None)
            today = now_local.strftime("%Y-%m-%d")
            yesterday = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
            last7_start = (now_local - timedelta(days=6)).strftime("%Y-%m-%d")
            prev7_start = (now_local - timedelta(days=13)).strftime("%Y-%m-%d")
            prev7_end = (now_local - timedelta(days=7)).strftime("%Y-%m-%d")

            def period_metrics(start_day, end_day):
                row = cur.execute("""
                    SELECT
                        COUNT(*) AS orders_count,
                        COALESCE(SUM(total),0) AS revenue,
                        COALESCE(AVG(total),0) AS avg_check
                    FROM orders
                    WHERE status='issued'
                      AND date(issued_at) BETWEEN ? AND ?
                """, (start_day, end_day)).fetchone()
                return dict(row)

            def pct_change(current, previous):
                current = float(current or 0)
                previous = float(previous or 0)
                if previous == 0:
                    return None if current else 0
                return round((current - previous) / previous * 100, 1)

            today_metrics = period_metrics(today, today)
            yesterday_metrics = period_metrics(yesterday, yesterday)
            last7_metrics = period_metrics(last7_start, today)
            prev7_metrics = period_metrics(prev7_start, prev7_end)

            comparison = {
                "today": today_metrics,
                "yesterday": yesterday_metrics,
                "last7": last7_metrics,
                "previous7": prev7_metrics,
                "today_revenue_change_pct": pct_change(today_metrics["revenue"], yesterday_metrics["revenue"]),
                "today_orders_change_pct": pct_change(today_metrics["orders_count"], yesterday_metrics["orders_count"]),
                "week_revenue_change_pct": pct_change(last7_metrics["revenue"], prev7_metrics["revenue"]),
                "week_orders_change_pct": pct_change(last7_metrics["orders_count"], prev7_metrics["orders_count"]),
            }

            service = dict(cur.execute("""
                SELECT
                    ROUND(COALESCE(AVG(CASE WHEN accepted_at IS NOT NULL
                        THEN (julianday(accepted_at)-julianday(created_at))*1440 END),0),1) AS avg_accept_minutes,
                    ROUND(COALESCE(AVG(CASE WHEN accepted_at IS NOT NULL AND ready_at IS NOT NULL
                        THEN (julianday(ready_at)-julianday(accepted_at))*1440 END),0),1) AS avg_prepare_minutes,
                    ROUND(COALESCE(AVG(CASE WHEN ready_at IS NOT NULL AND issued_at IS NOT NULL
                        THEN (julianday(issued_at)-julianday(ready_at))*1440 END),0),1) AS avg_pickup_minutes
                FROM orders
                WHERE created_at >= datetime('now','-30 days')
            """).fetchone())

            hourly = [dict(r) for r in cur.execute("""
                SELECT
                    CAST(strftime('%H', datetime(created_at,'+3 hours')) AS INTEGER) AS hour,
                    COUNT(*) AS orders_count
                FROM orders
                WHERE created_at >= datetime('now','-30 days')
                GROUP BY CAST(strftime('%H', datetime(created_at,'+3 hours')) AS INTEGER)
                ORDER BY hour
            """).fetchall()]

            stock_risks = [dict(r) for r in cur.execute("""
                SELECT
                    menu.id AS menu_id,
                    menu.name,
                    branches.id AS branch_id,
                    branches.name AS branch_name,
                    COALESCE(menu_stock.quantity,0) AS quantity
                FROM menu
                CROSS JOIN branches
                LEFT JOIN menu_stock ON menu_stock.menu_id=menu.id AND menu_stock.branch_id=branches.id
                WHERE menu.available=1 AND COALESCE(menu_stock.quantity,0) <= 3
                ORDER BY quantity ASC, branches.id, menu.name
                LIMIT 40
            """).fetchall()]

            low_rated = [dict(r) for r in cur.execute("""
                SELECT menu.name, COUNT(ratings.id) AS rating_count, ROUND(AVG(ratings.stars),2) AS avg_stars
                FROM ratings JOIN menu ON menu.id=ratings.menu_id
                GROUP BY menu.id
                HAVING COUNT(ratings.id) >= 2
                ORDER BY avg_stars ASC, rating_count DESC
                LIMIT 10
            """).fetchall()]

            summary["cancel_rate"] = round((int(summary.get("canceled_total") or 0) / all_count * 100), 1) if all_count else 0
            summary["expiry_rate"] = round((int(summary.get("expired_total") or 0) / all_count * 100), 1) if all_count else 0
            summary["online_payment_share"] = round((int(summary.get("paid_orders") or 0) / max(1, issued_count) * 100), 1) if issued_count else 0

            return {
                "summary": summary,
                "by_branch": by_branch,
                "by_status": by_status,
                "payment_statuses": payment_statuses,
                "popular_items": popular_items,
                "ratings": self.get_rating_stats(),
                "rating_summary": rating_summary,
                "rating_distribution": rating_distribution,
                "recent_feedback": recent_feedback,
                "daily": daily,
                "comparison": comparison,
                "service": service,
                "hourly": hourly,
                "stock_risks": stock_risks,
                "low_rated": low_rated,
                "writeoffs": self.get_writeoff_stats()
            }
