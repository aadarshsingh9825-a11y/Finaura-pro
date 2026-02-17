#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║         FINAURA PRO                      ║
║   SMART MONEY. SMARTER FUTURE.          ║
╚══════════════════════════════════════════╝

Paper trading platform with user accounts.
- Live prices from Yahoo Finance (real data)
- User accounts & portfolio saved permanently
- Prices refresh every 60 seconds

HOW TO RUN:
  Windows:  double-click START.bat
  Mac/Linux: python3 server.py  OR  bash START.sh
"""

import sys, subprocess, os

# Always run from the folder this script lives in
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Auto-install dependencies ──────────────────────────────────────────────────
def install(pkg):
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"  ⚠️  Could not install {pkg}: {e}")

for pkg in ["flask", "requests", "bs4"]:
    try:
        __import__(pkg)
    except ImportError:
        print(f"  📦 Installing {pkg}...")
        install("beautifulsoup4" if pkg == "bs4" else pkg)

# ── Core imports ───────────────────────────────────────────────────────────────
from flask import Flask, jsonify, request, session, send_from_directory, Response
import sqlite3, hashlib, threading, webbrowser, socket, time
from datetime import datetime
from functools import wraps

# requests is optional — app works without it (falls back to no live prices)
try:
    import requests as req
    HAS_REQUESTS = True
except ImportError:
    req = None
    HAS_REQUESTS = False
    print("  ⚠️  'requests' not available — live prices disabled, using simulated prices")

# ── App Setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "finaura-pro-secret-2026-xK9mNp")

# Database path — use /data volume on Railway if available, else local
_db_dir = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(_db_dir, "finaura_users.db")

# Railway provides PORT as env variable
PORT = int(os.environ.get("PORT", 5000))
INITIAL_BALANCE = 25000.0

SYMBOLS = [
    # Commercial Banks
    "NABIL","NICA","EBL","HBL","SANIMA","SBI","PRVU","KBL","NBL","SCB",
    # Development Banks
    "MNBBL","GBBL","LBBL","NIFRA",
    # Life Insurance
    "NLIC","LICN",
    # Non-Life Insurance
    "NICL","PRIN",
    # Hydropower
    "CHCL","BPCL","SHPC","API",
    # Microfinance
    "SKBBL","SWBBL",
    # Telecom / Others
    "NTC","CIT",
]

# ── Live Price Cache ───────────────────────────────────────────────────────────
_prices = {}
_prices_lock = threading.Lock()

NEPSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://merolagani.com/",
}

def fetch_merolagani():
    """
    Scrape live market data from merolagani.com/LatestMarket.aspx
    Returns dict of symbol -> price data
    """
    if not HAS_REQUESTS:
        return {}
    try:
        from bs4 import BeautifulSoup
        url = "https://merolagani.com/LatestMarket.aspx"
        r = req.get(url, headers=NEPSE_HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"id": "ctl00_ContentPlaceHolder1_LiveTrading1_gridView"})
        if not table:
            # Try any table with stock data
            table = soup.find("table", class_="table")
        results = {}
        if table:
            rows = table.find_all("tr")[1:]  # skip header
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 6:
                    continue
                try:
                    sym   = cols[1].get_text(strip=True)
                    ltp   = float(cols[2].get_text(strip=True).replace(",", "") or 0)
                    chg   = float(cols[5].get_text(strip=True).replace(",", "") or 0)
                    prev  = ltp - chg
                    chgp  = round((chg / prev * 100) if prev else 0, 2)
                    vol   = int(cols[6].get_text(strip=True).replace(",", "") or 0) if len(cols) > 6 else 0
                    high  = float(cols[3].get_text(strip=True).replace(",", "") or ltp) if len(cols) > 3 else ltp
                    low   = float(cols[4].get_text(strip=True).replace(",", "") or ltp) if len(cols) > 4 else ltp
                    if ltp > 0:
                        results[sym] = {
                            "symbol": sym, "price": round(ltp, 2),
                            "change": round(chg, 2), "changePercent": chgp,
                            "volume": vol, "high": round(high, 2),
                            "low": round(low, 2), "open": round(prev, 2),
                            "prevClose": round(prev, 2),
                        }
                except (ValueError, IndexError):
                    continue
        return results
    except Exception as e:
        print(f"  ⚠️  merolagani scrape error: {e}")
        return {}

def fetch_nepse_api():
    """
    Fetch today's prices from NEPSE official API.
    Endpoint: https://newweb.nepalstock.com.np/api/nots/nepse-data/today-price
    """
    if not HAS_REQUESTS:
        return {}
    try:
        url = "https://newweb.nepalstock.com.np/api/nots/nepse-data/today-price?&size=500&businessDate="
        r = req.get(url, headers=NEPSE_HEADERS, timeout=15, verify=False)
        data = r.json()
        results = {}
        items = data.get("content") or data.get("data") or []
        for item in items:
            sym  = item.get("symbol") or item.get("stockSymbol") or ""
            ltp  = float(item.get("closingPrice") or item.get("lastTradedPrice") or item.get("ltp") or 0)
            prev = float(item.get("previousClose") or item.get("prevClose") or ltp)
            chg  = round(ltp - prev, 2)
            chgp = round((chg / prev * 100) if prev else 0, 2)
            vol  = int(item.get("totalTradeQuantity") or item.get("volume") or 0)
            high = float(item.get("highPrice") or ltp)
            low  = float(item.get("lowPrice") or ltp)
            if sym and ltp > 0:
                results[sym] = {
                    "symbol": sym, "price": round(ltp, 2),
                    "change": round(chg, 2), "changePercent": chgp,
                    "volume": vol, "high": round(high, 2),
                    "low": round(low, 2), "open": round(prev, 2),
                    "prevClose": round(prev, 2),
                }
        return results
    except Exception as e:
        print(f"  ⚠️  NEPSE API error: {e}")
        return {}

def refresh_all_prices():
    """Fetch live NEPSE prices — tries NEPSE API first, falls back to merolagani scrape."""
    print(f"  🔄 Refreshing NEPSE prices... ({datetime.now().strftime('%H:%M:%S')})")

    # Try NEPSE official API first
    results = fetch_nepse_api()

    # Fall back to merolagani scrape if NEPSE API returned nothing
    if not results:
        print("  📡 NEPSE API empty, trying merolagani.com...")
        results = fetch_merolagani()

    if results:
        with _prices_lock:
            _prices.update(results)
        print(f"  ✅ Got live NEPSE prices for {len(results)} symbols")
    else:
        print("  ⚠️  Could not fetch live prices — using last cached values")

def price_loop():
    """Background thread — refresh every 5 minutes (NEPSE updates slowly)."""
    while True:
        try:
            refresh_all_prices()
        except Exception as e:
            print(f"  ⚠️ Price refresh error: {e}")
        time.sleep(300)  # 5 minutes

def get_price(symbol):
    with _prices_lock:
        return _prices.get(symbol)

def get_all_prices():
    with _prices_lock:
        return list(_prices.values())


# ── Database ───────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    """Create all tables if they don't exist. All data persists across restarts."""
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_balance (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 25000
        );

        CREATE TABLE IF NOT EXISTS portfolio (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            symbol    TEXT NOT NULL,
            shares    REAL NOT NULL,
            avg_price REAL NOT NULL,
            UNIQUE(user_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            symbol    TEXT NOT NULL,
            type      TEXT NOT NULL,
            shares    REAL NOT NULL,
            price     REAL NOT NULL,
            total     REAL NOT NULL,
            pnl       REAL DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watchlist (
            user_id INTEGER NOT NULL,
            symbol  TEXT NOT NULL,
            UNIQUE(user_id, symbol)
        );

        CREATE TABLE IF NOT EXISTS limit_orders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            symbol       TEXT NOT NULL,
            order_type   TEXT NOT NULL,
            target_price REAL NOT NULL,
            shares       REAL NOT NULL,
            status       TEXT DEFAULT 'active',
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    c.commit()
    c.close()
    print("✅ Database ready:", DB)

def get_balance(uid):
    c = db()
    row = c.execute("SELECT balance FROM user_balance WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return row["balance"] if row else INITIAL_BALANCE

def ensure_balance_row(uid):
    """Make sure a balance row exists for this user (handles new registrations after reset)."""
    c = db()
    existing = c.execute("SELECT 1 FROM user_balance WHERE user_id=?", (uid,)).fetchone()
    if not existing:
        c.execute("INSERT INTO user_balance(user_id, balance) VALUES(?, ?)", (uid, INITIAL_BALANCE))
        c.commit()
    c.close()

# ── Auth helpers ───────────────────────────────────────────────────────────────
def h(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if "uid" not in session:
            return jsonify(error="login_required"), 401
        return f(*a, **kw)
    return wrap

# ── Auth Routes ────────────────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def register():
    d  = request.json or {}
    un = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()
    if not un or not pw:
        return jsonify(error="Username and password required"), 400
    if len(un) < 3:
        return jsonify(error="Username must be at least 3 characters"), 400
    if len(pw) < 4:
        return jsonify(error="Password must be at least 4 characters"), 400
    try:
        c = db()
        c.execute("INSERT INTO users(username, password) VALUES(?, ?)", (un, h(pw)))
        c.commit()
        uid = c.execute("SELECT id FROM users WHERE username=?", (un,)).fetchone()["id"]
        c.close()
        ensure_balance_row(uid)
        session["uid"] = uid
        session["username"] = un
        print(f"  ✅ New user registered: {un}")
        return jsonify(ok=True, username=un)
    except sqlite3.IntegrityError:
        return jsonify(error="Username already taken"), 400

@app.route("/api/login", methods=["POST"])
def login():
    d  = request.json or {}
    un = (d.get("username") or "").strip()
    pw = (d.get("password") or "").strip()
    c  = db()
    row = c.execute("SELECT id FROM users WHERE username=? AND password=?", (un, h(pw))).fetchone()
    c.close()
    if not row:
        return jsonify(error="Wrong username or password"), 401
    uid = row["id"]
    ensure_balance_row(uid)
    session["uid"] = uid
    session["username"] = un
    print(f"  🔑 Login: {un}")
    return jsonify(ok=True, username=un)

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)

# ── User Data Route ────────────────────────────────────────────────────────────
@app.route("/api/me")
@login_required
def me():
    uid = session["uid"]
    c   = db()
    bal = get_balance(uid)
    pf  = [dict(r) for r in c.execute(
        "SELECT symbol, shares, avg_price FROM portfolio WHERE user_id=?", (uid,)
    ).fetchall()]
    tx  = [dict(r) for r in c.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY timestamp DESC LIMIT 500", (uid,)
    ).fetchall()]
    wl  = [r["symbol"] for r in c.execute(
        "SELECT symbol FROM watchlist WHERE user_id=?", (uid,)
    ).fetchall()]
    lo  = [dict(r) for r in c.execute(
        "SELECT * FROM limit_orders WHERE user_id=? AND status='active'", (uid,)
    ).fetchall()]
    c.close()
    return jsonify(
        username=session["username"],
        balance=bal,
        portfolio=pf,
        transactions=tx,
        watchlist=wl,
        limitOrders=lo
    )

# ── Trading Routes ─────────────────────────────────────────────────────────────
@app.route("/api/buy", methods=["POST"])
@login_required
def buy():
    uid  = session["uid"]
    d    = request.json or {}
    sym  = d.get("symbol", "").upper()
    shrs = float(d.get("shares", 0))
    if shrs <= 0 or not sym:
        return jsonify(error="Invalid parameters"), 400

    # Use live server-side price if available, fallback to client-sent price
    live = get_price(sym) or get_price(sym.replace('.', '-'))
    prc  = float(live["price"]) if live else float(d.get("price", 0))
    if prc <= 0:
        return jsonify(error="Price not available"), 400

    total = shrs * prc
    c     = db()
    bal   = get_balance(uid)

    if bal < total:
        c.close()
        return jsonify(error="Insufficient funds"), 400

    existing = c.execute(
        "SELECT shares, avg_price FROM portfolio WHERE user_id=? AND symbol=?", (uid, sym)
    ).fetchone()

    if existing:
        new_shares = existing["shares"] + shrs
        new_avg    = (existing["avg_price"] * existing["shares"] + total) / new_shares
        c.execute(
            "UPDATE portfolio SET shares=?, avg_price=? WHERE user_id=? AND symbol=?",
            (new_shares, new_avg, uid, sym)
        )
    else:
        c.execute(
            "INSERT INTO portfolio(user_id, symbol, shares, avg_price) VALUES(?,?,?,?)",
            (uid, sym, shrs, prc)
        )

    c.execute(
        "INSERT INTO transactions(user_id,symbol,type,shares,price,total) VALUES(?,?,?,?,?,?)",
        (uid, sym, "BUY", shrs, prc, total)
    )
    new_bal = bal - total
    c.execute("UPDATE user_balance SET balance=? WHERE user_id=?", (new_bal, uid))
    c.commit()
    c.close()
    return jsonify(ok=True, balance=new_bal, price=prc)

@app.route("/api/sell", methods=["POST"])
@login_required
def sell():
    uid  = session["uid"]
    d    = request.json or {}
    sym  = d.get("symbol", "").upper()
    shrs = float(d.get("shares", 0))
    if shrs <= 0 or not sym:
        return jsonify(error="Invalid parameters"), 400

    # Use live server-side price if available, fallback to client-sent price
    live = get_price(sym) or get_price(sym.replace('.', '-'))
    prc  = float(live["price"]) if live else float(d.get("price", 0))
    if prc <= 0:
        return jsonify(error="Price not available"), 400

    c   = db()
    pos = c.execute(
        "SELECT shares, avg_price FROM portfolio WHERE user_id=? AND symbol=?", (uid, sym)
    ).fetchone()

    if not pos or pos["shares"] < shrs:
        c.close()
        return jsonify(error="Not enough shares"), 400

    proceeds   = shrs * prc
    pnl        = (prc - pos["avg_price"]) * shrs
    new_shares = pos["shares"] - shrs

    if new_shares <= 0:
        c.execute("DELETE FROM portfolio WHERE user_id=? AND symbol=?", (uid, sym))
    else:
        c.execute(
            "UPDATE portfolio SET shares=? WHERE user_id=? AND symbol=?",
            (new_shares, uid, sym)
        )

    c.execute(
        "INSERT INTO transactions(user_id,symbol,type,shares,price,total,pnl) VALUES(?,?,?,?,?,?,?)",
        (uid, sym, "SELL", shrs, prc, proceeds, pnl)
    )
    bal     = get_balance(uid)
    new_bal = bal + proceeds
    c.execute("UPDATE user_balance SET balance=? WHERE user_id=?", (new_bal, uid))
    c.commit()
    c.close()
    return jsonify(ok=True, balance=new_bal, pnl=pnl, price=prc)

# ── Watchlist Routes ───────────────────────────────────────────────────────────
@app.route("/api/watchlist/add", methods=["POST"])
@login_required
def wl_add():
    uid = session["uid"]
    sym = (request.json or {}).get("symbol", "").upper()
    try:
        c = db()
        c.execute("INSERT INTO watchlist(user_id, symbol) VALUES(?,?)", (uid, sym))
        c.commit()
        c.close()
        return jsonify(ok=True)
    except sqlite3.IntegrityError:
        return jsonify(error="Already watching"), 400

@app.route("/api/watchlist/remove", methods=["POST"])
@login_required
def wl_remove():
    uid = session["uid"]
    sym = (request.json or {}).get("symbol", "").upper()
    c   = db()
    c.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (uid, sym))
    c.commit()
    c.close()
    return jsonify(ok=True)

# ── Limit Order Routes ─────────────────────────────────────────────────────────
@app.route("/api/limitorder/add", methods=["POST"])
@login_required
def lo_add():
    uid = session["uid"]
    d   = request.json or {}
    c   = db()
    c.execute(
        "INSERT INTO limit_orders(user_id,symbol,order_type,target_price,shares) VALUES(?,?,?,?,?)",
        (uid, d.get("symbol","").upper(), d.get("type","BUY"), float(d.get("targetPrice",0)), float(d.get("shares",0)))
    )
    c.commit()
    c.close()
    return jsonify(ok=True)

@app.route("/api/limitorder/cancel", methods=["POST"])
@login_required
def lo_cancel():
    uid      = session["uid"]
    order_id = (request.json or {}).get("id")
    c = db()
    c.execute(
        "UPDATE limit_orders SET status='cancelled' WHERE id=? AND user_id=?",
        (order_id, uid)
    )
    c.commit()
    c.close()
    return jsonify(ok=True)

# ── Live Prices Route ─────────────────────────────────────────────────────────
@app.route("/api/prices")
def get_prices():
    prices = get_all_prices()
    return jsonify(prices=prices, count=len(prices), ts=time.time())

# ── Static assets ──────────────────────────────────────────────────────────────
@app.route("/logo")
def logo():
    try:
        return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "finaura-logo.jpeg")
    except Exception:
        return "", 404

# ── Main page ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    try:
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradehub-complete.html")
        if not os.path.exists(html_path):
            return f"<h2>Missing file: tradehub-complete.html</h2><p>Make sure <b>tradehub-complete.html</b> is in the same folder as <b>server.py</b>.<br>Current folder: {os.path.dirname(os.path.abspath(__file__))}</p>", 500
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("</body>", AUTH_INJECT + "\n</body>")
        html = html.replace("initializeApp();", "checkAuthThenInit();")
        return Response(html, mimetype="text/html")
    except Exception as e:
        return f"<h2>Server error: {e}</h2>", 500

# ── Auth Injection (inserted into the HTML at runtime) ────────────────────────
AUTH_INJECT = r"""
<!-- ═══════════════════ AUTH OVERLAY ═══════════════════ -->
<style>
#authOverlay {
    display: flex;
    position: fixed;
    inset: 0;
    z-index: 9999;
    background: linear-gradient(135deg, #0f172a 0%, #1a3a2e 50%, #0f172a 100%);
    align-items: center;
    justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
#authOverlay.hidden { display: none; }
.auth-card {
    background: #1e293b;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px;
    padding: 48px 44px;
    width: 420px;
    box-shadow: 0 25px 80px rgba(0,0,0,0.6);
    text-align: center;
    animation: authSlideIn 0.4s ease;
}
@keyframes authSlideIn {
    from { opacity: 0; transform: translateY(30px); }
    to   { opacity: 1; transform: translateY(0); }
}
.auth-brand {
    font-size: 2rem;
    font-weight: 800;
    background: linear-gradient(135deg, #10b981, #3b82f6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 4px;
}
.auth-tagline {
    color: #64748b;
    font-size: 0.85rem;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 32px;
}
.auth-tabs {
    display: flex;
    background: rgba(255,255,255,0.05);
    border-radius: 10px;
    padding: 4px;
    margin-bottom: 28px;
    gap: 4px;
}
.auth-tab {
    flex: 1;
    padding: 9px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.9rem;
    color: #64748b;
    transition: all 0.2s;
}
.auth-tab.active {
    background: linear-gradient(135deg, rgba(16,185,129,0.2), rgba(59,130,246,0.2));
    color: #10b981;
}
.auth-input-wrap {
    position: relative;
    margin-bottom: 14px;
}
.auth-input-wrap span {
    position: absolute;
    left: 14px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 1rem;
}
.auth-input {
    width: 100%;
    padding: 13px 16px 13px 42px;
    background: rgba(255,255,255,0.05);
    border: 1.5px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    color: #e2e8f0;
    font-size: 0.95rem;
    outline: none;
    transition: border-color 0.2s;
}
.auth-input:focus { border-color: #10b981; }
.auth-input::placeholder { color: #475569; }
.auth-btn-main {
    width: 100%;
    padding: 14px;
    background: linear-gradient(135deg, #10b981, #3b82f6);
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 1rem;
    font-weight: 700;
    cursor: pointer;
    margin-top: 8px;
    transition: all 0.2s;
    letter-spacing: 0.5px;
}
.auth-btn-main:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 25px rgba(16,185,129,0.35);
}
.auth-btn-main:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
.auth-msg {
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 0.85rem;
    margin-bottom: 16px;
    display: none;
}
.auth-msg.error { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
.auth-msg.success { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
.auth-note {
    margin-top: 20px;
    color: #475569;
    font-size: 0.78rem;
    line-height: 1.5;
}
.auth-note strong { color: #94a3b8; }

/* User chip in nav */
#userChip {
    display: none;
    align-items: center;
    gap: 8px;
    background: rgba(16,185,129,0.1);
    border: 1px solid rgba(16,185,129,0.3);
    border-radius: 20px;
    padding: 6px 14px;
    color: #10b981;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
}
#userChip:hover { background: rgba(16,185,129,0.2); }
</style>

<div id="authOverlay">
  <div class="auth-card">
    <div class="auth-brand">🚀 Finaura Pro</div>
    <div class="auth-tagline">Smart Money. Smarter Future.</div>

    <div class="auth-tabs">
      <div class="auth-tab active" id="tabLogin" onclick="authSwitchTab('login')">Sign In</div>
      <div class="auth-tab" id="tabRegister" onclick="authSwitchTab('register')">Create Account</div>
    </div>

    <div class="auth-msg" id="authMsg"></div>

    <div class="auth-input-wrap">
      <span>👤</span>
      <input class="auth-input" type="text" id="authUser" placeholder="Username" autocomplete="username" onkeydown="if(event.key==='Enter') authSubmit()">
    </div>
    <div class="auth-input-wrap">
      <span>🔒</span>
      <input class="auth-input" type="password" id="authPass" placeholder="Password" autocomplete="current-password" onkeydown="if(event.key==='Enter') authSubmit()">
    </div>

    <button class="auth-btn-main" id="authBtn" onclick="authSubmit()">Sign In</button>

    <div class="auth-note">
      <strong>📊 Paper Trading Platform</strong><br>
      Your account, portfolio &amp; balance are saved permanently.<br>
      Log back in anytime to continue where you left off.
    </div>
  </div>
</div>

<script>
// ═══ AUTH STATE ════════════════════════════════════════════════════
let AUTH_MODE = 'login';
let CURRENT_USER = null;

function authSwitchTab(mode) {
    AUTH_MODE = mode;
    document.getElementById('tabLogin').classList.toggle('active', mode === 'login');
    document.getElementById('tabRegister').classList.toggle('active', mode === 'register');
    document.getElementById('authBtn').textContent = mode === 'login' ? 'Sign In' : 'Create Account';
    clearAuthMsg();
    document.getElementById('authUser').focus();
}

function showAuthMsg(msg, type) {
    const el = document.getElementById('authMsg');
    el.textContent = msg;
    el.className = 'auth-msg ' + type;
    el.style.display = 'block';
}

function clearAuthMsg() {
    const el = document.getElementById('authMsg');
    el.style.display = 'none';
    el.textContent = '';
}

async function authSubmit() {
    const un  = document.getElementById('authUser').value.trim();
    const pw  = document.getElementById('authPass').value.trim();
    const btn = document.getElementById('authBtn');

    if (!un || !pw) { showAuthMsg('Please enter username and password', 'error'); return; }

    btn.disabled = true;
    btn.textContent = '...';
    clearAuthMsg();

    const endpoint = AUTH_MODE === 'login' ? '/api/login' : '/api/register';
    try {
        const r = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: un, password: pw })
        });
        const d = await r.json();

        if (r.ok) {
            CURRENT_USER = d.username;
            showAuthMsg('Welcome, ' + d.username + '! Loading...', 'success');
            setTimeout(() => {
                document.getElementById('authOverlay').classList.add('hidden');
                startApp();
            }, 600);
        } else {
            showAuthMsg(d.error || 'An error occurred', 'error');
            btn.disabled = false;
            btn.textContent = AUTH_MODE === 'login' ? 'Sign In' : 'Create Account';
        }
    } catch(e) {
        showAuthMsg('Connection error. Is the server running?', 'error');
        btn.disabled = false;
        btn.textContent = AUTH_MODE === 'login' ? 'Sign In' : 'Create Account';
    }
}

async function logoutUser() {
    if (!confirm('Log out?')) return;
    await fetch('/api/logout', { method: 'POST' });
    location.reload();
}

// ═══ SYNC ENGINE ══════════════════════════════════════════════════
async function loadLivePrices() {
    try {
        const r = await fetch('/api/prices');
        if (!r.ok) return false;
        const d = await r.json();
        if (!d.prices || d.prices.length === 0) return false;

        // Update STOCKS array with live prices from Yahoo Finance
        d.prices.forEach(live => {
            // Yahoo uses BRK-B, HTML uses BRK.B — normalise
            const sym = live.symbol.replace('-', '.');
            const stock = STOCKS.find(s => s.symbol === sym || s.symbol === live.symbol);
            if (stock) {
                stock.price         = live.price;
                stock.change        = live.change;
                stock.changePercent = live.changePercent;
                stock.volume        = live.volume;
                stock.high          = live.high;
                stock.low           = live.low;
                stock.open          = live.open;
                stock.prevClose     = live.prevClose;
            }
        });
        console.log(`✅ Live prices loaded for ${d.prices.length} symbols`);
        return true;
    } catch(e) {
        console.warn('Could not load live prices, using simulated prices:', e);
        return false;
    }
}

async function syncFromServer() {
    const r = await fetch('/api/me');
    if (!r.ok) return;
    const d = await r.json();
    APP_STATE.balance   = d.balance;
    APP_STATE.portfolio = d.portfolio.map(p => ({
        symbol:   p.symbol,
        shares:   p.shares,
        avgPrice: p.avg_price
    }));
    APP_STATE.transactions = d.transactions.map(t => ({
        id:     t.id,
        type:   t.type,
        symbol: t.symbol,
        shares: t.shares,
        price:  t.price,
        total:  t.total,
        pnl:    t.pnl || 0,
        date:   t.timestamp
    }));
    APP_STATE.watchlists[0].stocks = d.watchlist;
    APP_STATE.limitOrders = d.limitOrders || [];
    CURRENT_USER = d.username;

    const chip = document.getElementById('userChip');
    if (chip) chip.textContent = '👤 ' + d.username;
}

// ═══ STARTUP ══════════════════════════════════════════════════════
async function checkAuthThenInit() {
    // This replaces the original initializeApp() call
    const r = await fetch('/api/me');
    if (r.ok) {
        // Already logged in (session persisted in same browser tab)
        document.getElementById('authOverlay').classList.add('hidden');
        startApp();
    }
    // else: show auth overlay (already visible)
}

async function startApp() {
    // 1. Load live prices from Yahoo Finance into the STOCKS array
    await loadLivePrices();

    // 2. Boot the UI (sets up charts, renders dashboard with real prices)
    initializeApp();

    // 3. Overwrite APP_STATE with saved portfolio/balance from database
    await syncFromServer();

    // 4. Re-render so saved portfolio shows immediately
    updateAccountBar();
    if (APP_STATE.currentPage === 'portfolio') renderPortfolioPage();

    // 5. Inject user chip into nav
    const navControls = document.querySelector('.nav-controls');
    if (navControls && !document.getElementById('userChip')) {
        const chip = document.createElement('div');
        chip.id = 'userChip';
        chip.style.cssText = 'display:flex;align-items:center;gap:6px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:20px;padding:6px 14px;color:#10b981;font-size:0.85rem;font-weight:600;cursor:pointer;';
        chip.textContent = '👤 ' + (CURRENT_USER || 'User');
        chip.onclick = logoutUser;
        chip.title = 'Click to log out';
        navControls.appendChild(chip);
    }

    // 6. Refresh live prices every 60s to stay in sync with server cache
    setInterval(async () => {
        await loadLivePrices();
        updateAccountBar();
        renderDashboard();
    }, 60000);

    // 7. Sync portfolio/balance every 30s
    setInterval(syncFromServer, 30000);
}
</script>
"""

# ── Startup (runs under both gunicorn and direct python) ──────────────────────
def _startup():
    init_db()
    threading.Thread(target=refresh_all_prices, daemon=True).start()
    threading.Thread(target=price_loop, daemon=True).start()

_startup()

# ── Launch ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "localhost"

    print()
    print("=" * 60)
    print("  🚀 FINAURA PRO — Starting...")
    print("=" * 60)
    print()

    is_cloud = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER")

    if not is_cloud:
        ip = local_ip()
        print()
        print("=" * 60)
        print(f"  ✅  http://localhost:{PORT}       ← open this")
        print(f"  ✅  http://{ip}:{PORT}   ← share on WiFi")
        print("  🛑  CTRL+C to stop")
        print("=" * 60)
        print()
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
