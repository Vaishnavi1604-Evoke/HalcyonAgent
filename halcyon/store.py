"""SQLite store for live orders.

The `orders` table keeps every column of deliveries.csv (see DATA_DICTIONARY.md),
so live orders can be analysed exactly like the history, plus new columns written by
the agent when it reads and allocates each order. Columns that are only known after
shipping (shipped_ts, delivered_date, days_late, on_time, redelivery_cost_usd) start
empty and are filled as the order moves on.
"""
from __future__ import annotations
import json, sqlite3, threading
from datetime import datetime
from .common import OUT

_lock = threading.RLock()

# deliveries.csv columns, in data-dictionary order
HISTORY_COLS = ["delivery_id", "order_id", "client_id", "order_channel", "email_format_id", "order_received_ts",
                "commitment_type", "promised_date", "address_status", "pick_list_date", "shipped_ts",
                "destination_region", "nearest_warehouse_id", "fulfilment_warehouse_id", "nearest_warehouse_had_stock",
                "distance_miles", "carrier_id", "carrier_rate_usd", "stops_on_route", "planned_route_km",
                "actual_route_km", "delivered_date", "days_late", "on_time", "redelivery_cost_usd"]
# new columns added by the agent
AGENT_COLS = ["uid", "client_name", "sku", "qty", "ship_to", "agent_status", "agent_reason", "pick_wave",
              "lane_on_time", "priority", "client_notice", "restock_alert", "extractor", "extraction_confidence",
              "missing_fields", "source_file", "raw_message", "ingested_at", "allocated_at", "stage", "allocation_trace", "expected_on_time", "causes", "transit_miles", "route_id", "route_stop", "eta", "eta_wall"]
ALL_COLS = HISTORY_COLS + AGENT_COLS
HISTORY_START = 48340  # last delivery_id in deliveries.csv


class Store:
    def __init__(self, path=OUT / "orders.db"):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _lock, self.db:
            cols = ",\n".join(f"{c} TEXT" + (" PRIMARY KEY" if c == "delivery_id" else "") for c in ALL_COLS)
            self.db.execute(f"CREATE TABLE IF NOT EXISTS orders({cols})")
            have = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
            for c in ALL_COLS:   # add any columns missing from an older database
                if c not in have: self.db.execute(f"ALTER TABLE orders ADD COLUMN {c} TEXT")
            self.db.executescript("""
            CREATE UNIQUE INDEX IF NOT EXISTS ix_uid ON orders(uid);
            CREATE INDEX IF NOT EXISTS ix_recv ON orders(order_received_ts);
            CREATE TABLE IF NOT EXISTS emails_seen(message_id TEXT PRIMARY KEY, checked_at TEXT, is_order INTEGER, sender TEXT, subject TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tracking_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT, event_type TEXT,
              source TEXT, carrier TEXT, event_time TEXT, happened_at TEXT, received_at TEXT, detail TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS ix_evt ON tracking_events(delivery_id, event_type);""")
            if "mailbox" not in {r[1] for r in self.db.execute("PRAGMA table_info(emails_seen)")}:
                self.db.execute("ALTER TABLE emails_seen ADD COLUMN mailbox TEXT")

    def has_uid(self, uid):
        with _lock: return self.db.execute("SELECT 1 FROM orders WHERE uid=?", (uid,)).fetchone() is not None

    def insert(self, row: dict):
        with _lock, self.db:
            r = self.db.execute("SELECT MAX(CAST(SUBSTR(delivery_id,2) AS INTEGER)) FROM orders").fetchone()[0]
            row = dict(row); row["delivery_id"] = f"D{(r or HISTORY_START) + 1:06d}"
            vals = [None if row.get(c) is None else str(row.get(c)) for c in ALL_COLS]
            self.db.execute(f"INSERT OR IGNORE INTO orders({','.join(ALL_COLS)}) VALUES({','.join('?' * len(ALL_COLS))})", vals)
            return row["delivery_id"]

    def execute(self, sql, args=()):
        with _lock, self.db: return self.db.execute(sql, args)

    def rows(self, sql, args=()):
        with _lock: return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def email_seen(self, mid):
        with _lock: return self.db.execute("SELECT 1 FROM emails_seen WHERE message_id=?", (mid,)).fetchone() is not None

    def mark_email(self, mid, is_order, sender, subject, mailbox=""):
        with _lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO emails_seen(message_id, checked_at, is_order, sender, subject, mailbox) VALUES(?,?,?,?,?,?)",
                            (mid, datetime.now().isoformat(timespec="seconds"), int(is_order), sender, subject, mailbox))

    def set_meta(self, **kv):
        with _lock, self.db:
            for k, v in kv.items():
                self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v, default=str)))

    def meta(self):
        with _lock: return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM meta")}

    def summary(self, day: str | None = None):
        day = day or datetime.now().strftime("%Y-%m-%d")
        q = lambda sql, *a: self.rows(sql, a)
        today = {r["order_channel"]: r["n"] for r in q("SELECT order_channel, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? GROUP BY order_channel", day + "%")}
        total = {r["order_channel"]: r["n"] for r in q("SELECT order_channel, COUNT(*) n FROM orders GROUP BY order_channel")}
        recent = q("SELECT delivery_id, order_channel channel, order_id order_ref, client_name, order_received_ts received, ingested_at, agent_status status, fulfilment_warehouse_id warehouse, carrier_id carrier FROM orders ORDER BY order_received_ts DESC, delivery_id DESC LIMIT 8")
        em = q("SELECT COUNT(*) n, SUM(is_order) o FROM emails_seen WHERE checked_at LIKE ?", day + "%")[0]
        ch = ["EDI", "API", "Email"]
        shop = q("SELECT COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND source_file='storefront'", day + "%")[0]["n"]
        esrc = {r["s"]: r["n"] for r in q("SELECT COALESCE(source_file,'') s, COUNT(*) n FROM orders WHERE order_received_ts LIKE ? AND order_channel='Email' GROUP BY s", day + "%")}
        email_sources = dict(outlook=esrc.get("outlook", 0) + esrc.get("imap", 0) + esrc.get("graph", 0),
                             simulated=sum(v for k, v in esrc.items() if k == "simulated" or k.startswith("file:")), pasted=esrc.get("pasted", 0))
        return dict(day=day, storefront_today=shop, email_sources=email_sources, today={c: today.get(c, 0) for c in ch}, all_time={c: total.get(c, 0) for c in ch},
                    recent=recent, emails_checked_today=em["n"] or 0, email_orders_today=em["o"] or 0)
