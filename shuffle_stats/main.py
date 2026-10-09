import os
import json
import sqlite3
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel

# ==================== КОНФИГ ====================
DB_PATH = os.environ.get("DB_PATH", "shuffle.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_SECRET_KEY")

# SCREENSHOT_DIR: если путь недоступен — откатываемся на папку рядом с main.py
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
        execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                type TEXT NOT NULL,
                chat TEXT NOT NULL,
                sender TEXT NOT NULL,
                receivers TEXT NOT NULL,
                amount_text TEXT,
                amount_type TEXT,
                crypto_symbol TEXT,
                amount_rub DOUBLE PRECISION DEFAULT 0,
                created_at TEXT NOT NULL,
                ts DOUBLE PRECISION
            )
        """)
    else:
        execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                chat TEXT NOT NULL,
                sender TEXT NOT NULL,
                receivers TEXT NOT NULL,
                amount_text TEXT,
                amount_type TEXT,
                crypto_symbol TEXT,
                amount_rub REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                ts REAL
            )
        """)
    _try("ALTER TABLE events ADD COLUMN ts DOUBLE PRECISION" if USE_POSTGRES
         else "ALTER TABLE events ADD COLUMN ts REAL")

    execute("CREATE INDEX IF NOT EXISTS idx_type ON events(type)")
    execute("CREATE INDEX IF NOT EXISTS idx_sender ON events(sender)")
    execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat)")

    if USE_POSTGRES:
        execute("""
            CREATE TABLE IF NOT EXISTS bulk_alerts (
                id SERIAL PRIMARY KEY,
                ts DOUBLE PRECISION NOT NULL,
                chat TEXT NOT NULL,
                sender TEXT NOT NULL,
                amount_text TEXT,
                amount_rub DOUBLE PRECISION DEFAULT 0,
                crypto TEXT,
                receivers TEXT NOT NULL,
                window_sec INTEGER DEFAULT 0
            )
        """)
    else:
        execute("""
            CREATE TABLE IF NOT EXISTS bulk_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                chat TEXT NOT NULL,
                sender TEXT NOT NULL,
                amount_text TEXT,
                amount_rub REAL DEFAULT 0,
                crypto TEXT,
                receivers TEXT NOT NULL,
                window_sec INTEGER DEFAULT 0
            )
        """)
    execute("CREATE INDEX IF NOT EXISTS idx_bulk_ts ON bulk_alerts(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_bulk_sender ON bulk_alerts(sender)")

    if USE_POSTGRES:
        execute("""
            CREATE TABLE IF NOT EXISTS player_activity (
                id SERIAL PRIMARY KEY,
                ts DOUBLE PRECISION NOT NULL,
                username TEXT NOT NULL,
                chat TEXT NOT NULL,
                text TEXT
            )
        """)
    else:
        execute("""
            CREATE TABLE IF NOT EXISTS player_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                username TEXT NOT NULL,
                chat TEXT NOT NULL,
                text TEXT
            )
        """)
    execute("CREATE INDEX IF NOT EXISTS idx_player_ts ON player_activity(ts)")
    execute("CREATE INDEX IF NOT EXISTS idx_player_user ON player_activity(username)")


init_db()
print(f"[DB] Using {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")


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
    created_at: Optional[str] = None
    ts: Optional[float] = None


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


# ==================== API ДЛЯ СКРИПТА ====================
@app.post("/api/event")
async def add_event(event: EventIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if event.type not in ("rain", "tip"):
        raise HTTPException(status_code=400, detail="type must be rain or tip")

    ts_val = event.ts if event.ts else time.time()
    created = event.created_at or datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
    p = ph()
    execute(f"""
        INSERT INTO events (type, chat, sender, receivers, amount_text, amount_type,
                            crypto_symbol, amount_rub, created_at, ts)
        VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
    """, (
        event.type, event.chat, event.sender, json.dumps(event.receivers, ensure_ascii=False),
        event.amount_text, event.amount_type, event.crypto_symbol, event.amount_rub, created, ts_val
    ))
    return {"ok": True}


@app.post("/api/bulk_alert")
async def add_bulk_alert(body: BulkAlertIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    p = ph()
    execute(f"""
        INSERT INTO bulk_alerts (ts, chat, sender, amount_text, amount_rub, crypto, receivers, window_sec)
        VALUES ({p},{p},{p},{p},{p},{p},{p},{p})
    """, (ts_val, body.chat, body.sender, body.amount_text, body.amount_rub,
          body.crypto, json.dumps(body.receivers, ensure_ascii=False), body.window_sec))
    return {"ok": True}


@app.post("/api/player_activity")
async def add_player_activity(body: PlayerActivityIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    ts_val = body.ts if body.ts else time.time()
    p = ph()
    execute(f"""
        INSERT INTO player_activity (ts, username, chat, text)
        VALUES ({p},{p},{p},{p})
    """, (ts_val, body.username, body.chat, body.text))
    return {"ok": True}


@app.post("/api/status")
async def post_status(payload: StatusPayload, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = time.time()
    with _state_lock:
        for inst in payload.instances:
            INSTANCE_STATES[inst.iid] = {
                **inst.dict(),
                "server_received_at": now,
            }
        for iid in payload.screenshot_requests:
            if iid not in SCREENSHOT_REQUESTS:
                SCREENSHOT_REQUESTS[iid] = {"requested_at": now, "done": False}
    return {"ok": True}


@app.get("/api/screenshot_req")
async def get_screenshot_req(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    with _state_lock:
        pending = [{"iid": iid} for iid, r in SCREENSHOT_REQUESTS.items() if not r.get("done")]
    return {"requests": pending}


@app.post("/api/screenshot_upload")
async def upload_screenshot(
    iid: int = Form(...),
    file: UploadFile = File(...),
    x_api_key: str = Header(default=""),
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    path = os.path.join(SCREENSHOT_DIR, f"inst{iid}_latest.png")
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    with _state_lock:
        prev = SCREENSHOT_REQUESTS.get(iid, {})
        SCREENSHOT_REQUESTS[iid] = {
            "requested_at": prev.get("requested_at", time.time()),
            "done": True,
            "path": path,
            "uploaded_at": time.time(),
        }
    return {"ok": True}


# ==================== API ДЛЯ UI ====================
@app.get("/api/status")
async def get_status():
    with _state_lock:
        instances = list(INSTANCE_STATES.values())
    instances.sort(key=lambda x: x.get("iid", 0))
    s = query_one("SELECT COUNT(*) AS c FROM events") or {"c": 0}
    tips = query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {"c": 0}
    rains = query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {"c": 0}
    first = query_one("SELECT MIN(ts) AS m FROM events") or {"m": None}
    return {
        "first_ts": first.get("m"),
        "total": s.get("c", 0),
        "tips": tips.get("c", 0),
        "rains": rains.get("c", 0),
        "instances": instances,
    }


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
async def api_events(
    since: Optional[float] = None,
    until: Optional[float] = None,
    type: Optional[str] = None,
    chat: Optional[str] = None,
    sender: Optional[str] = None,
    min_rub: Optional[float] = None,
    limit: int = 500,
):
    q = "SELECT * FROM events WHERE 1=1"
    p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if type: q += " AND type = ?"; p.append(type)
    if chat: q += " AND chat = ?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_rub is not None: q += " AND amount_rub >= ?"; p.append(min_rub)
    q += " ORDER BY ts DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    out = []
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
        out.append(r)
    return {"events": out}


@app.get("/api/chart")
async def api_chart(
    type: Optional[str] = None,
    chat: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
):
    if USE_POSTGRES:
        hour_expr = "CAST(EXTRACT(HOUR FROM to_timestamp(ts)) AS INTEGER)"
    else:
        hour_expr = "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER)"

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
        h = r.get("h")
        if h is None: continue
        buckets[int(h)] = {"count": r["c"], "sum": r["s"] or 0}
    return {"buckets": buckets}


@app.get("/api/chats")
async def api_chats():
    rows = query_all("SELECT DISTINCT chat FROM events WHERE chat IS NOT NULL ORDER BY chat")
    return {"chats": [r["chat"] for r in rows]}


@app.get("/api/bulk")
async def api_bulk(
    since: Optional[float] = None,
    until: Optional[float] = None,
    sender: Optional[str] = None,
    limit: int = 300,
):
    q = "SELECT * FROM bulk_alerts WHERE 1=1"
    p = []
    if since is not None: q += " AND ts >= ?"; p.append(since)
    if until is not None: q += " AND ts <= ?"; p.append(until)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    q += " ORDER BY ts DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))
    for r in rows:
        try: r["receivers"] = json.loads(r.get("receivers") or "[]")
        except: r["receivers"] = []
    return {"alerts": rows}


@app.get("/api/player")
async def api_player(
    username: Optional[str] = None,
    chat: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = 300,
):
    q = "SELECT * FROM player_activity WHERE 1=1"
    p = []
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if chat: q += " AND chat = ?"; p.append(chat)
    if since is not None: q += " AND ts >= ?"; p.append(since)
    q += " ORDER BY ts DESC LIMIT ?"
    p.append(limit)
    rows = query_all(q, tuple(p))

    if USE_POSTGRES:
        hour_expr = "CAST(EXTRACT(HOUR FROM to_timestamp(ts)) AS INTEGER)"
    else:
        hour_expr = "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER)"
    hq = f"SELECT {hour_expr} AS h, COUNT(*) AS c FROM player_activity WHERE 1=1"
    hp = []
    if username: hq += " AND username LIKE ?"; hp.append(f"%{username}%")
    if chat: hq += " AND chat = ?"; hp.append(chat)
    hq += " GROUP BY h"
    hrows = query_all(hq, tuple(hp))
    hourly = {h: 0 for h in range(24)}
    for r in hrows:
        if r.get("h") is not None:
            hourly[int(r["h"])] = r["c"]
    return {"rows": rows, "hourly": hourly}


@app.get("/api/health")
async def health():
    return {"ok": True}


# ==================== СТАРЫЕ API ====================
@app.get("/api/stats")
async def get_stats():
    total_rains = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {}).get("c", 0)
    total_tips = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {}).get("c", 0)
    total_rain_rub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'") or {}).get("s", 0)
    total_tip_rub = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'") or {}).get("s", 0)

    top_amount = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub),0) AS total, COALESCE(AVG(amount_rub),0) AS avg
        FROM events WHERE type='rain' GROUP BY sender ORDER BY total DESC LIMIT 100
    """)
    top_wins = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub),0) AS total, COALESCE(AVG(amount_rub),0) AS avg
        FROM events WHERE type='rain' GROUP BY sender ORDER BY wins DESC LIMIT 100
    """)
    top_tips = query_all("""
        SELECT sender AS nickname, COUNT(*) AS tips_sent, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip' GROUP BY sender ORDER BY total DESC LIMIT 100
    """)
    top_tip_receivers = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in query_all("SELECT receivers, amount_rub FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            for r in rcs:
                top_tip_receivers[r]["count"] += 1
                top_tip_receivers[r]["total"] += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass
    top_tip_recv = sorted(
        [{"nickname": k, "tips_received": v["count"], "total": v["total"]}
         for k, v in top_tip_receivers.items()],
        key=lambda x: x["total"], reverse=True
    )[:100]

    recent = query_all("""
        SELECT type, chat, sender, receivers, amount_text, crypto_symbol, amount_rub, created_at
        FROM events ORDER BY id DESC LIMIT 30
    """)
    channels = query_all("""
        SELECT chat,
               SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
               SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips,
               COALESCE(SUM(amount_rub),0) AS total_rub
        FROM events GROUP BY chat ORDER BY total_rub DESC
    """)
    winners = set()
    for row in query_all("SELECT receivers FROM events WHERE type='rain'"):
        try:
            for r in json.loads(row["receivers"] or "[]"): winners.add(r)
        except: pass

    return {
        "totals": {
            "rains": total_rains, "tips": total_tips,
            "rain_rub": round(total_rain_rub or 0, 2),
            "tip_rub": round(total_tip_rub or 0, 2),
            "unique_winners": len(winners),
        },
        "top_amount": top_amount, "top_wins": top_wins, "top_tips": top_tips,
        "top_tip_receivers": top_tip_recv, "channels": channels, "recent": recent,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/clear")
async def clear_database(body: dict, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if body.get("confirm") != "DELETE_ALL":
        raise HTTPException(status_code=400, detail="Подтверждение обязательно (confirm=DELETE_ALL)")
    if USE_POSTGRES:
        execute("TRUNCATE TABLE events RESTART IDENTITY CASCADE;")
    else:
        execute("DELETE FROM events;")
        try: execute("DELETE FROM sqlite_sequence WHERE name='events';")
        except: pass
    print("[ADMIN] 🗑 База очищена")
    return {"ok": True, "message": "Все данные удалены"}


@app.get("/api/user/{nickname}")
async def get_user(nickname: str):
    sent = query_one("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain' AND sender=?
    """, (nickname,)) or {"cnt": 0, "total": 0}

    won_cnt, won_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='rain'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                won_cnt += 1
                won_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass

    tips_sent = query_one("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip' AND sender=?
    """, (nickname,)) or {"cnt": 0, "total": 0}

    tips_recv_cnt, tips_recv_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                tips_recv_cnt += 1
                tips_recv_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass

    return {
        "nickname": nickname,
        "given_rains": {"count": sent["cnt"], "total_rub": round(sent["total"], 2)},
        "received_rains": {"count": won_cnt, "total_rub": round(won_total, 2)},
        "tips_sent": {"count": tips_sent["cnt"], "total_rub": round(tips_sent["total"], 2)},
        "tips_received": {"count": tips_recv_cnt, "total_rub": round(tips_recv_total, 2)},
    }


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
input,select{background:#171a21;color:#e6e6e6;border:1px solid #2a2f3a;padding:6px 10px;border-radius:5px;font-size:13px;max-width:200px}
.chart-wrap{background:#151821;border:1px solid #232a37;border-radius:8px;padding:16px;margin-bottom:16px}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:center;justify-content:center;z-index:50}
.modal img{max-width:92vw;max-height:92vh;border-radius:8px;border:1px solid #333}
.modal.on{display:flex}
.hint{color:#8b93a7;font-size:12px}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.kpi div{background:#151821;border:1px solid #232a37;border-radius:8px;padding:10px 16px;min-width:150px}
.kpi b{display:block;font-size:20px;color:#8ab4f8;margin-top:2px}
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
  <button data-tab="player">Активность</button>
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
      <input type="datetime-local" id="fSince">
      <input type="datetime-local" id="fUntil">
      <button class="btn" onclick="loadEvents()">Фильтр</button>
      <button class="btn" onclick="resetEvents()">Сброс</button>
    </div>
    <table><thead><tr><th>Время</th><th>Тип</th><th>Чат</th><th>От</th><th>Кому</th><th>Сумма</th><th>₽</th><th>Крипта</th></tr></thead>
    <tbody id="evBody"></tbody></table>
  </div>

  <div id="tab-chart" style="display:none">
    <div class="row-flex">
      <select id="cType"><option value="">Всё</option><option value="tip">Tip</option><option value="rain">Rain</option></select>
      <select id="cChat"><option value="">Все чаты</option></select>
      <button class="btn" onclick="loadChart()">Обновить</button>
      <span class="hint">— распределение по часам суток</span>
    </div>
    <div class="chart-wrap"><canvas id="hourCount" height="90"></canvas></div>
    <div class="chart-wrap"><canvas id="hourSum" height="90"></canvas></div>
  </div>

  <div id="tab-bulk" style="display:none">
    <div class="row-flex">
      <input type="text" id="bSender" placeholder="Отправитель">
      <button class="btn" onclick="loadBulk()">Фильтр</button>
    </div>
    <p class="hint">Срабатывает, когда один отправитель шлёт <b>одну и ту же сумму 3+ людям</b> в одном чате за 20 минут.</p>
    <table><thead><tr><th>Время</th><th>Чат</th><th>От</th><th>Сумма</th><th>Получателей</th><th>Кому</th></tr></thead>
    <tbody id="bulkBody"></tbody></table>
  </div>

  <div id="tab-player" style="display:none">
    <div class="row-flex">
      <input type="text" id="pUser" placeholder="username">
      <select id="pChat"><option value="">Все чаты</option></select>
      <button class="btn" onclick="loadPlayer()">Фильтр</button>
    </div>
    <div class="chart-wrap"><canvas id="playerHour" height="70"></canvas></div>
    <table><thead><tr><th>Время</th><th>Игрок</th><th>Чат</th><th>Сообщение</th></tr></thead>
    <tbody id="plBody"></tbody></table>
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

function switchTab(name){
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  ['status','events','chart','bulk','player'].forEach(t=>{
    $('tab-'+t).style.display = (t===name) ? '' : 'none';
  });
  if(name==='events') loadEvents();
  if(name==='chart') loadChart();
  if(name==='bulk') loadBulk();
  if(name==='player') loadPlayer();
}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));

async function refreshStatus(){
  try {
    const r = await fetch('/api/status').then(r=>r.json());
    $('uptime').textContent = r.first_ts
      ? 'Мониторинг с ' + new Date(r.first_ts*1000).toLocaleString('ru-RU')
      : 'Мониторинг только запущен';
    $('kpiTotal').textContent = `Всего: ${r.total||0} | Tips: ${r.tips||0} | Rains: ${r.rains||0}`;
    const now = Date.now()/1000;
    $('kpiRow').innerHTML = `
      <div>Всего событий<b>${r.total||0}</b></div>
      <div>Tips<b>${r.tips||0}</b></div>
      <div>Rains<b>${r.rains||0}</b></div>
      <div>Вкладок онлайн<b>${(r.instances||[]).filter(i=>now - (i.updated_at||0) < 40).length}</b></div>`;
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
      tr.innerHTML = `<td>${i.iid}</td>
        <td>${esc(i.mode||'')}</td>
        <td><b>${esc(i.current_chat||'—')}</b><br><span class="pill">${(i.chats||[]).map(esc).join(', ')}</span></td>
        <td><span class="${cls}">${esc(txt)}</span>${i.error?'<br><span class="hint">'+esc(i.error)+'</span>':''}</td>
        <td>${progress}</td>
        <td>${upd}</td>
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
    if(r.status === 404){ alert('Скриншот не удалось сделать'); return; }
  }
  alert('Таймаут');
}

function dtToTs(v){ return v ? Math.floor(new Date(v).getTime()/1000) : ''; }

async function loadChats(){
  const r = await fetch('/api/chats').then(r=>r.json());
  ['fChat','cChat','pChat'].forEach(id=>{
    const sel = $(id); const cur = sel.value;
    sel.innerHTML = '<option value="">Все чаты</option>' +
      r.chats.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    sel.value = cur;
  });
}

async function loadEvents(){
  const p = new URLSearchParams();
  if($('fSender').value) p.set('sender',$('fSender').value);
  if($('fChat').value) p.set('chat',$('fChat').value);
  if($('fType').value) p.set('type',$('fType').value);
  if($('fMinRub').value) p.set('min_rub',$('fMinRub').value);
  const s = dtToTs($('fSince').value); if(s) p.set('since', s);
  const u = dtToTs($('fUntil').value); if(u) p.set('until', u);
  const r = await fetch('/api/events?'+p).then(r=>r.json());
  const body = $('evBody'); body.innerHTML = '';
  r.events.forEach(e=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${e.ts?fmtTime(e.ts):esc(e.created_at)}</td>
      <td><span class="pill">${esc(e.type)}</span></td>
      <td>${esc(e.chat)}</td>
      <td>${esc(e.sender)}</td>
      <td>${(e.receivers||[]).map(esc).join(', ')}</td>
      <td>${esc(e.amount_text)}</td>
      <td>${fmtMoney(e.amount_rub)}</td>
      <td>${esc(e.crypto_symbol||'')}</td>`;
    body.appendChild(tr);
  });
}
function resetEvents(){
  ['fSender','fMinRub','fSince','fUntil'].forEach(id=>$(id).value='');
  $('fChat').value=''; $('fType').value='';
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
  chart1 = new Chart($('hourCount'), {
    type:'bar',
    data:{labels, datasets:[{label:'Кол-во событий', data:counts, backgroundColor:'#4a7ce0'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}, title:{display:true,text:'События по часам',color:'#e6e6e6'}},
             scales:{x:{ticks:{color:'#8b93a7'}}, y:{ticks:{color:'#8b93a7'}}}}
  });
  chart2 = new Chart($('hourSum'), {
    type:'bar',
    data:{labels, datasets:[{label:'Сумма ₽', data:sums, backgroundColor:'#71d68a'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}, title:{display:true,text:'Сумма по часам',color:'#e6e6e6'}},
             scales:{x:{ticks:{color:'#8b93a7'}}, y:{ticks:{color:'#8b93a7'}}}}
  });
}

async function loadBulk(){
  const p = new URLSearchParams();
  if($('bSender').value) p.set('sender',$('bSender').value);
  const r = await fetch('/api/bulk?'+p).then(r=>r.json());
  const body = $('bulkBody'); body.innerHTML='';
  r.alerts.forEach(a=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtTime(a.ts)}</td>
      <td>${esc(a.chat)}</td>
      <td>${esc(a.sender)}</td>
      <td>${esc(a.amount_text)}</td>
      <td>${(a.receivers||[]).length}</td>
      <td>${(a.receivers||[]).map(esc).join(', ')}</td>`;
    body.appendChild(tr);
  });
}

async function loadPlayer(){
  const p = new URLSearchParams();
  if($('pUser').value) p.set('username',$('pUser').value);
  if($('pChat').value) p.set('chat',$('pChat').value);
  const r = await fetch('/api/player?'+p).then(r=>r.json());
  const body = $('plBody'); body.innerHTML='';
  r.rows.forEach(x=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${fmtTime(x.ts)}</td>
      <td><b>${esc(x.username)}</b></td>
      <td>${esc(x.chat)}</td>
      <td>${esc(x.text)}</td>`;
    body.appendChild(tr);
  });
  const labels = [...Array(24).keys()].map(h=>String(h).padStart(2,'0')+':00');
  const vals = labels.map((_,h)=>r.hourly[h]||0);
  if(chartPlayer) chartPlayer.destroy();
  chartPlayer = new Chart($('playerHour'), {
    type:'bar',
    data:{labels, datasets:[{label:'Сообщений', data:vals, backgroundColor:'#c58af9'}]},
    options:{plugins:{legend:{labels:{color:'#e6e6e6'}}},
             scales:{x:{ticks:{color:'#8b93a7'}}, y:{ticks:{color:'#8b93a7'}}}}
  });
}

loadChats();
refreshStatus();
setInterval(refreshStatus, 3000);
</script></body></html>
"""


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page():
    return MONITOR_HTML


# ==================== БАЗОВЫЙ HTML ====================
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
    try:
        return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
    except:
        return "0 ₽"


def render_rank_table(rows, cnt_header="Кол-во"):
    """Универсальная таблица: # | ник | cnt | сумма."""
    if not rows:
        return "<tr><td colspan='4' class='muted'>Пока нет данных</td></tr>"
    html = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        html += f"""<tr>
            <td><span class="rank {rank_cls}">{i}</span></td>
            <td><b>{r['nickname']}</b></td>
            <td>{r['cnt']}</td>
            <td class="amount">{fmt_rub(r['total'])}</td>
        </tr>"""
    return html


def compute_receiver_stats(etype):
    """{ник: {cnt, total}} для получателей событий типа etype."""
    stats = defaultdict(lambda: {"cnt": 0, "total": 0.0})
    for row in query_all(f"SELECT amount_rub, receivers FROM events WHERE type='{etype}'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
        except Exception:
            continue
        if not rcs:
            continue
        share = (row["amount_rub"] or 0) / len(rcs)
        for r in rcs:
            if not r:
                continue
            stats[r]["cnt"] += 1
            stats[r]["total"] += share
    return stats


def sort_receiver_stats(stats, by="total", limit=50):
    arr = [{"nickname": k, "cnt": v["cnt"], "total": v["total"]}
           for k, v in stats.items()]
    arr.sort(key=lambda x: x[by], reverse=True)
    return arr[:limit]


# ==================== ГЛАВНАЯ ====================
@app.get("/", response_class=HTMLResponse)
async def page_home():
    t = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'") or {}).get("c", 0)
    tips = (query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'") or {}).get("c", 0)
    tr = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'") or {}).get("s", 0)
    ttr = (query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'") or {}).get("s", 0)

    content = f"""
    <div class="grid">
      <div class="card"><h3>Дождей</h3><div class="big">{t}</div><div class="sub">событий</div></div>
      <div class="card"><h3>Раздали всего</h3><div class="big">{fmt_rub(tr or 0)}</div></div>
      <div class="card"><h3>Чаевых</h3><div class="big">{tips}</div><div class="sub">событий</div></div>
      <div class="card"><h3>Чаевых сумма</h3><div class="big">{fmt_rub(ttr or 0)}</div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3>Последние события</h3><div id="recent"></div>
    </div>
    <script>
      document.addEventListener('stats-updated', (e) => renderRecent(e.detail.recent));
      async function loadOnce() {{ const r = await fetch('/api/stats'); const d = await r.json(); renderRecent(d.recent); }}
      function renderRecent(rows) {{
        const el = document.getElementById('recent');
        if (!rows) return;
        el.innerHTML = rows.map(r => {{
          const cls = r.type === 'rain' ? 'rain' : 'tip';
          const tag = r.type === 'rain' ? 'RAIN' : 'TIP';
          const amt = r.amount_text || '';
          const rub = r.amount_rub ? ' (' + Number(r.amount_rub).toFixed(0) + ' ₽)' : '';
          let recs = ''; try {{ recs = JSON.parse(r.receivers || '[]').slice(0,5).join(', '); }} catch(e) {{}}
          return `<div class="recent-row"><span class="tag ${{cls}}">${{tag}}</span> <span class="chat-pill">${{r.chat}}</span> <b style="margin-left:6px">${{r.sender}}</b><span class="muted"> → ${{recs}}</span><span style="float:right" class="amount">${{amt}}${{rub}}</span></div>`;
        }}).join('');
      }}
      loadOnce();
    </script>"""
    return BASE_HTML.format(title="Shuffle Rain Stats", active_home="active",
                             active_hosts="", active_receivers="", content=content)


# ==================== /hosts ====================
@app.get("/hosts", response_class=HTMLResponse)
async def page_hosts():
    rain_by_amount = query_all("""
        SELECT sender AS nickname, COUNT(*) AS cnt,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY total DESC LIMIT 50
    """)
    rain_by_count = query_all("""
        SELECT sender AS nickname, COUNT(*) AS cnt,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY cnt DESC LIMIT 50
    """)
    tip_by_amount = query_all("""
        SELECT sender AS nickname, COUNT(*) AS cnt,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip'
        GROUP BY sender ORDER BY total DESC LIMIT 50
    """)
    tip_by_count = query_all("""
        SELECT sender AS nickname, COUNT(*) AS cnt,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip'
        GROUP BY sender ORDER BY cnt DESC LIMIT 50
    """)

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>🌧 Раздал дождей — по сумме</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(rain_by_amount)}</tbody></table></div>

      <div class="card"><h3>🌧 Раздал дождей — по количеству</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(rain_by_count)}</tbody></table></div>

      <div class="card"><h3>💸 Отправил чаевых — по сумме</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(tip_by_amount)}</tbody></table></div>

      <div class="card"><h3>💸 Отправил чаевых — по количеству</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Кол-во</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(tip_by_count)}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(
        title="Shuffle — Раздающие",
        active_home="", active_hosts="active", active_receivers="",
        content=content,
    )


# ==================== /receivers ====================
@app.get("/receivers", response_class=HTMLResponse)
async def page_receivers():
    rain_stats = compute_receiver_stats("rain")
    tip_stats = compute_receiver_stats("tip")

    rain_by_amount = sort_receiver_stats(rain_stats, by="total")
    rain_by_count = sort_receiver_stats(rain_stats, by="cnt")
    tip_by_amount = sort_receiver_stats(tip_stats, by="total")
    tip_by_count = sort_receiver_stats(tip_stats, by="cnt")

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>🏆 Выиграл дождей — по сумме</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Выигрышей</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(rain_by_amount)}</tbody></table></div>

      <div class="card"><h3>🎯 Выиграл дождей — по частоте</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Выигрышей</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(rain_by_count)}</tbody></table></div>

      <div class="card"><h3>💰 Получил чаевых — по сумме</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(tip_by_amount)}</tbody></table></div>

      <div class="card"><h3>📬 Получил чаевых — по количеству</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead>
        <tbody>{render_rank_table(tip_by_count)}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(
        title="Shuffle — Получатели",
        active_home="", active_hosts="", active_receivers="active",
        content=content,
    )


# ==================== СТАРЫЕ /rating/* ====================
def render_table_legacy(rows, kind):
    if not rows:
        return "<tr><td colspan='4' class='muted'>Пока нет данных</td></tr>"
    html = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        cnt_field = 'tips_sent' if kind == 'tips' else 'wins'
        html += f"""<tr>
            <td><span class="rank {rank_cls}">{i}</span></td>
            <td><b>{r['nickname']}</b></td>
            <td>{r[cnt_field]}</td>
            <td class="amount">{fmt_rub(r['total'])}</td>
        </tr>"""
    return html


@app.get("/rating/amount", response_class=HTMLResponse)
async def page_amount():
    rows = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain' GROUP BY sender ORDER BY total DESC LIMIT 100
    """)
    content = f"""<div class="card"><h3>Топ по сумме дождей</h3>
    <table><thead><tr><th>#</th><th>Ник</th><th>Побед</th><th>Сумма</th></tr></thead>
    <tbody>{render_table_legacy(rows, 'amount')}</tbody></table></div>"""
    return BASE_HTML.format(title="Shuffle — По сумме", active_home="",
                             active_hosts="", active_receivers="", content=content)


@app.get("/rating/wins", response_class=HTMLResponse)
async def page_wins():
    rows = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain' GROUP BY sender ORDER BY wins DESC LIMIT 100
    """)
    content = f"""<div class="card"><h3>Топ по количеству дождей</h3>
    <table><thead><tr><th>#</th><th>Ник</th><th>Побед</th><th>Сумма</th></tr></thead>
    <tbody>{render_table_legacy(rows, 'wins')}</tbody></table></div>"""
    return BASE_HTML.format(title="Shuffle — По победам", active_home="",
                             active_hosts="", active_receivers="", content=content)


@app.get("/rating/tips", response_class=HTMLResponse)
async def page_tips():
    sent = query_all("""
        SELECT sender AS nickname, COUNT(*) AS tips_sent, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip' GROUP BY sender ORDER BY total DESC LIMIT 100
    """)
    recv_data = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in query_all("SELECT receivers, amount_rub FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            for r in rcs:
                recv_data[r]["count"] += 1
                recv_data[r]["total"] += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except: pass
    recv_sorted = sorted(
        [{"nickname": k, "tips_sent": v["count"], "total": v["total"]} for k, v in recv_data.items()],
        key=lambda x: x["total"], reverse=True
    )[:100]

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>Кто больше отправил</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Отправлено</th><th>Сумма</th></tr></thead>
        <tbody>{render_table_legacy(sent, 'tips')}</tbody></table></div>
      <div class="card"><h3>Кто больше получил</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead>
        <tbody>{render_table_legacy(recv_sorted, 'tips')}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(title="Shuffle — По чаевым", active_home="",
                             active_hosts="", active_receivers="", content=content)


@app.get("/user/{nickname}", response_class=HTMLResponse)
async def page_user(nickname: str):
    data = await get_user(nickname)
    g, rec = data["given_rains"], data["received_rains"]
    ti, to = data["tips_sent"], data["tips_received"]
    content = f"""
    <div class="card"><h3>Пользователь</h3><div class="big">{nickname}</div></div>
    <div class="grid" style="margin-top:16px">
      <div class="card"><h3>Раздал дождей</h3><div class="big">{g['count']}</div><div class="sub">{fmt_rub(g['total_rub'])}</div></div>
      <div class="card"><h3>Выиграл дождей</h3><div class="big">{rec['count']}</div><div class="sub">{fmt_rub(rec['total_rub'])}</div></div>
      <div class="card"><h3>Отправил чаевых</h3><div class="big">{ti['count']}</div><div class="sub">{fmt_rub(ti['total_rub'])}</div></div>
      <div class="card"><h3>Получил чаевых</h3><div class="big">{to['count']}</div><div class="sub">{fmt_rub(to['total_rub'])}</div></div>
    </div>"""
    return BASE_HTML.format(title=f"Shuffle — {nickname}", active_home="",
                             active_hosts="", active_receivers="", content=content)


# ==================== ADMIN ====================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Admin — Shuffle Rain</title>
<style>
body { margin:0; background:#0e1116; color:#e6e9ef; font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; display:flex; justify-content:center; align-items:center; min-height:100vh; padding:20px; }
.box { background:#161b22; border:1px solid #26303a; border-radius:12px; padding:32px 40px; max-width:520px; width:100%; }
h1 { margin:0 0 8px; font-size:22px; }
.sub { color:#8b949e; margin-bottom:24px; font-size:14px; }
input { width:100%; background:#0d1117; border:1px solid #30363d; color:#e6e9ef; padding:10px 12px; border-radius:8px; font-size:14px; margin-bottom:16px; font-family:monospace; }
button { width:100%; padding:12px; border:none; border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; background:#dc2626; color:#fff; }
button:disabled { background:#4b5563; cursor:not-allowed; }
.result { margin-top:16px; padding:12px; border-radius:8px; font-size:13px; display:none; }
.result.ok { background:#052e16; color:#4ade80; border:1px solid #14532d; display:block; }
.result.err { background:#2d0f0f; color:#f87171; border:1px solid #7f1d1d; display:block; }
.warn { background:#2d2410; border:1px solid #78350f; color:#fbbf24; padding:12px; border-radius:8px; margin-bottom:20px; font-size:13px; }
a { color:#7cc4ff; text-decoration:none; font-size:13px; }
</style></head><body>
<div class="box">
  <h1>⚙️ Админ-панель</h1>
  <div class="sub">Shuffle Rain Stats — управление БД</div>
  <div class="warn">⚠️ Кнопка ниже <b>удалит ВСЕ события</b> (rain + tips). Действие необратимо.</div>
  <input id="apiKey" type="password" placeholder="X-API-Key">
  <input id="confirm" type="text" placeholder="DELETE_ALL" autocomplete="off">
  <button id="clearBtn" onclick="clearDb()">🗑 Очистить базу данных</button>
  <div id="result" class="result"></div>
  <div style="margin-top:20px; text-align:center"><a href="/">← На главную</a></div>
</div>
<script>
async function clearDb() {
  const apiKey = document.getElementById('apiKey').value.trim();
  const confirm = document.getElementById('confirm').value.trim();
  const result = document.getElementById('result');
  const btn = document.getElementById('clearBtn');
  result.className = 'result'; result.textContent = '';
  if (!apiKey) { result.className='result err'; result.textContent='Введите API-ключ'; return; }
  if (confirm !== 'DELETE_ALL') { result.className='result err'; result.textContent='Для подтверждения введите DELETE_ALL'; return; }
  if (!window.confirm('Точно удалить ВСЕ данные? Это необратимо!')) return;
  btn.disabled = true; btn.textContent = '⏳ Очистка...';
  try {
    const r = await fetch('/api/clear', { method:'POST', headers:{'Content-Type':'application/json','X-API-Key':apiKey}, body: JSON.stringify({confirm:'DELETE_ALL'}) });
    const data = await r.json();
    if (r.ok) { result.className='result ok'; result.textContent='✅ '+(data.message||'Готово'); }
    else { result.className='result err'; result.textContent='❌ '+(data.detail||'Ошибка'); }
  } catch(e) { result.className='result err'; result.textContent='❌ '+e.message; }
  finally { btn.disabled=false; btn.textContent='🗑 Очистить базу данных'; }
}
</script></body></html>
"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
