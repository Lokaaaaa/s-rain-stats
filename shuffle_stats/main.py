import os, json, sqlite3, time, threading, re, hashlib
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
except (PermissionError, OSError):
    SCREENSHOT_DIR = _default_screenshots
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

if USE_POSTGRES:
    import psycopg2, psycopg2.extras

app = FastAPI(title="Shuffle Rain & Tips Stats")

_state_lock = threading.Lock()
INSTANCE_STATES = {}
SCREENSHOT_REQUESTS = {}

PROMO_CHANNEL = os.environ.get("PROMO_CHANNEL", "anyclaimershuffle")
PROMO_PARSE_INTERVAL = 5 * 60
BET_TO_RAIN_WINDOW_SEC = 900

LANG_MAP = {'ENGLISH':'английский','RUSSIAN':'русский','JAPANESE':'японский','TURKISH':'турецкий',
            'FRENCH':'французский','SPANISH':'испанский','PORTUGUESE':'португальский','KOREAN':'корейский',
            'VIETNAMESE':'вьетнамский','POLISH':'польский','INDONESIAN':'индонезийский','CHINESE':'китайский'}

# ==================== RATES ====================
RATES_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_rates.json")
RATE_ALIASES = {"TON":"GRAM","MATIC":"POL","POLYGON":"POL","USD":"USDT","USDC":"USDT","DAI":"USDT","BUSD":"USDT","TUSD":"USDT"}

_server_rates = {}
_server_rates_lock = threading.Lock()
_server_rates_updated = [0.0]

def _load_server_rates():
    global _server_rates
    if os.path.exists(RATES_CACHE_FILE):
        try:
            with open(RATES_CACHE_FILE, "r", encoding="utf-8") as f: data = json.load(f)
            with _server_rates_lock:
                _server_rates = data.get("rates", {}); _server_rates_updated[0] = data.get("last_update", 0)
            print(f"[RATES] ✅ Серверных курсов: {len(_server_rates)}")
        except Exception as e: print(f"[RATES] ⚠️ {e}")

def _save_server_rates():
    try:
        with _server_rates_lock: data = {"rates": _server_rates, "last_update": _server_rates_updated[0]}
        with open(RATES_CACHE_FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"[RATES] save: {e}")

def _fetch_server_rates():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = ("bitcoin,ethereum,solana,litecoin,dogecoin,ripple,tron,binancecoin,tether,usd-coin,shiba-inu,"
               "shuffle-2,polygon-ecosystem-token,avalanche-2,the-open-network,bonk,dogwifcoin,pump-fun,official-trump,dai")
        r = requests.get(url, params={"ids": ids, "vs_currencies": "rub,usd"}, timeout=15)
        if r.status_code != 200: return
        data = r.json()
        mapping = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","LTC":"litecoin","DOGE":"dogecoin","XRP":"ripple",
                   "TRX":"tron","BNB":"binancecoin","USDT":"tether","USDC":"usd-coin","SHIB":"shiba-inu",
                   "SHFL":"shuffle-2","POL":"polygon-ecosystem-token","AVAX":"avalanche-2","GRAM":"the-open-network",
                   "BONK":"bonk","WIF":"dogwifcoin","PUMP":"pump-fun","TRUMP":"official-trump","DAI":"dai"}
        new = {}
        for sym, cg in mapping.items():
            if cg in data:
                if "rub" in data[cg]: new[sym] = data[cg]["rub"]
                if "usd" in data[cg]: new[f"{sym}_USD"] = data[cg]["usd"]
        if "GRAM" in new: new["TON"] = new["GRAM"]
        if "GRAM_USD" in new: new["TON_USD"] = new["GRAM_USD"]
        if "POL" in new: new["MATIC"] = new["POL"]
        if "POL_USD" in new: new["MATIC_USD"] = new["POL_USD"]
        new["CASH"] = new.get("USDT", 95.0); new["CASH_USD"] = 1.0
        with _server_rates_lock:
            _server_rates.update(new); _server_rates_updated[0] = time.time()
        _save_server_rates()
        print(f"[RATES] 🔄 Обновлено {len(new)} курсов")
    except Exception as e: print(f"[RATES] fetch: {e}")

def server_rate_rub(symbol):
    if not symbol: return None
    s = symbol.upper().strip(); s = RATE_ALIASES.get(s, s)
    with _server_rates_lock: return _server_rates.get(s)

def server_rate_usd(symbol):
    if not symbol: return None
    s = symbol.upper().strip(); s = RATE_ALIASES.get(s, s)
    with _server_rates_lock: return _server_rates.get(f"{s}_USD")

def rates_refresh_thread():
    _fetch_server_rates()
    while True:
        time.sleep(3600)
        try: _fetch_server_rates()
        except Exception as e: print(f"[RATES] thread: {e}")

# ==================== БАЗА ====================
def get_db():
    if USE_POSTGRES: return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; return conn

def ph(): return "%s" if USE_POSTGRES else "?"

def query_all(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
        cur.execute(sql_pg, params)
        try: return [dict(r) for r in cur.fetchall()]
        except Exception: return []
    finally: conn.close()

def query_one(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if USE_POSTGRES else conn.cursor()
        cur.execute(sql_pg, params)
        row = cur.fetchone(); return dict(row) if row else None
    finally: conn.close()

def execute(sql, params=()):
    sql_pg = sql.replace("?", "%s") if USE_POSTGRES else sql
    conn = get_db()
    try:
        cur = conn.cursor(); cur.execute(sql_pg, params); conn.commit()
    finally: conn.close()

def _try(sql):
    try: execute(sql)
    except Exception: pass

def init_db():
    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY, type TEXT NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, receivers TEXT NOT NULL, receivers_info TEXT,
            amount_text TEXT, amount_type TEXT, crypto_symbol TEXT,
            amount_rub DOUBLE PRECISION DEFAULT 0, amount_usd DOUBLE PRECISION DEFAULT 0,
            created_at TEXT NOT NULL, ts DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, receivers TEXT NOT NULL, receivers_info TEXT,
            amount_text TEXT, amount_type TEXT, crypto_symbol TEXT,
            amount_rub REAL DEFAULT 0, amount_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL, ts REAL)""")
    for col, typ in [("ts", "DOUBLE PRECISION" if USE_POSTGRES else "REAL"),
                     ("context_messages", "TEXT"), ("context_bets", "TEXT"),
                     ("amount_usd", "DOUBLE PRECISION DEFAULT 0" if USE_POSTGRES else "REAL DEFAULT 0"),
                     ("receivers_info", "TEXT")]:
        _try(f"ALTER TABLE events ADD COLUMN {col} {typ}")
    for idx in ["idx_type ON events(type)", "idx_sender ON events(sender)",
                "idx_events_ts ON events(ts)", "idx_events_chat ON events(chat)"]:
        execute(f"CREATE INDEX IF NOT EXISTS {idx}")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS bulk_alerts (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, amount_text TEXT, amount_rub DOUBLE PRECISION DEFAULT 0,
            crypto TEXT, receivers TEXT NOT NULL, window_sec INTEGER DEFAULT 0, is_rubric INTEGER DEFAULT 0)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS bulk_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, amount_text TEXT, amount_rub REAL DEFAULT 0,
            crypto TEXT, receivers TEXT NOT NULL, window_sec INTEGER DEFAULT 0, is_rubric INTEGER DEFAULT 0)""")
    _try("ALTER TABLE bulk_alerts ADD COLUMN is_rubric INTEGER DEFAULT 0")
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
            crypto TEXT, outcome TEXT, source TEXT DEFAULT 'bets_page')""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, chat TEXT NOT NULL,
            sender TEXT NOT NULL, game TEXT, multiplier TEXT, amount_text TEXT,
            amount_rub REAL DEFAULT 0, amount_usd REAL DEFAULT 0, crypto TEXT,
            outcome TEXT, source TEXT DEFAULT 'bets_page')""")
    _try("ALTER TABLE bets ADD COLUMN source TEXT DEFAULT 'bets_page'")
    for idx in ["idx_bets_ts ON bets(ts)", "idx_bets_chat ON bets(chat)", "idx_bets_sender ON bets(sender)"]:
        execute(f"CREATE INDEX IF NOT EXISTS {idx}")

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
    _try("ALTER TABLE intents ADD COLUMN percent INTEGER DEFAULT 100")
    for idx in ["idx_intents_ts ON intents(ts)", "idx_intents_chat ON intents(chat)", "idx_intents_sender ON intents(sender)"]:
        execute(f"CREATE INDEX IF NOT EXISTS {idx}")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS promocodes (
            id SERIAL PRIMARY KEY, tg_msg_id TEXT UNIQUE, text TEXT NOT NULL, link TEXT,
            message_date TEXT, discovered_at DOUBLE PRECISION, used INTEGER DEFAULT 0,
            used_at DOUBLE PRECISION, used_for_vault INTEGER DEFAULT 0, vault_amount_usd DOUBLE PRECISION DEFAULT 0)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_msg_id TEXT UNIQUE, text TEXT NOT NULL,
            link TEXT, message_date TEXT, discovered_at REAL, used INTEGER DEFAULT 0,
            used_at REAL, used_for_vault INTEGER DEFAULT 0, vault_amount_usd REAL DEFAULT 0)""")
    _try("ALTER TABLE promocodes ADD COLUMN used_for_vault INTEGER DEFAULT 0")
    _try("ALTER TABLE promocodes ADD COLUMN vault_amount_usd DOUBLE PRECISION DEFAULT 0" if USE_POSTGRES
         else "ALTER TABLE promocodes ADD COLUMN vault_amount_usd REAL DEFAULT 0")
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
            messages TEXT, result TEXT, error TEXT, created_at REAL, completed_at REAL)""")
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
    for idx in ["idx_cp_chat ON chat_players(chat)", "idx_cp_user ON chat_players(username)",
                "idx_cp_last ON chat_players(last_seen)"]:
        execute(f"CREATE INDEX IF NOT EXISTS {idx}")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS player_message_ticks (
            username TEXT NOT NULL, chat TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (username, chat, ts))""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS player_message_ticks (
            username TEXT NOT NULL, chat TEXT NOT NULL, ts REAL NOT NULL,
            PRIMARY KEY (username, chat, ts))""")
    execute("CREATE INDEX IF NOT EXISTS idx_pmt_user_ts ON player_message_ticks(username, ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_pmt_ts ON player_message_ticks(ts)")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS player_profiles (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            lang TEXT DEFAULT 'ENGLISH', description TEXT DEFAULT '',
            is_top BOOLEAN DEFAULT FALSE, updated_at DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS player_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            lang TEXT DEFAULT 'ENGLISH', description TEXT DEFAULT '',
            is_top INTEGER DEFAULT 0, updated_at REAL)""")

    if USE_POSTGRES:
        execute("""CREATE TABLE IF NOT EXISTS vault_balances (
            currency TEXT PRIMARY KEY, amount DOUBLE PRECISION DEFAULT 0,
            amount_usd DOUBLE PRECISION DEFAULT 0, updated_at DOUBLE PRECISION)""")
        execute("""CREATE TABLE IF NOT EXISTS vault_transactions (
            id SERIAL PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, type TEXT NOT NULL,
            currency TEXT, amount DOUBLE PRECISION DEFAULT 0, amount_usd DOUBLE PRECISION DEFAULT 0,
            description TEXT)""")
        execute("""CREATE TABLE IF NOT EXISTS wager_state (
            id INTEGER PRIMARY KEY, amount_required DOUBLE PRECISION DEFAULT 0,
            deadline_ts DOUBLE PRECISION DEFAULT 0, updated_at DOUBLE PRECISION)""")
    else:
        execute("""CREATE TABLE IF NOT EXISTS vault_balances (
            currency TEXT PRIMARY KEY, amount REAL DEFAULT 0,
            amount_usd REAL DEFAULT 0, updated_at REAL)""")
        execute("""CREATE TABLE IF NOT EXISTS vault_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, type TEXT NOT NULL,
            currency TEXT, amount REAL DEFAULT 0, amount_usd REAL DEFAULT 0,
            description TEXT)""")
        execute("""CREATE TABLE IF NOT EXISTS wager_state (
            id INTEGER PRIMARY KEY, amount_required REAL DEFAULT 0,
            deadline_ts REAL DEFAULT 0, updated_at REAL)""")
    execute("CREATE INDEX IF NOT EXISTS idx_vt_ts ON vault_transactions(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_vt_type ON vault_transactions(type)")
    execute("CREATE TABLE IF NOT EXISTS vault_meta (k TEXT PRIMARY KEY, v TEXT)")
    execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

def meta_get(k, default=None):
    r = query_one("SELECT v FROM meta WHERE k=?", (k,)); return r["v"] if r else default

def meta_set(k, v):
    if USE_POSTGRES: execute("INSERT INTO meta (k,v) VALUES (%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, str(v)))
    else: execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, str(v)))

def vault_meta_get(k, default=None):
    r = query_one("SELECT v FROM vault_meta WHERE k=?", (k,)); return r["v"] if r else default

def vault_meta_set(k, v):
    if USE_POSTGRES: execute("INSERT INTO vault_meta (k,v) VALUES (%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (k, str(v)))
    else: execute("INSERT OR REPLACE INTO vault_meta (k,v) VALUES (?,?)", (k, str(v)))

def migrate_ts_once():
    try:
        if meta_get("ts_migrated_v1"): return
        if USE_POSTGRES:
            execute("""UPDATE events SET ts = EXTRACT(EPOCH FROM created_at::timestamptz)
                       WHERE ts IS NULL AND created_at IS NOT NULL""")
        else:
            execute("""UPDATE events SET ts = CAST(strftime('%s', created_at) AS REAL)
                       WHERE ts IS NULL AND created_at IS NOT NULL""")
        meta_set("ts_migrated_v1", "1"); print("[DB] ✅ ts миграция")
    except Exception as e: print(f"[DB] ❌ ts: {e}")

# ==================== CLEANUP ====================
def dedup_events():
    try:
        rows = query_all("""SELECT id, type, chat, sender, amount_text, amount_type, crypto_symbol, amount_rub, amount_usd
                            FROM events WHERE (amount_rub IS NULL OR amount_rub=0 OR amount_usd IS NULL OR amount_usd=0)
                            LIMIT 5000""")
        fixed = 0
        for r in rows:
            txt = r.get("amount_text") or ""
            crypto = (r.get("crypto_symbol") or "").upper().strip()
            value = None
            try: value = float(txt.replace("\u00a0", " ").replace(" ", "").replace(",", ".").replace("₽", "").replace("$", "").replace("€", ""))
            except: pass
            if value is None: continue
            new_rub = r.get("amount_rub") or 0; new_usd = r.get("amount_usd") or 0
            has_fiat = any(m in txt for m in ("₽", "$", "€", "£"))
            if crypto and not has_fiat:
                cr = server_rate_rub(crypto); cu = server_rate_usd(crypto)
                if cr and not new_rub: new_rub = value * cr
                if cu and not new_usd: new_usd = value * cu
            elif "₽" in txt and not new_rub: new_rub = value
            elif "$" in txt and not new_usd: new_usd = value
            elif not crypto:
                if not new_rub: new_rub = value
                if not new_usd and new_rub:
                    uu = server_rate_usd("USDT") or 1.0; ur = server_rate_rub("USDT") or 95.0
                    new_usd = new_rub / ur * uu
            if new_rub != (r.get("amount_rub") or 0) or new_usd != (r.get("amount_usd") or 0):
                p = ph(); execute(f"UPDATE events SET amount_rub={p}, amount_usd={p} WHERE id={p}", (new_rub, new_usd, r["id"])); fixed += 1
        if fixed: print(f"[DEDUP] ✅ Пересчитано {fixed}")
        dups = query_all("""SELECT a.id AS keep_id, b.id AS del_id FROM events a JOIN events b
            ON a.id < b.id AND a.type=b.type AND a.chat=b.chat AND a.sender=b.sender
            AND a.receivers=b.receivers AND a.amount_text=b.amount_text
            AND ABS(COALESCE(a.ts,0)-COALESCE(b.ts,0))<60 LIMIT 2000""")
        if dups:
            ids = list({d["del_id"] for d in dups})
            for i in range(0, len(ids), 500):
                ch = ids[i:i+500]; phs = ",".join([ph()] * len(ch))
                execute(f"DELETE FROM events WHERE id IN ({phs})", tuple(ch))
            print(f"[DEDUP] ✅ Удалено {len(ids)} дублей")
    except Exception as e: print(f"[DEDUP] ❌ {type(e).__name__}: {e}")

def dedup_thread():
    while True:
        time.sleep(600)
        try: dedup_events()
        except Exception as e: print(f"[DEDUP] thread: {e}")

def ticks_cleanup_thread():
    while True:
        time.sleep(6 * 3600)
        try:
            execute(f"DELETE FROM player_message_ticks WHERE ts < {ph()}", (time.time() - 7 * 86400,))
            print("[TICKS] ✅ Очистка")
        except Exception as e: print(f"[TICKS] ⚠️ {e}")

# ==================== МОДЕЛИ ====================
class EventIn(BaseModel):
    type: str; chat: str; sender: str
    receivers: List[str] = []; receivers_info: List[dict] = []
    amount_text: str = ""; amount_type: str = "fiat"; crypto_symbol: str = ""
    amount_rub: float = 0.0; amount_usd: float = 0.0
    created_at: Optional[str] = None; ts: Optional[float] = None
    context_messages: List[str] = []; context_bets: List[dict] = []

class BulkAlertIn(BaseModel):
    ts: Optional[float] = None; chat: str; sender: str; amount_text: str = ""
    amount_rub: float = 0.0; crypto: str = ""; receivers: List[str] = []; window_sec: int = 0

class PlayerActivityIn(BaseModel):
    ts: Optional[float] = None; username: str; chat: str; text: str = ""

class IntentIn(BaseModel):
    ts: Optional[float] = None; chat: str; sender: str; text: str = ""; percent: int = 100

class BetIn(BaseModel):
    ts: Optional[float] = None; chat: str; sender: str; game: str = ""; multiplier: str = ""
    amount_text: str = ""; amount_rub: float = 0.0; amount_usd: float = 0.0
    crypto: str = ""; outcome: str = "win"; source: str = "bets_page"

class InstanceStatus(BaseModel):
    iid: int; mode: Optional[str] = None; status: Optional[str] = None
    current_chat: Optional[str] = None; chats: Optional[List[str]] = None
    new_count: Optional[int] = 0; threshold: Optional[int] = None; error: Optional[str] = None
    updated_at: Optional[float] = None

class StatusPayload(BaseModel):
    instances: List[InstanceStatus] = []; screenshot_requests: List[int] = []

class ChatTaskIn(BaseModel):
    chat: str

class ChatTaskCompleteIn(BaseModel):
    task_id: int; messages: List[dict] = []; error: str = ""

class PlayerStatItem(BaseModel):
    name: str; msgs: int = 0; last_seen: float = 0.0

class PlayerTick(BaseModel):
    username: str; ts: float

class PlayersBatchIn(BaseModel):
    chat: str; players: List[PlayerStatItem] = []; ticks: List[PlayerTick] = []

class VaultBalanceIn(BaseModel):
    currency: str; amount: float; amount_usd: Optional[float] = None

class VaultTransactionIn(BaseModel):
    type: str; currency: str = "USDT"; amount: float = 0.0
    amount_usd: float = 0.0; description: str = ""; ts: Optional[float] = None

class VaultWagerIn(BaseModel):
    amount_required: float; deadline_ts: float; spent_now: Optional[float] = None

class VaultGoalIn(BaseModel):
    goal_usd: float; goal_text: str = ""

# ==================== РАСЧЁТ ====================
def _recalc_amounts(amount_text, amount_type, crypto_symbol):
    txt = amount_text or ""; crypto = (crypto_symbol or "").upper().strip()
    cleaned = (txt.replace("\u00a0", " ").replace(" ", "").replace("₽", "").replace("$", "").replace("€", ""))
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."): cleaned = cleaned.replace(".", "").replace(",", ".")
        else: cleaned = cleaned.replace(",", "")
    else: cleaned = cleaned.replace(",", ".")
    try: value = float(cleaned)
    except: return 0.0, 0.0
    has_fiat = any(m in txt for m in ("₽", "$", "€", "£", "₴", "₸", "₺"))
    if crypto and (amount_type == "crypto" or not has_fiat):
        cr = server_rate_rub(crypto) or 0.0; cu = server_rate_usd(crypto) or 0.0
        return value * cr, value * cu
    if "₽" in txt:
        ur = server_rate_rub("USDT") or 95.0; uu = server_rate_usd("USDT") or 1.0
        return value, value / ur * uu
    if "$" in txt:
        ur = server_rate_rub("USDT") or 95.0; uu = server_rate_usd("USDT") or 1.0
        return value / uu * ur, value
    if "€" in txt:
        usd = value * 1.08; ur = server_rate_rub("USDT") or 95.0; uu = server_rate_usd("USDT") or 1.0
        return usd / uu * ur, usd
    ur = server_rate_rub("USDT") or 95.0; uu = server_rate_usd("USDT") or 1.0
    return value, value / ur * uu

# ==================== API ====================
@app.post("/api/event")
async def add_event(event: EventIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    if event.type not in ("rain", "tip"): raise HTTPException(400, "type must be rain or tip")
    ts_val = event.ts if event.ts else time.time()
    created = event.created_at or datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
    dup = query_one("""SELECT id FROM events WHERE type=? AND chat=? AND sender=? AND receivers=? AND amount_text=? AND ts IS NOT NULL AND ABS(ts-?)<30 LIMIT 1""",
                    (event.type, event.chat, event.sender, json.dumps(event.receivers or [], ensure_ascii=False), event.amount_text, ts_val))
    if dup: return {"ok": True, "dup": True}
    rub = event.amount_rub or 0.0; usd = event.amount_usd or 0.0
    if rub == 0 or usd == 0:
        cr, cu = _recalc_amounts(event.amount_text, event.amount_type, event.crypto_symbol)
        if not rub: rub = cr
        if not usd: usd = cu
    p = ph()
    execute(f"""INSERT INTO events (type,chat,sender,receivers,receivers_info,amount_text,amount_type,crypto_symbol,amount_rub,amount_usd,created_at,ts,context_messages,context_bets)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (event.type, event.chat, event.sender, json.dumps(event.receivers or [], ensure_ascii=False),
             json.dumps(event.receivers_info or [], ensure_ascii=False), event.amount_text, event.amount_type, event.crypto_symbol,
             rub, usd, created, ts_val, json.dumps(event.context_messages or [], ensure_ascii=False),
             json.dumps(event.context_bets or [], ensure_ascii=False)))
    return {"ok": True}

@app.post("/api/events_batch")
async def events_batch(payload: dict, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    events = payload.get("events", []); inserted = 0
    for e in events:
        try:
            rub = float(e.get("amount_rub") or 0); usd = float(e.get("amount_usd") or 0)
            if rub == 0 or usd == 0:
                cr, cu = _recalc_amounts(e.get("amount_text", ""), e.get("amount_type", "fiat"), e.get("crypto_symbol", ""))
                if not rub: rub = cr
                if not usd: usd = cu
            p = ph()
            execute(f"""INSERT INTO events (type,chat,sender,receivers,receivers_info,amount_text,amount_type,crypto_symbol,amount_rub,amount_usd,created_at,ts,context_messages,context_bets)
                        VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
                    (e.get("type"), e.get("chat"), e.get("sender"),
                     json.dumps(e.get("receivers", []), ensure_ascii=False),
                     json.dumps(e.get("receivers_info", []), ensure_ascii=False),
                     e.get("amount_text", ""), e.get("amount_type", "fiat"),
                     e.get("crypto_symbol", ""), rub, usd,
                     datetime.fromtimestamp(e.get("ts", time.time()), tz=timezone.utc).isoformat(),
                     e.get("ts", time.time()),
                     json.dumps(e.get("context_messages", []), ensure_ascii=False),
                     json.dumps(e.get("context_bets", []), ensure_ascii=False)))
            inserted += 1
        except Exception as ex: print(f"[BATCH] insert err: {ex}")
    return {"ok": True, "inserted": inserted}

@app.post("/api/bulk_alert")
async def add_bulk_alert(body: BulkAlertIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    rub = body.amount_rub or 0.0
    if rub == 0:
        cr, _ = _recalc_amounts(body.amount_text, "crypto" if body.crypto else "fiat", body.crypto); rub = cr
    p = ph()
    execute(f"""INSERT INTO bulk_alerts (ts,chat,sender,amount_text,amount_rub,crypto,receivers,window_sec)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p})""",
            (ts_val, body.chat, body.sender, body.amount_text, rub, body.crypto,
             json.dumps(body.receivers, ensure_ascii=False), body.window_sec))
    return {"ok": True}

@app.post("/api/bulk/rubric/{alert_id}")
async def toggle_bulk_rubric(alert_id: int):
    row = query_one("SELECT id, is_rubric FROM bulk_alerts WHERE id=?", (alert_id,))
    if not row: raise HTTPException(404, "not found")
    nv = 0 if row.get("is_rubric") else 1
    execute("UPDATE bulk_alerts SET is_rubric=? WHERE id=?", (nv, alert_id))
    return {"ok": True, "is_rubric": nv}

@app.post("/api/player_activity")
async def add_player_activity(body: PlayerActivityIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    execute(f"INSERT INTO player_activity (ts,username,chat,text) VALUES ({ph()},{ph()},{ph()},{ph()})",
            (ts_val, body.username, body.chat, body.text))
    return {"ok": True}

@app.post("/api/bet")
async def add_bet(body: BetIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    rub = body.amount_rub or 0.0; usd = body.amount_usd or 0.0
    if rub == 0 or usd == 0:
        cr, cu = _recalc_amounts(body.amount_text, "crypto" if body.crypto else "fiat", body.crypto)
        if not rub: rub = cr
        if not usd: usd = cu
    p = ph()
    execute(f"""INSERT INTO bets (ts,chat,sender,game,multiplier,amount_text,amount_rub,amount_usd,crypto,outcome,source)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (ts_val, body.chat, body.sender, body.game, body.multiplier, body.amount_text,
             rub, usd, body.crypto, body.outcome, body.source or "bets_page"))
    return {"ok": True}

@app.post("/api/bets/clear_top")
async def clear_bets_top():
    meta_set("bets_top_cleared_at", time.time()); return {"ok": True}

@app.post("/api/intent")
async def add_intent(body: IntentIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    dup = query_one("SELECT id FROM intents WHERE chat=? AND sender=? AND text=? AND ABS(ts-?)<60 LIMIT 1",
                    (body.chat, body.sender, body.text, ts_val))
    if dup: return {"ok": True, "dup": True}
    p = ph()
    execute(f"INSERT INTO intents (ts,chat,sender,text,percent,matched_rain_id,matched_delta_sec) VALUES ({p},{p},{p},{p},{p},NULL,NULL)",
            (ts_val, body.chat, body.sender, body.text, body.percent or 100))
    rain = query_one("""SELECT id, ts FROM events WHERE type='rain' AND chat=? AND sender LIKE ?
                        AND ts IS NOT NULL AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT 1""",
                     (body.chat, f"%{body.sender}%", ts_val, ts_val + 300))
    if rain:
        delta = int(rain["ts"] - ts_val)
        execute("UPDATE intents SET matched_rain_id=?, matched_delta_sec=? WHERE chat=? AND sender=? AND text=? AND ABS(ts-?)<60",
                (rain["id"], delta, body.chat, body.sender, body.text, ts_val))
    return {"ok": True}

@app.post("/api/players_batch")
async def add_players_batch(body: PlayersBatchIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    now = time.time(); p = ph()
    for pl in body.players:
        name = (pl.name or "").strip()
        if not name or name == "?": continue
        msgs = max(0, int(pl.msgs)); last_seen = float(pl.last_seen) or now
        if USE_POSTGRES:
            execute(f"""INSERT INTO chat_players (chat,username,msg_count,first_seen,last_seen)
                        VALUES ({p},{p},{p},{p},{p})
                        ON CONFLICT (chat,username) DO UPDATE
                        SET msg_count=chat_players.msg_count+EXCLUDED.msg_count,
                            last_seen=GREATEST(chat_players.last_seen,EXCLUDED.last_seen)""",
                    (body.chat, name, msgs, last_seen, last_seen))
        else:
            execute(f"""INSERT INTO chat_players (chat,username,msg_count,first_seen,last_seen)
                        VALUES ({p},{p},{p},{p},{p})
                        ON CONFLICT (chat,username) DO UPDATE
                        SET msg_count=msg_count+excluded.msg_count,
                            last_seen=MAX(last_seen,excluded.last_seen)""",
                    (body.chat, name, msgs, last_seen, last_seen))
    if body.ticks:
        inserted = 0
        for t in body.ticks:
            un = (t.username or "").strip()
            if not un or un == "?": continue
            try: ts_val = float(t.ts or 0)
            except: continue
            if ts_val <= 0: continue
            if USE_POSTGRES:
                execute(f"INSERT INTO player_message_ticks (username,chat,ts) VALUES ({p},{p},{p}) ON CONFLICT DO NOTHING",
                        (un, body.chat, ts_val))
            else:
                execute(f"INSERT OR IGNORE INTO player_message_ticks (username,chat,ts) VALUES ({p},{p},{p})",
                        (un, body.chat, ts_val))
            inserted += 1
        if inserted: print(f"[TICKS] {body.chat}: +{inserted}")
    return {"ok": True}

@app.get("/api/player/activity")
async def api_player_activity(username: str, windows: str = "15,25,60,1440"):
    un = (username or "").strip()
    if not un: raise HTTPException(400, "username required")
    try: wins = sorted({int(w) for w in windows.split(",") if w.strip()})
    except: wins = [15, 25, 60, 1440]
    max_win = max(wins) if wins else 1440
    since = time.time() - max_win * 60
    all_chat_rows = query_all("SELECT chat, msg_count, last_seen FROM chat_players WHERE username=?", (un,))
    all_chats = {r["chat"]: {"msg_count": r.get("msg_count", 0) or 0, "last_seen_total": r.get("last_seen") or 0}
                 for r in all_chat_rows}
    tick_rows = query_all("""SELECT chat, ts FROM player_message_ticks WHERE username=? AND ts>=? ORDER BY ts DESC""",
                          (un, since))
    now = time.time()
    per_chat = {c: {"counts": {w: 0 for w in wins}, "last_ts": 0.0,
                    "total_msgs": m["msg_count"] or 0, "last_seen_total": m["last_seen_total"] or 0}
                for c, m in all_chats.items()}
    for r in tick_rows:
        c = r["chat"]; ts = float(r["ts"] or 0)
        if c not in per_chat: per_chat[c] = {"counts": {w: 0 for w in wins}, "last_ts": 0.0, "total_msgs": 0, "last_seen_total": 0}
        for w in wins:
            if now - ts <= w * 60: per_chat[c]["counts"][w] += 1
        if ts > per_chat[c]["last_ts"]: per_chat[c]["last_ts"] = ts
    total = {w: 0 for w in wins}; last_ts = 0.0
    for c, v in per_chat.items():
        for w in wins: total[w] += v["counts"][w]
        if v["last_ts"] > last_ts: last_ts = v["last_ts"]
    chats = []
    for c, v in per_chat.items():
        eff = max(v["last_ts"] or 0, v["last_seen_total"] or 0)
        chats.append({"chat": c, "counts": v["counts"], "last_ts": eff,
                      "minutes_ago": int((now - eff) / 60) if eff else None, "total_msgs": v["total_msgs"] or 0})
    chats.sort(key=lambda x: (-sum(x["counts"].values()), -x["last_ts"]))
    gl = max([c["last_ts"] for c in chats], default=0)
    return {"username": un, "windows": wins, "total": total, "last_ts": gl,
            "minutes_ago_total": int((now - gl) / 60) if gl else None, "chats": chats, "now": now, "known_chats": len(chats)}

@app.get("/api/user/{nickname}/rains_received")
async def user_rains_received(nickname: str, days: int = 30, limit: int = 500):
    since = time.time() - days * 86400
    rows = query_all("""SELECT id, ts, chat, sender, receivers, amount_text, amount_rub, amount_usd, crypto_symbol
                        FROM events WHERE type='rain' AND ts >= ? ORDER BY ts DESC LIMIT 5000""", (since,))
    out = []; total_rub = 0.0; total_usd = 0.0
    for r in rows:
        try: recs = json.loads(r.get("receivers") or "[]")
        except: recs = []
        if nickname not in recs: continue
        n = len(recs) or 1
        per_rub = (r.get("amount_rub") or 0) / n
        per_usd = (r.get("amount_usd") or 0) / n
        total_rub += per_rub; total_usd += per_usd
        out.append({"id": r["id"], "ts": r["ts"], "chat": r["chat"], "sender": r["sender"],
                    "amount_text": r["amount_text"], "crypto_symbol": r.get("crypto_symbol", ""),
                    "share_rub": round(per_rub, 2), "share_usd": round(per_usd, 2), "receivers_count": n})
        if len(out) >= limit: break
    return {"nickname": nickname, "wins": len(out), "total_rub": round(total_rub, 2),
            "total_usd": round(total_usd, 2), "items": out, "days": days}

@app.post("/api/status")
async def post_status(payload: StatusPayload, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    now = time.time()
    with _state_lock:
        for inst in payload.instances: INSTANCE_STATES[inst.iid] = {**inst.dict(), "server_received_at": now}
        for iid in payload.screenshot_requests:
            if iid not in SCREENSHOT_REQUESTS: SCREENSHOT_REQUESTS[iid] = {"requested_at": now, "done": False}
    return {"ok": True}

@app.get("/api/screenshot_req")
async def get_screenshot_req(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    with _state_lock:
        pending = [{"iid": iid, "requested_at": r.get("requested_at", 0)}
                   for iid, r in SCREENSHOT_REQUESTS.items() if not r.get("done")]
    return {"requests": pending}

@app.post("/api/screenshot_upload")
async def upload_screenshot(iid: int = Form(...), file: UploadFile = File(...), x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    path = os.path.join(SCREENSHOT_DIR, f"inst{iid}_latest.png")
    content = await file.read()
    with open(path, "wb") as f: f.write(content)
    with _state_lock:
        prev = SCREENSHOT_REQUESTS.get(iid, {})
        SCREENSHOT_REQUESTS[iid] = {"requested_at": prev.get("requested_at", time.time()),
                                     "done": True, "path": path, "uploaded_at": time.time()}
    return {"ok": True}

# ==================== WATCHLIST / PROFILES ====================
DEFAULT_WATCHLIST = ["Fab", "Fab434", "fab434"]

@app.get("/api/watchlist")
async def get_watchlist():
    v = meta_get("watchlist")
    if not v: return {"players": DEFAULT_WATCHLIST}
    try: return {"players": json.loads(v)}
    except: return {"players": DEFAULT_WATCHLIST}

@app.post("/api/watchlist/add")
async def watchlist_add(body: dict):
    name = (body.get("name") or "").strip()
    if not name: raise HTTPException(400, "name required")
    v = meta_get("watchlist")
    try: players = json.loads(v) if v else list(DEFAULT_WATCHLIST)
    except: players = list(DEFAULT_WATCHLIST)
    if name not in players: players.append(name)
    meta_set("watchlist", json.dumps(players, ensure_ascii=False))
    return {"ok": True, "players": players}

@app.post("/api/watchlist/remove")
async def watchlist_remove(body: dict):
    name = (body.get("name") or "").strip()
    v = meta_get("watchlist")
    try: players = json.loads(v) if v else list(DEFAULT_WATCHLIST)
    except: players = list(DEFAULT_WATCHLIST)
    players = [p for p in players if p != name]
    meta_set("watchlist", json.dumps(players, ensure_ascii=False))
    return {"ok": True, "players": players}

@app.get("/api/profile/{username}")
async def get_profile(username: str):
    p = query_one("SELECT * FROM player_profiles WHERE username=?", (username,))
    if not p: return {"profile": {"username": username, "lang": "ENGLISH", "description": "", "is_top": False}}
    return {"profile": p}

@app.post("/api/profile/{username}")
async def upsert_profile(username: str, body: dict):
    lang = (body.get("lang") or "ENGLISH").strip(); desc = (body.get("description") or "").strip()
    is_top = 1 if body.get("is_top") else 0; p = ph()
    if USE_POSTGRES:
        execute(f"""INSERT INTO player_profiles (username,lang,description,is_top,updated_at)
                    VALUES ({p},{p},{p},{p},{p}) ON CONFLICT (username) DO UPDATE
                    SET lang=EXCLUDED.lang,description=EXCLUDED.description,is_top=EXCLUDED.is_top,updated_at=EXCLUDED.updated_at""",
                (username, lang, desc, is_top, time.time()))
    else:
        execute(f"""INSERT INTO player_profiles (username,lang,description,is_top,updated_at)
                    VALUES ({p},{p},{p},{p},{p}) ON CONFLICT (username) DO UPDATE
                    SET lang=excluded.lang,description=excluded.description,is_top=excluded.is_top,updated_at=excluded.updated_at""",
                (username, lang, desc, is_top, time.time()))
    return {"ok": True}

# ==================== ПРОМОКОДЫ ====================
def fetch_promo_channel():
    try:
        r = requests.get(f"https://t.me/s/{PROMO_CHANNEL}", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, "html.parser"); messages = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg_el = wrap.select_one(".tgme_widget_message")
            if not msg_el: continue
            data_post = msg_el.get("data-post", "")
            if not data_post: continue
            time_el = wrap.select_one("time"); msg_date = time_el.get("datetime", "") if time_el else ""
            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text("\n", strip=True) if text_el else ""
            if not text: continue
            messages.append({"tg_msg_id": data_post, "text": text, "link": f"https://t.me/{data_post}", "message_date": msg_date})
        return messages
    except Exception as e:
        print(f"[PROMO] ❌ {type(e).__name__}: {e}"); return []

def promo_parser_thread():
    time.sleep(10)
    while True:
        try:
            messages = fetch_promo_channel(); new_count = 0
            for m in messages:
                if query_one("SELECT id FROM promocodes WHERE tg_msg_id=?", (m["tg_msg_id"],)): continue
                execute(f"""INSERT INTO promocodes (tg_msg_id,text,link,message_date,discovered_at,used)
                            VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},0)""",
                        (m["tg_msg_id"], m["text"], m["link"], m["message_date"], time.time()))
                new_count += 1
            if new_count: print(f"[PROMO] ✅ +{new_count} из @{PROMO_CHANNEL}")
        except Exception as e: print(f"[PROMO] ❌ {type(e).__name__}: {e}")
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
    if not row: raise HTTPException(404, "not found")
    nv = 0 if row["used"] else 1
    execute("UPDATE promocodes SET used=?, used_at=? WHERE id=?", (nv, time.time() if nv else None, promo_id))
    return {"ok": True, "used": nv}

@app.post("/api/promo/to_vault/{promo_id}")
async def promo_to_vault(promo_id: int, body: dict = None):
    row = query_one("SELECT * FROM promocodes WHERE id=?", (promo_id,))
    if not row: raise HTTPException(404, "not found")
    body = body or {}
    amount_usd = float(body.get("amount_usd") or 0)
    currency = (body.get("currency") or "USD").upper()
    execute("UPDATE promocodes SET used_for_vault=1, vault_amount_usd=? WHERE id=?", (amount_usd, promo_id))
    execute(f"""INSERT INTO vault_transactions (ts,type,currency,amount,amount_usd,description)
                VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()})""",
            (time.time(), "promo_code", currency, amount_usd, amount_usd, f"Промокод: {row['text'][:100]}"))
    return {"ok": True}

@app.get("/api/promocodes/stats")
async def api_promocodes_stats():
    total = (query_one("SELECT COUNT(*) AS c FROM promocodes") or {}).get("c", 0)
    used = (query_one("SELECT COUNT(*) AS c FROM promocodes WHERE used=1") or {}).get("c", 0)
    return {"total": total, "used": used, "unused": total - used}

@app.post("/api/promo/test")
async def api_promo_test():
    messages = fetch_promo_channel(); new_count = 0
    for m in messages:
        if query_one("SELECT id FROM promocodes WHERE tg_msg_id=?", (m["tg_msg_id"],)): continue
        execute(f"""INSERT INTO promocodes (tg_msg_id,text,link,message_date,discovered_at,used)
                    VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},0)""",
                (m["tg_msg_id"], m["text"], m["link"], m["message_date"], time.time()))
        new_count += 1
    return {"ok": True, "found": len(messages), "new": new_count}

# ==================== VAULT ====================
@app.get("/api/vault")
async def get_vault():
    balances = query_all("SELECT * FROM vault_balances ORDER BY currency")
    wager = query_one("SELECT * FROM wager_state WHERE id=1") or {"amount_required": 0, "deadline_ts": 0}
    goal_usd = float(vault_meta_get("goal_usd") or 0)
    goal_text = vault_meta_get("goal_text") or ""
    now = time.time()
    week_ago = now - 7 * 86400
    income_week = (query_one("SELECT COALESCE(SUM(amount_usd),0) AS s FROM vault_transactions WHERE ts>=? AND type='income'", (week_ago,)) or {"s": 0})["s"] or 0
    wager_week = (query_one("SELECT COALESCE(SUM(amount_usd),0) AS s FROM vault_transactions WHERE ts>=? AND type='wager_spend'", (week_ago,)) or {"s": 0})["s"] or 0
    vault_add_week = (query_one("SELECT COALESCE(SUM(amount_usd),0) AS s FROM vault_transactions WHERE ts>=? AND type='vault_add'", (week_ago,)) or {"s": 0})["s"] or 0
    total_usd = sum(float(b.get("amount_usd") or 0) for b in balances)
    days_left = max(0, int((wager.get("deadline_ts", 0) - now) / 86400)) if wager.get("deadline_ts") else None
    hours_left = max(0, int((wager.get("deadline_ts", 0) - now) / 3600)) if wager.get("deadline_ts") else None
    return {"balances": balances, "total_usd": round(total_usd, 2),
            "wager": {"amount_required": wager.get("amount_required", 0),
                      "deadline_ts": wager.get("deadline_ts", 0),
                      "days_left": days_left, "hours_left": hours_left,
                      "is_expired": (wager.get("deadline_ts", 0) > 0 and wager.get("deadline_ts", 0) < now)},
            "goal_usd": goal_usd, "goal_text": goal_text,
            "week": {"income": round(income_week, 2), "wager_spend": round(wager_week, 2), "vault_add": round(vault_add_week, 2)},
            "now": now}

@app.post("/api/vault/balance")
async def set_vault_balance(body: VaultBalanceIn):
    cur = body.currency.upper().strip()
    if not cur: raise HTTPException(400, "currency required")
    amt = float(body.amount or 0); amt_usd = body.amount_usd
    if amt_usd is None:
        if cur in ("USD", "USDT", "USDC", "DAI", "CASH"): amt_usd = amt
        else:
            rate = server_rate_usd(cur); amt_usd = amt * rate if rate else 0.0
    p = ph()
    if USE_POSTGRES:
        execute(f"""INSERT INTO vault_balances (currency,amount,amount_usd,updated_at) VALUES ({p},{p},{p},{p})
                    ON CONFLICT (currency) DO UPDATE SET amount=EXCLUDED.amount,amount_usd=EXCLUDED.amount_usd,updated_at=EXCLUDED.updated_at""",
                (cur, amt, amt_usd, time.time()))
    else:
        execute(f"""INSERT INTO vault_balances (currency,amount,amount_usd,updated_at) VALUES ({p},{p},{p},{p})
                    ON CONFLICT (currency) DO UPDATE SET amount=excluded.amount,amount_usd=excluded.amount_usd,updated_at=excluded.updated_at""",
                (cur, amt, amt_usd, time.time()))
    return {"ok": True}

@app.post("/api/vault/balance/delete")
async def delete_vault_balance(body: dict):
    cur = (body.get("currency") or "").upper().strip()
    if not cur: raise HTTPException(400, "currency required")
    execute("DELETE FROM vault_balances WHERE currency=?", (cur,))
    return {"ok": True}

def _add_to_balance(cur, delta_amt, delta_usd):
    cur = cur.upper().strip()
    row = query_one("SELECT amount, amount_usd FROM vault_balances WHERE currency=?", (cur,))
    new_amt = (float(row["amount"]) if row else 0.0) + delta_amt
    new_usd = (float(row["amount_usd"]) if row else 0.0) + delta_usd
    if new_amt < 0: new_amt = 0.0
    if new_usd < 0: new_usd = 0.0
    p = ph()
    if USE_POSTGRES:
        execute(f"""INSERT INTO vault_balances (currency,amount,amount_usd,updated_at) VALUES ({p},{p},{p},{p})
                    ON CONFLICT (currency) DO UPDATE SET amount=EXCLUDED.amount,amount_usd=EXCLUDED.amount_usd,updated_at=EXCLUDED.updated_at""",
                (cur, new_amt, new_usd, time.time()))
    else:
        execute(f"""INSERT INTO vault_balances (currency,amount,amount_usd,updated_at) VALUES ({p},{p},{p},{p})
                    ON CONFLICT (currency) DO UPDATE SET amount=excluded.amount,amount_usd=excluded.amount_usd,updated_at=excluded.updated_at""",
                (cur, new_amt, new_usd, time.time()))

@app.post("/api/vault/transaction")
async def add_vault_transaction(body: VaultTransactionIn):
    typ = body.type.strip()
    if typ not in ("income", "wager_spend", "vault_add", "vault_take", "promo_code"):
        raise HTTPException(400, "invalid type")
    ts_val = body.ts if body.ts else time.time()
    cur = (body.currency or "USDT").upper().strip()
    amount = float(body.amount or 0); amount_usd = float(body.amount_usd or 0)
    if amount_usd == 0 and amount != 0:
        if cur in ("USD", "USDT", "USDC", "DAI", "CASH"): amount_usd = amount
        else:
            rate = server_rate_usd(cur); amount_usd = amount * rate if rate else 0.0
    execute(f"""INSERT INTO vault_transactions (ts,type,currency,amount,amount_usd,description)
                VALUES ({ph()},{ph()},{ph()},{ph()},{ph()},{ph()})""",
            (ts_val, typ, cur, amount, amount_usd, body.description or ""))
    if typ == "vault_add" and amount: _add_to_balance(cur, amount, amount_usd)
    if typ == "vault_take" and amount: _add_to_balance(cur, -amount, -amount_usd)
    return {"ok": True}

@app.get("/api/vault/transactions")
async def list_vault_transactions(days: int = 30, type: Optional[str] = None, limit: int = 500):
    since = time.time() - days * 86400
    q = "SELECT * FROM vault_transactions WHERE ts>=?"; p = [since]
    if type: q += " AND type=?"; p.append(type)
    q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
    return {"transactions": query_all(q, tuple(p))}

@app.post("/api/vault/wager")
async def set_wager(body: VaultWagerIn):
    p = ph()
    if USE_POSTGRES:
        execute(f"""INSERT INTO wager_state (id,amount_required,deadline_ts,updated_at) VALUES (1,{p},{p},{p})
                    ON CONFLICT (id) DO UPDATE SET amount_required=EXCLUDED.amount_required,
                    deadline_ts=EXCLUDED.deadline_ts,updated_at=EXCLUDED.updated_at""",
                (body.amount_required, body.deadline_ts, time.time()))
    else:
        execute(f"""INSERT INTO wager_state (id,amount_required,deadline_ts,updated_at) VALUES (1,{p},{p},{p})
                    ON CONFLICT (id) DO UPDATE SET amount_required=excluded.amount_required,
                    deadline_ts=excluded.deadline_ts,updated_at=excluded.updated_at""",
                (body.amount_required, body.deadline_ts, time.time()))
    if body.spent_now is not None and body.spent_now > 0:
        execute(f"""INSERT INTO vault_transactions (ts,type,currency,amount,amount_usd,description)
                    VALUES ({ph()},'wager_spend','USD',{ph()},{ph()},'Ручная установка')""",
                (time.time(), body.spent_now, body.spent_now))
    return {"ok": True}

@app.post("/api/vault/goal")
async def set_goal(body: VaultGoalIn):
    vault_meta_set("goal_usd", body.goal_usd); vault_meta_set("goal_text", body.goal_text)
    return {"ok": True}

# ==================== AI ====================
@app.get("/api/chat/pending")
async def chat_pending(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    task = query_one("SELECT id, chat FROM chat_tasks WHERE status='pending' ORDER BY created_at ASC LIMIT 1")
    if not task: return {"task": None}
    execute("UPDATE chat_tasks SET status='reading' WHERE id=?", (task["id"],))
    return {"task": task}

@app.post("/api/chat/complete")
async def chat_complete(body: ChatTaskCompleteIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    task = query_one("SELECT * FROM chat_tasks WHERE id=?", (body.task_id,))
    if not task: raise HTTPException(404, "task not found")
    if body.error:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?", (body.error, time.time(), body.task_id))
        return {"ok": True, "status": "failed"}
    execute("UPDATE chat_tasks SET messages=? WHERE id=?", (json.dumps(body.messages, ensure_ascii=False), body.task_id))
    threading.Thread(target=_process_chat_task_groq, args=(body.task_id, task["chat"], body.messages), daemon=True).start()
    return {"ok": True, "status": "processing"}

def _process_chat_task_groq(task_id, chat_name, messages):
    if not GROQ_API_KEY or not GROQ_API_KEY.startswith("gsk_"):
        execute("UPDATE chat_tasks SET status='failed', error='GROQ_API_KEY не задан', completed_at=? WHERE id=?", (time.time(), task_id)); return
    lang = LANG_MAP.get(chat_name, chat_name.lower())
    lines = [f"{m.get('user', '?')}: {m.get('text', '')}" for m in (messages or [])[-50:] if m.get("text")]
    context = "\n".join(lines)
    sp = (f"Ты носитель {lang} и активный участник игрового чата казино. "
          f"Дан контекст из последних 50 сообщений. Предложи 5 коротких живых сообщений на {lang} (до 100 символов) "
          f"и 5 нейтральных фраз. Ответь СТРОГО JSON: "
          f'{{"topic": "...", "messages": ["..."], "phrases": ["..."]}}')
    models = [GROQ_MODEL, "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    content = None; last_error = ""
    for m in models:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                              json={"model": m, "messages": [{"role": "system", "content": sp}, {"role": "user", "content": context}],
                                    "max_tokens": 800, "temperature": 0.8}, timeout=30)
            if r.status_code == 200:
                content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip(); break
            elif r.status_code == 404: last_error = f"{m} not found"; continue
            else: last_error = f"HTTP {r.status_code}"; break
        except Exception as e: last_error = f"{type(e).__name__}"
    if not content:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?", (last_error, time.time(), task_id)); return
    mm = re.search(r'\{[\s\S]*\}', content)
    if not mm:
        execute("UPDATE chat_tasks SET status='failed', error='не-JSON', completed_at=? WHERE id=?", (time.time(), task_id)); return
    try: data = json.loads(mm.group(0))
    except Exception as e:
        execute("UPDATE chat_tasks SET status='failed', error=?, completed_at=? WHERE id=?", (f"JSON: {e}", time.time(), task_id)); return
    result = {"topic": data.get("topic", ""), "messages": data.get("messages", [])[:5], "phrases": data.get("phrases", [])[:5]}
    execute("UPDATE chat_tasks SET status='completed', result=?, completed_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), time.time(), task_id))
    print(f"[AI] ✅ Task #{task_id} ({chat_name})")

@app.post("/api/chat/request")
async def chat_request(body: ChatTaskIn):
    if not body.chat: raise HTTPException(400, "chat required")
    execute(f"INSERT INTO chat_tasks (chat,status,created_at) VALUES ({ph()},'pending',{ph()})", (body.chat, time.time()))
    task = query_one("SELECT id FROM chat_tasks WHERE chat=? AND status='pending' ORDER BY id DESC LIMIT 1", (body.chat,))
    return {"ok": True, "task_id": task["id"] if task else None}

@app.get("/api/chat/task/{task_id}")
async def chat_task_get(task_id: int):
    task = query_one("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
    if not task: raise HTTPException(404, "not found")
    result = None
    if task.get("result"):
        try: result = json.loads(task["result"])
        except: pass
    return {"id": task["id"], "chat": task["chat"], "status": task["status"], "result": result, "error": task.get("error"),
            "created_at": task.get("created_at"), "completed_at": task.get("completed_at")}

@app.get("/api/chat/latest")
async def chat_latest(chat: Optional[str] = None):
    q = "SELECT * FROM chat_tasks WHERE status='completed'"; p = []
    if chat: q += " AND chat=?"; p.append(chat)
    q += " ORDER BY completed_at DESC LIMIT 1"
    task = query_one(q, tuple(p))
    if not task: return {"task": None}
    result = None
    if task.get("result"):
        try: result = json.loads(task["result"])
        except: pass
    return {"task": {"id": task["id"], "chat": task["chat"], "result": result, "completed_at": task["completed_at"]}}

# ==================== API UI ====================
@app.get("/api/rates")
async def api_rates():
    with _server_rates_lock: return {"rates": dict(_server_rates), "updated_at": _server_rates_updated[0]}

@app.get("/api/status")
async def get_status():
    with _state_lock: instances = list(INSTANCE_STATES.values())
    instances.sort(key=lambda x: x.get("iid", 0))
    s = query_one("SELECT COUNT(*) AS c FROM events") or {"c": 0}
    tips = query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {"c": 0}
    rains = query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {"c": 0}
    first = query_one("SELECT MIN(ts) AS m FROM events WHERE ts IS NOT NULL") or {"m": None}
    bets_c = query_one("SELECT COUNT(*) AS c FROM bets") or {"c": 0}
    intents_c = query_one("SELECT COUNT(*) AS c FROM intents") or {"c": 0}
    return {"first_ts": first.get("m"), "total": s.get("c", 0), "tips": tips.get("c", 0), "rains": rains.get("c", 0),
            "bets": bets_c.get("c", 0), "intents": intents_c.get("c", 0), "instances": instances}

@app.post("/api/screenshot/{iid}")
async def request_screenshot(iid: int):
    with _state_lock: SCREENSHOT_REQUESTS[iid] = {"requested_at": time.time(), "done": False, "path": None}
    return {"ok": True}

@app.get("/api/screenshot/{iid}")
async def get_screenshot(iid: int):
    with _state_lock: r = SCREENSHOT_REQUESTS.get(iid)
    if not r or not r.get("done"): return JSONResponse(status_code=202, content={"pending": True})
    path = r.get("path")
    if not path or not os.path.exists(path): raise HTTPException(404, "not_available")
    return FileResponse(path, media_type="image/png")

@app.get("/api/events")
async def api_events(since: Optional[float] = None, until: Optional[float] = None,
                     type: Optional[str] = None, chat: Optional[str] = None,
                     sender: Optional[str] = None, min_rub: Optional[float] = None,
                     last_hours: Optional[int] = None, limit: int = 500):
    q = "SELECT * FROM events WHERE 1=1"; p = []
    if last_hours: since = time.time() - last_hours * 3600
    if since is not None: q += " AND ts>=?"; p.append(since)
    if until is not None: q += " AND ts<=?"; p.append(until)
    if type: q += " AND type=?"; p.append(type)
    if chat: q += " AND chat=?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_rub is not None: q += " AND amount_rub>=?"; p.append(min_rub)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
        try: r["receivers_info"] = json.loads(r.get("receivers_info") or "[]")
        except: r["receivers_info"] = []
        rc = len(r["receivers"]) or 1
        r["per_person_rub"] = (r.get("amount_rub") or 0) / rc
    return {"events": rows}

@app.get("/api/receivers_top")
async def api_receivers_top(limit: int = 100, metric: str = "total", days: int = 30, window_min: int = 25):
    since = time.time() - days * 86400
    rows = query_all("SELECT chat,ts,amount_rub,receivers,receivers_info FROM events WHERE type='rain' AND ts>=? ORDER BY ts DESC", (since,))
    stats = defaultdict(lambda: {"total_rub": 0.0, "wins": 0, "active_wins": 0, "unknown_activity_wins": 0, "chats": defaultdict(int), "last_seen": 0.0})
    for r in rows:
        try: recs = json.loads(r.get("receivers") or "[]")
        except: recs = []
        if not recs: continue
        try: rinfo = json.loads(r.get("receivers_info") or "[]")
        except: rinfo = []
        rmap = {x.get("name"): x for x in rinfo if isinstance(x, dict) and x.get("name")}
        amt = r.get("amount_rub") or 0; per = amt / len(recs); chat = r.get("chat") or "?"
        for nick in recs:
            if not nick: continue
            s = stats[nick]; s["total_rub"] += per; s["wins"] += 1; s["chats"][chat] += 1
            info = rmap.get(nick)
            if info:
                if info.get("wrote_in_25min"): s["active_wins"] += 1
                try: ls = float(info.get("last_msg_ts") or 0)
                except: ls = 0
                if ls > s["last_seen"]: s["last_seen"] = ls
            else: s["unknown_activity_wins"] += 1
    arr = []
    for nick, s in stats.items():
        known = s["wins"] - s["unknown_activity_wins"]
        pct = (s["active_wins"] / known * 100) if known > 0 else None
        arr.append({"nickname": nick, "total_rub": round(s["total_rub"], 2), "wins": s["wins"],
                    "active_wins": s["active_wins"], "known_wins": known,
                    "active_percent": round(pct, 1) if pct is not None else None,
                    "chats": dict(s["chats"]), "last_seen": s["last_seen"]})
    if metric == "count": arr.sort(key=lambda x: x["wins"], reverse=True)
    elif metric == "active_percent": arr.sort(key=lambda x: (x["active_percent"] if x["active_percent"] is not None else -1, x["wins"]), reverse=True)
    else: arr.sort(key=lambda x: x["total_rub"], reverse=True)
    return {"players": arr[:limit], "total": len(arr), "window_min": window_min, "days": days}

@app.get("/api/chart")
async def api_chart(type: Optional[str] = None, chat: Optional[str] = None,
                    since: Optional[float] = None, until: Optional[float] = None, last_hours: Optional[int] = None):
    if last_hours: since = time.time() - last_hours * 3600
    hour_expr = "CAST(EXTRACT(HOUR FROM to_timestamp(ts)) AS INTEGER)" if USE_POSTGRES else "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER)"
    q = f"SELECT {hour_expr} AS h, COUNT(*) AS c, COALESCE(SUM(amount_rub),0) AS s FROM events WHERE ts IS NOT NULL"
    p = []
    if since is not None: q += " AND ts>=?"; p.append(since)
    if until is not None: q += " AND ts<=?"; p.append(until)
    if type: q += " AND type=?"; p.append(type)
    if chat: q += " AND chat=?"; p.append(chat)
    q += " GROUP BY h"
    rows = query_all(q, tuple(p))
    buckets = {h: {"count": 0, "sum": 0.0} for h in range(24)}
    for r in rows:
        if r.get("h") is not None: buckets[int(r["h"])] = {"count": r["c"], "sum": r["s"] or 0}
    return {"buckets": buckets}

@app.get("/api/days")
async def api_days():
    day_expr = "to_char(to_timestamp(ts),'YYYY-MM-DD')" if USE_POSTGRES else "strftime('%Y-%m-%d',ts,'unixepoch')"
    rows = query_all(f"""SELECT {day_expr} AS day, COUNT(*) AS cnt,
                         SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
                         SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips
                         FROM events WHERE ts IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 90""")
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
    q = "SELECT id,ts,created_at,chat,sender,receivers,receivers_info,amount_text,amount_rub,amount_usd,crypto_symbol,context_messages,context_bets FROM events WHERE type='rain'"
    p = []
    if chat: q += " AND chat=?"; p.append(chat)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        for k in ("receivers", "receivers_info", "context_messages", "context_bets"):
            try: r[k] = json.loads(r.get(k) or "[]")
            except: r[k] = []
    return {"contexts": rows}

@app.get("/api/intents")
async def api_intents(chat: Optional[str] = None, sender: Optional[str] = None,
                      only_matched: Optional[int] = None, limit: int = 100):
    q = "SELECT * FROM intents WHERE 1=1"; p = []
    if chat: q += " AND chat=?"; p.append(chat)
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
                            ROUND(CAST(SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS NUMERIC)/CAST(COUNT(*) AS NUMERIC)*100,1) AS percent
                            FROM intents GROUP BY sender HAVING COUNT(*)>=2 ORDER BY percent DESC, matched DESC LIMIT 50""")
    else:
        rows = query_all("""SELECT sender AS nickname, COUNT(*) AS total,
                            SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS matched,
                            ROUND(AVG(percent),0) AS avg_percent,
                            ROUND(CAST(SUM(CASE WHEN matched_rain_id IS NOT NULL THEN 1 ELSE 0 END) AS REAL)/CAST(COUNT(*) AS REAL)*100,1) AS percent
                            FROM intents GROUP BY sender HAVING COUNT(*)>=2 ORDER BY percent DESC, matched DESC LIMIT 50""")
    return {"stats": rows}

@app.get("/api/bulk")
async def api_bulk(since: Optional[float] = None, until: Optional[float] = None, senders: Optional[str] = None, limit: int = 500):
    q = "SELECT * FROM bulk_alerts WHERE 1=1"; p = []
    if since is not None: q += " AND ts>=?"; p.append(since)
    if until is not None: q += " AND ts<=?"; p.append(until)
    if senders:
        names = [s.strip() for s in senders.split(",") if s.strip()]
        if names:
            phs = ",".join(["?"] * len(names)); q += f" AND sender IN ({phs})"; p.extend(names)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
    return {"alerts": rows}

@app.get("/api/player")
async def api_player(username: Optional[str] = None, chat: Optional[str] = None, since: Optional[float] = None, limit: int = 500):
    q = "SELECT * FROM player_activity WHERE 1=1"; p = []
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if chat: q += " AND chat=?"; p.append(chat)
    if since is not None: q += " AND ts>=?"; p.append(since)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    return {"rows": query_all(q, tuple(p)), "hourly": {h: 0 for h in range(24)}}

@app.get("/api/player_names")
async def api_player_names():
    rows = query_all("SELECT DISTINCT username FROM player_activity ORDER BY username")
    return {"names": [r["username"] for r in rows]}

@app.get("/api/players")
async def api_players(chat: Optional[str] = None, username: Optional[str] = None,
                      online_only: Optional[int] = None, min_msgs: Optional[int] = None, limit: int = 500):
    q = "SELECT chat,username,msg_count,first_seen,last_seen FROM chat_players WHERE 1=1"; p = []
    if chat: q += " AND chat=?"; p.append(chat)
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if min_msgs is not None: q += " AND msg_count>=?"; p.append(min_msgs)
    if online_only == 1: q += " AND last_seen>=?"; p.append(time.time() - 300)
    q += " ORDER BY last_seen DESC NULLS LAST, msg_count DESC LIMIT ?" if USE_POSTGRES else " ORDER BY last_seen DESC, msg_count DESC LIMIT ?"
    p.append(limit)
    return {"players": query_all(q, tuple(p))}

@app.post("/api/players/relocate")
async def api_players_relocate(body: dict):
    username = (body.get("username") or "").strip(); new_chat = (body.get("chat") or "").strip()
    if not username or not new_chat: raise HTTPException(400, "username, chat required")
    p = ph()
    if USE_POSTGRES:
        execute(f"""INSERT INTO chat_players (chat,username,msg_count,first_seen,last_seen)
                    VALUES ({p},{p},0,{p},{p}) ON CONFLICT (chat,username) DO NOTHING""",
                (new_chat, username, time.time(), time.time()))
    else:
        execute(f"""INSERT OR IGNORE INTO chat_players (chat,username,msg_count,first_seen,last_seen)
                    VALUES (?,?,0,?,?)""", (new_chat, username, time.time(), time.time()))
    return {"ok": True}

@app.get("/api/players_by_chat")
async def api_players_by_chat():
    cutoff = time.time() - 300
    rows = query_all("""SELECT chat, COUNT(*) AS total_players,
                        SUM(CASE WHEN last_seen>=? THEN 1 ELSE 0 END) AS online_players,
                        SUM(msg_count) AS total_messages
                        FROM chat_players GROUP BY chat ORDER BY total_players DESC""", (cutoff,))
    return {"chats": rows}

@app.get("/api/players_by_language")
async def api_players_by_language():
    if USE_POSTGRES:
        rows = query_all("""SELECT username, STRING_AGG(chat,',') AS chats_raw,
                            COUNT(DISTINCT chat) AS chat_count, SUM(msg_count) AS total_msgs, MAX(last_seen) AS last_seen
                            FROM chat_players GROUP BY username ORDER BY SUM(msg_count) DESC LIMIT 3000""")
    else:
        rows = query_all("""SELECT username, GROUP_CONCAT(chat) AS chats_raw,
                            COUNT(DISTINCT chat) AS chat_count, SUM(msg_count) AS total_msgs, MAX(last_seen) AS last_seen
                            FROM chat_players GROUP BY username ORDER BY SUM(msg_count) DESC LIMIT 3000""")
    out = []
    for r in rows:
        chats = list(set((r.get("chats_raw") or "").split(",")))
        r["chats"] = [c.strip() for c in chats if c.strip()]; r.pop("chats_raw", None); out.append(r)
    return {"players": out}

@app.get("/api/bets_to_rains")
async def api_bets_to_rains(window_sec: int = BET_TO_RAIN_WINDOW_SEC, last_hours: Optional[int] = None):
    since = time.time() - (last_hours * 3600 if last_hours else 7 * 86400)
    bets = query_all("SELECT id,ts,sender FROM bets WHERE ts>=? AND COALESCE(amount_usd,0)>=20 ORDER BY ts ASC", (since,))
    rains = query_all("SELECT ts,sender,amount_rub FROM events WHERE type='rain' AND ts>=? AND ts IS NOT NULL ORDER BY ts ASC", (since,))
    rlist = sorted([(r["ts"], (r.get("sender") or "").strip(), r.get("amount_rub") or 0) for r in rains if r.get("ts")])
    stats = defaultdict(lambda: {"bets": 0, "triggered": 0, "total_rain_rub": 0.0})
    for b in bets:
        s = (b.get("sender") or "").strip()
        if not s: continue
        stats[s]["bets"] += 1; t0 = b["ts"]
        for rt, rs, rr in rlist:
            if rt < t0: continue
            if rt > t0 + window_sec: break
            if rs != s: continue
            stats[s]["triggered"] += 1; stats[s]["total_rain_rub"] += rr; break
    arr = []
    for nick, d in stats.items():
        if d["bets"] < 3: continue
        pct = (d["triggered"] / d["bets"] * 100) if d["bets"] else 0
        arr.append({"nickname": nick, "bets": d["bets"], "triggered": d["triggered"], "percent": round(pct, 1), "rain_rub": round(d["total_rain_rub"], 2)})
    arr.sort(key=lambda x: (-x["percent"], -x["bets"]))
    return {"stats": arr[:100], "window_sec": window_sec}

@app.get("/api/bets")
async def api_bets(since: Optional[float] = None, until: Optional[float] = None, chat: Optional[str] = None,
                   sender: Optional[str] = None, min_usd: Optional[float] = None, source: Optional[str] = None,
                   last_hours: Optional[int] = None, limit: int = 500):
    if last_hours: since = time.time() - last_hours * 3600
    q = "SELECT * FROM bets WHERE 1=1"; p = []
    if since is not None: q += " AND ts>=?"; p.append(since)
    if until is not None: q += " AND ts<=?"; p.append(until)
    if chat: q += " AND chat=?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_usd is not None: q += " AND amount_usd>=?"; p.append(min_usd)
    if source: q += " AND source=?"; p.append(source)
    q += " ORDER BY ts DESC NULLS LAST, id DESC LIMIT ?" if USE_POSTGRES else " ORDER BY ts DESC, id DESC LIMIT ?"
    p.append(limit)
    return {"bets": query_all(q, tuple(p))}

@app.get("/api/bets_stats")
async def api_bets_stats():
    since = time.time() - 86400
    rows = query_all("""SELECT chat, COUNT(*) AS cnt, COALESCE(SUM(amount_usd),0) AS total_usd,
                        COALESCE(AVG(amount_usd),0) AS avg_usd FROM bets WHERE ts>=? GROUP BY chat ORDER BY total_usd DESC""", (since,))
    return {"stats": rows}

@app.get("/api/bets_top_senders")
async def api_bets_top_senders(last_hours: Optional[int] = None):
    cleared = float(meta_get("bets_top_cleared_at") or 0)
    since = time.time() - (last_hours * 3600 if last_hours else 7 * 86400); since = max(since, cleared)
    rows = query_all("""SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_usd),0) AS total_usd
                        FROM bets WHERE ts>=? GROUP BY sender ORDER BY total_usd DESC LIMIT 30""", (since,))
    return {"senders": rows, "cleared_at": cleared, "since": since}

@app.get("/api/health")
async def health():
    return {"ok": True, "groq": bool(GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"))}

@app.get("/api/stats")
async def get_stats():
    total_rains = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {}).get("c", 0)
    total_tips = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {}).get("c", 0)
    trub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'") or {}).get("s", 0)
    ttrub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'") or {}).get("s", 0)
    recent = query_all("""SELECT type,chat,sender,receivers,amount_text,crypto_symbol,amount_rub,amount_usd,created_at,ts
                          FROM events ORDER BY id DESC LIMIT 30""")
    for r in recent:
        try: rcs = json.loads(r.get("receivers") or "[]")
        except: rcs = []
        r["receivers"] = rcs
        r["per_person_rub"] = (r.get("amount_rub") or 0) / max(len(rcs), 1)
    return {"totals": {"rains": total_rains, "tips": total_tips,
                       "rain_rub": round(trub or 0, 2), "tip_rub": round(ttrub or 0, 2)},
            "recent": recent, "generated_at": datetime.now(timezone.utc).isoformat()}

@app.post("/api/clear")
async def clear_database(body: dict, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY: raise HTTPException(401, "Invalid API key")
    if body.get("confirm") != "DELETE_ALL": raise HTTPException(400, "Подтверждение обязательно")
    if USE_POSTGRES: execute("TRUNCATE TABLE events RESTART IDENTITY CASCADE;")
    else: execute("DELETE FROM events;")
    return {"ok": True, "message": "Все данные удалены"}

# ==================== BASE HTML ====================
BASE_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{margin:0;background:#0e1116;color:#e6e9ef;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif}}
a{{color:#7cc4ff;text-decoration:none}}
header{{background:#161b22;border-bottom:1px solid #26303a;padding:14px 24px;display:flex;align-items:center;gap:24px;flex-wrap:wrap}}
.brand{{font-weight:700;font-size:18px;color:#fff}}
nav a{{margin-right:16px;color:#c9d1d9}}
nav a.active{{color:#7cc4ff;font-weight:600}}
main{{max-width:1200px;margin:0 auto;padding:24px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
.card{{background:#161b22;border:1px solid #26303a;border-radius:12px;padding:16px 20px}}
.card h3{{margin:0 0 8px;color:#8b949e;font-weight:500;font-size:13px;text-transform:uppercase}}
.card .big{{font-size:26px;font-weight:700;color:#fff}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #26303a}}
th{{color:#8b949e;font-weight:500;font-size:13px}}
td.amount{{color:#4ade80;font-weight:600}}
tr:hover td{{background:#1c222b}}
.rank{{display:inline-block;width:28px;text-align:center;color:#8b949e}}
.rank.g{{color:#f5c542}}.rank.s{{color:#c0c0c0}}.rank.b{{color:#cd7f32}}
.tag{{display:inline-block;background:#21262d;padding:2px 8px;border-radius:6px;font-size:11px;color:#8b949e;margin-left:6px}}
.muted{{color:#8b949e}}
.chat-pill{{display:inline-block;background:#21262d;padding:2px 8px;border-radius:6px;font-size:11px;color:#7cc4ff}}
.hint-btn{{display:inline-block;width:18px;height:18px;line-height:18px;text-align:center;border-radius:50%;
  background:#1e88e5;color:#fff;font-size:12px;cursor:help;font-weight:bold;margin-left:8px;
  position:relative;vertical-align:middle;flex-shrink:0;user-select:none}}
.hint-btn:hover{{background:#42a5f5}}
.hint-btn:hover::after{{content:attr(data-hint);position:absolute;top:26px;left:-200px;width:420px;
  background:#1a1f28;color:#e6e6e6;border:1px solid #2f3d55;border-radius:8px;padding:14px;
  font-size:12px;font-weight:normal;line-height:1.6;z-index:1000;white-space:pre-wrap;
  box-shadow:0 8px 30px rgba(0,0,0,.8);text-align:left;pointer-events:none}}
</style></head><body>
<header>
  <div class="brand">🌧 SHUFFLE RAIN <small>статистика</small></div>
  <nav>
    <a href="/" class="{active_home}">Главная</a>
    <a href="/hosts" class="{active_hosts}">Раздающие</a>
    <a href="/receivers" class="{active_receivers}">Получатели</a>
    <a href="/monitor">Мониторинг</a>
  </nav>
</header>
<main>{content}</main>
</body></html>"""

def fmt_rub(x):
    try: return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
    except: return "0 ₽"

def render_rank_table(rows):
    if not rows: return "<tr><td colspan='4' class='muted'>Пока нет данных</td></tr>"
    html = ""
    for i, r in enumerate(rows, 1):
        rc = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        html += f"""<tr><td><span class="rank {rc}">{i}</span></td>
            <td><b>{r['nickname']}</b></td><td>{r['cnt']}</td>
            <td class="amount">{fmt_rub(r['total'])}</td></tr>"""
    return html

# ==================== PAGES ====================
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
      function esc(s){{return (s==null?'':String(s)).replace(/[<>&"]/g,c=>({{'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}}[c]));}}
      function fmtR(v){{return (v||0).toFixed(0);}}
      async function loadOnce() {{ const r = await fetch('/api/stats'); const d = await r.json(); renderRecent(d.recent); }}
      function renderRecent(rows) {{
        const el = document.getElementById('recent'); if (!rows) return;
        el.innerHTML = rows.map(r => {{
          const tag = r.type === 'rain' ? 'RAIN' : 'TIP';
          const amt = r.amount_text || '';
          const rub = r.amount_rub ? ' (' + fmtR(r.amount_rub) + ' ₽)' : '';
          const per = r.per_person_rub ? ' <span style="color:#8ab4f8;font-size:11px">по ' + fmtR(r.per_person_rub) + ' ₽</span>' : '';
          const recs = (r.receivers || []);
          const recsHtml = recs.slice(0,5).map(esc).join(', ') + (recs.length > 5 ? ` ...и ещё ${{recs.length-5}}` : '');
          return `<div style="padding:8px 0;border-bottom:1px solid #26303a;font-size:13px"><span class="tag">${{tag}}</span> <span class="chat-pill">${{r.chat}}</span> <b style="margin-left:6px">${{esc(r.sender)}}</b><span class="muted"> → ${{recsHtml}}</span>${{per}}<span style="float:right;color:#4ade80">${{esc(amt)}}${{rub}}</span></div>`;
        }}).join('');
      }}
      loadOnce();
      setInterval(loadOnce, 15000);
    </script>"""
    return BASE_HTML.format(title="Shuffle Rain Stats", active_home="active",
                             active_hosts="", active_receivers="", content=content)

@app.get("/receivers", response_class=HTMLResponse)
async def page_receivers():
    content = """
    <div class="card">
      <h3 style="display:flex;align-items:center;gap:6px">🎯 Топ активных получателей<span class="hint-btn" data-hint="Кто чаще всего получает дожди.&#10;&#10;Сортировка:&#10;• По сумме — сколько всего ₽ получил&#10;• По кол-ву — сколько раз попал под дождь&#10;• По % активных — какой процент выигрышей пришёл, когда он писал в чате за 25 мин до дождя.&#10;&#10;Чаты — где именно его дождило.">?</span></h3>
      <p style="color:#8b949e;font-size:12px">«Активный» = писал в том же чате за 25 минут до дождя.</p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
        <select id="rtMetric" onchange="loadReceiversTop()" style="background:#0d1117;color:#e6e6e6;border:1px solid #30363d;padding:6px;border-radius:5px">
          <option value="total">По сумме ₽</option><option value="count">По кол-ву</option><option value="active_percent">По % активных</option>
        </select>
        <select id="rtLimit" onchange="loadReceiversTop()" style="background:#0d1117;color:#e6e6e6;border:1px solid #30363d;padding:6px;border-radius:5px">
          <option value="20">Топ-20</option>
          <option value="50">Топ-50</option>
          <option value="100" selected>Топ-100</option>
          <option value="200">Топ-200</option>
          <option value="500">Все (до 500)</option>
        </select>
        <select id="rtDays" onchange="loadReceiversTop()" style="background:#0d1117;color:#e6e6e6;border:1px solid #30363d;padding:6px;border-radius:5px">
          <option value="7">7 дней</option><option value="30" selected>30 дней</option><option value="90">90 дней</option>
        </select>
        <button onclick="loadReceiversTop()" style="background:#2563eb;color:#fff;border:none;padding:6px 12px;border-radius:5px;cursor:pointer">Обновить</button>
        <span id="rtStatus" style="color:#8b949e;font-size:12px"></span>
      </div>
      <table><thead><tr><th>#</th><th>Ник</th><th>Получил ₽</th><th>Дождей</th><th>Активных</th><th>%</th><th>Чаты</th></tr></thead>
      <tbody id="rtBody"><tr><td colspan="7" class="muted">Загрузка...</td></tr></tbody></table>
    </div>
    <script>
      const esc = s => (s==null?'':String(s)).replace(/[<>&"]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
      const fmtMoney = v => (v||0).toLocaleString('ru-RU',{maximumFractionDigits:2});
      async function loadReceiversTop() {
        const m = document.getElementById('rtMetric').value;
        const l = document.getElementById('rtLimit').value;
        const d = document.getElementById('rtDays').value;
        document.getElementById('rtStatus').textContent = '⏳...';
        const r = await fetch('/api/receivers_top?metric='+m+'&limit='+l+'&days='+d).then(r=>r.json());
        const body = document.getElementById('rtBody');
        if (!r.players.length) { body.innerHTML = '<tr><td colspan="7" class="muted">Нет данных</td></tr>'; document.getElementById('rtStatus').textContent=''; return; }
        body.innerHTML = r.players.map((p,i) => {
          const chats = Object.entries(p.chats||{}).sort((a,b)=>b[1]-a[1]).slice(0,6).map(([c,n])=>'<span class="tag">'+esc(c)+':'+n+'</span>').join(' ');
          let pct = '<span class="muted">—</span>';
          if (p.active_percent !== null && p.active_percent !== undefined) {
            const col = p.active_percent >= 60 ? '#4ade80' : (p.active_percent >= 30 ? '#fbbf24' : '#f87171');
            pct = '<span style="color:'+col+';font-weight:600">'+p.active_percent+'%</span>';
          }
          return '<tr><td>'+(i+1)+'</td><td><b>'+esc(p.nickname)+'</b></td><td class="amount">'+fmtMoney(p.total_rub)+' ₽</td><td>'+p.wins+'</td><td>'+p.active_wins+'/'+p.known_wins+'</td><td>'+pct+'</td><td>'+chats+'</td></tr>';
        }).join('');
        document.getElementById('rtStatus').textContent = 'Найдено: '+r.total;
      }
      loadReceiversTop();
    </script>"""
    return BASE_HTML.format(title="Shuffle — Получатели", active_home="", active_hosts="", active_receivers="active", content=content)

@app.get("/hosts", response_class=HTMLResponse)
async def page_hosts():
    r1 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='rain' GROUP BY sender ORDER BY total DESC LIMIT 50")
    r2 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='rain' GROUP BY sender ORDER BY cnt DESC LIMIT 50")
    t1 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='tip' GROUP BY sender ORDER BY total DESC LIMIT 50")
    t2 = query_all("SELECT sender AS nickname, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total FROM events WHERE type='tip' GROUP BY sender ORDER BY cnt DESC LIMIT 50")

    def row_with_profile(r, i):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        nick = r['nickname']
        safe_nick = nick.replace("'", "\\'")
        return f"""<tr><td><span class="rank {rank_cls}">{i}</span></td>
            <td><a href="#" onclick="openProfile('{safe_nick}');return false"><b>{nick}</b></a></td>
            <td>{r['cnt']}</td><td class="amount">{fmt_rub(r['total'])}</td></tr>"""

    def table(rows):
        if not rows: return "<tr><td colspan='4' class='muted'>Пусто</td></tr>"
        return "".join(row_with_profile(r, i) for i, r in enumerate(rows, 1))

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3 style="display:flex;align-items:center;gap:6px">🌧 Раздал дождей — по сумме<span class="hint-btn" data-hint="Кто раздал больше всего ₽ за дожди.&#10;&#10;Клик по нику → профиль с активностью 15/25/60/1440м, ставками и последними сообщениями.">?</span></h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{table(r1)}</tbody></table></div>
      <div class="card"><h3 style="display:flex;align-items:center;gap:6px">🌧 Раздал дождей — по кол-ву<span class="hint-btn" data-hint="Кто чаще всего делал дожди.&#10;Даже если суммы маленькие — этот игрок заливает регулярно.">?</span></h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{table(r2)}</tbody></table></div>
      <div class="card"><h3 style="display:flex;align-items:center;gap:6px">💸 Отправил чаевых — по сумме<span class="hint-btn" data-hint="Кто раздал больше всего ₽ за чаевые (tips).">?</span></h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{table(t1)}</tbody></table></div>
      <div class="card"><h3 style="display:flex;align-items:center;gap:6px">💸 Отправил чаевых — по кол-ву<span class="hint-btn" data-hint="Кто чаще всего отправлял чаевые.">?</span></h3><table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead><tbody>{table(t2)}</tbody></table></div>
    </div>
    <div id="profileModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);align-items:center;justify-content:center;z-index:50;padding:20px">
      <div style="background:#161b22;border:1px solid #26303a;border-radius:12px;max-width:700px;width:100%;padding:24px;max-height:80vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h2 id="profName" style="margin:0;color:#7cc4ff"></h2>
          <button onclick="document.getElementById('profileModal').style.display='none'" style="background:#2a3340;color:#e6e6e6;border:1px solid #334055;padding:4px 12px;border-radius:5px;cursor:pointer">✕</button>
        </div>
        <div style="margin-bottom:12px"><label style="color:#8b949e;font-size:12px">Язык:</label>
          <select id="profLang" style="background:#0d1117;color:#e6e6e6;border:1px solid #30363d;padding:6px;border-radius:5px;margin-left:8px">
            <option>ENGLISH</option><option>RUSSIAN</option><option>JAPANESE</option><option>TURKISH</option>
            <option>SPANISH</option><option>PORTUGUESE</option><option>FRENCH</option><option>KOREAN</option>
            <option>CHINESE</option><option>POLISH</option><option>VIETNAMESE</option><option>INDONESIAN</option>
          </select>
        </div>
        <div style="margin-bottom:12px"><label style="color:#8b949e;font-size:12px">Описание:</label>
          <textarea id="profDesc" style="width:100%;background:#0d1117;color:#e6e6e6;border:1px solid #30363d;padding:8px;border-radius:5px;margin-top:6px;min-height:80px"></textarea>
        </div>
        <div style="margin-bottom:16px"><label style="color:#8b949e;font-size:12px"><input type="checkbox" id="profTop"> Добавить в watch-list</label></div>
        <div id="profContent"></div>
        <button onclick="saveProfile()" style="background:#2563eb;color:#fff;border:none;padding:10px 20px;border-radius:6px;cursor:pointer;font-weight:600">💾 Сохранить</button>
      </div>
    </div>
    <script>
      let currentNick = null;
      async function openProfile(nick) {{
        currentNick = nick;
        document.getElementById('profileModal').style.display = 'flex';
        document.getElementById('profName').textContent = nick;
        const [prof, bets, msgs, activity] = await Promise.all([
          fetch('/api/profile/' + encodeURIComponent(nick)).then(r=>r.json()),
          fetch('/api/bets?sender=' + encodeURIComponent(nick) + '&limit=20').then(r=>r.json()),
          fetch('/api/player?username=' + encodeURIComponent(nick) + '&limit=15').then(r=>r.json()),
          fetch('/api/player/activity?username=' + encodeURIComponent(nick) + '&windows=15,25,60,1440').then(r=>r.json()).catch(()=>({{}}))
        ]);
        document.getElementById('profLang').value = prof.profile.lang || 'ENGLISH';
        document.getElementById('profDesc').value = prof.profile.description || '';
        document.getElementById('profTop').checked = !!prof.profile.is_top;
        const wins = (activity.windows || [15,25,60,1440]);
        const lbl = w => w === 1440 ? '24ч' : (w === 60 ? '1ч' : (w + 'м'));
        const actRow = wins.map(w => '<div style="background:#151821;border:1px solid #232a37;border-radius:8px;padding:8px 14px;min-width:100px">'
          + '<span style="color:#8b949e;font-size:11px">За ' + lbl(w) + '</span>'
          + '<b style="display:block;font-size:18px;color:' + (((activity.total||{{}})[w]>0)?'#4ade80':'#666') + '">' + ((activity.total||{{}})[w]||0) + '</b></div>').join('');
        const chatsAct = (activity.chats||[]).slice(0,12).map(c => {{
          const cells = wins.map(w => '<td style="text-align:center;color:' + ((c.counts[w]>0)?'#4ade80':'#555') + '">' + (c.counts[w]||0) + '</td>').join('');
          return '<tr><td><b>' + c.chat + '</b></td>' + cells + '</tr>';
        }}).join('');
        const actHtml = (activity.chats && activity.chats.length) ? '<table><thead><tr><th>Чат</th>' + wins.map(w=>'<th style="text-align:center">'+lbl(w)+'</th>').join('') + '</tr></thead><tbody>' + chatsAct + '</tbody></table>' : '<div class="muted">Нет данных</div>';
        const betsHtml = (bets.bets||[]).map(b => '<div style="padding:6px 10px;border-left:3px solid #71d68a;margin:4px 0;font-size:12px">🎰 ' + b.game + ' · ' + b.multiplier + ' · ' + b.amount_text + ' ' + (b.crypto||'') + ' (~$' + (b.amount_usd||0).toFixed(2) + ')</div>').join('') || '<div class="muted">Нет ставок</div>';
        const msgsHtml = (msgs.rows||[]).map(m => '<div style="padding:4px 8px;border-left:3px solid #2a2f3a;margin:4px 0;font-size:12px"><b>' + m.chat + '</b>: ' + m.text + '</div>').join('') || '<div class="muted">Нет сообщений</div>';
        document.getElementById('profContent').innerHTML =
          '<h4 style="color:#8b949e;font-size:12px;margin:16px 0 8px">📊 Активность</h4>'
          + '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px">' + actRow + '</div>'
          + actHtml
          + '<h4 style="color:#8b949e;font-size:12px;margin:16px 0 8px">Последние ставки</h4>' + betsHtml
          + '<h4 style="color:#8b949e;font-size:12px;margin:16px 0 8px">Последние сообщения</h4>' + msgsHtml;
      }}
      async function saveProfile() {{
        if(!currentNick) return;
        const body = {{ lang: document.getElementById('profLang').value, description: document.getElementById('profDesc').value, is_top: document.getElementById('profTop').checked }};
        await fetch('/api/profile/' + encodeURIComponent(currentNick), {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body)}});
        if(body.is_top) {{ await fetch('/api/watchlist/add', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{name: currentNick}})}}); }}
        alert('Сохранено');
      }}
    </script>"""
    return BASE_HTML.format(title="Shuffle — Раздающие", active_home="", active_hosts="active", active_receivers="", content=content)

# ==================== MONITOR ====================
MONITOR_HTML = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shuffle Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e6e6;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;background:#171a21;border-bottom:1px solid #2a2f3a;position:sticky;top:0;z-index:10;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
header a{color:#7cc4ff;text-decoration:none;font-size:13px}
.tabs{display:flex;gap:4px;padding:10px 20px;border-bottom:1px solid #2a2f3a;background:#12151b;flex-wrap:wrap}
.tabs button{background:transparent;color:#aab;border:1px solid transparent;padding:7px 12px;border-radius:6px;cursor:pointer;font-size:13px;display:inline-flex;align-items:center}
.tabs button.active{background:#202633;color:#fff;border-color:#2f3d55}
main{padding:20px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #20242e}
th{color:#8b93a7;font-weight:500;font-size:12px;text-transform:uppercase}
tr:hover td{background:#161a22}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:#232a37;color:#aab}
.pill.ok{background:#1e3a26;color:#71d68a}
.pill.warn{background:#3a3520;color:#e0c66a}
.pill.err{background:#3a1f1f;color:#e07a7a}
.pill.chat{background:#2a1f3a;color:#c58af9}
.pill.rubric{background:#3a2f1f;color:#e0c66a}
.btn{background:#2a3340;color:#e6e6e6;border:1px solid #334055;padding:5px 10px;border-radius:5px;cursor:pointer;font-size:12px}
.btn:hover{background:#334055}
.btn.danger{background:#3a1f1f;border-color:#7f1d1d}
.btn.green{background:#1e3a26;border-color:#2d5a3a;color:#71d68a}
.row-flex{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input,select,textarea{background:#171a21;color:#e6e6e6;border:1px solid #2a2f3a;padding:6px 10px;border-radius:5px;font-size:13px;max-width:280px;font-family:inherit}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:50}
.modal img{max-width:92vw;max-height:92vh;border-radius:8px}
.modal.on{display:flex}
.hint{color:#8b93a7;font-size:12px}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.kpi div{background:#151821;border:1px solid #232a37;border-radius:8px;padding:10px 16px;min-width:140px}
.kpi b{display:block;font-size:20px;color:#8ab4f8;margin-top:2px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.chip{background:#2a3340;color:#e6e6e6;padding:4px 8px;border-radius:14px;font-size:12px;display:inline-flex;align-items:center;gap:6px}
.chip .x{color:#f87171;cursor:pointer;font-weight:bold}
.bet-row{color:#71d68a}
.bet-row.chat{color:#c58af9}
.promo-card{background:#151821;border:1px solid #232a37;border-radius:8px;padding:12px 16px;margin-bottom:10px;display:flex;gap:12px;align-items:flex-start}
.promo-card.used{opacity:0.5}
.promo-card.vaulted{border-color:#2d5a3a}
.promo-cb{width:22px;height:22px;cursor:pointer;accent-color:#71d68a}
.promo-body{flex:1}
.promo-text{white-space:pre-wrap;font-size:13px;line-height:1.5}
.promo-meta{font-size:11px;color:#8b93a7;margin-top:6px}
.ai-grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}
.ai-col{background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px}
.ai-col h3{margin:0 0 10px;font-size:12px;color:#8b93a7;text-transform:uppercase}
.ai-msg{padding:10px 12px;background:#1a1f28;border-left:3px solid #7cc4ff;border-radius:4px;margin-bottom:8px;font-size:13px;cursor:pointer}
.ai-msg.copied{border-left-color:#71d68a;background:#1e3a26}
.ai-phrase{padding:8px 12px;background:#1a1f28;border-left:3px solid #c58af9;border-radius:4px;margin-bottom:6px;font-size:13px;cursor:pointer}
.ai-phrase.copied{border-left-color:#71d68a;background:#1e3a26}
.ai-topic{background:#202633;border:1px solid #2f3d55;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px}
.lang-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.lang-card{background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px}
.lang-card h4{margin:0 0 8px;font-size:13px;color:#8ab4f8}
.lang-card .player-row{display:flex;padding:4px 0;font-size:12px;border-bottom:1px solid #1e232e}
.recv-wrap{max-width:420px;font-size:12px}
.recv-visible{color:#aab}
.recv-hidden{display:none;color:#aab}
.recv-toggle{color:#7cc4ff;cursor:pointer;font-size:11px;margin-left:4px;text-decoration:underline}
tr.rubric-row td{background:#2a2410 !important}
.vault-card{background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px 18px;margin-bottom:12px}
.vault-card h3{margin:0 0 10px;font-size:13px;color:#8ab4f8;text-transform:uppercase}
.vault-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1e232e;font-size:13px}
.vault-row:last-child{border:none}
.vault-amount{color:#71d68a;font-weight:600}
.vault-warn{color:#fbbf24}
.vault-danger{color:#f87171}
.hint-btn{display:inline-block;width:18px;height:18px;line-height:18px;text-align:center;border-radius:50%;
  background:#1e88e5;color:#fff;font-size:12px;cursor:help;margin-left:8px;font-weight:bold;
  position:relative;vertical-align:middle;flex-shrink:0;border:none;padding:0;
  font-family:Arial,sans-serif;user-select:none}
.hint-btn:hover{background:#42a5f5}
.hint-btn:hover::after{content:attr(data-hint);position:absolute;top:26px;left:-200px;width:420px;
  background:#1a1f28;color:#e6e6e6;border:1px solid #2f3d55;border-radius:8px;padding:14px;
  font-size:12px;font-weight:normal;line-height:1.6;z-index:1000;white-space:pre-wrap;
  box-shadow:0 8px 30px rgba(0,0,0,.8);text-align:left;
  pointer-events:none}
</style></head><body>
<header>
  <h1>🎰 Shuffle Monitor</h1>
  <span id="uptime" style="color:#8ab4f8;font-size:13px">—</span>
  <a href="/" style="margin-left:16px">← На сайт</a>
  <span style="margin-left:auto" class="pill" id="kpiTotal">—</span>
</header>
<div class="tabs">
  <button data-tab="status" class="active">Вкладки<span class="hint-btn" data-hint="Состояние всех Chrome-инстансов.&#10;&#10;📸 — сделать скриншот вкладки.">?</span></button>
  <button data-tab="events">События<span class="hint-btn" data-hint="Все дожди и типсы.&#10;&#10;Колонки:&#10;• ₽ — сумма в рублях&#10;• ₽/чел — сколько каждому получателю&#10;• $ — сумма в долларах&#10;&#10;«+N ещё» — раскрыть всех получателей.">?</span></button>
  <button data-tab="bulk">Bulk-раздачи<span class="hint-btn" data-hint="Одинаковая сумма 3+ людям за 20 мин.&#10;&#10;📌 Рубрика — отметить как «это рубрика, не спам».&#10;Строка станет жёлтой.">?</span></button>
  <button data-tab="bets">Ставки<span class="hint-btn" data-hint="Выигрышные ставки:&#10;• 📊 — из таблицы ставок&#10;• 💬 — из чата (фиолетовый)&#10;&#10;Фильтр по $ и множителю.">?</span></button>
  <button data-tab="betsrains">Ставки→Дожди<span class="hint-btn" data-hint="Кто после СВОЕЙ ставки САМ залил дождь.&#10;Окно: 15 мин по умолчанию.&#10;&#10;Минимум 3 ставки ≥$20 за период.">?</span></button>
  <button data-tab="contexts">Контексты<span class="hint-btn" data-hint="Дожди с контекстом:&#10;последние 15 сообщений + 5 ставок до дождя.">?</span></button>
  <button data-tab="intents">Интенты<span class="hint-btn" data-hint="Обещания сделать дождь (Groq AI).&#10;&#10;AI% — уверенность модели (≥70% — сохраняется).&#10;Δ — через сколько секунд реально залил.">?</span></button>
  <button data-tab="promos">Промокоды<span class="hint-btn" data-hint="Парсится из Telegram-канала.&#10;✅ — использован.&#10;💰 В хранилище — добавить в учёт хранилища.">?</span></button>
  <button data-tab="me">👤 Мой профиль<span class="hint-btn" data-hint="Введи свой ник и запомни.&#10;&#10;Показывает:&#10;• Активность по чатам (15м / 25м / 1ч / 24ч)&#10;• Мои выигранные дожди&#10;• Мои ставки&#10;• Хранилище (балансы, вейджер, цель, транзакции)">?</span></button>
  <button data-tab="ai">🤖 AI<span class="hint-btn" data-hint="AI читает 50 последних сообщений чата и предлагает:&#10;• 5 ответов по теме&#10;• 5 нейтральных фраз">?</span></button>
  <button data-tab="players">👥 Игроки<span class="hint-btn" data-hint="Все игроки по чатам.&#10;Сообщений / последний раз / онлайн.&#10;↗ Язык — переместить в другой чат.">?</span></button>
  <button data-tab="langs">🌍 Языки<span class="hint-btn" data-hint="Кто в каких чатах активен.&#10;Сколько сообщений в каждом.">?</span></button>
</div>
<main>
  <div id="tab-status"><div class="kpi" id="kpiRow"></div>
    <table><thead><tr><th>#</th><th>Режим</th><th>Чат</th><th>Статус</th><th>Обновлено</th><th>📸</th></tr></thead>
    <tbody id="instBody"></tbody></table></div>

  <div id="tab-events" style="display:none">
    <div class="row-flex">
      <input type="text" id="fSender" placeholder="Отправитель">
      <select id="fChat"><option value="">Все чаты</option></select>
      <select id="fType"><option value="">Все</option><option value="tip">Tip</option><option value="rain">Rain</option></select>
      <select id="fRange"><option value="">Всё</option><option value="1">1 час</option><option value="24">24 часа</option><option value="168">7 дней</option></select>
      <button class="btn" onclick="loadEvents()">Фильтр</button>
    </div>
    <table><thead><tr><th>Время</th><th>Тип</th><th>Чат</th><th>От</th><th>Кому</th><th>Сумма</th><th>₽</th><th>₽/чел</th><th>$</th></tr></thead>
    <tbody id="evBody"></tbody></table></div>

  <div id="tab-bulk" style="display:none">
    <div class="row-flex">
      <input type="text" id="bSenderInput" placeholder="Ник + Enter" style="width:180px">
      <button class="btn" onclick="addBulkChip()">+ Добавить</button>
      <button class="btn" onclick="clearBulkChips()">Очистить</button>
    </div>
    <div class="chips" id="bulkChips"></div>
    <table><thead><tr><th>Время</th><th>Чат</th><th>От</th><th>Сумма</th><th>₽/чел</th><th>₽ всего</th><th>Кому</th><th>Действие</th></tr></thead>
    <tbody id="bulkBody"></tbody></table></div>

  <div id="tab-bets" style="display:none">
    <div class="row-flex">
      <input type="text" id="betSender" placeholder="Отправитель">
      <select id="betChat"><option value="">Все чаты</option></select>
      <select id="betSource"><option value="">Все источники</option><option value="bets_page">Таблица</option><option value="chat">Чат</option></select>
      <input type="number" id="betMinUsd" placeholder="Мин. $" value="50">
      <select id="betRange"><option value="">Всё</option><option value="24" selected>24 часа</option><option value="168">7 дней</option></select>
      <button class="btn" onclick="loadBets()">Фильтр</button>
      <button class="btn" onclick="loadBetsTop()">🏆 Топ</button>
      <button class="btn danger" onclick="clearBetsTop()">🗑 Очистить</button>
    </div>
    <div id="betStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>Время</th><th>Источник</th><th>Чат</th><th>Игрок</th><th>Игра</th><th>Множ.</th><th>Сумма</th><th>$</th><th>₽</th></tr></thead>
    <tbody id="betsBody"></tbody></table></div>

  <div id="tab-betsrains" style="display:none">
    <div class="row-flex">
      <input type="number" id="b2rWindow" value="900" style="width:100px">
      <span class="hint">сек — окно после СВОЕЙ же ставки</span>
      <button class="btn" onclick="loadBetsToRains()">Обновить</button>
    </div>
    <div id="b2rStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>#</th><th>Ник</th><th>Ставок ≥$20</th><th>Сам залил</th><th>%</th><th>₽</th></tr></thead>
    <tbody id="b2rBody"></tbody></table></div>

  <div id="tab-contexts" style="display:none">
    <div class="row-flex">
      <select id="ctxChat"><option value="">Все чаты</option></select>
      <input type="number" id="ctxLimit" value="30" style="width:80px">
      <button class="btn" onclick="loadContexts()">Обновить</button>
    </div>
    <div id="ctxList"></div></div>

  <div id="tab-intents" style="display:none">
    <div class="row-flex">
      <select id="intChat"><option value="">Все чаты</option></select>
      <input type="text" id="intSender" placeholder="Игрок">
      <select id="intOnly"><option value="">Все</option><option value="1">Сбывшиеся</option><option value="0">Несбывшиеся</option></select>
      <button class="btn" onclick="loadIntents()">Фильтр</button>
      <button class="btn" onclick="loadIntentsStats()">🏆 Рейтинг</button>
    </div>
    <div id="intStats" style="margin-bottom:16px"></div>
    <table><thead><tr><th>Время</th><th>Чат</th><th>Игрок</th><th>Текст</th><th>AI%</th><th>Статус</th><th>Δ</th></tr></thead>
    <tbody id="intBody"></tbody></table></div>

  <div id="tab-promos" style="display:none">
    <div class="row-flex">
      <select id="promoFilter"><option value="">Все</option><option value="0">Неиспользованные</option><option value="1">Использованные</option></select>
      <button class="btn" onclick="loadPromos()">Обновить</button>
      <button class="btn" onclick="testPromo()">🔍 Проверить</button>
      <span class="hint" id="promoStats"></span>
    </div>
    <div id="promoList"></div></div>

  <div id="tab-me" style="display:none">

    <div class="vault-card">
      <div class="row-flex" style="justify-content:space-between;margin-bottom:0">
        <h3 style="margin:0">👤 Мой профиль</h3>
        <div class="row-flex" style="margin:0">
          <input type="text" id="meNick" placeholder="Твой ник (напр. Merami)" style="width:200px">
          <button class="btn green" onclick="loadMe()" style="font-weight:600">🔎 Проверить</button>
          <button class="btn" onclick="saveMeNick()">💾 Запомнить</button>
          <span class="hint" id="meStatus"></span>
        </div>
      </div>
      <div id="meTop"></div>
    </div>

    <div class="vault-card">
      <h3>📊 Активность по чатам</h3>
      <div id="meActivity"></div>
    </div>

    <div class="vault-card">
      <h3>🏆 Мои выигранные дожди</h3>
      <div class="row-flex">
        <select id="meRainDays" onchange="loadMeRains()" style="max-width:160px">
          <option value="7">7 дней</option>
          <option value="30" selected>30 дней</option>
          <option value="90">90 дней</option>
        </select>
        <span class="hint" id="meRainsStatus"></span>
      </div>
      <div id="meRains"></div>
    </div>

    <div class="vault-card">
      <h3>🎰 Мои последние ставки</h3>
      <div id="meBets"></div>
    </div>

    <div id="vaultKpi" class="kpi"></div>
    <div id="vaultWagerCard" class="vault-card"></div>

    <div class="vault-card">
      <h3>💼 Балансы валют</h3>
      <div id="vaultBalances"></div>
      <div class="row-flex" style="margin-top:12px">
        <input type="text" id="vbCurrency" placeholder="Валюта (USDT/SHFL/SOL...)" style="width:180px">
        <input type="number" id="vbAmount" placeholder="Сумма" step="any" style="width:140px">
        <input type="number" id="vbAmountUsd" placeholder="$ (auto)" step="any" style="width:160px">
        <button class="btn green" onclick="setVaultBalance()">💾 Сохранить</button>
      </div>
    </div>

    <div class="vault-card">
      <h3>⚙️ Вейджер</h3>
      <div class="row-flex">
        <input type="number" id="wgAmount" placeholder="$ сумма" step="any" style="width:140px">
        <input type="datetime-local" id="wgDeadline">
        <button class="btn green" onclick="saveWager()">💾 Сохранить</button>
      </div>
      <p class="hint">Пример: $3000, дедлайн 16.10.2026 15:00</p>
    </div>

    <div class="vault-card">
      <h3>🎯 Цель накопления</h3>
      <div class="row-flex">
        <input type="number" id="goalUsd" placeholder="$ цель" step="any" style="width:140px">
        <input type="text" id="goalText" placeholder="Описание" style="width:280px">
        <button class="btn green" onclick="saveGoal()">💾 Сохранить</button>
      </div>
    </div>

    <div class="vault-card">
      <h3>➕ Новая транзакция</h3>
      <div class="row-flex">
        <select id="txType">
          <option value="income">💰 Получил $</option>
          <option value="wager_spend">🎲 Потратил на вейджер</option>
          <option value="vault_add">📥 Убрал в хранилище</option>
          <option value="vault_take">📤 Взял из хранилища</option>
        </select>
        <select id="txCurrency"><option>USD</option><option>USDT</option><option>USDC</option><option>SHFL</option><option>SOL</option><option>LTC</option><option>ETH</option><option>BTC</option></select>
        <input type="number" id="txAmount" placeholder="Сумма" step="any" style="width:120px">
        <input type="number" id="txAmountUsd" placeholder="$ (auto)" step="any" style="width:160px">
        <input type="text" id="txDesc" placeholder="Описание" style="width:280px">
        <button class="btn green" onclick="addTransaction()">➕ Добавить</button>
      </div>
    </div>

    <div class="vault-card">
      <h3>📜 Последние транзакции</h3>
      <table><thead><tr><th>Время</th><th>Тип</th><th>Валюта</th><th>Сумма</th><th>$</th><th>Описание</th></tr></thead>
      <tbody id="txList"></tbody></table>
    </div>

  </div>

  <div id="tab-ai" style="display:none">
    <div class="row-flex">
      <select id="aiChat"><option value="">Выбери чат</option></select>
      <button class="btn" onclick="runAI()">🤖 Получить</button>
      <button class="btn" onclick="loadAILatest()">📋 Последний</button>
      <span class="hint" id="aiStatus"></span>
    </div>
    <div id="aiTopic"></div>
    <div id="aiResult"></div></div>

  <div id="tab-players" style="display:none">
    <div class="row-flex">
      <select id="plChatFilter"><option value="">Все чаты</option></select>
      <input type="text" id="plUserFilter" placeholder="Ник">
      <label style="color:#aab;font-size:12px"><input type="checkbox" id="plOnlineOnly"> Только онлайн</label>
      <button class="btn" onclick="loadPlayersTab()">Обновить</button>
    </div>
    <div id="plChatsSummary" style="margin-bottom:16px"></div>
    <table><thead><tr><th>Чат</th><th>Игрок</th><th>Сообщений</th><th>Последний раз</th><th>Статус</th><th>Действие</th></tr></thead>
    <tbody id="plPlayersBody"></tbody></table></div>

  <div id="tab-langs" style="display:none">
    <div class="row-flex">
      <input type="text" id="langFilter" placeholder="Поиск по нику">
      <button class="btn" onclick="loadLanguages()">Обновить</button>
    </div>
    <div id="langGrid" class="lang-grid"></div></div>
</main>
<div class="modal" id="imgModal" onclick="this.classList.remove('on')"><img id="imgModalImg" src=""></div>

<script>
const $ = id => document.getElementById(id);
const fmtTime = ts => new Date(ts*1000).toLocaleString('ru-RU');
const fmtMoney = v => (v||0).toLocaleString('ru-RU',{maximumFractionDigits:2});
const esc = s => (s==null?'':String(s)).replace(/[<>&"]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

let bulkChips = [], aiPollTimer = null;

function renderReceivers(recs, collapseAfter) {
  recs = recs || [];
  if (!recs.length) return '<span class="hint">—</span>';
  const n = collapseAfter || 5;
  if (recs.length <= n) return '<span class="recv-visible">'+recs.map(esc).join(', ')+'</span>';
  const id = 'recv_' + Math.random().toString(36).slice(2,9);
  return '<span class="recv-visible">'+recs.slice(0,n).map(esc).join(', ')+'</span>'
    + '<span class="recv-hidden" id="'+id+'">, '+recs.slice(n).map(esc).join(', ')+'</span>'
    + ' <span class="recv-toggle" onclick="document.getElementById(\''+id+'\').style.display=\'inline\';this.style.display=\'none\'">+'+(recs.length-n)+' ещё</span>';
}

function switchTab(name){
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  ['status','events','bulk','bets','betsrains','contexts','intents','promos','ai','players','langs','me'].forEach(t=>{
    const el = $('tab-'+t); if(el) el.style.display = (t===name) ? '' : 'none';
  });
  if(name==='events') { loadChats(); loadEvents(); }
  if(name==='bulk') loadBulk();
  if(name==='bets') { loadChats(); loadBets(); }
  if(name==='betsrains') loadBetsToRains();
  if(name==='contexts') { loadChats(); loadContexts(); }
  if(name==='intents') { loadChats(); loadIntents(); }
  if(name==='promos') loadPromos();
  if(name==='me') { loadVault(); loadMe(); }
  if(name==='ai') { loadChats(); loadAILatest(); }
  if(name==='players') { loadChats(); loadPlayersTab(); }
  if(name==='langs') loadLanguages();
}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

async function refreshStatus(){
  try {
    const r = await fetch('/api/status').then(r=>r.json());
    $('uptime').textContent = r.first_ts ? 'с ' + new Date(r.first_ts*1000).toLocaleDateString('ru-RU') : '—';
    $('kpiTotal').textContent = `Всего: ${r.total||0} | Tips: ${r.tips||0} | Rains: ${r.rains||0} | Bets: ${r.bets||0} | Intents: ${r.intents||0}`;
    const now = Date.now()/1000;
    $('kpiRow').innerHTML = '<div>Всего<b>'+(r.total||0)+'</b></div><div>Tips<b>'+(r.tips||0)+'</b></div><div>Rains<b>'+(r.rains||0)+'</b></div><div>Bets<b>'+(r.bets||0)+'</b></div><div>Intents<b>'+(r.intents||0)+'</b></div><div>Онлайн<b>'+(r.instances||[]).filter(i=>now-(i.updated_at||0)<40).length+'</b></div>';
    const body = $('instBody'); body.innerHTML = '';
    (r.instances||[]).forEach(i=>{
      const online = now - (i.updated_at||0) < 40;
      let cls='pill', txt=i.status||'—';
      if(!online){ cls+=' err'; txt='offline'; }
      else if(i.status==='ok') cls+=' ok';
      else if(i.status==='waiting'||i.status==='rotating') cls+=' warn';
      else if(['error','timeout','verify_error','chat_not_loaded'].includes(i.status)) cls+=' err';
      const upd = i.updated_at ? Math.round(now - i.updated_at)+'с' : '—';
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>'+i.iid+'</td><td>'+esc(i.mode||'')+'</td><td><b>'+esc(i.current_chat||'—')+'</b></td><td><span class="'+cls+'">'+esc(txt)+'</span></td><td>'+upd+'</td><td><button class="btn" onclick="reqShot('+i.iid+')">📸</button></td>';
      body.appendChild(tr);
    });
  } catch(e){}
}
async function reqShot(iid){
  await fetch('/api/screenshot/'+iid,{method:'POST'});
  let t=0; while(t++<40){ await new Promise(r=>setTimeout(r,1500));
    const r = await fetch('/api/screenshot/'+iid);
    if(r.status===200){ $('imgModalImg').src = URL.createObjectURL(await r.blob()); $('imgModal').classList.add('on'); return; }
    if(r.status===404){ alert('Не удалось'); return; }
  } alert('Таймаут');
}
async function loadChats(){
  const r = await fetch('/api/chats').then(r=>r.json());
  ['fChat','betChat','ctxChat','intChat','aiChat','plChatFilter'].forEach(id=>{
    const sel = $(id); if(!sel) return; const cur = sel.value;
    const ph = id==='aiChat' ? '<option value="">Выбери чат</option>' : '<option value="">Все чаты</option>';
    sel.innerHTML = ph + r.chats.map(c=>'<option value="'+esc(c)+'">'+esc(c)+'</option>').join('');
    sel.value = cur;
  });
}
async function loadEvents(){
  const p = new URLSearchParams();
  if($('fSender').value) p.set('sender',$('fSender').value);
  if($('fChat').value) p.set('chat',$('fChat').value);
  if($('fType').value) p.set('type',$('fType').value);
  if($('fRange').value) p.set('last_hours',$('fRange').value);
  const r = await fetch('/api/events?'+p).then(r=>r.json());
  const body = $('evBody'); body.innerHTML = '';
  r.events.forEach(e=>{
    const tr = document.createElement('tr'); const per = e.per_person_rub || 0;
    tr.innerHTML = '<td>'+(e.ts?fmtTime(e.ts):esc(e.created_at))+'</td>'
      + '<td><span class="pill">'+esc(e.type)+'</span></td><td>'+esc(e.chat)+'</td>'
      + '<td>'+esc(e.sender)+'</td><td class="recv-wrap">'+renderReceivers(e.receivers)+'</td>'
      + '<td>'+esc(e.amount_text)+'</td><td>'+fmtMoney(e.amount_rub)+'</td>'
      + '<td style="color:#8ab4f8">'+(per?fmtMoney(per):'—')+'</td>'
      + '<td style="color:#71d68a">$'+fmtMoney(e.amount_usd||0)+'</td>';
    body.appendChild(tr);
  });
}
function renderBulkChips(){
  $('bulkChips').innerHTML = bulkChips.length ? bulkChips.map(n=>'<span class="chip">'+esc(n)+'<span class="x" onclick="removeBulkChip(\''+esc(n)+'\')">×</span></span>').join('') : '<span class="hint">—</span>';
}
function addBulkChip(){ const v = $('bSenderInput').value.trim(); if(!v) return; if(!bulkChips.includes(v)) bulkChips.push(v); $('bSenderInput').value=''; renderBulkChips(); loadBulk(); }
function removeBulkChip(n){ bulkChips = bulkChips.filter(x=>x!==n); renderBulkChips(); loadBulk(); }
function clearBulkChips(){ bulkChips = []; renderBulkChips(); loadBulk(); }
async function loadBulk(){
  const p = new URLSearchParams(); if(bulkChips.length) p.set('senders', bulkChips.join(','));
  const r = await fetch('/api/bulk?'+p).then(r=>r.json());
  const body = $('bulkBody'); body.innerHTML='';
  if(!r.alerts.length){ body.innerHTML = '<tr><td colspan="8" class="hint">Нет данных</td></tr>'; return; }
  r.alerts.forEach(a=>{
    const recCount = (a.receivers||[]).length; const rubEach = a.amount_rub || 0;
    const isRubric = a.is_rubric ? true : false;
    const tr = document.createElement('tr'); if(isRubric) tr.className = 'rubric-row';
    tr.innerHTML = '<td>'+fmtTime(a.ts)+'</td><td>'+esc(a.chat)+(isRubric?' <span class="pill rubric">📌 РУБРИКА</span>':'')+'</td>'
      + '<td>'+esc(a.sender)+'</td><td>'+esc(a.amount_text)+'</td><td>'+fmtMoney(rubEach)+' ₽</td>'
      + '<td style="color:#71d68a">'+fmtMoney(rubEach*recCount)+' ₽</td>'
      + '<td class="recv-wrap">'+renderReceivers(a.receivers)+'</td>'
      + '<td><button class="btn" onclick="toggleRubric('+a.id+')">'+(isRubric?'✓ Снять':'📌 Рубрика')+'</button></td>';
    body.appendChild(tr);
  });
}
async function toggleRubric(id){ await fetch('/api/bulk/rubric/'+id,{method:'POST'}); loadBulk(); }

async function loadBets(){
  const p = new URLSearchParams();
  if($('betSender').value) p.set('sender',$('betSender').value);
  if($('betChat').value) p.set('chat',$('betChat').value);
  if($('betSource').value) p.set('source',$('betSource').value);
  if($('betMinUsd').value) p.set('min_usd',$('betMinUsd').value);
  if($('betRange').value) p.set('last_hours',$('betRange').value);
  const r = await fetch('/api/bets?'+p).then(r=>r.json());
  const body = $('betsBody'); body.innerHTML='';
  if(!r.bets.length){ body.innerHTML = '<tr><td colspan="9" class="hint">Нет ставок</td></tr>'; return; }
  r.bets.forEach(b=>{
    const fromChat = b.source === 'chat';
    const tr = document.createElement('tr'); tr.className = 'bet-row' + (fromChat?' chat':'');
    tr.innerHTML = '<td>'+fmtTime(b.ts)+'</td><td><span class="pill '+(fromChat?'chat':'')+'">'+(fromChat?'💬 Чат':'📊')+'</span></td>'
      + '<td>'+esc(b.chat)+'</td><td><b>'+esc(b.sender)+'</b></td><td>'+esc(b.game)+'</td><td>'+esc(b.multiplier)+'</td>'
      + '<td>'+esc(b.amount_text)+' '+esc(b.crypto)+'</td>'
      + '<td style="color:#71d68a">$'+fmtMoney(b.amount_usd)+'</td><td>'+fmtMoney(b.amount_rub)+'</td>';
    body.appendChild(tr);
  });
}
async function loadBetsTop(){
  const r = await fetch('/api/bets_top_senders').then(r=>r.json());
  const el = $('betStats');
  if(!r.senders.length){ el.innerHTML = '<p class="hint">Пусто</p>'; return; }
  el.innerHTML = '<div class="kpi">' + r.senders.map(s=>'<div>'+esc(s.nickname)+'<b>'+s.cnt+'</b><span class="hint">$'+fmtMoney(s.total_usd)+'</span></div>').join('') + '</div>';
}
async function clearBetsTop(){ if(!confirm('Очистить топ?')) return; await fetch('/api/bets/clear_top',{method:'POST'}); alert('OK'); loadBetsTop(); }
async function loadBetsToRains(){
  const w = $('b2rWindow').value || 900;
  const r = await fetch('/api/bets_to_rains?window_sec='+w).then(r=>r.json());
  const body = $('b2rBody'); body.innerHTML='';
  $('b2rStats').innerHTML = '<div class="kpi"><div>Окно<b>'+r.window_sec+'с</b></div><div>Игроков<b>'+r.stats.length+'</b></div></div>';
  if(!r.stats.length){ body.innerHTML = '<tr><td colspan="6" class="hint">Нет</td></tr>'; return; }
  r.stats.forEach((s,i)=>{ const cls = s.percent>=50?'ok':(s.percent>=25?'warn':'err');
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>'+(i+1)+'</td><td><b>'+esc(s.nickname)+'</b></td><td>'+s.bets+'</td><td>'+s.triggered+'</td><td><span class="pill '+cls+'">'+s.percent+'%</span></td><td>'+fmtMoney(s.rain_rub)+'</td>';
    body.appendChild(tr);
  });
}
async function loadContexts(){
  const p = new URLSearchParams();
  if($('ctxChat').value) p.set('chat',$('ctxChat').value);
  if($('ctxLimit').value) p.set('limit',$('ctxLimit').value);
  const r = await fetch('/api/contexts?'+p).then(r=>r.json());
  const el = $('ctxList');
  if(!r.contexts.length){ el.innerHTML = '<p class="hint">Нет</p>'; return; }
  el.innerHTML = r.contexts.map(c=>{
    const rub = c.amount_rub ? fmtMoney(c.amount_rub) + ' ₽' : '';
    const msgs = (c.context_messages||[]).slice(-15); const bets = (c.context_bets||[]).slice(-5);
    const mH = msgs.length ? msgs.map((m,i)=>'<div style="padding:4px 8px;border-left:3px solid #2a2f3a;margin:4px 0;color:#aab;font-size:12px">'+(i+1)+'. '+esc(m)+'</div>').join('') : '<div class="hint">—</div>';
    const bH = bets.length ? bets.map(b=>'<div style="padding:4px 8px;border-left:3px solid #71d68a;margin:4px 0;color:#71d68a;font-size:12px">🎰 <b>'+esc(b.sender||'?')+'</b> · '+esc(b.game||'')+' · '+esc(b.multiplier||'')+'</div>').join('') : '<div class="hint">—</div>';
    return '<div style="background:#151821;border:1px solid #232a37;border-radius:8px;padding:14px;margin-bottom:12px">'
      + '<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:10px">'
      + '<span class="pill">🌧 RAIN</span><b>'+esc(c.chat)+'</b>'
      + '<span style="color:#8b93a7;font-size:12px">'+(c.ts?fmtTime(c.ts):esc(c.created_at))+'</span>'
      + '<span style="margin-left:auto;color:#71d68a;font-weight:bold">'+esc(c.amount_text)+' '+esc(c.crypto_symbol||'')+' '+(rub?'· '+rub:'')+'</span></div>'
      + '<div style="color:#aab;font-size:12px;margin-bottom:6px">👤 <b>'+esc(c.sender)+'</b> → '+(c.receivers||[]).length+'</div>'
      + '<div class="recv-wrap" style="margin-bottom:8px">'+renderReceivers(c.receivers,8)+'</div>'
      + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px"><div>'+mH+'</div><div>'+bH+'</div></div></div>';
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
    tr.innerHTML = '<td>'+fmtTime(x.ts)+'</td><td>'+esc(x.chat)+'</td><td><b>'+esc(x.sender)+'</b></td>'
      + '<td>'+esc(x.text)+'</td><td><span class="pill '+(x.percent>=70?'ok':'warn')+'">'+(x.percent||100)+'%</span></td>'
      + '<td><span class="pill '+(matched?'ok':'err')+'">'+(matched?'✅':'❌')+'</span></td>'
      + '<td>'+(x.matched_delta_sec ? Math.round(x.matched_delta_sec)+'с' : '—')+'</td>';
    body.appendChild(tr);
  });
}
async function loadIntentsStats(){
  const r = await fetch('/api/intents_stats').then(r=>r.json());
  const el = $('intStats');
  if(!r.stats.length){ el.innerHTML = '<p class="hint">Нет</p>'; return; }
  el.innerHTML = '<div class="kpi">' + r.stats.map(s=>{const cls = s.percent>=50?'ok':(s.percent>=25?'warn':'err');
    return '<div>'+esc(s.nickname)+'<b>'+s.percent+'%</b><span class="hint"><span class="pill '+cls+'">'+s.matched+'/'+s.total+'</span></span></div>';
  }).join('') + '</div>';
}
async function loadPromos(){
  const filter = $('promoFilter').value;
  const p = new URLSearchParams(); if(filter === '0') p.set('only_unused','1');
  const r = await fetch('/api/promocodes?'+p).then(r=>r.json());
  const stats = await fetch('/api/promocodes/stats').then(r=>r.json());
  $('promoStats').textContent = 'Всего: '+stats.total+' | Исп: '+stats.used+' | Осталось: '+stats.unused;
  const el = $('promoList');
  if(!r.promos.length){ el.innerHTML = '<p class="hint">Пусто</p>'; return; }
  const filtered = filter==='1' ? r.promos.filter(x=>x.used) : r.promos;
  el.innerHTML = filtered.map(x=>{
    const vaulted = !!x.used_for_vault;
    const vaultBadge = vaulted ? ' <span class="pill ok">💰 в хранилище ($'+fmtMoney(x.vault_amount_usd)+')</span>' : '';
    const vaultBtn = vaulted ? '' : '<button class="btn green" onclick="promoToVault('+x.id+')">💰 В хранилище</button>';
    return '<div class="promo-card '+(x.used?'used':'')+' '+(vaulted?'vaulted':'')+'">'
      + '<input type="checkbox" class="promo-cb" '+(x.used?'checked':'')+' onchange="togglePromo('+x.id+')">'
      + '<div class="promo-body"><div class="promo-text">'+esc(x.text)+'</div>'
      + '<div class="promo-meta">'+(x.message_date?'📅 '+esc(x.message_date):'')+(x.link?' · <a href="'+esc(x.link)+'" target="_blank">TG</a>':'')+vaultBadge+'</div></div>'
      + '<div style="display:flex;flex-direction:column;gap:6px">'+vaultBtn+'</div></div>';
  }).join('');
}
async function togglePromo(id){ await fetch('/api/promo/toggle/'+id,{method:'POST'}); loadPromos(); }
async function promoToVault(id){
  const amount = prompt('Сколько $ получил за промокод?', '0'); if(amount === null) return;
  await fetch('/api/promo/to_vault/'+id, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount_usd: parseFloat(amount)||0, currency: 'USD'})});
  alert('✅ Добавлено'); loadPromos(); loadVault();
}
async function testPromo(){
  const btn = event.target; btn.disabled=true; btn.textContent='⏳';
  try { const r = await fetch('/api/promo/test',{method:'POST'}).then(r=>r.json()); alert('Найдено: '+r.found+', новых: '+r.new); loadPromos(); }
  catch(e){ alert('Ошибка'); } finally { btn.disabled=false; btn.textContent='🔍 Проверить'; }
}

// ===== МОЙ ПРОФИЛЬ =====
function saveMeNick(){
  const v = $('meNick').value.trim(); if(!v) return;
  localStorage.setItem('myNick', v);
  alert('✅ Ник сохранён: ' + v);
}
async function loadMe(){
  const un = $('meNick').value.trim();
  if(!un) return;
  const s = $('meStatus'); if(s) s.textContent = '⏳...';
  const topEl = $('meTop'); const actEl = $('meActivity');
  if(topEl) topEl.innerHTML = '';
  if(actEl) actEl.innerHTML = '';
  try {
    const d = await fetch('/api/player/activity?username=' + encodeURIComponent(un) + '&windows=15,25,60,1440').then(r=>r.json());
    const wins = d.windows || [15,25,60,1440];
    const lbl = w => w === 1440 ? '24ч' : (w === 60 ? '1ч' : (w + 'м'));
    const lastInfo = d.minutes_ago_total !== null && d.minutes_ago_total !== undefined
      ? 'Последнее сообщение: <b style="color:#8ab4f8">' + d.minutes_ago_total + ' мин назад</b>'
      : '<span class="muted">Нет активности за сутки</span>';
    if(topEl) topEl.innerHTML = '<div style="color:#8b949e;font-size:13px;margin-top:10px">' + lastInfo + '</div>';
    const totalRow = wins.map(w => {
      const v = d.total[w] || 0;
      const col = v > 0 ? '#4ade80' : '#666';
      return '<div style="background:#151821;border:1px solid #232a37;border-radius:8px;padding:10px 16px;min-width:110px">'
        + '<span style="color:#8b949e;font-size:12px">За ' + lbl(w) + '</span>'
        + '<b style="display:block;font-size:22px;color:' + col + '">' + v + '</b></div>';
    }).join('');
    const rows = (d.chats || []).map(c => {
      const cells = wins.map(w => {
        const v = c.counts[w] || 0;
        return '<td style="text-align:center;color:' + (v>0?'#4ade80':'#555') + ';font-weight:' + (v>0?'600':'400') + '">' + v + '</td>';
      }).join('');
      const totalRecent = wins.reduce((a,w) => a + (c.counts[w]||0), 0);
      const rowStyle = totalRecent === 0 ? 'style="opacity:0.45"' : '';
      const ll = c.minutes_ago === null || c.minutes_ago === undefined ? '—'
        : (c.minutes_ago < 60 ? c.minutes_ago + 'м'
           : (c.minutes_ago < 1440 ? Math.floor(c.minutes_ago/60) + 'ч'
              : Math.floor(c.minutes_ago/1440) + 'д'));
      return '<tr ' + rowStyle + '><td><b>' + c.chat + '</b> <span class="muted" style="font-size:11px">' + (c.total_msgs||0) + ' всего</span></td>' + cells + '<td>' + ll + '</td></tr>';
    }).join('');
    const head = '<tr><th>Чат</th>' + wins.map(w => '<th style="text-align:center">' + lbl(w) + '</th>').join('') + '<th>Последнее</th></tr>';
    if(actEl) actEl.innerHTML = '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">' + totalRow + '</div>'
      + (rows ? '<table><thead>' + head + '</thead><tbody>' + rows + '</tbody></table>' : '<div class="muted">Нет чатов</div>');
    if(s) s.textContent = '✅';
    loadMeRains();
    loadMeBets();
  } catch(e) {
    if(s) s.textContent = '❌ ' + e.message;
  }
}
async function loadMeRains(){
  const un = $('meNick').value.trim(); if(!un) return;
  const days = $('meRainDays') ? $('meRainDays').value : 30;
  const st = $('meRainsStatus'); if(st) st.textContent = '⏳';
  const el = $('meRains'); if(!el) return;
  const d = await fetch('/api/user/' + encodeURIComponent(un) + '/rains_received?days=' + days).then(r=>r.json());
  if(st) st.textContent = 'Найдено: ' + d.wins + ' | ' + fmtMoney(d.total_rub) + ' ₽ (~$' + fmtMoney(d.total_usd) + ')';
  if(!d.items || !d.items.length){ el.innerHTML = '<div class="muted">Нет выигранных дождей за период</div>'; return; }
  el.innerHTML = '<table><thead><tr><th>Время</th><th>Чат</th><th>От</th><th>Сумма</th><th>Твоя доля ₽</th><th>Доля $</th></tr></thead><tbody>'
    + d.items.map(i => '<tr><td>' + fmtTime(i.ts) + '</td><td>' + esc(i.chat) + '</td>'
        + '<td><b>' + esc(i.sender) + '</b></td>'
        + '<td>' + esc(i.amount_text) + ' ' + esc(i.crypto_symbol||'') + ' <span class="muted">(÷' + i.receivers_count + ')</span></td>'
        + '<td style="color:#71d68a;font-weight:600">' + fmtMoney(i.share_rub) + ' ₽</td>'
        + '<td style="color:#8ab4f8">$' + fmtMoney(i.share_usd) + '</td></tr>').join('')
    + '</tbody></table>';
}
async function loadMeBets(){
  const un = $('meNick').value.trim(); if(!un) return;
  const el = $('meBets'); if(!el) return;
  const r = await fetch('/api/bets?sender=' + encodeURIComponent(un) + '&limit=20').then(r=>r.json());
  if(!r.bets || !r.bets.length){ el.innerHTML = '<div class="muted">Нет ставок</div>'; return; }
  el.innerHTML = '<table><thead><tr><th>Время</th><th>Чат</th><th>Игра</th><th>Множ.</th><th>Сумма</th><th>$</th></tr></thead><tbody>'
    + r.bets.map(b => '<tr><td>' + fmtTime(b.ts) + '</td><td>' + esc(b.chat) + '</td>'
        + '<td>' + esc(b.game) + '</td><td>' + esc(b.multiplier) + '</td>'
        + '<td>' + esc(b.amount_text) + ' ' + esc(b.crypto||'') + '</td>'
        + '<td style="color:#71d68a">$' + fmtMoney(b.amount_usd) + '</td></tr>').join('')
    + '</tbody></table>';
}
(function initMe(){
  const saved = localStorage.getItem('myNick');
  if(saved && $('meNick')) $('meNick').value = saved;
})();

// ===== VAULT =====
async function loadVault(){
  const r = await fetch('/api/vault').then(r=>r.json());
  const w = r.wager; const daysLeft = w.days_left; const hasWager = w.amount_required > 0;
  $('vaultKpi').innerHTML = '<div>Всего в хранилище<b style="color:#71d68a">$'+fmtMoney(r.total_usd)+'</b></div>'
    + (hasWager ? '<div>Вейджер<b style="color:'+(w.is_expired?'#f87171':(daysLeft<7?'#fbbf24':'#8ab4f8'))+'">$'+fmtMoney(w.amount_required)+'</b></div>' : '')
    + (r.goal_usd > 0 ? '<div>Цель<b style="color:#8ab4f8">$'+fmtMoney(r.goal_usd)+'</b><span class="hint">'+esc(r.goal_text||'')+'</span></div>' : '')
    + '<div>Доход 7д<b style="color:#71d68a">+$'+fmtMoney(r.week.income)+'</b></div>'
    + '<div>Вейджер 7д<b style="color:#f87171">-$'+fmtMoney(r.week.wager_spend)+'</b></div>'
    + '<div>В хранилище 7д<b style="color:#71d68a">+$'+fmtMoney(r.week.vault_add)+'</b></div>';
  if(hasWager){
    const dl = new Date(w.deadline_ts*1000).toLocaleString('ru-RU');
    const exp = w.is_expired; const hrs = w.hours_left;
    const leftText = exp ? '<span class="vault-danger">⚠️ ИСТЁК</span>' : (hrs < 24 ? '<span class="vault-warn">осталось '+hrs+'ч</span>' : 'осталось '+daysLeft+'д');
    $('vaultWagerCard').innerHTML = '<h3>⚙️ Вейджер</h3>'
      + '<div class="vault-row"><span>Требуется</span><span class="vault-amount">$'+fmtMoney(w.amount_required)+'</span></div>'
      + '<div class="vault-row"><span>Дедлайн</span><span>'+dl+' · '+leftText+'</span></div>';
  } else {
    $('vaultWagerCard').innerHTML = '<h3>⚙️ Вейджер</h3><p class="hint">Не задан</p>';
  }
  const bl = $('vaultBalances');
  if(!r.balances.length){ bl.innerHTML = '<p class="hint">Пусто</p>'; }
  else {
    bl.innerHTML = r.balances.map(b=>'<div class="vault-row"><span><b>'+esc(b.currency)+'</b></span><span><span class="vault-amount">'+fmtMoney(b.amount)+' '+esc(b.currency)+'</span> <span class="hint">≈ $'+fmtMoney(b.amount_usd)+'</span> <button class="btn danger" style="margin-left:8px;padding:2px 6px;font-size:11px" onclick="delBalance(\''+esc(b.currency)+'\')">🗑</button></span></div>').join('');
  }
  if(hasWager){
    $('wgAmount').value = w.amount_required;
    if(w.deadline_ts){
      const d = new Date(w.deadline_ts*1000);
      const pad = n => String(n).padStart(2,'0');
      $('wgDeadline').value = d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
    }
  }
  if(r.goal_usd > 0) $('goalUsd').value = r.goal_usd;
  if(r.goal_text) $('goalText').value = r.goal_text;
  const tr = await fetch('/api/vault/transactions?days=60&limit=200').then(r=>r.json());
  $('txList').innerHTML = (tr.transactions||[]).map(t=>'<tr><td>'+fmtTime(t.ts)+'</td><td><span class="pill">'+esc(t.type)+'</span></td><td>'+esc(t.currency||'')+'</td><td>'+fmtMoney(t.amount)+'</td><td style="color:'+(t.amount_usd>=0?'#71d68a':'#f87171')+'">$'+fmtMoney(t.amount_usd)+'</td><td class="hint">'+esc(t.description||'')+'</td></tr>').join('') || '<tr><td colspan="6" class="hint">Нет</td></tr>';
}
async function setVaultBalance(){
  const c = $('vbCurrency').value.trim().toUpperCase(); if(!c) return alert('Валюта');
  const a = parseFloat($('vbAmount').value)||0;
  const au = $('vbAmountUsd').value ? parseFloat($('vbAmountUsd').value) : null;
  await fetch('/api/vault/balance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({currency:c,amount:a,amount_usd:au})});
  $('vbCurrency').value=''; $('vbAmount').value=''; $('vbAmountUsd').value=''; loadVault();
}
async function delBalance(c){ if(!confirm('Удалить '+c+'?')) return; await fetch('/api/vault/balance/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({currency:c})}); loadVault(); }
async function saveWager(){
  const a = parseFloat($('wgAmount').value)||0; const dl = $('wgDeadline').value;
  let ts = 0; if(dl){ ts = new Date(dl).getTime()/1000; }
  await fetch('/api/vault/wager',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount_required:a,deadline_ts:ts})});
  alert('✅ Сохранён'); loadVault();
}
async function saveGoal(){
  const g = parseFloat($('goalUsd').value)||0; const t = $('goalText').value.trim();
  await fetch('/api/vault/goal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal_usd:g,goal_text:t})});
  alert('✅ Сохранена'); loadVault();
}
async function addTransaction(){
  const t = $('txType').value; const c = $('txCurrency').value;
  const a = parseFloat($('txAmount').value)||0;
  const au = $('txAmountUsd').value ? parseFloat($('txAmountUsd').value) : 0;
  const d = $('txDesc').value.trim();
  if(!a) return alert('Сумма');
  await fetch('/api/vault/transaction',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:t,currency:c,amount:a,amount_usd:au,description:d})});
  $('txAmount').value=''; $('txAmountUsd').value=''; $('txDesc').value='';
  loadVault();
}

// ===== AI =====
async function runAI(){
  const chat = $('aiChat').value; if(!chat){ alert('Выбери чат'); return; }
  const btn = event.target; btn.disabled=true; btn.textContent='⏳';
  $('aiStatus').textContent = 'Отправляю...';
  try {
    const r = await fetch('/api/chat/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat})}).then(r=>r.json());
    const taskId = r.task_id; if(!taskId){ alert('Ошибка'); return; }
    if(aiPollTimer) clearInterval(aiPollTimer); let tries = 0;
    aiPollTimer = setInterval(async () => {
      tries++; const t = await fetch('/api/chat/task/'+taskId).then(r=>r.json());
      if(t.status === 'pending' || t.status === 'reading'){ $('aiStatus').textContent = (tries*2)+'с'; }
      else if(t.status === 'completed'){ clearInterval(aiPollTimer); aiPollTimer=null; $('aiStatus').textContent='✅'; btn.disabled=false; btn.textContent='🤖 Получить'; renderAIResult(t.result, chat); }
      else if(t.status === 'failed'){ clearInterval(aiPollTimer); aiPollTimer=null; $('aiStatus').textContent='❌'; btn.disabled=false; btn.textContent='🤖 Получить'; }
      if(tries > 60){ clearInterval(aiPollTimer); aiPollTimer=null; btn.disabled=false; btn.textContent='🤖 Получить'; }
    }, 2000);
  } catch(e){ alert('Ошибка'); btn.disabled=false; btn.textContent='🤖 Получить'; }
}
function renderAIResult(result, chat){
  if(!result){ $('aiResult').innerHTML='<p class="hint">Нет</p>'; return; }
  $('aiTopic').innerHTML = result.topic ? '<div class="ai-topic"><b>Тема:</b> '+esc(result.topic)+'</div>' : '';
  $('aiResult').innerHTML = '<div class="ai-grid"><div class="ai-col"><h3>💬 Сообщения</h3>'
    + (result.messages||[]).map(m=>'<div class="ai-msg" onclick="copyMsg(this,\''+esc(m).replace(/'/g,"&#39;")+'\')">'+esc(m)+'</div>').join('')
    + '</div><div class="ai-col"><h3>🙋 Фразы</h3>'
    + (result.phrases||[]).map(p=>'<div class="ai-phrase" onclick="copyMsg(this,\''+esc(p).replace(/'/g,"&#39;")+'\')">'+esc(p)+'</div>').join('')
    + '</div></div>';
}
function copyMsg(el, text){
  const doCopy = async () => { if(navigator.clipboard && window.isSecureContext){ try { await navigator.clipboard.writeText(text); return true; } catch(e){} }
    const ta = document.createElement('textarea'); ta.value = text; ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta);
    ta.focus(); ta.select(); try { document.execCommand('copy'); return true; } catch(e){ return false; } finally { document.body.removeChild(ta); } };
  doCopy().then(()=>{ el.classList.add('copied'); const o = el.textContent; el.textContent = '✅ '+o; setTimeout(()=>{ el.classList.remove('copied'); el.textContent = o; },1200); });
}
async function loadAILatest(){
  const chat = $('aiChat').value || null; const p = new URLSearchParams(); if(chat) p.set('chat',chat);
  const r = await fetch('/api/chat/latest?'+p).then(r=>r.json());
  if(!r.task){ $('aiResult').innerHTML='<p class="hint">Нет</p>'; return; }
  renderAIResult(r.task.result, r.task.chat);
  $('aiStatus').textContent = 'Последний: '+fmtTime(r.task.completed_at);
}

// ===== PLAYERS =====
async function loadPlayersTab(){
  const sum = await fetch('/api/players_by_chat').then(r=>r.json());
  const sEl = $('plChatsSummary');
  if(sum.chats && sum.chats.length){
    sEl.innerHTML = '<div class="kpi">' + sum.chats.map(c=>'<div>'+esc(c.chat)+'<b>'+c.total_players+'</b><span class="hint">🟢 '+c.online_players+' · '+fmtMoney(c.total_messages)+'</span></div>').join('') + '</div>';
  }
  const p = new URLSearchParams();
  if($('plChatFilter').value) p.set('chat',$('plChatFilter').value);
  if($('plUserFilter').value) p.set('username',$('plUserFilter').value);
  if($('plOnlineOnly').checked) p.set('online_only','1');
  p.set('limit','1000');
  const r = await fetch('/api/players?'+p).then(r=>r.json());
  const body = $('plPlayersBody'); body.innerHTML='';
  if(!r.players.length){ body.innerHTML = '<tr><td colspan="6" class="hint">Нет</td></tr>'; return; }
  const now = Date.now()/1000;
  r.players.forEach(x=>{
    const online = (now - (x.last_seen||0)) < 300;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td><b>'+esc(x.chat)+'</b></td><td>'+esc(x.username)+'</td>'
      + '<td style="color:#8ab4f8;font-weight:bold">'+x.msg_count+'</td>'
      + '<td>'+(x.last_seen?fmtTime(x.last_seen):'—')+'</td>'
      + '<td>'+(online?'<span class="pill ok">🟢</span>':'<span class="pill">off</span>')+'</td>'
      + '<td><button class="btn" onclick="relocatePlayer(\''+esc(x.username)+'\')">↗ Язык</button></td>';
    body.appendChild(tr);
  });
}
async function relocatePlayer(username){
  const nc = prompt('Куда переместить "'+username+'"?'); if(!nc) return;
  await fetch('/api/players/relocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username, chat:nc.toUpperCase()})});
  loadPlayersTab();
}
async function loadLanguages(){
  const r = await fetch('/api/players_by_language').then(r=>r.json());
  const f = ($('langFilter').value || '').toLowerCase();
  const players = (r.players||[]).filter(p => !f || (p.username||'').toLowerCase().includes(f));
  const el = $('langGrid');
  if(!players.length){ el.innerHTML = '<p class="hint">Нет</p>'; return; }
  el.innerHTML = players.slice(0,500).map(p=>'<div class="lang-card"><h4>'+esc(p.username)+'</h4>'
    + '<div class="hint">Сообщений: '+(p.total_msgs||0)+' · Чатов: '+(p.chat_count||0)+'</div>'
    + (p.chats||[]).map(c=>'<div class="player-row">'+esc(c)+'</div>').join('')+'</div>').join('');
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

# ==================== ADMIN ====================
ADMIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Admin</title>
<style>body{margin:0;background:#0e1116;color:#e6e9ef;font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.box{background:#161b22;border:1px solid #26303a;border-radius:12px;padding:32px 40px;max-width:520px;width:100%}
input{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6e9ef;padding:10px;border-radius:8px;margin-bottom:16px;font-family:monospace;box-sizing:border-box}
button{width:100%;padding:12px;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;background:#dc2626;color:#fff}
</style></head><body><div class="box">
<h2>⚙️ Admin</h2>
<input id="apiKey" type="password" placeholder="X-API-Key">
<input id="confirm" type="text" placeholder="DELETE_ALL">
<button onclick="clearDb()">🗑 Очистить events</button>
<div id="r" style="margin-top:12px"></div></div><script>
async function clearDb(){
  const k=document.getElementById('apiKey').value.trim(); const c=document.getElementById('confirm').value.trim();
  const r=document.getElementById('r');
  if(!k||c!=='DELETE_ALL'){r.textContent='Заполни';return;}
  const res=await fetch('/api/clear',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':k},body:JSON.stringify({confirm:'DELETE_ALL'})});
  const d=await res.json();
  r.textContent = res.ok ? '✅ Готово' : '❌ '+(d.detail||'Ошибка');
}
</script></body></html>"""

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML

# ==================== STARTUP ====================
init_db()
print(f"[DB] Using {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
migrate_ts_once()

@app.on_event("startup")
async def startup_event():
    _load_server_rates()
    threading.Thread(target=rates_refresh_thread, daemon=True).start()
    threading.Thread(target=dedup_events, daemon=True).start()
    threading.Thread(target=dedup_thread, daemon=True).start()
    threading.Thread(target=ticks_cleanup_thread, daemon=True).start()
    threading.Thread(target=promo_parser_thread, daemon=True).start()
    print(f"[PROMO] Поток @{PROMO_CHANNEL} запущен")
    if GROQ_API_KEY and GROQ_API_KEY.startswith("gsk_"): print("[AI] ✅ GROQ")
    else: print("[AI] ⚠️ GROQ_API_KEY не задан")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
