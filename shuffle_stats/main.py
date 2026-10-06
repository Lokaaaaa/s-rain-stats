import os
import json
import sqlite3
import requests
from datetime import datetime, timezone
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional

# ==================== КОНФИГУРАЦИЯ ====================
DB_PATH = os.environ.get("DB_PATH", "shuffle.db")
API_KEY = os.environ.get("API_KEY", "CHANGE_ME_SECRET_KEY")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Shuffle Rain & Tips Stats")
templates = Jinja2Templates(directory=BASE_DIR)

# ==================== БАЗА ====================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
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
    );
    CREATE INDEX IF NOT EXISTS idx_type ON events(type);
    CREATE INDEX IF NOT EXISTS idx_created ON events(created_at);
    CREATE INDEX IF NOT EXISTS idx_sender ON events(sender);
    """)
    conn.commit()
    conn.close()


init_db()


# ==================== МОДЕЛИ ====================
class EventIn(BaseModel):
    type: str                      # 'rain' | 'tip'
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
    conn = get_db()
    conn.execute("""
        INSERT INTO events (type, chat, sender, receivers, amount_text, amount_type,
                            crypto_symbol, amount_rub, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event.type, event.chat, event.sender, json.dumps(event.receivers, ensure_ascii=False),
        event.amount_text, event.amount_type, event.crypto_symbol, event.amount_rub, created
    ))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    cur = conn.cursor()

    total_rains = cur.execute("SELECT COUNT(*) FROM events WHERE type='rain'").fetchone()[0]
    total_tips = cur.execute("SELECT COUNT(*) FROM events WHERE type='tip'").fetchone()[0]
    total_rain_rub = cur.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM events WHERE type='rain'").fetchone()[0]
    total_tip_rub = cur.execute("SELECT COALESCE(SUM(amount_rub), 0) FROM events WHERE type='tip'").fetchone()[0]

    top_amount = cur.execute("""
        SELECT sender AS nickname,
               COUNT(*) AS wins,
               COALESCE(SUM(amount_rub), 0) AS total,
               COALESCE(AVG(amount_rub), 0) AS avg
        FROM events WHERE type='rain'
        GROUP BY sender
        ORDER BY total DESC LIMIT 100
    """).fetchall()

    top_wins = cur.execute("""
        SELECT sender AS nickname,
               COUNT(*) AS wins,
               COALESCE(SUM(amount_rub), 0) AS total,
               COALESCE(AVG(amount_rub), 0) AS avg
        FROM events WHERE type='rain'
        GROUP BY sender
        ORDER BY wins DESC LIMIT 100
    """).fetchall()

    top_tips = cur.execute("""
        SELECT sender AS nickname,
               COUNT(*) AS tips_sent,
               COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='tip'
        GROUP BY sender
        ORDER BY total DESC LIMIT 100
    """).fetchall()

    # Топ получателей чаевых
    top_tip_receivers = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in cur.execute("SELECT receivers, amount_rub FROM events WHERE type='tip'").fetchall():
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

    # Recent 30
    recent = cur.execute("""
        SELECT type, chat, sender, receivers, amount_text, crypto_symbol, amount_rub, created_at
        FROM events
        ORDER BY id DESC LIMIT 30
    """).fetchall()

    # Статистика по каналам
    channels = cur.execute("""
        SELECT chat,
               SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
               SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips,
               COALESCE(SUM(amount_rub), 0) AS total_rub
        FROM events
        GROUP BY chat
        ORDER BY total_rub DESC
    """).fetchall()

    winners = set()
    for row in cur.execute("SELECT receivers FROM events WHERE type='rain'").fetchall():
        try:
            for r in json.loads(row["receivers"] or "[]"):
                winners.add(r)
        except:
            pass

    conn.close()

    return {
        "totals": {
            "rains": total_rains,
            "tips": total_tips,
            "rain_rub": round(total_rain_rub, 2),
            "tip_rub": round(total_tip_rub, 2),
            "unique_winners": len(winners),
        },
        "top_amount": [dict(r) for r in top_amount],
        "top_wins": [dict(r) for r in top_wins],
        "top_tips": [dict(r) for r in top_tips],
        "top_tip_receivers": top_tip_recv,
        "channels": [dict(r) for r in channels],
        "recent": [dict(r) for r in recent],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/user/{nickname}")
async def get_user(nickname: str):
    conn = get_db()
    cur = conn.cursor()

    sent = cur.execute("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='rain' AND sender=?
    """, (nickname,)).fetchone()

    won_cnt = 0
    won_total = 0.0
    for row in cur.execute("SELECT amount_rub, receivers FROM events WHERE type='rain'").fetchall():
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                won_cnt += 1
                per = (row["amount_rub"] or 0) / max(len(rcs), 1)
                won_total += per
        except:
            pass

    tips_sent = cur.execute("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub), 0) AS total
        FROM events WHERE type='tip' AND sender=?
    """, (nickname,)).fetchone()

    tips_recv_cnt = 0
    tips_recv_total = 0.0
    for row in cur.execute("SELECT amount_rub, receivers FROM events WHERE type='tip'").fetchall():
        try:
            rcs = json.loads(row["receivers"] or "[]")
            if nickname in rcs:
                tips_recv_cnt += 1
                per = (row["amount_rub"] or 0) / max(len(rcs), 1)
                tips_recv_total += per
        except:
            pass

    conn.close()

    return {
        "nickname": nickname,
        "given_rains": {"count": sent["cnt"], "total_rub": round(sent["total"], 2)},
        "received_rains": {"count": won_cnt, "total_rub": round(won_total, 2)},
        "tips_sent": {"count": tips_sent["cnt"], "total_rub": round(tips_sent["total"], 2)},
        "tips_received": {"count": tips_recv_cnt, "total_rub": round(tips_recv_total, 2)},
    }


# ==================== HTML ====================
BASE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #0e1116; color: #e6e9ef;
         font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
  a {{ color: #7cc4ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  header {{ background: #161b22; border-bottom: 1px solid #26303a; padding: 14px 24px;
           display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }}
  .brand {{ font-weight: 700; font-size: 18px; color: #fff; }}
  .brand small {{ color: #8b949e; font-weight: 400; margin-left: 8px; }}
  nav a {{ margin-right: 16px; color: #c9d1d9; }}
  nav a.active {{ color: #7cc4ff; font-weight: 600; }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 16px; }}
  .card {{ background: #161b22; border: 1px solid #26303a; border-radius: 12px;
          padding: 16px 20px; }}
  .card h3 {{ margin: 0 0 8px; color: #8b949e; font-weight: 500; font-size: 13px;
             text-transform: uppercase; letter-spacing: .5px; }}
  .card .big {{ font-size: 26px; font-weight: 700; color: #fff; }}
  .card .sub {{ color: #6e7681; font-size: 12px; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #26303a; }}
  th {{ color: #8b949e; font-weight: 500; font-size: 13px; }}
  td.amount {{ color: #4ade80; font-weight: 600; }}
  tr:hover td {{ background: #1c222b; }}
  .rank {{ display: inline-block; width: 28px; text-align: center; color: #8b949e; }}
  .rank.g {{ color: #f5c542; }} .rank.s {{ color: #c0c0c0; }} .rank.b {{ color: #cd7f32; }}
  .tag {{ display: inline-block; background: #21262d; padding: 2px 8px; border-radius: 6px;
         font-size: 11px; color: #8b949e; margin-left: 6px; }}
  .tag.rain {{ color: #7cc4ff; }}
  .tag.tip {{ color: #f5c542; }}
  .live-dot {{ display: inline-block; width: 8px; height: 8px; background: #4ade80;
              border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
  .recent-row {{ padding: 8px 0; border-bottom: 1px solid #26303a; font-size: 13px; }}
  .recent-row:last-child {{ border-bottom: none; }}
  .muted {{ color: #8b949e; }}
  .chat-pill {{ display:inline-block; background:#21262d; padding:2px 8px; border-radius:6px;
                font-size:11px; color:#7cc4ff; }}
</style>
</head>
<body>
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
<main>
{content}
</main>
<script>
  setInterval(async () => {{
    try {{
      const r = await fetch('/api/stats');
      const data = await r.json();
      document.dispatchEvent(new CustomEvent('stats-updated', {{ detail: data }}));
    }} catch(e) {{}}
  }}, 10000);
</script>
</body>
</html>
"""


def fmt_rub(x):
    try:
        return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"
    except:
        return "0 ₽"


@app.get("/", response_class=HTMLResponse)
async def page_home():
    conn = get_db()
    cur = conn.cursor()
    t = cur.execute("SELECT COUNT(*) FROM events WHERE type='rain'").fetchone()[0]
    tips = cur.execute("SELECT COUNT(*) FROM events WHERE type='tip'").fetchone()[0]
    tr = cur.execute("SELECT COALESCE(SUM(amount_rub),0) FROM events WHERE type='rain'").fetchone()[0]
    ttr = cur.execute("SELECT COALESCE(SUM(amount_rub),0) FROM events WHERE type='tip'").fetchone()[0]
    conn.close()

    content = f"""
    <div class="grid">
      <div class="card"><h3>Дождей</h3><div class="big">{t}</div><div class="sub">событий</div></div>
      <div class="card"><h3>Раздали всего</h3><div class="big">{fmt_rub(tr)}</div></div>
      <div class="card"><h3>Чаевых</h3><div class="big">{tips}</div><div class="sub">событий</div></div>
      <div class="card"><h3>Чаевых сумма</h3><div class="big">{fmt_rub(ttr)}</div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h3>Последние события</h3>
      <div id="recent"></div>
    </div>
    <script>
      document.addEventListener('stats-updated', (e) => renderRecent(e.detail.recent));
      async function loadOnce() {{
        const r = await fetch('/api/stats');
        const d = await r.json();
        renderRecent(d.recent);
      }}
      function renderRecent(rows) {{
        const el = document.getElementById('recent');
        if (!rows) return;
        el.innerHTML = rows.map(r => {{
          const cls = r.type === 'rain' ? 'rain' : 'tip';
          const tag = r.type === 'rain' ? 'RAIN' : 'TIP';
          const amt = r.amount_text ? r.amount_text : '';
          const rub = r.amount_rub ? ' (' + Number(r.amount_rub).toFixed(0) + ' ₽)' : '';
          let recs = '';
          try {{ recs = JSON.parse(r.receivers || '[]').slice(0,5).join(', '); }} catch(e) {{}}
          return `<div class="recent-row">
            <span class="tag ${{cls}}">${{tag}}</span>
            <span class="chat-pill">${{r.chat}}</span>
            <b style="margin-left:6px">${{r.sender}}</b>
            <span class="muted"> → ${{recs}}</span>
            <span style="float:right" class="amount">${{amt}}${{rub}}</span>
          </div>`;
        }}).join('');
      }}
      loadOnce();
    </script>
    """
    return BASE_HTML.format(title="Shuffle Rain Stats", active_home="active",
                             active_amount="", active_wins="", active_tips="",
                             content=content)


def render_table(rows, kind):
    if not rows:
        return "<tr><td colspan='4' class='muted'>Пока нет данных</td></tr>"
    html = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        if kind == "tips":
            html += f"""<tr>
                <td><span class="rank {rank_cls}">{i}</span></td>
                <td><b>{r['nickname']}</b></td>
                <td>{r['tips_sent']}</td>
                <td class="amount">{fmt_rub(r['total'])}</td>
            </tr>"""
        else:
            html += f"""<tr>
                <td><span class="rank {rank_cls}">{i}</span></td>
                <td><b>{r['nickname']}</b></td>
                <td>{r['wins']}</td>
                <td class="amount">{fmt_rub(r['total'])}</td>
            </tr>"""
    return html


@app.get("/rating/amount", response_class=HTMLResponse)
async def page_amount():
    conn = get_db()
    rows = conn.execute("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY total DESC LIMIT 100
    """).fetchall()
    conn.close()
    content = f"""
    <div class="card">
      <h3>Топ по сумме дождей</h3>
      <table><thead><tr><th>#</th><th>Ник</th><th>Побед</th><th>Сумма</th></tr></thead>
      <tbody>{render_table(rows, 'amount')}</tbody></table>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Рейтинг по сумме", active_home="",
                             active_amount="active", active_wins="", active_tips="",
                             content=content)


@app.get("/rating/wins", response_class=HTMLResponse)
async def page_wins():
    conn = get_db()
    rows = conn.execute("""
        SELECT sender AS nickname, COUNT(*) AS wins,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='rain'
        GROUP BY sender ORDER BY wins DESC LIMIT 100
    """).fetchall()
    conn.close()
    content = f"""
    <div class="card">
      <h3>Топ по количеству дождей</h3>
      <table><thead><tr><th>#</th><th>Ник</th><th>Побед</th><th>Сумма</th></tr></thead>
      <tbody>{render_table(rows, 'wins')}</tbody></table>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Рейтинг по победам", active_home="",
                             active_amount="", active_wins="active", active_tips="",
                             content=content)


@app.get("/rating/tips", response_class=HTMLResponse)
async def page_tips():
    conn = get_db()
    sent = conn.execute("""
        SELECT sender AS nickname, COUNT(*) AS tips_sent,
               COALESCE(SUM(amount_rub),0) AS total
        FROM events WHERE type='tip'
        GROUP BY sender ORDER BY total DESC LIMIT 100
    """).fetchall()

    recv_data = defaultdict(lambda: {"count": 0, "total": 0.0})
    for row in conn.execute("SELECT receivers, amount_rub FROM events WHERE type='tip'").fetchall():
        try:
            rcs = json.loads(row["receivers"] or "[]")
            for r in rcs:
                recv_data[r]["count"] += 1
                recv_data[r]["total"] += (row["amount_rub"] or 0) / max(len(rcs), 1)
        except:
            pass
    recv_sorted = sorted(
        [{"nickname": k, "tips_sent": v["count"], "total": v["total"]}
         for k, v in recv_data.items()],
        key=lambda x: x["total"], reverse=True
    )[:100]
    conn.close()

    sent_html = render_table(sent, 'tips')
    recv_html = render_table(recv_sorted, 'tips')

    content = f"""
    <div class="grid" style="grid-template-columns: 1fr 1fr">
      <div class="card">
        <h3>Кто больше отправил чаевых</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Отправлено</th><th>Сумма</th></tr></thead>
        <tbody>{sent_html}</tbody></table>
      </div>
      <div class="card">
        <h3>Кто больше получил чаевых</h3>
        <table><thead><tr><th>#</th><th>Ник</th><th>Получено</th><th>Сумма</th></tr></thead>
        <tbody>{recv_html}</tbody></table>
      </div>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Рейтинг по чаевым", active_home="",
                             active_amount="", active_wins="", active_tips="active",
                             content=content)


@app.get("/channels", response_class=HTMLResponse)
async def page_channels():
    conn = get_db()
    rows = conn.execute("""
        SELECT chat,
               SUM(CASE WHEN type='rain' THEN 1 ELSE 0 END) AS rains,
               SUM(CASE WHEN type='tip' THEN 1 ELSE 0 END) AS tips,
               COALESCE(SUM(amount_rub), 0) AS total_rub
        FROM events
        GROUP BY chat
        ORDER BY total_rub DESC
    """).fetchall()
    conn.close()

    body = ""
    for i, r in enumerate(rows, 1):
        rank_cls = "g" if i == 1 else "s" if i == 2 else "b" if i == 3 else ""
        body += f"""<tr>
            <td><span class="rank {rank_cls}">{i}</span></td>
            <td><span class="chat-pill">{r['chat']}</span></td>
            <td>{r['rains']}</td>
            <td>{r['tips']}</td>
            <td class="amount">{fmt_rub(r['total_rub'])}</td>
        </tr>"""

    content = f"""
    <div class="card">
      <h3>Активность по каналам</h3>
      <table><thead><tr><th>#</th><th>Канал</th><th>Дождей</th><th>Чаевых</th><th>Сумма</th></tr></thead>
      <tbody>{body or '<tr><td colspan=5 class=muted>Пусто</td></tr>'}</tbody></table>
    </div>"""
    return BASE_HTML.format(title="Shuffle — Каналы", active_home="",
                             active_amount="", active_wins="", active_tips="",
                             content=content)


@app.get("/user/{nickname}", response_class=HTMLResponse)
async def page_user(nickname: str):
    data = await get_user(nickname)
    g = data["given_rains"]
    rec = data["received_rains"]
    tips_in = data["tips_sent"]
    tips_out = data["tips_received"]
    content = f"""
    <div class="card">
      <h3>Пользователь</h3>
      <div class="big">{nickname}</div>
    </div>
    <div class="grid" style="margin-top:16px">
      <div class="card"><h3>Раздал дождей</h3><div class="big">{g['count']}</div>
        <div class="sub">{fmt_rub(g['total_rub'])}</div></div>
      <div class="card"><h3>Выиграл дождей</h3><div class="big">{rec['count']}</div>
        <div class="sub">{fmt_rub(rec['total_rub'])}</div></div>
      <div class="card"><h3>Отправил чаевых</h3><div class="big">{tips_in['count']}</div>
        <div class="sub">{fmt_rub(tips_in['total_rub'])}</div></div>
      <div class="card"><h3>Получил чаевых</h3><div class="big">{tips_out['count']}</div>
        <div class="sub">{fmt_rub(tips_out['total_rub'])}</div></div>
    </div>"""
    return BASE_HTML.format(title=f"Shuffle — Профиль {nickname}", active_home="",
                             active_amount="", active_wins="", active_tips="",
                             content=content)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))