import os
import json
import sqlite3
import time
import threading
import re
from datetime import datetime, timezone
from collections import defaultdict
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel

import requests
from bs4 import BeautifulSoup

# ==================== КОНФИГ ====================
DB_PATH = os.environ.get("DB_PATH", "shuffle.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_SECRET_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-20b"

_default_screenshots = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", _default_screenshots)
try:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
except (PermissionError, OSError) as e:
    print(f"[WARN] Не могу создать {SCREENSHOT_DIR} ({e}). Использую {_default_screenshots}")
    SCREENSHOT_DIR = _default_screenshots
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

app = FastAPI(title="Shuffle Rain & Tips Stats")

_state_lock = threading.Lock()
INSTANCE_STATES = {}
SCREENSHOT_REQUESTS = {}

PROMO_CHANNEL = os.environ.get("PROMO_CHANNEL", "anyclaimershuffle")
PROMO_PARSE_INTERVAL = 5 * 60

# Окно для связки "ставка → дождь"
BET_TO_RAIN_WINDOW_SEC = 900  # 15 минут

LANG_MAP = {
    'ENGLISH': 'английский', 'RUSSIAN': 'русский', 'JAPANESE': 'японский',
    'TURKISH': 'турецкий', 'FRENCH': 'французский', 'SPANISH': 'испанский',
    'PORTUGUESE': 'португальский', 'KOREAN': 'корейский', 'VIETNAMESE': 'вьетнамский',
    'POLISH': 'польский', 'INDONESIAN': 'индонезийский', 'CHINESE': 'китайский',
}


# ==================== RATES (серверные) ====================
RATES_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_rates.json")

# Алиасы иконок Shuffle → ключ в rates
RATE_ALIASES = {
    "TON": "GRAM",
    "MATIC": "POL",
    "POLYGON": "POL",
    "USD": "USDT",
    "USDC": "USDT",
    "DAI": "USDT",
    "BUSD": "USDT",
    "TUSD": "USDT",
}

_server_rates = {}
_server_rates_lock = threading.Lock()
_server_rates_updated = [0.0]


def _load_server_rates():
    global _server_rates
    if os.path.exists(RATES_CACHE_FILE):
        try:
            with open(RATES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with _server_rates_lock:
                _server_rates = data.get("rates", {})
                _server_rates_updated[0] = data.get("last_update", 0)
            print(f"[RATES] ✅ Серверных курсов: {len(_server_rates)}")
        except Exception as e:
            print(f"[RATES] ⚠️ {e}")


def _save_server_rates():
    try:
        with _server_rates_lock:
            data = {"rates": _server_rates, "last_update": _server_rates_updated[0]}
        with open(RATES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RATES] save: {e}")


def _fetch_server_rates():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = ("bitcoin,ethereum,solana,litecoin,dogecoin,ripple,tron,"
               "binancecoin,tether,usd-coin,shiba-inu,"
               "shuffle-2,polygon-ecosystem-token,avalanche-2,"
               "the-open-network,bonk,dogwifcoin,pump-fun,"
               "official-trump,dai")
        params = {"ids": ids, "vs_currencies": "rub,usd"}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return
        data = r.json()
        mapping = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "LTC": "litecoin", "DOGE": "dogecoin", "XRP": "ripple",
            "TRX": "tron", "BNB": "binancecoin",
            "USDT": "tether", "USDC": "usd-coin", "SHIB": "shiba-inu",
            "SHFL": "shuffle-2", "POL": "polygon-ecosystem-token",
            "AVAX": "avalanche-2", "GRAM": "the-open-network",
            "BONK": "bonk", "WIF": "dogwifcoin", "PUMP": "pump-fun",
            "TRUMP": "official-trump", "DAI": "dai",
        }
        new = {}
        for sym, cg in mapping.items():
            if cg in data:
                if "rub" in data[cg]:
                    new[sym] = data[cg]["rub"]
                if "usd" in data[cg]:
                    new[f"{sym}_USD"] = data[cg]["usd"]
        # TON = GRAM (тот же токен)
        if "GRAM" in new:
            new["TON"] = new["GRAM"]
        if "GRAM_USD" in new:
            new["TON_USD"] = new["GRAM_USD"]
        if "POL" in new:
            new["MATIC"] = new["POL"]
        if "POL_USD" in new:
            new["MATIC_USD"] = new["POL_USD"]
        new["CASH"] = new.get("USDT", 95.0)
        new["CASH_USD"] = 1.0
        with _server_rates_lock:
            _server_rates.update(new)
            _server_rates_updated[0] = time.time()
        _save_server_rates()
        print(f"[RATES] 🔄 Обновлено {len(new)} курсов")
    except Exception as e:
        print(f"[RATES] fetch: {e}")


def server_rate_rub(symbol):
    if not symbol:
        return None
    s = symbol.upper().strip()
    s = RATE_ALIASES.get(s, s)
    with _server_rates_lock:
        return _server_rates.get(s)


def server_rate_usd(symbol):
    if not symbol:
        return None
    s = symbol.upper().strip()
    s = RATE_ALIASES.get(s, s)
    with _server_rates_lock:
        return _server_rates.get(f"{s}_USD")


def rates_refresh_thread():
    # первое обновление сразу
    _fetch_server_rates()
    while True:
        time.sleep(3600)
        try:
            _fetch_server_rates()
        except Exception as e:
            print(f"[RATES] thread: {e}")


# ==================== БАЗА ====================
def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ph():
    return "%s" if USE_POSTGRES else "?"


def query_all(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(sql_pg, params)
        try:
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
    finally:
        conn.close()


def query_one(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        if USE_POSTGRES:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(sql_pg, params)
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def execute(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql_pg, params)
        conn.commit()
    finally:
        conn.close()


def _try(sql):
    try:
        execute(sql)
    except Exception:
        pass


def init_db():
    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY, type TEXT NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, receivers TEXT NOT NULL, amount_text TEXT,
            amount_type TEXT, crypto_symbol TEXT, amount_rub DOUBLE PRECISION DEFAULT 0,
            amount_usd DOUBLE PRECISION DEFAULT 0,
            created_at TEXT NOT NULL, ts DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, receivers TEXT NOT NULL, amount_text TEXT,
            amount_type TEXT, crypto_symbol TEXT, amount_rub REAL DEFAULT 0,
            amount_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL, ts REAL)""")
    _try("ALTER TABLE events ADD COLUMN ts DOUBLE PRECISION" if USE_POSTGRES else "ALTER TABLE events ADD COLUMN ts REAL")
    _try("ALTER TABLE events ADD COLUMN context_messages TEXT" if USE_POSTGRES else "ALTER TABLE events ADD COLUMN context_messages TEXT")
    _try("ALTER TABLE events ADD COLUMN context_bets TEXT" if USE_POSTGRES else "ALTER TABLE events ADD COLUMN context_bets TEXT")
    _try("ALTER TABLE events ADD COLUMN amount_usd DOUBLE PRECISION DEFAULT 0" if USE_POSTGRES else "ALTER TABLE events ADD COLUMN amount_usd REAL DEFAULT 0")

    execute("CREATE INDEX IF NOT EXISTS idx_type ON events(type)")
    execute("CREATE INDEX IF NOT EXISTS idx_sender ON events(sender)")
    execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS bulk_alerts (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, amount_text TEXT, amount_rub DOUBLE PRECISION DEFAULT 0,
            crypto TEXT, receivers TEXT NOT NULL, window_sec INTEGER DEFAULT 0)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS bulk_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, amount_text TEXT, amount_rub REAL DEFAULT 0,
            crypto TEXT, receivers TEXT NOT NULL, window_sec INTEGER DEFAULT 0)""")
    execute("CREATE INDEX IF NOT EXISTS idx_bulk_ts ON bulk_alerts(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_bulk_sender ON bulk_alerts(sender)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS player_activity (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, username TEXT NOT NULL,
            chat TEXT NOT NULL, text TEXT)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS player_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, username TEXT NOT NULL,
            chat TEXT NOT NULL, text TEXT)""")
    execute("CREATE INDEX IF NOT EXISTS idx_player_ts ON player_activity(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_player_user ON player_activity(username)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, game TEXT, multiplier TEXT, amount_text TEXT,
            amount_rub DOUBLE PRECISION DEFAULT 0, amount_usd DOUBLE PRECISION DEFAULT 0,
            crypto TEXT, outcome TEXT)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, game TEXT, multiplier TEXT, amount_text TEXT,
            amount_rub REAL DEFAULT 0, amount_usd REAL DEFAULT 0, crypto TEXT, outcome TEXT)""")
    execute("CREATE INDEX IF NOT EXISTS idx_bets_ts ON bets(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_bets_chat ON bets(chat)")
    execute("CREATE INDEX IF NOT EXISTS idx_bets_sender ON bets(sender)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS intents (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, text TEXT, percent INTEGER DEFAULT 100,
            matched_rain_id INTEGER, matched_delta_sec INTEGER)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, text TEXT, percent INTEGER DEFAULT 100,
            matched_rain_id INTEGER, matched_delta_sec INTEGER)""")
    _try("ALTER TABLE intents ADD COLUMN percent INTEGER DEFAULT 100" if USE_POSTGRES else "ALTER TABLE intents ADD COLUMN percent INTEGER DEFAULT 100")
    execute("CREATE INDEX IF NOT EXISTS idx_intents_ts ON intents(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_intents_chat ON intents(chat)")
    execute("CREATE INDEX IF NOT EXISTS idx_intents_sender ON intents(sender)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS promocodes (
            id SERIAL PRIMARY KEY, tg_msg_id TEXT UNIQUE, text TEXT NOT NULL, link TEXT,
            message_date TEXT, discovered_at DOUBLE PRECISION, used INTEGER DEFAULT 0,
            used_at DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_msg_id TEXT UNIQUE, text TEXT NOT NULL,
            link TEXT, message_date TEXT, discovered_at REAL, used INTEGER DEFAULT 0,
            used_at REAL)""")
    execute("CREATE INDEX IF NOT EXISTS idx_promo_used ON promocodes(used)")
    execute("CREATE INDEX IF NOT EXISTS idx_promo_date ON promocodes(discovered_at)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS chat_tasks (
            id SERIAL PRIMARY KEY, chat TEXT NOT NULL, status TEXT DEFAULT 'pending',
            messages TEXT, result TEXT, error TEXT,
            created_at DOUBLE PRECISION, completed_at DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS chat_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL, status TEXT DEFAULT 'pending',
            messages TEXT, result TEXT, error TEXT,
            created_at REAL, completed_at REAL)""")
    execute("CREATE INDEX IF NOT EXISTS idx_task_status ON chat_tasks(status)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS chat_players (
            id SERIAL PRIMARY KEY, chat TEXT NOT NULL, username TEXT NOT NULL,
            msg_count INTEGER DEFAULT 0, first_seen DOUBLE PRECISION, last_seen DOUBLE PRECISION,
            UNIQUE(chat, username))""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS chat_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL, username TEXT NOT NULL,
            msg_count INTEGER DEFAULT 0, first_seen REAL, last_seen REAL,
            UNIQUE(chat, username))""")
    execute("CREATE INDEX IF NOT EXISTS idx_cp_chat ON chat_players(chat)")
    execute("CREATE INDEX IF NOT EXISTS idx_cp_user ON chat_players(username)")
    execute("CREATE INDEX IF NOT EXISTS idx_cp_last ON chat_players(last_seen)")

    execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")


# ==================== META ====================
def meta_get(k, default=None):
    r = query_one("SELECT v FROM meta WHERE k=?", (k,))
    return r["v"] if r else default


def meta_set(k, v):
    if USE_POSTGRES:
        execute("INSERT INTO meta (k,v) VALUES (%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, str(v)))
    else:
        execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, str(v)))


def migrate_ts_once():
    try:
        if meta_get("ts_migrated_v1"):
            return
        print("[DB] Миграция ts...")
        if USE_POSTGRES:
            execute("""UPDATE events SET ts = EXTRACT(EPOCH FROM created_at::timestamptz)
                       WHERE ts IS NULL AND created_at IS NOT NULL""")
        else:
            execute("""UPDATE events SET ts = CAST(strftime('%s', created_at) AS REAL)
                       WHERE ts IS NULL AND created_at IS NOT NULL""")
        meta_set("ts_migrated_v1", "1")
        print("[DB] ✅ ts миграция ок")
    except Exception as e:
        print(f"[DB] ❌ ts: {e}")


# ==================== ДЕДУП + ПЕРЕСЧЁТ ====================
def dedup_events():
    """Удаляет дубликаты событий и пересчитывает нулевые суммы."""
    try:
        # 1) Пересчёт нулевых amount_rub / amount_usd у events
        rows = query_all("""SELECT id, type, chat, sender, amount_text, amount_type,
                            crypto_symbol, amount_rub, amount_usd
                            FROM events
                            WHERE (amount_rub IS NULL OR amount_rub = 0
                                   OR amount_usd IS NULL OR amount_usd = 0)
                            LIMIT 5000""")
        fixed = 0
        for r in rows:
            txt = r.get("amount_text") or ""
            crypto = (r.get("crypto_symbol") or "").upper().strip()
            value = None
            try:
                value = float(txt.replace("\u00a0", " ").replace(" ", "").replace(",", ".").replace("₽", "").replace("$", "").replace("€", ""))
            except Exception:
                pass
            if value is None:
                continue
            new_rub = r.get("amount_rub") or 0
            new_usd = r.get("amount_usd") or 0
            has_fiat_marker = any(m in txt for m in ("₽", "$", "€", "£"))
            # Если есть крипто-символ и нет фиат-маркера — конвертим из крипты
            if crypto and not has_fiat_marker:
                cr = server_rate_rub(crypto)
                cu = server_rate_usd(crypto)
                if cr and (not new_rub):
                    new_rub = value * cr
                if cu and (not new_usd):
                    new_usd = value * cu
            elif "₽" in txt and not new_rub:
                new_rub = value
            elif "$" in txt and not new_usd:
                new_usd = value
            elif not crypto:
                # нет символа — оставляем как рубли
                if not new_rub:
                    new_rub = value
                if not new_usd and new_rub:
                    uu = server_rate_usd("USDT") or 1.0
                    ur = server_rate_rub("USDT") or 95.0
                    new_usd = new_rub / ur * uu
            if new_rub != (r.get("amount_rub") or 0) or new_usd != (r.get("amount_usd") or 0):
                p = ph()
                execute(f"UPDATE events SET amount_rub={p}, amount_usd={p} WHERE id={p}",
                        (new_rub, new_usd, r["id"]))
                fixed += 1
        if fixed:
            print(f"[DEDUP] ✅ Пересчитано {fixed} событий с 0-суммой")

        # 2) Удаление дублей events: (type, chat, sender, amount_rub, receivers, ts в пределах 60с)
        dups = query_all("""
            SELECT a.id AS keep_id, b.id AS del_id
            FROM events a
            JOIN events b
              ON a.id < b.id
             AND a.type = b.type
             AND a.chat = b.chat
             AND a.sender = b.sender
             AND a.receivers = b.receivers
             AND a.amount_text = b.amount_text
             AND ABS(COALESCE(a.ts,0) - COALESCE(b.ts,0)) < 60
            LIMIT 2000
        """)
        if dups:
            ids = list({d["del_id"] for d in dups})
            CHUNK = 500
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i+CHUNK]
                phs = ",".join([ph()] * len(chunk))
                execute(f"DELETE FROM events WHERE id IN ({phs})", tuple(chunk))
            print(f"[DEDUP] ✅ Удалено {len(ids)} дублей events")

        # 3) Удаление дублей bulk_alerts
        dups_b = query_all("""
            SELECT a.id AS keep_id, b.id AS del_id
            FROM bulk_alerts a
            JOIN bulk_alerts b
              ON a.id < b.id
             AND a.chat = b.chat
             AND a.sender = b.sender
             AND a.amount_text = b.amount_text
             AND ABS(COALESCE(a.ts,0) - COALESCE(b.ts,0)) < 60
            LIMIT 2000
        """)
        if dups_b:
            ids = list({d["del_id"] for d in dups_b})
            CHUNK = 500
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i+CHUNK]
                phs = ",".join([ph()] * len(chunk))
                execute(f"DELETE FROM bulk_alerts WHERE id IN ({phs})", tuple(chunk))
            print(f"[DEDUP] ✅ Удалено {len(ids)} дублей bulk")
    except Exception as e:
        print(f"[DEDUP] ❌ {type(e).__name__}: {e}")


def dedup_thread():
    while True:
        time.sleep(10 * 60)
        try:
            dedup_events()
        except Exception as e:
            print(f"[DEDUP] thread: {e}")


# ==================== МОДЕЛИ ====================
class EventIn(BaseModel):
    type: str
    chat: str
    sender: str
    receivers: List[str] = []
    amount_text: str = ""
    amount_type: str = "fiat"
    crypto_symbol: str = ""
    amount_rub: float = 0.0
    amount_usd: float = 0.0
    created_at: Optional[str] = None
    ts: Optional[float] = None
    context_messages: List[str] = []
    context_bets: List[dict] = []


class BulkAlertIn(BaseModel):
    ts: Optional[float] = None
    chat: str
    sender: str
    amount_text: str = ""
    amount_rub: float = 0.0
    crypto: str = ""
    receivers: List[str] = []
    window_sec: int = 0


class PlayerActivityIn(BaseModel):
    ts: Optional[float] = None
    username: str
    chat: str
    text: str = ""


class IntentIn(BaseModel):
    ts: Optional[float] = None
    chat: str
    sender: str
    text: str = ""
    percent: int = 100


class BetIn(BaseModel):
    ts: Optional[float] = None
    chat: str
    sender: str
    game: str = ""
    multiplier: str = ""
    amount_text: str = ""
    amount_rub: float = 0.0
    amount_usd: float = 0.0
    crypto: str = ""
    outcome: str = "win"


class InstanceStatus(BaseModel):
    iid: int
    mode: Optional[str] = None
    status: Optional[str] = None
    current_chat: Optional[str] = None
    chats: Optional[List[str]] = None
    new_count: Optional[int] = 0
    threshold: Optional[int] = None
    error: Optional[str] = None
    updated_at: Optional[float] = None


class StatusPayload(BaseModel):
    instances: List[InstanceStatus] = []
    screenshot_requests: List[int] = []


class ChatTaskIn(BaseModel):
    chat: str


class ChatTaskCompleteIn(BaseModel):
    task_id: int
    messages: List[dict] = []
    error: str = ""


class PlayerStatItem(BaseModel):
    name: str
    msgs: int = 0
    last_seen: float = 0.0


class PlayersBatchIn(BaseModel):
    chat: str
    players: List[PlayerStatItem] = []


# ==================== API ДЛЯ СКРИПТА ====================
def _recalc_amounts(amount_text, amount_type, crypto_symbol):
    """Пересчитывает 0-суммы используя серверные курсы. Возвращает (rub, usd)."""
    txt = amount_text or ""
    crypto = (crypto_symbol or "").upper().strip()
    value = None
    cleaned = (txt.replace("\u00a0", " ").replace(" ", "")
                  .replace("₽", "").replace("$", "").replace("€", ""))
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except Exception:
        return 0.0, 0.0
    has_fiat = any(m in txt for m in ("₽", "$", "€", "£", "₴", "₸", "₺"))

    if crypto and (amount_type == "crypto" or not has_fiat):
        cr = server_rate_rub(crypto) or 0.0
        cu = server_rate_usd(crypto) or 0.0
        return value * cr, value * cu
    if "₽" in txt:
        rub = value
        ur = server_rate_rub("USDT") or 95.0
        uu = server_rate_usd("USDT") or 1.0
        return rub, rub / ur * uu
    if "$" in txt:
        usd = value
        ur = server_rate_rub("USDT") or 95.0
        uu = server_rate_usd("USDT") or 1.0
        rub = usd / uu * ur if uu else usd * ur
        return rub, usd
    if "€" in txt:
        usd = value * 1.08
        ur = server_rate_rub("USDT") or 95.0
        uu = server_rate_usd("USDT") or 1.0
        rub = usd / uu * ur if uu else usd * ur
        return rub, usd
    # нет символа — рубли по-умолчанию
    rub = value
    ur = server_rate_rub("USDT") or 95.0
    uu = server_rate_usd("USDT") or 1.0
    return rub, rub / ur * uu


@app.post("/api/event")
async def add_event(event: EventIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if event.type not in ("rain", "tip"):
        raise HTTPException(status_code=400, detail="type must be rain or tip")
    ts_val = event.ts if event.ts else time.time()
    created = event.created_at or datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
    dup = query_one("""SELECT id FROM events WHERE type=? AND chat=? AND sender=? AND receivers=? AND amount_text=? AND ts IS NOT NULL AND ABS(ts - ?) < 30 LIMIT 1""",
                    (event.type, event.chat, event.sender, json.dumps(event.receivers or [], ensure_ascii=False), event.amount_text, ts_val))
    if dup:
        return {"ok": True, "dup": True}

    rub = event.amount_rub or 0.0
    usd = event.amount_usd or 0.0
    # серверный fallback если клиент не смог посчитать
    if rub == 0 or usd == 0:
        cr, cu = _recalc_amounts(event.amount_text, event.amount_type, event.crypto_symbol)
        if not rub: rub = cr
        if not usd: usd = cu

    p = ph()
    execute(f"""INSERT INTO events (type, chat, sender, receivers, amount_text, amount_type,
                crypto_symbol, amount_rub, amount_usd, created_at, ts, context_messages, context_bets)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (event.type, event.chat, event.sender, json.dumps(event.receivers or [], ensure_ascii=False),
             event.amount_text, event.amount_type, event.crypto_symbol, rub, usd, created, ts_val,
             json.dumps(event.context_messages or [], ensure_ascii=False),
             json.dumps(event.context_bets or [], ensure_ascii=False)))
    return {"ok": True}


@app.post("/api/bulk_alert")
async def add_bulk_alert(body: BulkAlertIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    rub = body.amount_rub or 0.0
    if rub == 0:
        cr, _ = _recalc_amounts(body.amount_text, "crypto" if body.crypto else "fiat", body.crypto)
        rub = cr
    p = ph()
    execute(f"""INSERT INTO bulk_alerts (ts, chat, sender, amount_text, amount_rub, crypto, receivers, window_sec)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p})""",
            (ts_val, body.chat, body.sender, body.amount_text, rub, body.crypto,
             json.dumps(body.receivers, ensure_ascii=False), body.window_sec))
    return {"ok": True}


@app.post("/api/player_activity")
async def add_player_activity(body: PlayerActivityIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    p = ph()
    execute(f"INSERT INTO player_activity (ts, username, chat, text) VALUES ({p},{p},{p},{p})",
            (ts_val, body.username, body.chat, body.text))
    return {"ok": True}


@app.post("/api/bet")
async def add_bet(body: BetIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    rub = body.amount_rub or 0.0
    usd = body.amount_usd or 0.0
    if rub == 0 or usd == 0:
        cr, cu = _recalc_amounts(body.amount_text, "crypto" if body.crypto else "fiat", body.crypto)
        if not rub: rub = cr
        if not usd: usd = cu
    p = ph()
    execute(f"""INSERT INTO bets (ts, chat, sender, game, multiplier, amount_text,
                amount_rub, amount_usd, crypto, outcome) VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (ts_val, body.chat, body.sender, body.game, body.multiplier, body.amount_text,
             rub, usd, body.crypto, body.outcome))
    return {"ok": True}


@app.post("/api/intent")
async def add_intent(body: IntentIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    dup = query_one("SELECT id FROM intents WHERE chat=? AND sender=? AND text=? AND ABS(ts-?) < 60 LIMIT 1",
                    (body.chat, body.sender, body.text, ts_val))
    if dup:
        return {"ok": True, "dup": True}
    p = ph()
    execute(f"INSERT INTO intents (ts, chat, sender, text, percent, matched_rain_id, matched_delta_sec) VALUES ({p},{p},{p},{p},{p},NULL,NULL)",
            (ts_val, body.chat, body.sender, body.text, body.percent or 100))
    WINDOW = 300
    rain = query_one("""SELECT id, ts FROM events WHERE type='rain' AND chat=? AND sender LIKE ?
                        AND ts IS NOT NULL AND ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1""",
                     (body.chat, f"%{body.sender}%", ts_val, ts_val + WINDOW))
    if rain:
        delta = int(rain["ts"] - ts_val)
        execute("UPDATE intents SET matched_rain_id=?, matched_delta_sec=? WHERE chat=? AND sender=? AND text=? AND ABS(ts-?) < 60",
                (rain["id"], delta, body.chat, body.sender, body.text, ts_val))
        print(f"[INTENT] ✅ {body.sender} обещал и залил через {delta}с ({body.chat})")
    return {"ok": True}


@app.post("/api/players_batch")
async def add_players_batch(body: PlayersBatchIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = time.time()
    p = ph()
    for pl in body.players:
        name = (pl.name or "").strip()
        if not name or name == "?":
            continue
        msgs = max(0, int(pl.msgs))
        last_seen = float(pl.last_seen) or now
        if USE_POSTGRES:
            execute(f"""INSERT INTO chat_players (chat, username, msg_count, first_seen, last_seen)
                VALUES ({p},{p},{p},{p},{p})
                ON CONFLICT (chat, username) DO UPDATE
                SET msg_count = chat_players.msg_count + EXCLUDED.msg_count,
                    last_seen = GREATEST(chat_players.last_seen, EXCLUDED.last_seen)""",
                (body.chat, name, msgs, last_seen, last_seen))
        else:
            execute(f"""INSERT INTO chat_players (chat, username, msg_count, first_seen, last_seen)
                VALUES ({p},{p},{p},{p},{p})
                ON CONFLICT (chat, username) DO UPDATE
                SET msg_count = msg_count + excluded.msg_count,
                    last_seen = MAX(last_seen, excluded.last_seen)""",
                (body.chat, name, msgs, last_seen, last_seen))
    return {"ok": True}


@app.post("/api/events_batch")
async def events_batch(payload: dict, x_api_key: str = Header(default="")):
    """Батчевая вставка событий от клиента."""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    events = payload.get("events", [])
    inserted = 0
    for e in events:
        try:
            rub = float(e.get("amount_rub") or 0)
            usd = float(e.get("amount_usd") or 0)
            if rub == 0 or usd == 0:
                cr, cu = _recalc_amounts(e.get("amount_text", ""), e.get("amount_type", "fiat"), e.get("crypto_symbol", ""))
                if not rub: rub = cr
                if not usd: usd = cu
            p = ph()
            execute(f"""INSERT INTO events (type, chat, sender, receivers, amount_text, amount_type,
                        crypto_symbol, amount_rub, amount_usd, created_at, ts, context_messages, context_bets)
                        VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                    (e.get("type"), e.get("chat"), e.get("sender"),
                     json.dumps(e.get("receivers", []), ensure_ascii=False),
                     e.get("amount_text", ""), e.get("amount_type", "fiat"),
                     e.get("crypto_symbol", ""), rub, usd,
                     datetime.fromtimestamp(e.get("ts", time.time()), tz=timezone.utc).isoformat(),
                     e.get("ts", time.time()),
                     json.dumps(e.get("context_messages", []), ensure_ascii=False),
                     json.dumps(e.get("context_bets", []), ensure_ascii=False)))
            inserted += 1
        except Exception as ex:
            print(f"[BATCH] insert err: {ex}")
    return {"ok": True, "inserted": inserted}


@app.post("/api/status")
async def post_status(payload: StatusPayload, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = time.time()
    with _state_lock:
        for inst in payload.instances:
            INSTANCE_STATES[inst.iid] = {**inst.dict(), "server_received_at": now}
        for iid in payload.screenshot_requests:
            if iid not in SCREENSHOT_REQUESTS:
                SCREENSHOT_REQUESTS[iid] = {"requested_at": now, "done": False}
    return {"ok": True}


@app.get("/api/screenshot_req")
async def get_screenshot_req(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    with _state_lock:
        pending = [{"iid": iid, "requested_at": r.get("requested_at", 0)}
                   for iid, r in SCREENSHOT_REQUESTS.items() if not r.get("done")]
    return {"requests": pending}


@app.post("/api/screenshot_upload")
async def upload_screenshot(iid: int = Form(...), file: UploadFile = File(...), x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    path = os.path.join(SCREENSHOT_DIR, f"inst{iid}_latest.png")
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    with _state_lock:
        prev = SCREENSHOT_REQUESTS.get(iid, {})
        SCREENSHOT_REQUESTS[iid] = {"requested_at": prev.get("requested_at", time.time()),
                                     "done": True, "path": path, "uploaded_at": time.time()}
    return {"ok": True}


# ==================== WATCHLIST ====================
DEFAULT_WATCHLIST = ["Fab", "Fab434", "fab434"]


@app.get("/api/watchlist")
async def get_watchlist():
    v = meta_get("watchlist")
    if not v:
        return {"players": DEFAULT_WATCHLIST}
    try:
        return {"players": json.loads(v)}
    except Exception:
        return {"players": DEFAULT_WATCHLIST}


@app.post("/api/watchlist/add")
async def watchlist_add(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    v = meta_get("watchlist")
    try:
        players = json.loads(v) if v else list(DEFAULT_WATCHLIST)
    except Exception:
        players = list(DEFAULT_WATCHLIST)
    if name not in players:
        players.append(name)
    meta_set("watchlist", json.dumps(players, ensure_ascii=False))
    return {"ok": True, "players": players}


@app.post("/api/watchlist/remove")
async def watchlist_remove(body: dict):
    name = (body.get("name") or "").strip()
    v = meta_get("watchlist")
    try:
        players = json.loads(v) if v else list(DEFAULT_WATCHLIST)
    except Exception:
        players = list(DEFAULT_WATCHLIST)
    players = [p for p in players if p != name]
    meta_set("watchlist", json.dumps(players, ensure_ascii=False))
    return {"ok": True, "players": players}


# ==================== ПРОМОКОДЫ ====================
def fetch_promo_channel():
    url = f"https://t.me/s/{PROMO_CHANNEL}"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"[PROMO] ⚠️ HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        messages = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg_el = wrap.select_one(".tgme_widget_message")
            if not msg_el: continue
            data_post = msg_el.get("data-post", "")
            if not data_post: continue
            time_el = wrap.select_one("time")
            msg_date = time_el.get("datetime", "") if time_el else ""
            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n", strip=True) if text_el else ""
            if not text: continue
            messages.append({"tg_msg_id": data_post, "text": text,
                             "link": f"https://t.me/{data_post}", "message_date": msg_date})
        return messages
    except Exception as e:
        print(f"[PROMO] ❌ {type(e).__name__}: {e}")
        return []


def promo_parser_thread():
    time.sleep(10)
    while True:
        try:
            messages = fetch_promo_channel()
            new_count = 0
            for m in messages:
                if query_one("SELECT id FROM promocodes WHERE tg_msg_id=?", (m["tg_msg_id"],)): continue
                p = ph()
                execute(f"""INSERT INTO promocodes (tg_msg_id, text, link, message_date, discovered_at, used)
                            VALUES ({p},{p},{p},{p},{p},0)""",
                        (m["tg_msg_id"], m["text"], m["link"], m["message_date"], time.time()))
                new_count += 1
            if new_count > 0:
                print(f"[PROMO] ✅ +{new_count} новых из @{PROMO_CHANNEL}")
        except Exception as e:
            print(f"[PROMO] ❌ {type(e).__name__}: {e}")
        time.sleep(PROMO_PARSE_INTERVAL)


@app.get("/api/promocodes")
async def api_promocodes(only_unused: Optional[int] = None, limit: int = 200):
    q = "SELECT * FROM promocodes WHERE 1=1"; p = []
    if only_unused == 1: q += " AND used = 0"
    q += " ORDER BY discovered_at DESC LIMIT ?"; p.append(limit)
    return {"promos": query_all(q, tuple(p))}


@app.post("/api/promo/toggle/{promo_id}")
async def api_promo_toggle(promo_id: int):
    row = query_one("SELECT id, used FROM promocodes WHERE id=?", (promo_id,))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    new_used = 0 if row["used"] else 1
    execute("UPDATE promocodes SET used=?, used_at=? WHERE id=?",
            (new_used, time.time() if new_used else None, promo_id))
    return {"ok": True, "used": new_used}


@app.get("/api/promocodes/stats")
async def api_promocodes_stats():
    total = (query_one("SELECT COUNT(*) AS c FROM promocodes") or {}).get("c", 0)
    used = (query_one("SELECT COUNT(*) AS c FROM promocodes WHERE used=1") or {}).get("c", 0)
    return {"total": total, "used": used, "unused": total - used}


@app.post("/api/promo/test")
async def api_promo_test():
    messages = fetch_promo_channel()
    new_count = 0
    for m in messages:
        if query_one("SELECT id FROM promocodes WHERE tg_msg_id=?", (m["tg_msg_id"],)): continue
        p = ph()
        execute(f"""INSERT INTO promocodes (tg_msg_id, text, link, message_date, discovered_at, used)
                    VALUES ({p},{p},{p},{p},{p},0)""",
                (m["tg_msg_id"], m["text"], m["link"], m["message_date"], time.time()))
        new_count += 1
    return {"ok": True, "found": len(messages), "new": new_count}


# ==================== AI ASSISTANT ====================
@app.get("/api/chat/pending")
async def chat_pending(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    task = query_one("SELECT id, chat FROM chat_tasks WHERE status='pending' ORDER BY created_at ASC LIMIT 1")
    if not task:
        return {"task": None}
    execute("UPDATE chat_tasks SET status='reading' WHERE id=?", (task["id"],))
    return {"task": task}


@app.post("/api/chat/complete")
async def chat_complete(body: ChatTaskCompleteIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    task = query_one("SELECT * FROM chat_tasks WHERE id=?", (body.task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if body.error:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (body.error, time.time(), body.task_id))
        return {"ok": True, "status": "failed"}
    execute("UPDATE chat_tasks SET messages=? WHERE id=?",
            (json.dumps(body.messages, ensure_ascii=False), body.task_id))
    threading.Thread(target=_process_chat_task_groq,
                     args=(body.task_id, task["chat"], body.messages), daemon=True).start()
    return {"ok": True, "status": "processing"}


def _process_chat_task_groq(task_id, chat_name, messages):
    if not GROQ_API_KEY or not GROQ_API_KEY.startswith("gsk_"):
        execute("UPDATE chat_tasks SET status='failed', error='GROQ_API_KEY не задан', completed_at=? WHERE id=?",
                (time.time(), task_id))
        return
    lang = LANG_MAP.get(chat_name, chat_name.lower())
    lines = []
    for m in (messages or [])[-50:]:
        user = m.get("user", "?")
        text = m.get("text", "")
        if text:
            lines.append(f"{user}: {text}")
    context = "\n".join(lines)

    system_prompt = (
        f"Ты носитель {lang} языка и активный участник игрового чата казино. "
        f"Тебе дан контекст из последних 50 сообщений чата. "
        f"Твоя задача — предложить 5 живых коротких сообщений на {lang} языке, "
        f"которые я мог бы отправить в чат, чтобы выглядеть как носитель и не отходить от текущей темы беседы. "
        f"Сообщения должны быть естественными, короткими (до 100 символов), в стиле чата. "
        f"Также предложи 5 нейтральных фраз типа 'всем удачи', 'добрый вечер', 'как у вас успехи' — "
        f"которые можно вставить в любой момент.\n\n"
        f"ВАЖНО: ответь СТРОГО в формате JSON без пояснений, вот такой структуры:\n"
        f'{{"topic": "короткое описание текущей темы беседы на русском", '
        f'"messages": ["сообщение 1", "сообщение 2", "сообщение 3", "сообщение 4", "сообщение 5"], '
        f'"phrases": ["фраза 1", "фраза 2", "фраза 3", "фраза 4", "фраза 5"]}}'
    )

    models_to_try = [GROQ_MODEL, "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    content = None
    last_error = ""
    for model_name in models_to_try:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Контекст чата:\n{context}"},
                    ],
                    "max_tokens": 800,
                    "temperature": 0.8,
                },
                timeout=30,
            )
            if r.status_code == 200:
                content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                break
            elif r.status_code == 404:
                last_error = f"{model_name} not found"
                continue
            else:
                last_error = f"HTTP {r.status_code}: {r.text[:150]}"
                break
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:150]}"

    if not content:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (last_error, time.time(), task_id))
        return
    m = re.search(r'\{[\s\S]*\}', content)
    if not m:
        execute("UPDATE chat_tasks SET status='failed', error='Groq вернул не-JSON', completed_at=? WHERE id=?",
                (time.time(), task_id))
        return
    try:
        data = json.loads(m.group(0))
    except Exception as e:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (f"JSON parse: {e}", time.time(), task_id))
        return
    result = {"topic": data.get("topic", ""),
              "messages": data.get("messages", [])[:5],
              "phrases": data.get("phrases", [])[:5]}
    execute("UPDATE chat_tasks SET status='completed', result=?, completed_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), time.time(), task_id))
    print(f"[AI] ✅ Task #{task_id} ({chat_name})")


@app.post("/api/chat/request")
async def chat_request(body: ChatTaskIn):
    if not body.chat:
        raise HTTPException(status_code=400, detail="chat required")
    p = ph()
    execute(f"INSERT INTO chat_tasks (chat, status, created_at) VALUES ({p}, 'pending', {p})",
            (body.chat, time.time()))
    task = query_one("SELECT id FROM chat_tasks WHERE chat=? AND status='pending' ORDER BY id DESC LIMIT 1",
                     (body.chat,))
    return {"ok": True, "task_id": task["id"] if task else None}


@app.get("/api/chat/task/{task_id}")
async def chat_task_get(task_id: int):
    task = query_one("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="not found")
    result = None
    if task.get("result"):
        try: result = json.loads(task["result"])
        except: pass
    return {"id": task["id"], "chat": task["chat"], "status": task["status"],
            "result": result, "error": task.get("error"),
            "created_at": task.get("created_at"), "completed_at": task.get("completed_at")}


@app.get("/api/chat/latest")
async def chat_latest(chat: Optional[str] = None):
    q = "SELECT * FROM chat_tasks WHERE status='completed'"; p = []
    if chat:
        q += " AND chat=?"; p.append(chat)
    q += " ORDER BY completed_at DESC LIMIT 1"
    task = query_one(q, tuple(p))
    if not task:
        return {"task": None}
    result = None
    if task.get("result"):
        try: result = json.loads(task["result"])
        except: pass
    return {"task": {"id": task["id"], "chat": task["chat"], "result": result,
                     "completed_at": task["completed_at"]}}


# ==================== API ДЛЯ UI ====================
@app.get("/api/rates")
async def api_rates():
    with _server_rates_lock:
        return {"rates": dict(_server_rates), "updated_at": _server_rates_updated[0]}


@app.get("/api/status")
async def get_status():
    with _state_lock:
        instances = list(INSTANCE_STATES.values())
    instances.sort(key=lambda x: x.get("iid", 0))
    s = query_one("SELECT COUNT(*) AS c FROM events") or {"c": 0}
    tips = query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {"c": 0}
    rains = query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {"c": 0}
    first = query_one("SELECT MIN(ts) AS m FROM events WHERE ts IS NOT NULL") or {"m": None}
    bets_count = query_one("SELECT COUNT(*) AS c FROM bets") or {"c": 0}
    intents_count = query_one("SELECT COUNT(*) AS c FROM intents") or {"c": 0}
    return {"first_ts": first.get("m"), "total": s.get("c", 0), "tips": tips.get("c", 0),
            "rains": rains.get("c", 0), "bets": bets_count.get("c", 0),
            "intents": intents_count.get("c", 0), "instances": instances}


@app.post("/api/screenshot/{iid}")
async def request_screenshot(iid: int):
    with _state_lock:
        SCREENSHOT_REQUESTS[iid] = {"requested_at": time.time(), "done": False, "path": None}
    return {"ok": True}


@app.get("/api/screenshot/{iid}")
async def get_screenshot(iid: int):
    with _state_lock:
        r = SCREENSHOT_REQUESTS.get(iid)
    if not r or not r.get("done"):
        return JSONResponse(status_code=202, content={"pending": True})
    path = r.get("path")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not_available")
    return FileResponse(path, media_type="image/png")


@app.get("/api/events")
async def api_events(since: Optional[float] = None, until: Optional[float] = None,
                     type: Optional[str] = None, chat: Optional[str] = None,
                     sender: Optional[str] = None, min_rub: Optional[float] = None, limit: int = 500):
    q = "SELECT * FROM events WHERE 1=1"; p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if type: q += " AND type = ?"; p.append(type)
    if chat: q += " AND chat = ?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_rub is not None: q += " AND amount_rub >= ?"; p.append(min_rub)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
        # добавляем per-person
        rc = len(r["receivers"]) or 1
        r["per_person_rub"] = (r.get("amount_rub") or 0) / rc
    return {"events": rows}


@app.get("/api/chart")
async def api_chart(type: Optional[str] = None, chat: Optional[str] = None,
                    since: Optional[float] = None, until: Optional[float] = None):
    hour_expr = "CAST(EXTRACT(HOUR FROM to_timestamp(ts)) AS INTEGER)" if USE_POSTGRES else "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER)"
    q = f"SELECT {hour_expr} AS h, COUNT(*) AS c, COALESCE(SUM(amount_rub),0) AS s FROM events WHERE ts IS NOT NULL"
    p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if type: q += " AND type = ?"; p.append(type)
    if chat: q += " AND chat = ?"; p.append(chat)
    q += " GROUP BY h"
    rows = query_all(q, tuple(p))
    buckets = {h: {"count": 0, "sum": 0.0} for h in range(24)}
    for r in rows:
        if r.get("h") is not None:
            buckets[int(r["h"])] = {"count": r["c"], "sum": r["s"] or 0}
    return {"buckets": buckets}


@app.get("/api/days")
async def api_days():
    day_expr = "to_char(to_timestamp(ts), 'YYYY-MM-DD')" if USE_POSTGRES else "strftime('%Y-%m-%d', ts, 'unixepoch')"
    rows = query_all(f"""SELECT {day_expr} AS day, COUNT(*) AS cnt,
                         SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
                         SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips
                         FROM events WHERE ts IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 90""")
    import hashlib
    out = []
    for r in rows:
        h = hashlib.md5(f"{r['day']}|{r['cnt']}".encode()).hexdigest()[:6]
        out.append({"day": r["day"], "count": r["cnt"], "rains": r["rains"], "tips": r["tips"], "hash": h})
    return {"days": out}


@app.get("/api/chats")
async def api_chats():
    rows = query_all("SELECT DISTINCT chat FROM events WHERE chat IS NOT NULL ORDER BY chat")
    return {"chats": [r["chat"] for r in rows]}


@app.get("/api/contexts")
async def api_contexts(chat: Optional[str] = None, limit: int = 30):
    q = "SELECT id, ts, created_at, chat, sender, receivers, amount_text, amount_rub, amount_usd, crypto_symbol, context_messages, context_bets FROM events WHERE type='rain'"
    p = []
    if chat: q += " AND chat = ?"; p.append(chat)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        for k in ("receivers", "context_messages", "context_bets"):
            try: r[k] = json.loads(r.get(k) or "[]")
            except: r[k] = []
    return {"contexts": rows}


@app.get("/api/intents")
async def api_intents(chat: Optional[str] = None, sender: Optional[str] = None,
                      only_matched: Optional[int] = None, limit: int = 100):
    q = "SELECT * FROM intents WHERE 1=1"; p = []
    if chat: q += " AND chat = ?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if only_matched == 1: q += " AND matched_rain_id IS NOT NULL"
    if only_matched == 0: q += " AND matched_rain_id IS NULL"
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    return {"intents": query_all(q, tuple(p))}


@app.get("/api/intents_stats")
async def api_intents_stats():
    if USE_POSTGRES:
        rows = query_all("""SELECT sender AS nickname, COUNT(*) AS total,
                            SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS matched,
                            ROUND(AVG(percent),0) AS avg_percent,
                            ROUND(CAST(SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS NUMERIC)
                                  / CAST(COUNT(*) AS NUMERIC) * 100, 1) AS percent
                            FROM intents GROUP BY sender HAVING COUNT(*) >= 2
                            ORDER BY percent DESC, matched DESC LIMIT 50""")
    else:
        rows = query_all("""SELECT sender AS nickname, COUNT(*) AS total,
                            SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS matched,
                            ROUND(AVG(percent),0) AS avg_percent,
                            ROUND(CAST(SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS REAL)
                                  / CAST(COUNT(*) AS REAL) * 100, 1) AS percent
                            FROM intents GROUP BY sender HAVING COUNT(*) >= 2
                            ORDER BY percent DESC, matched DESC LIMIT 50""")
    return {"stats": rows}


@app.get("/api/bulk")
async def api_bulk(since: Optional[float] = None, until: Optional[float] = None,
                   senders: Optional[str] = None, limit: int = 500):
    q = "SELECT * FROM bulk_alerts WHERE 1=1"; p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if senders:
        names = [s.strip() for s in senders.split(",") if s.strip()]
        if names:
            phs = ",".join(["?"] * len(names))
            q += f" AND sender IN ({phs})"; p.extend(names)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
    return {"alerts": rows}


@app.get("/api/player")
async def api_player(username: Optional[str] = None, chat: Optional[str] = None,
                     since: Optional[float] = None, limit: int = 500):
    q = "SELECT * FROM player_activity WHERE 1=1"; p = []
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if chat: q += " AND chat = ?"; p.append(chat)
    if since is not None: q += " AND ts >= ?"; p.append(since)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    hour_expr = "CAST(EXTRACT(HOUR FROM to_timestamp(ts)) AS INTEGER)" if USE_POSTGRES else "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER)"
    hq = f"SELECT {hour_expr} AS h, COUNT(*) AS c FROM player_activity WHERE 1=1"; hp = []
    if username: hq += " AND username LIKE ?"; hp.append(f"%{username}%")
    if chat: hq += " AND chat = ?"; hp.append(chat)
    hq += " GROUP BY h"
    hrows = query_all(hq, tuple(hp))
    hourly = {h: 0 for h in range(24)}
    for r in hrows:
        if r.get("h") is not None:
            hourly[int(r["h"])] = r["c"]
    return {"rows": rows, "hourly": hourly}


@app.get("/api/player_names")
async def api_player_names():
    rows = query_all("SELECT DISTINCT username FROM player_activity ORDER BY username")
    return {"names": [r["username"] for r in rows]}


@app.get("/api/players")
async def api_players(chat: Optional[str] = None, username: Optional[str] = None,
                      online_only: Optional[int] = None, min_msgs: Optional[int] = None,
                      limit: int = 500):
    q = "SELECT chat, username, msg_count, first_seen, last_seen FROM chat_players WHERE 1=1"
    p = []
    if chat: q += " AND chat = ?"; p.append(chat)
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if min_msgs is not None: q += " AND msg_count >= ?"; p.append(min_msgs)
    if online_only == 1: q += " AND last_seen >= ?"; p.append(time.time() - 300)
    q += " ORDER BY last_seen DESC NULLS LAST, msg_count DESC LIMIT ?" if USE_POSTGRES else " ORDER BY last_seen DESC, msg_count DESC LIMIT ?"
    p.append(limit)
    return {"players": query_all(q, tuple(p))}


@app.get("/api/players_by_chat")
async def api_players_by_chat():
    cutoff = time.time() - 300
    rows = query_all("""SELECT chat, COUNT(*) AS total_players,
                        SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS online_players,
                        SUM(msg_count) AS total_messages
                        FROM chat_players GROUP BY chat ORDER BY total_players DESC""", (cutoff,))
    return {"chats": rows}


@app.get("/api/players_by_language")
async def api_players_by_language():
    """Распределение игроков по языковым чатам."""
    rows = query_all("""SELECT username,
                        GROUP_CONCAT(chat) AS chats_raw,
                        COUNT(DISTINCT chat) AS chat_count,
                        SUM(msg_count) AS total_msgs,
                        MAX(last_seen) AS last_seen
                        FROM chat_players GROUP BY username
                        ORDER BY total_msgs DESC LIMIT 2000""")
    out = []
    for r in rows:
        chats = list(set((r.get("chats_raw") or "").split(",")))
        r["chats"] = [c.strip() for c in chats if c.strip()]
        r.pop("chats_raw", None)
        out.append(r)
    return {"players": out}


@app.get("/api/bets_to_rains")
async def api_bets_to_rains(window_sec: int = BET_TO_RAIN_WINDOW_SEC):
    """
    Статистика: у кого после выигрышной ставки был дождь в течение N секунд.
    """
    since = time.time() - 7 * 86400
    bets = query_all("""SELECT id, ts, sender FROM bets
                        WHERE ts >= ? AND COALESCE(amount_usd,0) >= 20
                        ORDER BY ts ASC""", (since,))
    rains = query_all("""SELECT ts, sender, amount_rub FROM events
                        WHERE type='rain' AND ts >= ? AND ts IS NOT NULL
                        ORDER BY ts ASC""", (since,))
    # индексируем дожди по времени
    rain_list = sorted([(r["ts"], r["sender"], r.get("amount_rub") or 0) for r in rains if r.get("ts")])
    stats = defaultdict(lambda: {"bets": 0, "triggered": 0, "total_rain_rub": 0.0})
    for b in bets:
        s = (b.get("sender") or "").strip()
        if not s: continue
        stats[s]["bets"] += 1
        t0 = b["ts"]
        # ищем ближайший rain в течение window
        for rt, rs, rr in rain_list:
            if rt < t0: continue
            if rt > t0 + window_sec: break
            # сам игрок или любой другой раздал
            stats[s]["triggered"] += 1
            stats[s]["total_rain_rub"] += rr
            break
    arr = []
    for nick, d in stats.items():
        if d["bets"] < 3: continue
        pct = (d["triggered"] / d["bets"]) * 100 if d["bets"] else 0
        arr.append({"nickname": nick, "bets": d["bets"], "triggered": d["triggered"],
                    "percent": round(pct, 1), "rain_rub": round(d["total_rain_rub"], 2)})
    arr.sort(key=lambda x: (-x["percent"], -x["bets"]))
    return {"stats": arr[:100], "window_sec": window_sec}


@app.get("/api/players_stats")
async def api_players_stats(chat: Optional[str] = None, limit: int = 50):
    q_base = "SELECT chat, username, msg_count, first_seen, last_seen FROM chat_players WHERE 1=1"
    p = []
    if chat:
        q_base += " AND chat = ?"; p.append(chat)
    q_base += " ORDER BY msg_count DESC LIMIT ?"
    p.append(limit)
    top_by_msgs = query_all(q_base, tuple(p))
    q_online = "SELECT chat, username, msg_count, first_seen, last_seen FROM chat_players WHERE last_seen >= ?"
    p2 = [time.time() - 300]
    if chat:
        q_online += " AND chat = ?"; p2.append(chat)
    q_online += " ORDER BY last_seen DESC LIMIT ?"
    p2.append(limit)
    recent_online = query_all(q_online, tuple(p2))
    return {"top_by_msgs": top_by_msgs, "recent_online": recent_online}


@app.get("/api/bets")
async def api_bets(since: Optional[float] = None, until: Optional[float] = None,
                   chat: Optional[str] = None, sender: Optional[str] = None,
                   min_usd: Optional[float] = None, limit: int = 500):
    q = "SELECT * FROM bets WHERE 1=1"; p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if chat: q += " AND chat = ?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_usd is not None: q += " AND amount_usd >= ?"; p.append(min_usd)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    return {"bets": query_all(q, tuple(p))}


@app.get("/api/bets_stats")
async def api_bets_stats():
    since = time.time() - 86400
    rows = query_all("""SELECT chat, COUNT(*) AS cnt, COALESCE(SUM(amount_usd),0) AS total_usd,
                        COALESCE(AVG(amount_usd),0) AS avg_usd
                        FROM bets WHERE ts >= ? GROUP BY chat ORDER BY total_usd DESC""", (since,))
    return {"stats": rows}


@app.get("/api/bets_top_senders")
async def api_bets_top_senders():
    since = time.time() - 7 * 86400
    rows = query_all("""SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_usd),0) AS total_usd
                        FROM bets WHERE ts >= ? GROUP BY sender ORDER BY total_usd DESC LIMIT 30""", (since,))
    return {"senders": rows}


@app.get("/api/health")
async def health():
    return {"ok": True, "groq": bool(GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"))}


# ==================== СТАРЫЕ API ====================
@app.get("/api/stats")
async def get_stats():
    total_rains = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {}).get("c", 0)
    total_tips = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {}).get("c", 0)
    total_rain_rub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'") or {}).get("s", 0)
    total_tip_rub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'") or {}).get("s", 0)
    top_amount = query_all("""SELECT sender AS nickname, COUNT(*) AS wins, COALESCE(SUM(amount_rub),0) AS total,
                              COALESCE(AVG(amount_rub),0) AS avg FROM events WHERE type='rain'
                              GROUP BY sender ORDER BY total DESC LIMIT 100""")
    top_wins = query_all("""SELECT sender AS nickname, COUNT(*) AS wins, COALESCE(SUM(amount_rub),0) AS total,
                            COALESCE(AVG(amount_rub),0) AS avg FROM events WHERE type='rain'
                            GROUP BY sender ORDER BY wins DESC LIMIT 100""")
    top_tips = query_all("""SELECT sender AS nickname, COUNT(*) AS tips_sent, COALESCE(SUM(amount_rub),0) AS total
                            FROM events WHERE type='tip' GROUP BY sender ORDER BY total DESC LIMIT 100""")
    top_tip_receivers = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in query_all("SELECT receivers, amount_rub FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            for r in rcs:
                top_tip_receivers[r]["count"] += 1
                top_tip_receivers[r]["total"] += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass
    top_tip_recv = sorted([{"nickname": k, "tips_received": v["count"], "total": v["total"]}
                           for k, v in top_tip_receivers.items()],
                          key=lambda x: x["total"], reverse=True)[:100]
    recent = query_all("""SELECT type, chat, sender, receivers, amount_text, crypto_symbol, amount_rub, amount_usd, created_at, ts
                          FROM events ORDER BY id DESC LIMIT 30""")
    for r in recent:
        try:
            rcs = json.loads(r.get("receivers") or "[]")
        except:
            rcs = []
        r["receivers"] = rcs
        r["per_person_rub"] = (r.get("amount_rub") or 0) / max(len(rcs), 1)
    channels = query_all("""SELECT chat, SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
                            SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips,
                            COALESCE(SUM(amount_rub),0) AS total_rub
                            FROM events GROUP BY chat ORDER BY total_rub DESC""")
    winners = set()
    for row in query_all("SELECT receivers FROM events WHERE type='rain'"):
        try:
            for r in json.loads(row["receivers"] or "[]"): winners.add(r)
        except: pass
    return {"totals": {"rains": total_rains, "tips": total_tips,
                       "rain_rub": round(total_rain_rub or 0, 2),
                       "tip_rub": round(total_tip_rub or 0, 2),
                       "unique_winners": len(winners)},
            "top_amount": top_amount, "top_wins": top_wins, "top_tips": top_tips,
            "top_tip_receivers": top_tip_recv, "channels": channels, "recent": recent,
            "generated_at": datetime.now(timezone.utc).isoformat()}


@app.post("/api/clear")
async def clear_database(body: dict, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if body.get("confirm") != "DELETE_ALL":
        raise HTTPException(status_code=400, detail="Подтверждение обязательно")
    if USE_POSTGRES:
        execute("TRUNCATE TABLE events RESTART IDENTITY CASCADE;")
    else:
        execute("DELETE FROM events;")
    return {"ok": True, "message": "Все данные удалены"}


@app.get("/api/user/{nickname}")
async def get_user(nickname: str):
    sent = query_one("SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='rain' AND sender=?", (nickname,)) or {"cnt": 0, "total": 0}
    won_cnt, won_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='rain'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                won_cnt += 1
                won_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass
    tips_sent = query_one("SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='tip' AND sender=?", (nickname,)) or {"cnt": 0, "total": 0}
    tr_cnt, tr_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                tr_cnt += 1
                tr_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass
    return {"nickname": nickname,
            "given_rains": {"count": sent["cnt"], "total_rub": round(sent["total"], 2)},
            "received_rains": {"count": won_cnt, "total_rub": round(won_total, 2)},
            "tips_sent": {"count": tips_sent["cnt"], "total_rub": round(tips_sent["total"], 2)},
            "tips_received": {"count": tr_cnt, "total_rub": round(tr_total, 2)}}


# ==================== MONITOR DASHBOARD ====================
MONITOR_HTML = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shuffle Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e6e6;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;background:#171a21;border-bottom:1px solid #2a2f3a;position:sticky;top:0;z-index:10;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
header a{color:#7cc4ff;text-decoration:none;font-size:13px}
.uptime{color:#8ab4f8;font-size:13px}
.tabs{display:flex;gap:4px;padding:10px 20px;border-bottom:1px solid #2a2f3a;background:#12151b;flex-wrap:wrap}
.tabs button{background:transparent;color:#aab;border:1px solid transparent;padding:7px 14px;border-radius:6px;cursor:pointer;font-size:13px}
.tabs button.active{background:#202633;color:#fff;border-color:#2f3d55}
main{padding:20px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #20242e}
th{color:#8b93a7;font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
tr:hover td{background:#161a22}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:#232a37;color:#aab}
.pill.ok{background:#1e3a26;color:#71d68a}
.pill.warn{background:#3a3520;color:#e0c66a}
.pill.err{background:#3a1f1f;color:#e07a7a}
.btn{background:#2a3340;color:#e6e6e6;border:1px solid #334055;padding:5px 10px;border-radius:5px;cursor:pointer;font-size:12px}
.btn:hover{background:#334055}
.row-flex{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input,select{background:#171a21;color:#e6e6e6;border:1px solid #2a2f3a;padding:6px 10px;border-radius:5px;font-size:13px;max-width:220px}
.chart-wrap{background:#151821;border:1px solid #232a37;border-radius:8px;padding:16px;margin-bottom:16px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:50}
.modal img{max-width:92vw;max-height:92vh;border-radius:8px;border:1px solid #333}
.modal.on{display:flex}
.hint{color:#8b93a7;font-size:12px}
.hint-btn{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;
  background:#232a37;color:#8ab4f8;font-size:12px;cursor:help;margin-left:6px;font-weight:bold;
  position:relative}
.hint-btn:hover::after{content:attr(data-hint);position:absolute;top:24px;left:-200px;width:420px;
  background:#1a1f28;color:#e6e6e6;border:1px solid #2f3d55;border-radius:8px;padding:12px;
  font-size:12px;font-weight:normal;line-height:1.5;z-index:100;white-space:pre-wrap;
  box-shadow:0 4px 20px rgba(0,0,0,.5)}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.kpi div{background:#151821;border:1px solid #232a37;border-radius:8px;padding:10px 16px;min-width:140px}
.kpi b{display:block;font-size:20px;color:#8ab4f8;margin-top:2px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.chip{background:#2a3340;color:#e6e6e6;padding:4px 8px;border-radius:14px;font-size:12px;display:inline-flex;align-items:center;gap:6px}
.chip .x{color:#f87171;cursor:pointer;font-weight:bold;padding:0 2px}
.player-tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px;border-bottom:1px solid #2a2f3a;padding-bottom:8px}
.player-tab{background:#1a1f28;border:1px solid #2a2f3a;color:#aab;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.player-tab.active{background:#202633;color:#fff;border-color:#2f3d55}
.player-tab .rm{color:#f87171;margin-left:8px;cursor:pointer;font-weight:bold}
.bet-row{color:#71d68a}
.promo-card{background:#151821;border:1px solid #232a37;border-radius:8px;padding:12px 16px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start}
.promo-card.used{opacity:0.5}
.promo-cb{width:22px;height:22px;margin-top:3px;cursor:pointer;accent-color:#71d68a}
.promo-body{flex:1}
.promo-text{white-space:pre-wrap;font-size:13px;line-height:1.5;color:#e6e6e6}
.promo-meta{font-size:11px;color:#8b93a7;margin-top:6px}
.promo-meta a{color:#7cc4ff;text-decoration:none}
.ai-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}
.ai-col{background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px}
.ai-col h3{margin:0 0 10px;font-size:12px;color:#8b93a7;text-transform:uppercase;letter-spacing:.05em;font-weight:500}
.ai-msg{padding:10px 12px;background:#1a1f28;border-left:3px solid #7cc4ff;border-radius:4px;margin-bottom:8px;font-size:13px;cursor:pointer}
.ai-msg:hover{background:#232a37}
.ai-msg.copied{border-left-color:#71d68a;background:#1e3a26}
.ai-phrase{padding:8px 12px;background:#1a1f28;border-left:3px solid #c58af9;border-radius:4px;margin-bottom:6px;font-size:13px;cursor:pointer}
.ai-phrase:hover{background:#232a37}
.ai-phrase.copied{border-left-color:#71d68a;background:#1e3a26}
.ai-topic{background:#202633;border:1px solid #2f3d55;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px}
.lang-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.lang-card{background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px}
.lang-card h4{margin:0 0 8px;font-size:13px;color:#8ab4f8}
.lang-card .player-row{display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #1e232e}
</style></head><body>
<header>
  <h1>🎰 Shuffle Monitor</h1>
  <span class="uptime" id="uptime">—</span>
  <a href="/" style="margin-left:16px">← На сайт статистики</a>
  <span style="margin-left:auto" class="pill" id="kpiTotal">—</span>
</header>
<div class="tabs">
  <button data-tab="status" class="active">Вкладки</button>
  <button data-tab="events">События</button>
  <button data-tab="chart">График</button>
  <button data-tab="bulk">Bulk-раздачи</button>
  <button data-tab="bets">Ставки</button>
  <button data-tab="betsrains">Ставки→Дожди</button>
  <button data-tab="player">Активность</button>
  <button data-tab="contexts">Контексты дождей</button>
  <button data-tab="intents">Интенты</button>
  <button data-tab="promos">Промокоды</button>
  <button data-tab="ai">🤖 AI Ассистент</button>
  <button data-tab="players">👥 Игроки</button>
  <button data-tab="langs">🌍 Языки</button>
</div>
<main>
  <div id="tab-status">
    <div class="kpi" id="kpiRow"></div>
    <table><thead><tr><th>#</th><th>Режим</th><th>Чат</th><th>Статус</th><th>Прогресс</th><th>Обновлено</th><th>📸</th></tr></thead>
    <tbody id="instBody"></tbody></table>
  </div>

  <div id="tab-events" style="display:none">
    <div class="row-flex">
      <input type="text" id="fSender" placeholder="Отправитель">
      <select id="fChat"><option value="">Все чаты</option></select>
      <select id="fType"><option value="">Все типы</option><option value="tip">Tip</option><option value="rain">Rain</option></select>
      <input type="number" id="fMinRub" placeholder="Мин. ₽">
      <select id="fDay"><option value="">Все дни</option></select>
      <button class="btn" onclick="loadEvents()">Фильтр</button>
      <button class="btn" onclick="resetEvents()">Сброс</button>
    </div>
    <table><thead><tr><th>Время</th><th>Тип</th><th>Чат</th><th>От</th><th>Кому</th><th>Сумма</th><th>₽</th><th>₽/чел</th><th>$</th></tr></thead>
    <tbody id="evBody"></tbody></table>
  </div>

  <div id="tab-chart" style="display:none">
    <div class="row-flex">
      <select id="cType"><option value="">Всё</option><option value="tip">Tip</option><option value="rain">Rain</option></select>
      <select id="cChat"><option value="">Все чаты</option></select>
      <button class="btn" onclick="loadChart()">Обновить</button>
    </div>
    <div class="chart-wrap"><canvas id="hourCount" height="90"></canvas></div>
    <div class="chart-wrap"><canvas id="hourSum" height="90"></canvas></div>
  </div>

  <div id="tab-bulk" style="display:none">
    <div class="row-flex">
      <input type="text" id="bSenderInput" placeholder="Ник + Enter" style="width:180px">
      <button class="btn" onclick="addBulkChip()">+ Добавить</button>
      <button class="btn" onclick="clearBulkChips()">Очистить</button>
    </div>
    <div class="chips" id="bulkChips"></div>
    <table><thead><tr><th>Время</th><th>Чат</th><th>От</th><th>Сумма</th><th>₽ каждому</th><th>₽ всего</th><th>Получателей</th><th>Кому</th></tr></thead>
    <tbody id="bulkBody"></tbody></table>
  </div>

  <div id="tab-bets" style="display:none">
    <div class="row-flex">
      <input type="text" id="betSender" placeholder="Отправитель">
      <select id="betChat"><option value="">Все чаты</option></select>
      <input type="number" id="betMinUsd" placeholder="Мин. $" value="50">
      <button class="btn" onclick="loadBets()">Фильтр</button>
      <button class="btn" onclick="loadBetsStats()">📊 По чатам (24ч)</button>
      <button class="btn" onclick="loadBetsTop()">🏆 Топ (7д)</button>
    </div>
    <div id="betStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>Время</th><th>Чат</th><th>Игрок</th><th>Игра</th><th>Множитель</th><th>Сумма</th><th>$</th><th>₽</th></tr></thead>
    <tbody id="betsBody"></tbody></table>
  </div>

  <div id="tab-betsrains" style="display:none">
    <div class="row-flex">
      <input type="number" id="b2rWindow" value="900" style="width:100px">
      <span class="hint">секунд — окно после ставки</span>
      <button class="btn" onclick="loadBetsToRains()">Обновить</button>
    </div>
    <div id="b2rStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>#</th><th>Ник</th><th>Ставок ≥$20</th><th>Привели к дождю</th><th>%</th><th>Сумма дождей ₽</th></tr></thead>
    <tbody id="b2rBody"></tbody></table>
  </div>

  <div id="tab-player" style="display:none">
    <div class="row-flex">
      <input type="text" id="pNewPlayer" placeholder="Новый ник">
      <button class="btn" onclick="addPlayer()">+ Add</button>
      <select id="pChat"><option value="">Все чаты</option></select>
    </div>
    <div class="player-tabs" id="playerTabs"></div>
    <div class="chart-wrap"><canvas id="playerHour" height="60"></canvas></div>
    <table><thead><tr><th>Время</th><th>Игрок</th><th>Чат</th><th>Сообщение</th></tr></thead>
    <tbody id="plBody"></tbody></table>
  </div>

  <div id="tab-contexts" style="display:none">
    <div class="row-flex">
      <select id="ctxChat"><option value="">Все чаты</option></select>
      <input type="number" id="ctxLimit" value="30" style="width:80px">
      <button class="btn" onclick="loadContexts()">Обновить</button>
    </div>
    <div id="ctxList"></div>
  </div>

  <div id="tab-intents" style="display:none">
    <div class="row-flex">
      <select id="intChat"><option value="">Все чаты</option></select>
      <input type="text" id="intSender" placeholder="Игрок">
      <select id="intOnly">
        <option value="">Все</option>
        <option value="1">Сбывшиеся</option>
        <option value="0">Несбывшиеся</option>
      </select>
      <button class="btn" onclick="loadIntents()">Фильтр</button>
      <button class="btn" onclick="loadIntentsStats()">🏆 Рейтинг</button>
    </div>
    <div id="intStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>Время</th><th>Чат</th><th>Игрок</th><th>Текст</th><th>AI%</th><th>Статус</th><th>Δ</th></tr></thead>
    <tbody id="intBody"></tbody></table>
  </div>

  <div id="tab-promos" style="display:none">
    <div class="row-flex">
      <select id="promoFilter">
        <option value="">Все</option>
        <option value="0">Неиспользованные</option>
        <option value="1">Использованные</option>
      </select>
      <button class="btn" onclick="loadPromos()">Обновить</button>
      <button class="btn" onclick="testPromo()">🔍 Проверить сейчас</button>
      <span class="hint" id="promoStats"></span>
    </div>
    <div id="promoList"></div>
  </div>

  <div id="tab-ai" style="display:none">
    <div class="row-flex">
      <select id="aiChat"><option value="">Выбери чат</option></select>
      <button class="btn" onclick="runAI()">🤖 Получить сообщения</button>
      <button class="btn" onclick="loadAILatest()">📋 Последний результат</button>
      <span class="hint" id="aiStatus"></span>
    </div>
    <div id="aiTopic"></div>
    <div id="aiResult"></div>
  </div>

  <div id="tab-players" style="display:none">
    <div class="row-flex">
      <select id="plChatFilter"><option value="">Все чаты</option></select>
      <input type="text" id="plUserFilter" placeholder="Ник">
      <label style="color:#aab;font-size:12px">
        <input type="checkbox" id="plOnlineOnly"> Только онлайн
      </label>
      <input type="number" id="plMinMsgs" placeholder="Мин. сообщений" style="width:140px">
      <button class="btn" onclick="loadPlayersTab()">Обновить</button>
    </div>
    <div id="plChatsSummary" style="margin-bottom:16px"></div>
    <table><thead><tr>
      <th>Чат</th><th>Игрок</th><th>Сообщений</th><th>Первый раз</th><th>Последний раз</th><th>Статус</th>
    </tr></thead>
    <tbody id="plPlayersBody"></tbody></table>
  </div>

  <div id="tab-langs" style="display:none">
    <div class="row-flex">
      <input type="text" id="langFilter" placeholder="Поиск по нику">
      <button class="btn" onclick="loadLanguages()">Обновить</button>
      <span class="hint">Показывает где игрок активен (EN/RU/…) и сколько сообщений в каждом чате.</span>
    </div>
    <div id="langGrid" class="lang-grid"></div>
  </div>
</main>

<div class="modal" id="imgModal" onclick="this.classList.remove('on')">
  <img id="imgModalImg" src="">
</div>

<script>
const $ = id => document.getElementById(id);
const fmtTime = ts => new Date(ts*1000).toLocaleString('ru-RU');
const fmtMoney = v => (v||0).toLocaleString('ru-RU',{maximumFractionDigits:2});
const esc = s => (s==null?'':String(s)).replace(/[<>&"]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

let bulkChips = [];
let currentPlayer = null;
let aiPollTimer = null;

function switchTab(name){
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  ['status','events','chart','bulk','bets','betsrains','player','contexts','intents','promos','ai','players','langs'].forEach(t=>{
    const el = $('tab-'+t); if(el) el.style.display = (t===name) ? '' : 'none';
  });
  if(name==='events') { loadDays(); loadEvents(); }
  if(name==='chart') loadChart();
  if(name==='bulk') loadBulk();
  if(name==='bets') { loadChats(); loadBets(); }
  if(name==='betsrains') loadBetsToRains();
  if(name==='player') loadPlayers();
  if(name==='contexts') { loadChats(); loadContexts(); }
  if(name==='intents') { loadChats(); loadIntents(); }
  if(name==='promos') loadPromos();
  if(name==='ai') { loadChats(); loadAILatest(); }
  if(name==='players') { loadChats(); loadPlayersTab(); }
  if(name==='langs') loadLanguages();
}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

async function refreshStatus(){
  try {
    const r = await fetch('/api/status').then(r=>r.json());
    $('uptime').textContent = r.first_ts ? 'Мониторинг с ' + new Date(r.first_ts*1000).toLocaleString('ru-RU') : 'Мониторинг только запущен';
    $('kpiTotal').textContent = `Всего: ${r.total||0} | Tips: ${r.tips||0} | Rains: ${r.rains||0} | Bets: ${r.bets||0} | Intents: ${r.intents||0}`;
    const now = Date.now()/1000;
    $('kpiRow').innerHTML = `
      <div>Всего<b>${r.total||0}</b></div>
      <div>Tips<b>${r.tips||0}</b></div>
      <div>Rains<b>${r.rains||0}</b></div>
      <div>Bets<b>${r.bets||0}</b></div>
      <div>Intents<b>${r.intents||0}</b></div>
      <div>Онлайн<b>${(r.instances||[]).filter(i=>now - (i.updated_at||0) < 40).length}</b></div>`;
    const body = $('instBody'); body.innerHTML = '';
    (r.instances||[]).forEach(i=>{
      const online = now - (i.updated_at||0) < 40;
      let cls='pill', txt=i.status||'—';
      if(!online){ cls+=' err'; txt='offline'; }
      else if(i.status==='ok') cls+=' ok';
      else if(i.status==='waiting'||i.status==='rotating') cls+=' warn';
      else if(['error','timeout','verify_error','chat_not_loaded'].includes(i.status)) cls+=' err';
      const progress = i.threshold ? `${i.new_count||0}/${i.threshold}` : '—';
      const upd = i.updated_at ? Math.round(now - i.updated_at)+'с' : '—';
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${i.iid}</td><td>${esc(i.mode||'')}</td>
        <td><b>${esc(i.current_chat||'—')}</b><br><span class="pill">${(i.chats||[]).map(esc).join(', ')}</span></td>
        <td><span class="${cls}">${esc(txt)}</span>${i.error?'<br><span class="hint">'+esc(i.error)+'</span>':''}</td>
        <td>${progress}</td><td>${upd}</td>
        <td><button class="btn" onclick="reqShot(${i.iid})">📸</button></td>`;
      body.appendChild(tr);
    });
  } catch(e){ console.error(e); }
}

async function reqShot(iid){
  await fetch('/api/screenshot/'+iid, {method:'POST'});
  let tries=0;
  while(tries++ < 40){
    await new Promise(r=>setTimeout(r,1500));
    const r = await fetch('/api/screenshot/'+iid);
    if(r.status === 200){
      $('imgModalImg').src = URL.createObjectURL(await r.blob());
      $('imgModal').classList.add('on');
      return;
    }
    if(r.status === 404){ alert('Не удалось'); return; }
  }
  alert('Таймаут');
}

async function loadChats(){
  const r = await fetch('/api/chats').then(r=>r.json());
  ['fChat','cChat','pChat','betChat','ctxChat','intChat','aiChat','plChatFilter'].forEach(id=>{
    const sel = $(id); if(!sel) return;
    const cur = sel.value;
    const placeholder = id === 'aiChat' ? '<option value="">Выбери чат</option>' : '<option value="">Все чаты</option>';
    sel.innerHTML = placeholder + r.chats.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    sel.value = cur;
  });
}

async function loadDays(){
  const r = await fetch('/api/days').then(r=>r.json());
  const sel = $('fDay');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Все дни</option>';
  r.days.forEach(d=>{ sel.innerHTML += `<option value="${d.day}">${d.day} (${d.count}) — #${d.hash}</option>`; });
  sel.value = cur;
}

async function loadEvents(){
  const p = new URLSearchParams();
  if($('fSender').value) p.set('sender',$('fSender').value);
  if($('fChat').value) p.set('chat',$('fChat').value);
  if($('fType').value) p.set('type',$('fType').value);
  if($('fMinRub').value) p.set('min_rub',$('fMinRub').value);
  const day = $('fDay').value;
  if(day){
    const start = Math.floor(new Date(day+'T00:00:00').getTime()/1000);
    p.set('since', start); p.set('until', start + 86400);
  }
  const r = await fetch('/api/events?'+p).then(r=>r.json());
  const body = $('evBody'); body.innerHTML = '';
  r.events.forEach(e=>{
    const tr = document.createElement('tr');
    const per = e.per_person_rub || 0;
    tr.innerHTML = `<td>${e.ts?fmtTime(e.ts):esc(e.created_at)}</td>
      <td><span class="pill">${esc(e.type)}</span></td><td>${esc(e.chat)}</td>
      <td>${esc(e.sender)}</td><td>${(e.receivers||[]).map(esc).join(', ')}</td>
      <td>${esc(e.amount_text)}</td><td>${fmtMoney(e.amount_rub)}</td>
      <td style="color:#8ab4f8">${per?fmtMoney(per):'—'}</td>
      <td style="color:#71d68a">$${fmtMoney(e.amount_usd||0)}</td>`;
    body.appendChild(tr);
  });
}
function resetEvents(){
  ['fSender','fMinRub'].forEach(id=>$(id).value='');
  $('fChat').value=''; $('fType').value=''; $('fDay').value='';
  loadEvents();
}

let chart1=null, chart2=null, chartPlayer=null;
async function loadChart(){
  const p = new URLSearchParams();
  if($('cType').value) p.set('type',$('cType').value);
  if($('cChat').value) p.set('chat',$('cChat').value);
  const r = await fetch('/api/chart?'+p).then(r=>r.json());
  const labels = [...Array(24).keys()].map(h=>String(h).padStart(2,'0')+':00');
  const counts = labels.map((_,h)=>r.buckets[h]?.count||0);
  const sums   = labels.map((_,h)=>r.buckets[h]?.sum||0);
  if(chart1) chart1.destroy();
  if(chart2) chart2.destroy();
  const tl = $('cType').value ? ($('cType').value === 'tip' ? 'Tip' : 'Rain') : 'Все';
  chart1 = new Chart($('hourCount'), {type:'bar',
    data:{labels, datasets:[{label:`Кол-во (${tl})`, data:counts, backgroundColor:'#4a7ce0'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}, title:{display:true,text:`События — ${tl}`,color:'#e6e6e6'}},
             scales:{x:{ticks:{color:'#8b93a7'}}, y:{ticks:{color:'#8b93a7'}}}}});
  chart2 = new Chart($('hourSum'), {type:'bar',
    data:{labels, datasets:[{label:`Сумма ₽ (${tl})`, data:sums, backgroundColor:'#71d68a'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}, title:{display:true,text:`Сумма — ${tl}`,color:'#e6e6e6'}},
             scales:{x:{ticks:{color:'#8b93a7'}}, y:{ticks:{color:'#8b93a7'}}}}});
}

function renderBulkChips(){
  const el = $('bulkChips');
  el.innerHTML = bulkChips.length ? bulkChips.map(n =>
    `<span class="chip">${esc(n)}<span class="x" onclick="removeBulkChip('${esc(n)}')">×</span></span>`
  ).join('') : '<span class="hint">Фильтр не задан</span>';
}
function addBulkChip(){
  const v = $('bSenderInput').value.trim();
  if(!v) return;
  if(!bulkChips.includes(v)) bulkChips.push(v);
  $('bSenderInput').value='';
  renderBulkChips(); loadBulk();
}
function removeBulkChip(n){ bulkChips = bulkChips.filter(x=>x!==n); renderBulkChips(); loadBulk(); }
function clearBulkChips(){ bulkChips = []; renderBulkChips(); loadBulk(); }
$('bSenderInput') && $('bSenderInput').addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); addBulkChip(); }});

async function loadBulk(){
  const p = new URLSearchParams();
  if(bulkChips.length) p.set('senders', bulkChips.join(','));
  const r = await fetch('/api/bulk?'+p).then(r=>r.json());
  const body = $('bulkBody'); body.innerHTML='';
  if(!r.alerts.length){ body.innerHTML = '<tr><td colspan="8" class="hint">Нет данных</td></tr>'; return; }
  r.alerts.forEach(a=>{
    const recCount = (a.receivers||[]).length;
    const rubEach = a.amount_rub || 0;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtTime(a.ts)}</td><td>${esc(a.chat)}</td><td>${esc(a.sender)}</td>
      <td>${esc(a.amount_text)}</td><td>${fmtMoney(rubEach)} ₽</td>
      <td style="color:#71d68a;font-weight:bold">${fmtMoney(rubEach*recCount)} ₽</td>
      <td>${recCount}</td><td>${(a.receivers||[]).map(esc).join(', ')}</td>`;
    body.appendChild(tr);
  });
}

async function loadBets(){
  const p = new URLSearchParams();
  if($('betSender').value) p.set('sender',$('betSender').value);
  if($('betChat').value) p.set('chat',$('betChat').value);
  if($('betMinUsd').value) p.set('min_usd',$('betMinUsd').value);
  const r = await fetch('/api/bets?'+p).then(r=>r.json());
  const body = $('betsBody'); body.innerHTML='';
  if(!r.bets.length){ body.innerHTML = '<tr><td colspan="8" class="hint">Нет ставок</td></tr>'; return; }
  r.bets.forEach(b=>{
    const tr = document.createElement('tr'); tr.className='bet-row';
    tr.innerHTML = `<td>${fmtTime(b.ts)}</td><td>${esc(b.chat)}</td><td><b>${esc(b.sender)}</b></td>
      <td>${esc(b.game)}</td><td>${esc(b.multiplier)}</td>
      <td>${esc(b.amount_text)} ${esc(b.crypto)}</td>
      <td style="color:#71d68a;font-weight:bold">$${fmtMoney(b.amount_usd)}</td>
      <td>${fmtMoney(b.amount_rub)}</td>`;
    body.appendChild(tr);
  });
}

async function loadBetsStats(){
  const r = await fetch('/api/bets_stats').then(r=>r.json());
  const el = $('betStats');
  if(!r.stats.length){ el.innerHTML = '<p class="hint">Пусто</p>'; return; }
  el.innerHTML = '<div class="kpi">' + r.stats.map(s=>`<div>${esc(s.chat)}<b>${s.cnt} шт.</b>
    <span class="hint">$${fmtMoney(s.total_usd)} (avg $${fmtMoney(s.avg_usd)})</span></div>`).join('') + '</div>';
}
async function loadBetsTop(){
  const r = await fetch('/api/bets_top_senders').then(r=>r.json());
  const el = $('betStats');
  if(!r.senders.length){ el.innerHTML = '<p class="hint">Пусто</p>'; return; }
  el.innerHTML = '<div class="kpi">' + r.senders.map(s=>`<div>${esc(s.nickname)}<b>${s.cnt} шт.</b>
    <span class="hint">$${fmtMoney(s.total_usd)}</span></div>`).join('') + '</div>';
}

async function loadBetsToRains(){
  const w = $('b2rWindow').value || 900;
  const r = await fetch('/api/bets_to_rains?window_sec='+w).then(r=>r.json());
  const body = $('b2rBody'); body.innerHTML='';
  $('b2rStats').innerHTML = `<div class="kpi">
    <div>Окно<b>${r.window_sec}с</b></div>
    <div>Всего игроков<b>${r.stats.length}</b></div>
    <div>Всего сработавших<b>${r.stats.reduce((a,x)=>a+x.triggered,0)}</b></div>
  </div>`;
  if(!r.stats.length){ body.innerHTML = '<tr><td colspan="6" class="hint">Нет данных</td></tr>'; return; }
  r.stats.forEach((s, i)=>{
    const cls = s.percent >= 50 ? 'ok' : (s.percent >= 25 ? 'warn' : 'err');
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${i+1}</td><td><b>${esc(s.nickname)}</b></td>
      <td>${s.bets}</td><td>${s.triggered}</td>
      <td><span class="pill ${cls}">${s.percent}%</span></td>
      <td>${fmtMoney(s.rain_rub)}</td>`;
    body.appendChild(tr);
  });
}

async function loadPlayers(){
  const wl = await fetch('/api/watchlist').then(r=>r.json());
  const names = wl.players || [];
  const el = $('playerTabs');
  if(!names.length){ el.innerHTML = '<span class="hint">Пусто</span>'; $('plBody').innerHTML = ''; return; }
  if(!currentPlayer || !names.includes(currentPlayer)) currentPlayer = names[0];
  el.innerHTML = names.map(n =>
    `<div class="player-tab ${n===currentPlayer?'active':''}" onclick="selectPlayer('${esc(n)}')">
      ${esc(n)} <span class="rm" onclick="event.stopPropagation();removePlayer('${esc(n)}')">×</span></div>`
  ).join('');
  loadPlayerActivity();
}
async function selectPlayer(n){ currentPlayer = n; await loadPlayers(); }
async function addPlayer(){
  const v = $('pNewPlayer').value.trim(); if(!v) return;
  await fetch('/api/watchlist/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:v})});
  $('pNewPlayer').value=''; currentPlayer = v; await loadPlayers();
}
async function removePlayer(name){
  if(!confirm(`Удалить ${name}?`)) return;
  await fetch('/api/watchlist/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  if(currentPlayer === name) currentPlayer = null; await loadPlayers();
}
async function loadPlayerActivity(){
  if(!currentPlayer) return;
  const p = new URLSearchParams(); p.set('username', currentPlayer);
  if($('pChat').value) p.set('chat', $('pChat').value);
  const r = await fetch('/api/player?'+p).then(r=>r.json());
  const body = $('plBody'); body.innerHTML='';
  r.rows.forEach(x=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtTime(x.ts)}</td><td><b>${esc(x.username)}</b></td><td>${esc(x.chat)}</td><td>${esc(x.text)}</td>`;
    body.appendChild(tr);
  });
  const labels = [...Array(24).keys()].map(h=>String(h).padStart(2,'0')+':00');
  const vals = labels.map((_,h)=>r.hourly[h]||0);
  if(chartPlayer) chartPlayer.destroy();
  chartPlayer = new Chart($('playerHour'), {type:'bar',
    data:{labels, datasets:[{label:`Сообщений — ${currentPlayer}`, data:vals, backgroundColor:'#c58af9'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}}, scales:{x:{ticks:{color:'#8b93a7'}},y:{ticks:{color:'#8b93a7'}}}}});
}
$('pChat') && $('pChat').addEventListener('change', loadPlayerActivity);

async function loadContexts(){
  const p = new URLSearchParams();
  if($('ctxChat').value) p.set('chat',$('ctxChat').value);
  if($('ctxLimit').value) p.set('limit',$('ctxLimit').value);
  const r = await fetch('/api/contexts?'+p).then(r=>r.json());
  const el = $('ctxList');
  if(!r.contexts.length){ el.innerHTML = '<p class="hint">Нет контекстов</p>'; return; }
  el.innerHTML = r.contexts.map(c => {
    const rub = c.amount_rub ? fmtMoney(c.amount_rub) + ' ₽' : '';
    const recs = (c.receivers||[]).slice(0,10).join(', ');
    const msgs = (c.context_messages||[]).slice(-15);
    const bets = (c.context_bets||[]).slice(-5);
    const msgsHtml = msgs.length ? msgs.map((m,i)=>`<div style="padding:4px 8px;border-left:3px solid #2a2f3a;margin:4px 0;color:#aab;font-size:12px"><span style="color:#666">${i+1}.</span> ${esc(m)}</div>`).join('') : '<div class="hint">Пусто</div>';
    const betsHtml = bets.length ? bets.map(b=>`<div style="padding:4px 8px;border-left:3px solid #71d68a;margin:4px 0;color:#71d68a;font-size:12px">🎰 <b>${esc(b.sender||'?')}</b> · ${esc(b.game||'')} · ${esc(b.multiplier||'')} · ${esc(b.amount_text||'')}</div>`).join('') : '<div class="hint">Пусто</div>';
    return `<div style="background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px;margin-bottom:12px">
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
        <span class="pill">🌧 RAIN</span><b>${esc(c.chat)}</b>
        <span style="color:#8b93a7;font-size:12px">${c.ts?fmtTime(c.ts):esc(c.created_at)}</span>
        <span style="margin-left:auto;color:#71d68a;font-weight:bold">${esc(c.amount_text)} ${esc(c.crypto_symbol||'')} ${rub?'· '+rub:''}</span>
      </div>
      <div style="color:#aab;font-size:12px;margin-bottom:6px">👤 <b>${esc(c.sender)}</b> → ${(c.receivers||[]).length} получ.: ${esc(recs)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px">
        <div><div style="color:#8b93a7;font-size:11px;text-transform:uppercase;margin-bottom:6px">Сообщения до</div>${msgsHtml}</div>
        <div><div style="color:#8b93a7;font-size:11px;text-transform:uppercase;margin-bottom:6px">Ставки до</div>${betsHtml}</div>
      </div></div>`;
  }).join('');
}

async function loadIntents(){
  const p = new URLSearchParams();
  if($('intChat').value) p.set('chat',$('intChat').value);
  if($('intSender').value) p.set('sender',$('intSender').value);
  if($('intOnly').value !== '') p.set('only_matched',$('intOnly').value);
  const r = await fetch('/api/intents?'+p).then(r=>r.json());
  const body = $('intBody'); body.innerHTML='';
  if(!r.intents.length){ body.innerHTML = '<tr><td colspan="7" class="hint">Нет</td></tr>'; return; }
  r.intents.forEach(x=>{
    const matched = !!x.matched_rain_id;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtTime(x.ts)}</td><td>${esc(x.chat)}</td><td><b>${esc(x.sender)}</b></td>
      <td>${esc(x.text)}</td>
      <td><span class="pill ${x.percent>=70?'ok':'warn'}">${x.percent||100}%</span></td>
      <td><span class="pill ${matched?'ok':'err'}">${matched?'✅ сбылось':'❌ не сбылось'}</span></td>
      <td>${x.matched_delta_sec ? Math.round(x.matched_delta_sec)+'с' : '—'}</td>`;
    body.appendChild(tr);
  });
}
async function loadIntentsStats(){
  const r = await fetch('/api/intents_stats').then(r=>r.json());
  const el = $('intStats');
  if(!r.stats.length){ el.innerHTML = '<p class="hint">Нет данных</p>'; return; }
  el.innerHTML = '<div class="kpi">' + r.stats.map(s=>{
    const cls = s.percent >= 50 ? 'ok' : (s.percent >= 25 ? 'warn' : 'err');
    return `<div>${esc(s.nickname)}<b>${s.percent}%</b><span class="hint"><span class="pill ${cls}">${s.matched}/${s.total}</span> AI:${s.avg_percent||'—'}%</span></div>`;
  }).join('') + '</div>';
}

async function loadPromos(){
  const filter = $('promoFilter').value;
  const p = new URLSearchParams();
  if(filter === '0') p.set('only_unused', '1');
  const r = await fetch('/api/promocodes?'+p).then(r=>r.json());
  const stats = await fetch('/api/promocodes/stats').then(r=>r.json());
  $('promoStats').textContent = `Всего: ${stats.total} | Исп: ${stats.used} | Осталось: ${stats.unused}`;
  const el = $('promoList');
  if(!r.promos.length){ el.innerHTML = '<p class="hint">Пусто. Нажми «🔍 Проверить сейчас».</p>'; return; }
  const filtered = filter === '1' ? r.promos.filter(x=>x.used) : r.promos;
  if(!filtered.length){ el.innerHTML = '<p class="hint">Пусто по фильтру</p>'; return; }
  el.innerHTML = filtered.map(x=>`<div class="promo-card ${x.used?'used':''}">
    <input type="checkbox" class="promo-cb" ${x.used?'checked':''} onchange="togglePromo(${x.id})">
    <div class="promo-body"><div class="promo-text">${esc(x.text)}</div>
    <div class="promo-meta">${x.message_date?'📅 '+esc(x.message_date):''}${x.link?' · <a href="'+esc(x.link)+'" target="_blank">TG</a>':''}${x.used&&x.used_at?' · ✅ '+fmtTime(x.used_at):''}</div>
    </div></div>`).join('');
}
async function togglePromo(id){ await fetch('/api/promo/toggle/'+id,{method:'POST'}); loadPromos(); }
async function testPromo(){
  const btn = event.target; btn.disabled=true; btn.textContent='⏳';
  try { const r = await fetch('/api/promo/test',{method:'POST'}).then(r=>r.json());
    alert(`Найдено: ${r.found}, новых: ${r.new}`); loadPromos();
  } catch(e){ alert('Ошибка'); } finally { btn.disabled=false; btn.textContent='🔍 Проверить сейчас'; }
}

async function runAI(){
  const chat = $('aiChat').value;
  if(!chat){ alert('Выбери чат'); return; }
  const btn = event.target; btn.disabled=true; btn.textContent='⏳ Читаю чат...';
  $('aiStatus').textContent = 'Отправляю задачу...';
  $('aiTopic').innerHTML = '';
  $('aiResult').innerHTML = '';
  try {
    const r = await fetch('/api/chat/request', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({chat})}).then(r=>r.json());
    const taskId = r.task_id;
    if(!taskId){ alert('Не удалось создать задачу'); return; }
    if(aiPollTimer) clearInterval(aiPollTimer);
    let tries = 0;
    aiPollTimer = setInterval(async () => {
      tries++;
      const t = await fetch('/api/chat/task/'+taskId).then(r=>r.json());
      if(t.status === 'pending' || t.status === 'reading'){
        $('aiStatus').textContent = `Ожидаю чтения чата клиентом... (${tries*2}с)`;
      } else if(t.status === 'completed'){
        clearInterval(aiPollTimer); aiPollTimer = null;
        $('aiStatus').textContent = '✅ Готово';
        btn.disabled=false; btn.textContent='🤖 Получить сообщения';
        renderAIResult(t.result, chat);
      } else if(t.status === 'failed'){
        clearInterval(aiPollTimer); aiPollTimer = null;
        $('aiStatus').textContent = '❌ Ошибка';
        $('aiTopic').innerHTML = `<div class="ai-topic" style="color:#f87171">Ошибка: ${esc(t.error||'неизвестная')}</div>`;
        btn.disabled=false; btn.textContent='🤖 Получить сообщения';
      }
      if(tries > 60){ clearInterval(aiPollTimer); aiPollTimer = null; $('aiStatus').textContent = '⏱ Таймаут'; btn.disabled=false; btn.textContent='🤖 Получить сообщения'; }
    }, 2000);
  } catch(e){ alert('Ошибка: '+e.message); btn.disabled=false; btn.textContent='🤖 Получить сообщения'; }
}

function renderAIResult(result, chat){
  if(!result){ $('aiResult').innerHTML = '<p class="hint">Нет результата</p>'; return; }
  const topic = result.topic || '';
  const msgs = result.messages || [];
  const phrases = result.phrases || [];
  $('aiTopic').innerHTML = topic ? `<div class="ai-topic"><b>Тема беседы (${esc(chat)}):</b> ${esc(topic)}</div>` : '';
  $('aiResult').innerHTML = `<div class="ai-grid">
    <div class="ai-col">
      <h3>💬 5 сообщений по контексту (клик = копировать)</h3>
      ${msgs.map((m,i)=>`<div class="ai-msg" onclick="copyMsg(this, '${esc(m).replace(/'/g,"&#39;")}')">${esc(m)}</div>`).join('')}
    </div>
    <div class="ai-col">
      <h3>🙋 5 безопасных фраз</h3>
      ${phrases.map((p,i)=>`<div class="ai-phrase" onclick="copyMsg(this, '${esc(p).replace(/'/g,"&#39;")}')">${esc(p)}</div>`).join('')}
    </div>
  </div>`;
}

function copyMsg(el, text){
  const doCopy = async () => {
    if(navigator.clipboard && window.isSecureContext){
      try { await navigator.clipboard.writeText(text); return true; } catch(e){}
    }
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand('copy'); return true; } catch(e){ return false; }
    finally { document.body.removeChild(ta); }
  };
  doCopy().then(() => {
    el.classList.add('copied');
    const orig = el.textContent;
    el.textContent = '✅ ' + orig;
    setTimeout(()=>{ el.classList.remove('copied'); el.textContent = orig; }, 1200);
  });
}

async function loadAILatest(){
  const chat = $('aiChat').value || null;
  const p = new URLSearchParams();
  if(chat) p.set('chat', chat);
  const r = await fetch('/api/chat/latest?'+p).then(r=>r.json());
  if(!r.task){
    $('aiTopic').innerHTML = '';
    $('aiResult').innerHTML = '<p class="hint">Нет сохранённых результатов</p>';
    return;
  }
  renderAIResult(r.task.result, r.task.chat);
  $('aiStatus').textContent = `Последний: ${fmtTime(r.task.completed_at)}`;
}

async function loadPlayersTab(){
  const sum = await fetch('/api/players_by_chat').then(r=>r.json());
  const sEl = $('plChatsSummary');
  if(sum.chats && sum.chats.length){
    sEl.innerHTML = '<div class="kpi">' + sum.chats.map(c=>`
      <div>${esc(c.chat)}<b>${c.total_players} игроков</b>
        <span class="hint">🟢 ${c.online_players} онлайн · ${fmtMoney(c.total_messages)} сообщений</span>
      </div>`).join('') + '</div>';
  } else {
    sEl.innerHTML = '<p class="hint">Пока нет данных</p>';
  }
  const p = new URLSearchParams();
  if($('plChatFilter').value) p.set('chat', $('plChatFilter').value);
  if($('plUserFilter').value) p.set('username', $('plUserFilter').value);
  if($('plOnlineOnly').checked) p.set('online_only', '1');
  if($('plMinMsgs').value) p.set('min_msgs', $('plMinMsgs').value);
  p.set('limit', '1000');
  const r = await fetch('/api/players?'+p).then(r=>r.json());
  const body = $('plPlayersBody'); body.innerHTML='';
  if(!r.players.length){
    body.innerHTML = '<tr><td colspan="6" class="hint">Нет данных по фильтру</td></tr>';
    return;
  }
  const now = Date.now()/1000;
  r.players.forEach(x=>{
    const online = (now - (x.last_seen||0)) < 300;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><b>${esc(x.chat)}</b></td><td>${esc(x.username)}</td>
      <td style="color:#8ab4f8;font-weight:bold">${x.msg_count}</td>
      <td>${x.first_seen?fmtTime(x.first_seen):'—'}</td>
      <td>${x.last_seen?fmtTime(x.last_seen):'—'}</td>
      <td>${online?'<span class="pill ok">🟢 online</span>':'<span class="pill">offline</span>'}</td>`;
    body.appendChild(tr);
  });
}

async function loadLanguages(){
  const r = await fetch('/api/players_by_language').then(r=>r.json());
  const f = ($('langFilter').value || '').toLowerCase();
  const players = (r.players||[]).filter(p => !f || (p.username||'').toLowerCase().includes(f));
  const el = $('langGrid');
  if(!players.length){ el.innerHTML = '<p class="hint">Нет данных</p>'; return; }
  el.innerHTML = players.map(p => `
    <div class="lang-card">
      <h4>${esc(p.username)}</h4>
      <div class="hint">Всего сообщений: ${p.total_msgs||0} · чатов: ${p.chat_count||0}</div>
      ${(p.chats||[]).map(c=>`<div class="player-row"><span>${esc(c)}</span></div>`).join('')}
    </div>`).join('');
}

renderBulkChips();
loadChats();
refreshStatus();
setInterval(refreshStatus, 3000);
</script></body></html>
"""


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page():
    return MONITOR_HTML


# ==================== BASE HTML (главная, /hosts, /receivers) ====================
BASE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #0e1116; color: #e6e9ef; font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
a {{ color: #7cc4ff; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
header {{ background: #161b22; border-bottom: 1px solid #26303a; padding: 14px 24px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }}
.brand {{ font-weight: 700; font-size: 18px; color: #fff; }}
.brand small {{ color: #8b949e; font-weight: 400; margin-left: 8px; }}
nav a {{ margin-right: 16px; color: #c9d1d9; }}
nav a.active {{ color: #7cc4ff; font-weight: 600; }}
main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 16px; }}
.card {{ background: #161b22; border: 1px solid #26303a; border-radius: 12px; padding: 16px 20px; }}
.card h3 {{ margin: 0 0 8px; color: #8b949e; font-weight: 500; font-size: 13px; text-transform: uppercase; letter-spacing: .5px; }}
.card .big {{ font-size: 26px; font-weight: 700; color: #fff; }}
.card .sub {{ color: #6e7681; font-size: 12px; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #26303a; }}
th {{ color: #8b949e; font-weight: 500; font-size: 13px; }}
td.amount {{ color: #4ade80; font-weight: 600; }}
tr:hover td {{ background: #1c222b; }}
.rank {{ display: inline-block; width: 28px; text-align: center; color: #8b949e; }}
.rank.g {{ color: #f5c542; }} .rank.s {{ color: #c0c0c0; }} .rank.b {{ color: #cd7f32; }}
.tag {{ display: inline-block; background: #21262d; padding: 2px 8px; border-radius: 6px; font-size: 11px; color: #8b949e; margin-left: 6px; }}
.tag.rain {{ color: #7cc4ff; }} .tag.tip {{ color: #f5c542; }}
.live-dot {{ display: inline-block; width: 8px; height: 8px; background: #4ade80; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
.recent-row {{ padding: 8px 0; border-bottom: 1px solid #26303a; font-size: 13px; }}
.recent-row:last-child {{ border-bottom: none; }}
.muted {{ color: #8b949e; }}
.chat-pill {{ display:inline-block; background:#21262d; padding:2px 8px; border-radius:6px; font-size:11px; color:#7cc4ff; }}
.per-person {{ color: #8ab4f8; font-size: 11px; margin-left: 4px; }}
</style></head><body>
<header>
  <div class="brand">🌧 SHUFFLE RAIN <small>статистика</small></div>
  <nav>
    <a href="/" class="{active_home}">Главная</a>
    <a href="/hosts" class="{active_hosts}">Раздающие</a>
    <a href="/receivers" class="{active_receivers}">Получатели</a>
    <a href="/monitor">Мониторинг</a>
  </nav>
  <div style="margin-left:auto"><span class="live-dot"></span><span class="muted">LIVE</span></div>
</header>
<main>{content}</main>
<script>
setInterval(async () => {{
  try {{
    const r = await fetch('/api/stats');
    const data = await r.json();
    document.dispatchEvent(new CustomEvent('stats-updated', {{ detail: data }}));
  }} catch(e) {{}}
}}, 10000);
</script>
</body></html>"""


def fmt_rub(x):
    try: return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
    except: return "0 ₽"


def render_rank_table(rows):
    if not rows:
        return "<tr><td colspan='4' class='muted'>Пока нет данных</td></tr>"
    html = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        html += f"""<tr><td><span class="rank {rank_cls}">{i}</span></td>
            <td><b>{r['nickname']}</b></td><td>{r['cnt']}</td>
            <td class="amount">{fmt_rub(r['total'])}</td></tr>"""
    return html


def compute_receiver_stats(etype):
    stats = defaultdict(lambda: {"cnt": 0, "total": 0.0})
    for row in query_all(f"SELECT amount_rub, receivers FROM events WHERE type='{etype}'"):
        try: rcs = json.loads(row["receivers"] or "[]")
        except: continue
        if not rcs: continue
        share = (row["amount_rub"] or 0) / len(rcs)
        for r in rcs:
            if r:
                stats[r]["cnt"] += 1
                stats[r]["total"] += share
    return stats


def sort_receiver_stats(stats, by="total", limit=50):
    arr = [{"nickname": k, "cnt": v["cnt"], "total": v["total"]} for k, v in stats.items()]
    arr.sort(key=lambda x: x[by], reverse=True)
    return arr[:limit]


@app.get("/", response_class=HTMLResponse)
async def page_home():
    t = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {}).get("c", 0)
    tips = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {}).get("c", 0)
    tr = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'") or {}).get("s", 0)
    ttr = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'") or {}).get("s", 0)
    content = f"""
    <div class="grid">
      <div class="card"><h3>Дождей</h3><div class="big">{t}</div></div>
      <div class="card"><h3>Раздали всего</h3><div class="big">{fmt_rub(tr or 0)}</div></div>
      <div class="card"><h3>Чаевых</h3><div class="big">{tips}</div></div>
      <div class="card"><h3>Чаевых сумма</h3><div class="big">{fmt_rub(ttr or 0)}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>Последние события</h3><div id="recent"></div></div>
    <script>
      document.addEventListener('stats-updated', (e) => renderRecent(e.detail.recent));
      async function loadOnce() {{ const r = await fetch('/api/stats'); const d = await r.json(); renderRecent(d.recent); }}
      function renderRecent(rows) {{
        const el = document.getElementById('recent'); if (!rows) return;
        el.innerHTML = rows.map(r => {{
          const cls = r.type === 'rain' ? 'rain' : 'tip';
          const tag = r.type === 'rain' ? 'RAIN' : 'TIP';
          const amt = r.amount_text || '';
          const rub = r.amount_rub ? ' (' + Number(r.amount_rub).toFixed(0) + ' ₽)' : '';
          const per = r.per_person_rub ? ' <span class="per-person">по ' + Number(r.per_person_rub).toFixed(0) + ' ₽</span>' : '';
          let recs = ''; try {{ recs = (r.receivers || []).slice(0,5).join(', '); }} catch(e) {{}}
          return `<div class="recent-row"><span class="tag ${{cls}}">${{tag}}</span> <span class="chat-pill">${{r.chat}}</span> <b style="margin-left:6px">${{r.sender}}</b><span class="muted"> → ${{recs}}</span>${{per}}<span style="float:right" class="amount">${{amt}}${{rub}}</span></div>`;
        }}).join('');
      }}
      loadOnce();
    </script>"""
    return BASE_HTML.format(title="Shuffle Rain Stats", active_home="active",
                             active_hosts="", active_receivers="", content=content)


@app.get("/hosts", response_class=HTMLResponse)
async def page_hosts():
    r1 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='rain' GROUP BY sender ORDER BY total DESC LIMIT 50")
    r2 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='rain' GROUP BY sender ORDER BY cnt DESC LIMIT 50")
    t1 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='tip' GROUP BY sender ORDER BY total DESC LIMIT 50")
    t2 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='tip' GROUP BY sender ORDER BY cnt DESC LIMIT 50")
    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>🌧 Раздал дождей — по сумме</h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(r1)}</tbody></table></div>
      <div class="card"><h3>🌧 Раздал дождей — по кол-ву</h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(r2)}</tbody></table></div>
      <div class="card"><h3>💸 Отправил чаевых — по сумме</h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(t1)}</tbody></table></div>
      <div class="card"><h3>💸 Отправил чаевых — по кол-ву</h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(t2)}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Раздающие", active_home="", active_hosts="active", active_receivers="", content=content)


@app.get("/receivers", response_class=HTMLResponse)
async def page_receivers():
    rain_stats = compute_receiver_stats("rain")
    tip_stats = compute_receiver_stats("tip")
    ra = sort_receiver_stats(rain_stats, by="total")
    rc = sort_receiver_stats(rain_stats, by="cnt")
    ta = sort_receiver_stats(tip_stats, by="total")
    tc = sort_receiver_stats(tip_stats, by="cnt")
    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>🏆 Выиграл дождей — по сумме</h3><table><thead><tr><th>#</th><th>Ник</th><th>Выигрышей</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(ra)}</tbody></table></div>
      <div class="card"><h3>🎯 Выиграл дождей — по частоте</h3><table><thead><tr><th>#</th><th>Ник</th><th>Выигрышей</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(rc)}</tbody></table></div>
      <div class="card"><h3>💰 Получил чаевых — по сумме</h3><table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(ta)}</tbody></table></div>
      <div class="card"><h3>📬 Получил чаевых — по кол-ву</h3><table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead><tbody>{render_rank_table(tc)}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Получатели", active_home="", active_hosts="", active_receivers="active", content=content)


# ==================== ADMIN ====================
ADMIN_HTML = """
<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Admin</title>
<style>
body{margin:0;background:#0e1116;color:#e6e9ef;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.box{background:#161b22;border:1px solid #26303a;border-radius:12px;padding:32px 40px;max-width:520px;width:100%}
h1{margin:0 0 8px;font-size:22px}.sub{color:#8b949e;margin-bottom:24px;font-size:14px}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6e9ef;padding:10px 12px;border-radius:8px;font-size:14px;margin-bottom:16px;font-family:monospace}
button{width:100%;padding:12px;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;background:#dc2626;color:#fff}
button:disabled{background:#4b5563;cursor:not-allowed}
.result{margin-top:16px;padding:12px;border-radius:8px;font-size:13px;display:none}
.result.ok{background:#052e16;color:#4ade80;border:1px solid #14532d;display:block}
.result.err{background:#2d0f0f;color:#f87171;border:1px solid #7f1d1d;display:block}
.warn{background:#2d2410;border:1px solid #78350f;color:#fbbf24;padding:12px;border-radius:8px;margin-bottom:20px;font-size:13px}
a{color:#7cc4ff;text-decoration:none;font-size:13px}
</style></head><body>
<div class="box">
  <h1>⚙️ Админ-панель</h1>
  <div class="sub">Shuffle Rain Stats</div>
  <div class="warn">⚠️ Удалит ВСЕ события. Необратимо.</div>
  <input id="apiKey" type="password" placeholder="X-API-Key">
  <input id="confirm" type="text" placeholder="DELETE_ALL" autocomplete="off">
  <button id="clearBtn" onclick="clearDb()">🗑 Очистить базу данных</button>
  <div id="result" class="result"></div>
  <div style="margin-top:20px;text-align:center"><a href="/">← На главную</a></div>
</div>
<script>
async function clearDb() {
  const apiKey = document.getElementById('apiKey').value.trim();
  const confirm = document.getElementById('confirm').value.trim();
  const result = document.getElementById('result');
  const btn = document.getElementById('clearBtn');
  result.className='result'; result.textContent='';
  if(!apiKey){ result.className='result err'; result.textContent='Введите API-ключ'; return; }
  if(confirm !== 'DELETE_ALL'){ result.className='result err'; result.textContent='Введите DELETE_ALL'; return; }
  if(!window.confirm('Точно удалить ВСЕ данные?')) return;
  btn.disabled=true; btn.textContent='⏳';
  try {
    const r = await fetch('/api/clear', { method:'POST', headers:{'Content-Type':'application/json','X-API-Key':apiKey}, body: JSON.stringify({confirm:'DELETE_ALL'}) });
    const data = await r.json();
    if(r.ok){ result.className='result ok'; result.textContent='✅ '+(data.message||'Готово'); }
    else { result.className='result err'; result.textContent='❌ '+(data.detail||'Ошибка'); }
  } catch(e){ result.className='result err'; result.textContent='❌ '+e.message; }
  finally { btn.disabled=false; btn.textContent='🗑 Очистить базу данных'; }
}
</script></body></html>
"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


# ==================== STARTUP ====================
@app.on_event("startup")
async def startup_event():
    # 1) Курсы валют
    _load_server_rates()
    threading.Thread(target=rates_refresh_thread, daemon=True).start()
    print("[RATES] Поток обновления курсов запущен")

    # 2) Один проход дедупликации + пересчёта нулевых сумм
    threading.Thread(target=dedup_events, daemon=True).start()
    # 3) Регулярный дедуп каждые 10 минут
    threading.Thread(target=dedup_thread, daemon=True).start()

    # 4) Промокоды
    threading.Thread(target=promo_parser_thread, daemon=True).start()
    print(f"[PROMO] Поток мониторинга @{PROMO_CHANNEL} запущен")

    if GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"):
        print(f"[AI] ✅ GROQ_API_KEY задан, модель: {GROQ_MODEL}")
    else:
        print("[AI] ⚠️ GROQ_API_KEY не задан")


init_db()
print(f"[DB] Using {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
migrate_ts_once()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
