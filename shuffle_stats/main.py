import os
import json
import sqlite3
import requests
from datetime import datetime, timezone
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

# ==================== КОНФИГ ====================
DB_PATH = os.environ.get("DB_PATH", "shuffle.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_SECRET_KEY")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

app = FastAPI(title="Shuffle Rain & Tips Stats")


# ==================== БАЗА ====================
def get_db():
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ph():
    """Placeholder для SQL: ? для SQLite, %s для Postgres."""
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
        except:
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
                created_at TEXT NOT NULL
            )
        """)
        execute("CREATE INDEX IF NOT EXISTS idx_type ON events(type)")
        execute("CREATE INDEX IF NOT EXISTS idx_sender ON events(sender)")
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
                created_at TEXT NOT NULL
            )
        """)
        execute("CREATE INDEX IF NOT EXISTS idx_type ON events(type)")
        execute("CREATE INDEX IF NOT EXISTS idx_sender ON events(sender)")


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


# ==================== API ====================
@app.post("/api/event")
async def add_event(event: EventIn, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if event.type not in ("rain", "tip"):
        raise HTTPException(status_code=400, detail="type must be rain or tip")

    created = event.created_at or datetime.now(timezone.utc).isoformat()
    p = ph()
    execute(f"""
        INSERT INTO events (type, chat, sender, receivers, amount_text, amount_type,
                            crypto_symbol, amount_rub, created_at)
        VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
    """, (
        event.type, event.chat, event.sender, json.dumps(event.receivers, ensure_ascii=False),
        event.amount_text, event.amount_type, event.crypto_symbol, event.amount_rub, created
    ))
    return {"ok": True}


@app.get("/api/stats")
async def get_stats():
    total_rains = query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'")["c"]
    total_tips = query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'")["c"]
    total_rain_rub = query_one("SELECT COALESCE(SUM(amount_rub), 0) AS s FROM events WHERE type='rain'")["s"]
    total_tip_rub = query_one("SELECT COALESCE(SUM(amount_rub), 0) AS s FROM events WHERE type='tip'")["s"]

    top_amount = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub), 0) AS total,
               COALESCE(AVG(amount_rub), 0) AS avg
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY total DESC LIMIT 100
    """)
    top_wins = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub), 0) AS total,
               COALESCE(AVG(amount_rub), 0) AS avg
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY wins DESC LIMIT 100
    """)
    top_tips = query_all("""
        SELECT sender AS nickname, COUNT(*) AS tips_sent,
               COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='tip'
        GROUP BY sender ORDER BY total DESC LIMIT 100
    """)

    top_tip_receivers = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in query_all("SELECT receivers, amount_rub FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            for r in rcs:
                top_tip_receivers[r]["count"] += 1
                top_tip_receivers[r]["total"] += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except:
            pass
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
               COALESCE(SUM(amount_rub), 0) AS total_rub
        FROM events GROUP BY chat ORDER BY total_rub DESC
    """)

    winners = set()
    for row in query_all("SELECT receivers FROM events WHERE type='rain'"):
        try:
            for r in json.loads(row["receivers"] or "[]"):
                winners.add(r)
        except:
            pass

    return {
        "totals": {
            "rains": total_rains, "tips": total_tips,
            "rain_rub": round(total_rain_rub or 0, 2),
            "tip_rub": round(total_tip_rub or 0, 2),
            "unique_winners": len(winners),
        },
        "top_amount": top_amount,
        "top_wins": top_wins,
        "top_tips": top_tips,
        "top_tip_receivers": top_tip_recv,
        "channels": channels,
        "recent": recent,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/clear")
async def clear_database(body: dict, x_api_key: str = Header(default="")):
    """
    Полная очистка таблицы events.
    Требует X-API-Key в заголовке, совпадающий с API_KEY.
    Body: {"confirm": "DELETE_ALL"}  — защита от случайного вызова
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if body.get("confirm") != "DELETE_ALL":
        raise HTTPException(status_code=400, detail="Подтверждение обязательно (confirm=DELETE_ALL)")

    if USE_POSTGRES:
        execute("TRUNCATE TABLE events RESTART IDENTITY CASCADE;")
    else:
        # SQLite: TRUNCATE нет, используем DELETE + сброс AUTOINCREMENT
        execute("DELETE FROM events;")
        try:
            execute("DELETE FROM sqlite_sequence WHERE name='events';")
        except:
            pass

    print("[ADMIN] 🗑 База очищена")
    return {"ok": True, "message": "Все данные удалены"}


@app.get("/api/user/{nickname}")
async def get_user(nickname: str):
    sent = query_one("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='rain' AND sender=?
    """, (nickname,))

    won_cnt, won_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='rain'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                won_cnt += 1
                won_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except:
            pass

    tips_sent = query_one("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='tip' AND sender=?
    """, (nickname,))

    tips_recv_cnt, tips_recv_total = 0, 0.0
    for row in query_all("SELECT amount_rub, receivers FROM events WHERE type='tip'"):
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                tips_recv_cnt += 1
                tips_recv_total += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except:
            pass

    return {
        "nickname": nickname,
        "given_rains": {"count": sent["cnt"], "total_rub": round(sent["total"], 2)},
        "received_rains": {"count": won_cnt, "total_rub": round(won_total, 2)},
        "tips_sent": {"count": tips_sent["cnt"], "total_rub": round(tips_sent["total"], 2)},
        "tips_received": {"count": tips_recv_cnt, "total_rub": round(tips_recv_total, 2)},
    }


# ==================== HTML (оставляем как было) ====================
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
    <a href="/rating/amount" class="{active_amount}">По сумме</a>
    <a href="/rating/wins" class="{active_wins}">По победам</a>
    <a href="/rating/tips" class="{active_tips}">По чаевым</a>
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


@app.get("/", response_class=HTMLResponse)
async def page_home():
    t = query_one("SELECT COUNT(*) AS c FROM events WHERE type='rain'")["c"]
    tips = query_one("SELECT COUNT(*) AS c FROM events WHERE type='tip'")["c"]
    tr = query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='rain'")["s"]
    ttr = query_one("SELECT COALESCE(SUM(amount_rub),0) AS s FROM events WHERE type='tip'")["s"]

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
                             active_amount="", active_wins="", active_tips="", content=content)


def render_table(rows, kind):
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
    <tbody>{render_table(rows, 'amount')}</tbody></table></div>"""
    return BASE_HTML.format(title="Shuffle — По сумме", active_home="",
                             active_amount="active", active_wins="", active_tips="", content=content)


@app.get("/rating/wins", response_class=HTMLResponse)
async def page_wins():
    rows = query_all("""
        SELECT sender AS nickname, COUNT(*) AS wins, COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain' GROUP BY sender ORDER BY wins DESC LIMIT 100
    """)
    content = f"""<div class="card"><h3>Топ по количеству дождей</h3>
    <table><thead><tr><th>#</th><th>Ник</th><th>Побед</th><th>Сумма</th></tr></thead>
    <tbody>{render_table(rows, 'wins')}</tbody></table></div>"""
    return BASE_HTML.format(title="Shuffle — По победам", active_home="",
                             active_amount="", active_wins="active", active_tips="", content=content)


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
        except:
            pass
    recv_sorted = sorted(
        [{"nickname": k, "tips_sent": v["count"], "total": v["total"]} for k, v in recv_data.items()],
        key=lambda x: x["total"], reverse=True
    )[:100]

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card"><h3>Кто больше отправил</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Отправлено</th><th>Сумма</th></tr></thead>
        <tbody>{render_table(sent, 'tips')}</tbody></table></div>
      <div class="card"><h3>Кто больше получил</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead>
        <tbody>{render_table(recv_sorted, 'tips')}</tbody></table></div>
    </div>"""
    return BASE_HTML.format(title="Shuffle — По чаевым", active_home="",
                             active_amount="", active_wins="", active_tips="active", content=content)


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
                             active_amount="", active_wins="", active_tips="", content=content)


ADMIN_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — Shuffle Rain</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #0e1116; color: #e6e9ef;
         font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         display: flex; justify-content: center; align-items: center;
         min-height: 100vh; padding: 20px; }
  .box { background: #161b22; border: 1px solid #26303a; border-radius: 12px;
         padding: 32px 40px; max-width: 520px; width: 100%; }
  h1 { margin: 0 0 8px; font-size: 22px; }
  .sub { color: #8b949e; margin-bottom: 24px; font-size: 14px; }
  label { display: block; color: #8b949e; font-size: 13px;
          margin-bottom: 6px; }
  input[type=password], input[type=text] {
    width: 100%; background: #0d1117; border: 1px solid #30363d;
    color: #e6e9ef; padding: 10px 12px; border-radius: 8px;
    font-size: 14px; margin-bottom: 16px; font-family: monospace;
  }
  input:focus { outline: none; border-color: #7cc4ff; }
  button { width: 100%; padding: 12px; border: none; border-radius: 8px;
           font-size: 15px; font-weight: 600; cursor: pointer; }
  .btn-clear { background: #dc2626; color: #fff; }
  .btn-clear:hover { background: #b91c1c; }
  .btn-clear:disabled { background: #4b5563; cursor: not-allowed; }
  .result { margin-top: 16px; padding: 12px; border-radius: 8px;
            font-size: 13px; display: none; white-space: pre-wrap; }
  .result.ok { background: #052e16; color: #4ade80; border: 1px solid #14532d; display: block; }
  .result.err { background: #2d0f0f; color: #f87171; border: 1px solid #7f1d1d; display: block; }
  .warn { background: #2d2410; border: 1px solid #78350f; color: #fbbf24;
          padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 13px; }
  a { color: #7cc4ff; text-decoration: none; font-size: 13px; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="box">
  <h1>⚙️ Админ-панель</h1>
  <div class="sub">Shuffle Rain Stats — управление БД</div>

  <div class="warn">
    ⚠️ Кнопка ниже <b>удалит ВСЕ события</b> (rain + tips) из базы.
    Действие необратимо.
  </div>

  <label for="apiKey">API-ключ</label>
  <input id="apiKey" type="password" placeholder="X-API-Key" />

  <label for="confirm">Введите <b>DELETE_ALL</b> для подтверждения</label>
  <input id="confirm" type="text" placeholder="DELETE_ALL" autocomplete="off" />

  <button id="clearBtn" class="btn-clear" onclick="clearDb()">🗑 Очистить базу данных</button>

  <div id="result" class="result"></div>

  <div style="margin-top:20px; text-align:center">
    <a href="/">← На главную</a>
  </div>
</div>

<script>
async function clearDb() {
  const apiKey = document.getElementById('apiKey').value.trim();
  const confirm = document.getElementById('confirm').value.trim();
  const result = document.getElementById('result');
  const btn = document.getElementById('clearBtn');

  result.className = 'result';
  result.textContent = '';

  if (!apiKey) {
    result.className = 'result err';
    result.textContent = 'Введите API-ключ';
    return;
  }
  if (confirm !== 'DELETE_ALL') {
    result.className = 'result err';
    result.textContent = 'Для подтверждения введите DELETE_ALL (заглавными буквами)';
    return;
  }

  if (!window.confirm('Точно удалить ВСЕ данные? Это необратимо!')) return;

  btn.disabled = true;
  btn.textContent = '⏳ Очистка...';

  try {
    const r = await fetch('/api/clear', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': apiKey
      },
      body: JSON.stringify({ confirm: 'DELETE_ALL' })
    });
    const data = await r.json();
    if (r.ok) {
      result.className = 'result ok';
      result.textContent = '✅ ' + (data.message || 'База очищена');
    } else {
      result.className = 'result err';
      result.textContent = '❌ ' + (data.detail || 'Ошибка');
    }
  } catch (e) {
    result.className = 'result err';
    result.textContent = '❌ Ошибка сети: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '🗑 Очистить базу данных';
  }
}
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
