import sqlite3
from datetime import datetime, timezone, timedelta

DB_NAME = "database.db"
cuiaba_tz = timezone(timedelta(hours=-4))


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT,
            product_url TEXT,
            formatted_message TEXT,
            media_url TEXT,
            media_type TEXT DEFAULT 'image', -- NOVO: 'image' ou 'video'
            priority INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            target_date TEXT,
            scheduled_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        cursor.execute("ALTER TABLE queue ADD COLUMN media_type TEXT DEFAULT 'image'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    default_configs = {
        "start_time": "08:00",
        "end_time": "22:00",
        "interval_minutes": "30",
        "is_active": "1",
        "auto_refill_enabled": "1",  # 1 = Ativado (Opção A: abastece quando a fila zera)
        "auto_refill_count": "15",  # Quantidade gerada automaticamente por lote
        "auto_keywords": "",  # Palavras-chave opcionais separadas por vírgula
    }

    for key, val in default_configs.items():
        cursor.execute(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, val)
        )

    conn.commit()
    conn.close()


def get_config(key: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else ""


def update_config(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def is_offer_recent(
    product_url: str,
    item_id: int | str | None = None,
    days: int = 2,
    target_date: str | None = None,
) -> bool:
    """Verifica se o link ou itemId já foi adicionado nos últimos 'days' dias ou na data alvo."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cutoff_date = (datetime.now(cuiaba_tz) - timedelta(days=days)).strftime("%Y-%m-%d")

    if target_date:
        cursor.execute(
            "SELECT product_url FROM queue WHERE target_date >= ? OR target_date = ?",
            (cutoff_date, target_date),
        )
    else:
        cursor.execute(
            "SELECT product_url FROM queue WHERE target_date >= ?",
            (cutoff_date,),
        )

    existing_urls = [row[0] for row in cursor.fetchall() if row[0]]
    conn.close()

    item_str = str(item_id) if item_id else None
    for url in existing_urls:
        if url == product_url:
            return True
        if item_str and item_str in url:
            return True

    return False


def add_to_queue(
    store: str,
    product_url: str,
    message: str,
    media_url: str = None,
    media_type: str = "image",
    priority: int = 0,
    target_date: str = None,
):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_cuiaba = datetime.now(cuiaba_tz).strftime("%Y-%m-%d %H:%M:%S")
    if not target_date:
        target_date = datetime.now(cuiaba_tz).strftime("%Y-%m-%d")

    cursor.execute(
        """
        INSERT INTO queue (store, product_url, formatted_message, media_url, media_type, priority, status, target_date, scheduled_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
    """,
        (
            store,
            product_url,
            message,
            media_url,
            media_type,
            priority,
            target_date,
            now_cuiaba,
            now_cuiaba,
        ),
    )
    conn.commit()
    conn.close()


def get_next_pending_offer(target_date: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, store, product_url, formatted_message, media_url, priority, COALESCE(media_type, 'image')
        FROM queue
        WHERE status = 'pending' AND target_date = ?
        ORDER BY priority DESC, id ASC
        LIMIT 1
    """,
        (target_date,),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def mark_offer_as_sent(offer_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE queue SET status = 'sent' WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()


def delete_offer(offer_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM queue WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()
