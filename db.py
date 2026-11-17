#!/usr/bin/env python3
"""SQLite storage for the Nintendo product catalog and private app credentials."""
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

DB_PATH = Path(__file__).resolve().parent / "data" / "timeline.db"

# Environment overrides, handy for tests and for moving the DB elsewhere.
DB_PATH = Path(os.environ.get("NOTIFIER_DB", str(DB_PATH))).expanduser()

_PERSIAN_ARABIC_TEXT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+")


def _clean_game_title(value):
    """Remove store/platform wording and Persian script from displayed game titles."""
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\bNintendo\s+Switch\s*2\b", " ", value, flags=re.I)
    value = re.sub(r"\bNintendo\b", " ", value, flags=re.I)
    value = _PERSIAN_ARABIC_TEXT.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n-–—|,:;.")


def _local_image_url(value):
    """Serve catalog images from the app's persistent local cache."""
    return f"/api/image?url={quote(value, safe='')}" if value else ""

# Internal defaults used by authentication and the background refresh loop.
DEFAULT_SETTINGS = {
    "password": "",
    "secret_key": "",
}

SITE_SOURCES = {
    "dreamkala": ("dreamkala_nintendo",),
    "igamer": ("igamer_nintendo",),
    "nakhlmarket": ("nakhlmarket_nintendo",),
    "progame": ("progame_nintendo",),
    "psparsi": ("psparsi_nintendo", "psparsi_secondhand"),
    "pspro": ("pspro_nintendo",),
    "rosegamers": ("rosegamers_nintendo",),
    "techsiro": ("techsiro_nintendo_new", "techsiro_nintendo_secondhand"),
    "westgamestore": ("westgamestore_new", "westgamestore_secondhand", "westgamestore_nintendo"),
}


# ---------- Connection ----------

@contextmanager
def get_conn():
    """Open a SQLite connection, commit on success, always close it."""
    os.makedirs(DB_PATH.parent, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- Helpers ----------

def _normalize_ts(value):
    """
    Coerce any incoming timestamp into an ISO-8601 string.

    Sources may send ISO strings, datetime objects, or RFC-822 dates
    (RSS <pubDate>). Ordering the timeline relies on a single format,
    so everything is normalized here. Falls back to "now".
    """
    if isinstance(value, datetime):
        return value.isoformat()

    if value:
        raw = str(value).strip()
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed is not None:
                return parsed.isoformat()
        except (TypeError, ValueError):
            pass

    return datetime.now().isoformat()


# ---------- Schema ----------

def init_db():
    """Create the product catalog, migrate old timeline columns, and seed credentials."""
    with get_conn() as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone()
        if table:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
            if "category" in columns or "is_read" in columns or "view_later" in columns:
                old_items = [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]
                for index_name in ("idx_items_unique", "idx_items_ts", "idx_items_cat"):
                    conn.execute(f"DROP INDEX IF EXISTS {index_name}")
                conn.execute("ALTER TABLE items RENAME TO items_legacy")
                _create_items_table(conn)
                _migrate_legacy_items(conn, old_items)
                conn.execute("DROP TABLE items_legacy")
            else:
                # Add catalog facets to databases created by earlier standalone versions.
                if "condition" not in columns:
                    conn.execute("ALTER TABLE items ADD COLUMN condition TEXT NOT NULL DEFAULT ''")
                if "in_stock" not in columns:
                    conn.execute("ALTER TABLE items ADD COLUMN in_stock INTEGER NOT NULL DEFAULT 0")
                if "platform" not in columns:
                    conn.execute("ALTER TABLE items ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
                if "image_url" not in columns:
                    conn.execute("ALTER TABLE items ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        else:
            _create_items_table(conn)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ts ON items(timestamp DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_image_url ON items(image_url)")
        # Split the previous combined WestGameStore source into condition-specific sources.
        conn.execute("UPDATE items SET source = CASE WHEN condition = 'used' THEN 'westgamestore_secondhand' ELSE 'westgamestore_new' END WHERE source = 'westgamestore_nintendo'")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def _create_items_table(conn):
    conn.execute("""
        CREATE TABLE items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT NOT NULL,
            title      TEXT NOT NULL,
            text       TEXT NOT NULL DEFAULT '',
            link       TEXT NOT NULL DEFAULT '',
            timestamp  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            condition  TEXT NOT NULL DEFAULT '',
            in_stock   INTEGER NOT NULL DEFAULT 0,
            platform   TEXT NOT NULL DEFAULT '',
            image_url  TEXT NOT NULL DEFAULT '',
            UNIQUE(source, link)
        )
    """)


def _migrate_legacy_items(conn, rows):
    """Keep one current row per product link and discard obsolete timeline flags."""
    products = {}
    for row in rows:
        key = (row.get("source") or "unknown", row.get("link") or row.get("title") or "")
        previous = products.get(key)
        products[key] = {
            "id": previous["id"] if previous else row.get("id"),
            "source": key[0],
            "title": row.get("title") or "No title",
            "text": row.get("text") or "",
            "link": row.get("link") or "",
            "timestamp": row.get("timestamp") or datetime.now().isoformat(),
            "created_at": previous["created_at"] if previous else row.get("created_at"),
            "condition": row.get("condition") or "",
            "in_stock": row.get("in_stock") or 0,
            "platform": row.get("platform") or "",
            "image_url": row.get("image_url") or "",
        }
    for row in products.values():
        conn.execute(
            """INSERT INTO items
               (id, source, title, text, link, timestamp, created_at, condition, in_stock, platform, image_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["source"], row["title"], row["text"], row["link"],
             row["timestamp"], row["created_at"] or datetime.now().isoformat(),
             row["condition"], row["in_stock"], row["platform"], row["image_url"]),
        )


# ---------- Items ----------

def upsert_item(item):
    """Insert or refresh one catalog product, using its stable product URL."""
    now = datetime.now().isoformat()
    source = item.get("source") or item.get("_source") or "westgamestore_new"
    title = item.get("title") or "No title"
    text = item.get("text") or ""
    link = item.get("link") or ""
    condition = item.get("condition") or ""
    in_stock = int(bool(item.get("in_stock", False)))
    platform = item.get("platform") or ""
    image_url = item.get("image_url") or ""
    ts = _normalize_ts(item.get("timestamp"))
    with get_conn() as conn:
        row = None
        if link:
            row = conn.execute(
                "SELECT id, title, text, condition, in_stock, platform, image_url FROM items WHERE source = ? AND link = ? ORDER BY id DESC LIMIT 1",
                (source, link),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, title, text, condition, in_stock, platform, image_url FROM items WHERE source = ? AND title = ? ORDER BY id DESC LIMIT 1",
                (source, title),
            ).fetchone()
        if row:
            # Preserve a previously known thumbnail if a page temporarily omits it.
            image_url = image_url or row["image_url"]
            changed = (row["title"] != title or row["text"] != text or row["condition"] != condition
                       or row["in_stock"] != in_stock or row["platform"] != platform
                       or row["image_url"] != image_url)
            conn.execute(
                "UPDATE items SET title = ?, text = ?, link = ?, timestamp = ?, condition = ?, in_stock = ?, platform = ?, image_url = ? WHERE id = ?",
                (title, text, link, ts, condition, in_stock, platform, image_url, row["id"]),
            )
            return row["id"], False, changed
        cur = conn.execute(
            "INSERT INTO items (source, title, text, link, timestamp, created_at, condition, in_stock, platform, image_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, title, text, link, ts, now, condition, in_stock, platform, image_url),
        )
        return cur.lastrowid, True, True


def _filter_sql(alias, search):
    """Build the catalog search filter."""
    sql = ""
    params = []
    if search:
        sql += f" AND ({alias}.title LIKE ? OR {alias}.text LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    return sql, params


def get_items(limit=None, search=None, sites=None):
    """Return products grouped by normalized game title, with prices by shop."""
    with get_conn() as conn:
        filter_sql, params = _filter_sql("items", search)
        if sites is not None:
            source_names = [source for site in sites
                            for source in SITE_SOURCES.get(site, (f"{site}_nintendo",))]
            if not source_names:
                return []
            placeholders = ",".join("?" for _ in source_names)
            filter_sql += f" AND items.source IN ({placeholders})"
            params.extend(source_names)
        q = "SELECT id, source, title, text, link, timestamp, created_at, condition, in_stock, platform, image_url FROM items WHERE 1=1" + filter_sql
        q += " ORDER BY title COLLATE NOCASE, id DESC"
        rows = conn.execute(q, params).fetchall()

    shops = {
        "westgamestore_new": "WestGameStore",
        "westgamestore_secondhand": "WestGameStore",
        "psparsi_nintendo": "PSParsi",
        "psparsi_secondhand": "PSParsi",
        "dreamkala_nintendo": "DreamKala",
        "techsiro_nintendo_new": "TechSiro",
        "techsiro_nintendo_secondhand": "TechSiro",
        "rosegamers_nintendo": "RoseGamers",
        "progame_nintendo": "ProGame",
        "pspro_nintendo": "PSPro",
        "igamer_nintendo": "iGamer",
        "nakhlmarket_nintendo": "NakhlMarket",
    }
    grouped = {}
    for raw in rows:
        row = dict(raw)
        row["title"] = _clean_game_title(row["title"]) or f"Game {row['id']}"
        key = unicodedata.normalize("NFKC", row["title"]).casefold()
        key = re.sub(r"[^\w]+", " ", key, flags=re.UNICODE).strip()
        match = re.search(r"(?:قیمت\s*:\s*)?([^\n]*\d[\d٬,\.\s]*\s*تومان)", row["text"], re.I)
        price = match.group(1).strip() if match else ""
        digits = re.sub(r"\D", "", price)
        if not row["in_stock"] or not digits or int(digits) <= 0:
            continue
        product = grouped.setdefault(key, {
            "id": row["id"], "title": row["title"], "text": "", "link": row["link"],
            "timestamp": row["timestamp"], "created_at": row["created_at"],
            "in_stock": True, "conditions": [], "platforms": [], "vendors": [],
            "image_url": _local_image_url(row["image_url"]),
        })
        if not product["image_url"] and row["image_url"]:
            product["image_url"] = _local_image_url(row["image_url"])
        if row["timestamp"] > product["timestamp"]:
            product["timestamp"] = row["timestamp"]
        if row["condition"] and row["condition"] not in product["conditions"]:
            product["conditions"].append(row["condition"])
        if row["platform"] and row["platform"] not in product["platforms"]:
            product["platforms"].append(row["platform"])
        offer = {
            "name": shops.get(row["source"], row["source"]),
            "condition": row["condition"], "platform": row["platform"],
            "price": price, "link": row["link"], "image_url": row["image_url"],
        }
        if not any(existing["name"] == offer["name"] and existing["condition"] == offer["condition"]
                   and existing["platform"] == offer["platform"] and existing["price"] == offer["price"]
                   for existing in product["vendors"]):
            product["vendors"].append(offer)
    items = list(grouped.values())
    for item in items:
        item["vendors"].sort(
            key=lambda offer: (
                int(re.sub(r"\D", "", offer["price"]) or 0),
                offer["name"].casefold(),
            )
        )
    if limit:
        items = items[:int(limit)]
    return items


def count():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]


def is_catalog_image(url):
    """Only proxy image URLs already discovered by a store scraper."""
    if not url:
        return False
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM items WHERE image_url = ? LIMIT 1", (url,)).fetchone() is not None


# ---------- Settings ----------

def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def ensure_secret_key():
    """Generate a secret key on first run if missing."""
    import secrets

    if not get_setting("secret_key"):
        set_setting("secret_key", secrets.token_hex(32))
