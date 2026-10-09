import sqlite3
import threading
import json
import os

# По умолчанию — рядом с app.py. На Render через env-переменную
# укажешь путь на Persistent Disk: /var/data/monitor.db
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.db"))
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots"))

_lock = threading.Lock()
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema()
    return _conn


def _init_schema():
    with _lock:
        c = _conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, type TEXT NOT NULL, chat TEXT,
            sender TEXT, receivers TEXT, amount_text TEXT,
            amount_rub REAL, crypto TEXT, extra TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS bulk_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, chat TEXT, sender TEXT, amount_text TEXT,
            amount_rub REAL, crypto TEXT, receivers TEXT, window_sec INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS player_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, username TEXT, chat TEXT, text TEXT)""")
        for tbl, col in [
            ("events", "ts"), ("events", "type"), ("events", "sender"), ("events", "chat"),
            ("bulk_alerts", "ts"), ("bulk_alerts", "sender"),
            ("player_activity", "ts"), ("player_activity", "username"), ("player_activity", "chat"),
        ]:
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_{col} ON {tbl}({col})")
        _conn.commit()


# ---------- meta ----------
def meta_get(k, default=None):
    with _lock:
        r = _get_conn().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k, v):
    with _lock:
        _get_conn().execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, str(v)))
        _get_conn().commit()


# ---------- events ----------
def add_event(ts, etype, chat, sender, receivers, amount_text, amount_rub, crypto, extra=None):
    with _lock:
        _get_conn().execute(
            "INSERT INTO events (ts,type,chat,sender,receivers,amount_text,amount_rub,crypto,extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, etype, chat, sender, json.dumps(receivers or []),
             amount_text, amount_rub or 0, crypto, json.dumps(extra) if extra else None))
        _get_conn().commit()


def query_events(since=None, until=None, etype=None, chat=None,
                 sender=None, min_rub=None, limit=500):
    q = "SELECT * FROM events WHERE 1=1"; p = []
    if since: q += " AND ts>=?"; p.append(since)
    if until: q += " AND ts<=?"; p.append(until)
    if etype: q += " AND type=?"; p.append(etype)
    if chat: q += " AND chat=?"; p.append(chat)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    if min_rub is not None: q += " AND amount_rub>=?"; p.append(min_rub)
    q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
    with _lock:
        rows = _get_conn().execute(q, p).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["receivers"] = json.loads(d.get("receivers") or "[]")
        except: d["receivers"] = []
        out.append(d)
    return out


def hourly_histogram(since=None, until=None, etype=None, chat=None):
    q = ("SELECT CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS h, "
         "COUNT(*) c, SUM(amount_rub) s FROM events WHERE 1=1")
    p = []
    if since: q += " AND ts>=?"; p.append(since)
    if until: q += " AND ts<=?"; p.append(until)
    if etype: q += " AND type=?"; p.append(etype)
    if chat: q += " AND chat=?"; p.append(chat)
    q += " GROUP BY h"
    with _lock:
        rows = _get_conn().execute(q, p).fetchall()
    res = {h: {"count": 0, "sum": 0.0} for h in range(24)}
    for r in rows:
        res[r["h"]] = {"count": r["c"], "sum": r["s"] or 0}
    return res


def first_event_ts():
    with _lock:
        r = _get_conn().execute("SELECT MIN(ts) m FROM events").fetchone()
    return r["m"] if r and r["m"] else None


def distinct_chats():
    with _lock:
        rows = _get_conn().execute(
            "SELECT DISTINCT chat FROM events WHERE chat IS NOT NULL ORDER BY chat"
        ).fetchall()
    return [r["chat"] for r in rows]


def stats_summary():
    with _lock:
        c = _get_conn().cursor()
        first = c.execute("SELECT MIN(ts) m FROM events").fetchone()["m"]
        cnt = c.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        tips = c.execute("SELECT COUNT(*) c FROM events WHERE type='tip'").fetchone()["c"]
        rains = c.execute("SELECT COUNT(*) c FROM events WHERE type='rain'").fetchone()["c"]
    return {"first_ts": first, "total": cnt, "tips": tips, "rains": rains}


# ---------- bulk ----------
def add_bulk_alert(ts, chat, sender, amount_text, amount_rub, crypto, receivers, window_sec):
    with _lock:
        _get_conn().execute(
            "INSERT INTO bulk_alerts (ts,chat,sender,amount_text,amount_rub,crypto,receivers,window_sec) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, chat, sender, amount_text, amount_rub or 0, crypto,
             json.dumps(receivers), window_sec))
        _get_conn().commit()


def query_bulk(since=None, until=None, sender=None, limit=300):
    q = "SELECT * FROM bulk_alerts WHERE 1=1"; p = []
    if since: q += " AND ts>=?"; p.append(since)
    if until: q += " AND ts<=?"; p.append(until)
    if sender: q += " AND sender LIKE ?"; p.append(f"%{sender}%")
    q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
    with _lock:
        rows = _get_conn().execute(q, p).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try: d["receivers"] = json.loads(d.get("receivers") or "[]")
        except: d["receivers"] = []
        out.append(d)
    return out


# ---------- player activity ----------
def add_player_activity(ts, username, chat, text):
    with _lock:
        _get_conn().execute(
            "INSERT INTO player_activity (ts,username,chat,text) VALUES (?,?,?,?)",
            (ts, username, chat, text))
        _get_conn().commit()


def query_player(username=None, chat=None, since=None, limit=300):
    q = "SELECT * FROM player_activity WHERE 1=1"; p = []
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    if chat: q += " AND chat=?"; p.append(chat)
    if since: q += " AND ts>=?"; p.append(since)
    q += " ORDER BY ts DESC LIMIT ?"; p.append(limit)
    with _lock:
        rows = _get_conn().execute(q, p).fetchall()
    return [dict(r) for r in rows]


def player_histogram(username=None):
    q = ("SELECT CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) AS h, "
         "COUNT(*) c FROM player_activity WHERE 1=1")
    p = []
    if username: q += " AND username LIKE ?"; p.append(f"%{username}%")
    q += " GROUP BY h"
    with _lock:
        rows = _get_conn().execute(q, p).fetchall()
    res = {h: 0 for h in range(24)}
    for r in rows:
        res[r["h"]] = r["c"]
    return res