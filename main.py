import os
import json
import time
import smtplib
import requests
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta

BASE = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","DOGEUSDT","XRPUSDT",
    "LINKUSDT","INJUSDT","AVAXUSDT","NEARUSDT","AAVEUSDT",
    "ADAUSDT","SUIUSDT","LTCUSDT","BCHUSDT","BNBUSDT"
]

STATE_FILE = "state.json"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PFT7-GitHub-Scanner/0.2"})

def api_get(path, params=None, timeout=15):
    r = SESSION.get(BASE + path, params=params or {}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if str(j.get("code")) != "00000":
        raise RuntimeError(f"Bitget API error {path}: {j}")
    return j

def candles(symbol, interval, limit):
    j = api_get("/api/v3/market/candles", {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit),
        "type": "market",
    })
    rows = []
    for x in j.get("data", []):
        rows.append({
            "ts": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
            "turnover": float(x[6]),
        })
    return sorted(rows, key=lambda z: z["ts"])

def closed_only(rows, interval_ms):
    now = int(time.time() * 1000)
    return [x for x in rows if x["ts"] + interval_ms <= now - 5000]

def oi_history(symbol, period="5m"):
    j = api_get("/api/v3/market/open-interest-history", {
        "symbol": symbol,
        "period": period,
    })
    raw = j.get("data", {}).get("list", [])
    rows = []
    for x in raw:
        rows.append({
            "ts": int(x["timestamp"]),
            "oi": float(x["openInterest"]),
            "oi_value": float(x.get("openInterestValue") or 0),
        })
    return sorted(rows, key=lambda z: z["ts"])

def funding_map():
    j = api_get("/api/v3/market/current-fund-rate", {
        "category": CATEGORY
    })
    return {
        x["symbol"]: float(x.get("fundingRate") or 0)
        for x in j.get("data", [])
    }

def active_buy_sell(symbol):
    j = api_get("/api/v3/market/futures-active-buy-sell", {
        "symbol": symbol,
        "period": "5m",
    })
    rows = sorted(j.get("data", []), key=lambda x: int(x["ts"]))
    if not rows:
        return None
    x = rows[-1]
    buy = float(x.get("buyVolume") or 0)
    sell = float(x.get("sellVolume") or 0)
    total = buy + sell
    return {
        "buy": buy,
        "sell": sell,
        "buy_share": buy / total if total else 0.5,
        "ts": int(x["ts"]),
    }

def pct(a, b):
    return (b / a - 1.0) if a else 0.0

def oi_change(rows, lookback_points):
    if len(rows) < lookback_points + 1:
        return None
    old = rows[-lookback_points - 1]["oi"]
    new = rows[-1]["oi"]
    return pct(old, new)

def fmt_pct(x):
    if x is None:
        return "n/a"
    return f"{x*100:+.2f}%"

def failed_auction(h1, direction):
    if len(h1) < 26:
        return False, None

    sig = h1[-1]
    prev = h1[-25:-1]
    rng = sig["high"] - sig["low"]
    if rng <= 0:
        return False, sig

    if direction == "LONG":
        prior_low = min(x["low"] for x in prev)
        lower_wick = min(sig["open"], sig["close"]) - sig["low"]
        close_pos = (sig["close"] - sig["low"]) / rng
        ok = (
            sig["low"] < prior_low
            and sig["close"] > prior_low
            and lower_wick / rng >= 0.40
            and close_pos >= 0.60
        )
    else:
        prior_high = max(x["high"] for x in prev)
        upper_wick = sig["high"] - max(sig["open"], sig["close"])
        close_pos = (sig["close"] - sig["low"]) / rng
        ok = (
            sig["high"] > prior_high
            and sig["close"] < prior_high
            and upper_wick / rng >= 0.40
            and close_pos <= 0.40
        )
    return ok, sig

def choose_direction(ret6, ret24):
    if ret6 <= -0.05 or ret24 <= -0.08:
        return "LONG"
    if ret6 >= 0.05 or ret24 >= 0.08:
        return "SHORT"
    return None

def calc_levels(direction, entry, sig):
    if direction == "LONG":
        stop = sig["low"] * 0.997
        tp1, tp2, tp3 = entry * 1.03, entry * 1.05, entry * 1.08
        stop_dist = (entry - stop) / entry
    else:
        stop = sig["high"] * 1.003
        tp1, tp2, tp3 = entry * 0.97, entry * 0.95, entry * 0.92
        stop_dist = (stop - entry) / entry
    return stop, tp1, tp2, tp3, stop_dist

def scan_symbol(symbol, funding):
    h1 = closed_only(candles(symbol, "1H", 40), 60 * 60 * 1000)
    m15 = closed_only(candles(symbol, "15m", 24), 15 * 60 * 1000)

    if len(h1) < 26 or len(m15) < 8:
        return None

    last = h1[-1]["close"]
    close6 = h1[-7]["close"]
    close24 = h1[-25]["close"]

    ret6 = pct(close6, last)
    ret24 = pct(close24, last)
    direction = choose_direction(ret6, ret24)

    if not direction:
        return {
            "symbol": symbol,
            "direction": "-",
            "score": 0,
            "status": "NO_SHOCK",
            "ret6": ret6,
            "ret24": ret24,
        }

    oi = oi_history(symbol, "5m")
    oi60 = oi_change(oi, 12)
    oi15 = oi_change(oi, 3)

    flow = active_buy_sell(symbol)
    buy_share = flow["buy_share"] if flow else 0.5

    auction_ok, sig = failed_auction(h1, direction)

    last15 = m15[-1]
    prev15 = m15[-2]
    ret15 = pct(prev15["close"], last15["close"])

    if direction == "LONG":
        shock_ok = ret6 <= -0.05 or ret24 <= -0.08
        oi_crowd_ok = oi60 is not None and oi60 >= 0.05
        funding_ok = funding < 0
        flow_flip_ok = buy_share >= 0.55
        confirm_ok = bool(auction_ok and last15["close"] > sig["high"])
        unwind_ok = bool(oi15 is not None and oi15 <= -0.02 and ret15 > 0)
    else:
        shock_ok = ret6 >= 0.05 or ret24 >= 0.08
        oi_crowd_ok = oi60 is not None and oi60 >= 0.05
        funding_ok = funding > 0
        flow_flip_ok = buy_share <= 0.45
        confirm_ok = bool(auction_ok and last15["close"] < sig["low"])
        unwind_ok = bool(oi15 is not None and oi15 <= -0.02 and ret15 < 0)

    conditions = {
        "shock_move": shock_ok,
        "oi_crowding": oi_crowd_ok,
        "funding_crowded": funding_ok,
        "active_flow_flip": flow_flip_ok,
        "failed_auction": auction_ok,
        "15m_confirmation": confirm_ok,
        "oi_unwind": unwind_ok,
    }

    score = sum(bool(x) for x in conditions.values())
    entry = last15["close"]
    stop, tp1, tp2, tp3, stop_dist = calc_levels(direction, entry, sig)
    valid_risk = stop_dist <= 0.03

    if score >= 7 and valid_risk:
        status = "A+ CONFIRMED"
    elif score >= 6 and valid_risk:
        status = "WATCH 6/7"
    else:
        status = "NO_TRADE"

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "status": status,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "stop_dist": stop_dist,
        "ret6": ret6,
        "ret24": ret24,
        "oi60": oi60,
        "oi15": oi15,
        "funding": funding,
        "buy_share": buy_share,
        "conditions": conditions,
        "signal_ts": sig["ts"] if sig else 0,
    }

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"alerts": {}}
        data.setdefault("alerts", {})
        return data
    except Exception:
        return {"alerts": {}}

def save_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    clean = {}
    for k, v in state.get("alerts", {}).items():
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if t >= cutoff:
                clean[k] = v
        except Exception:
            pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"alerts": clean}, f, ensure_ascii=False, indent=2)

def alert_key(r):
    # 6/7 and 7/7 are distinct alerts, but repeated runs of the same signal are suppressed.
    return f"{r['symbol']}|{r['direction']}|{r['signal_ts']}|{r['score']}"

def send_email(subject, body):
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    recipient = os.getenv("ALERT_TO", "").strip()

    if not user or not password or not recipient:
        print("[EMAIL DISABLED] Missing SMTP_USER / SMTP_APP_PASSWORD / ALERT_TO.")
        return False

    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL(host, port, timeout=25) as s:
        s.login(user, password)
        s.send_message(msg)
    return True

def render_alert(r):
    checks = "\n".join(
        f"{'YES' if ok else 'NO '}  {name}"
        for name, ok in r["conditions"].items()
    )

    return f"""PFT-7 EXPERIMENTAL ALERT

Coin: {r['symbol']}
Direction: {r['direction']}
Score: {r['score']}/7
Status: {r['status']}

Reference entry: {r['entry']:.10g}
Reference stop: {r['stop']:.10g}
Stop distance: {r['stop_dist']*100:.2f}%
TP1 (+/-3%): {r['tp1']:.10g}
TP2 (+/-5%): {r['tp2']:.10g}
TP3 (+/-8%): {r['tp3']:.10g}

6h move: {fmt_pct(r['ret6'])}
24h move: {fmt_pct(r['ret24'])}
OI ~60m: {fmt_pct(r['oi60'])}
OI ~15m: {fmt_pct(r['oi15'])}
Funding: {r['funding']*100:+.4f}%
Active buy share 5m: {r['buy_share']*100:.1f}%

CONDITIONS
{checks}

This scanner is experimental research. It is not a guaranteed profitable system
and it must not be treated as an automatic order instruction.
"""

def main():
    print("=== PFT-7 Scanner v0.2 / GitHub Actions ===")
    print(datetime.now(timezone.utc).isoformat())

    state = load_state()

    try:
        fm = funding_map()
    except Exception as e:
        print(f"[FATAL] Funding map failed: {type(e).__name__}: {e}")
        save_state(state)
        raise

    results = []

    for i, symbol in enumerate(SYMBOLS):
        try:
            r = scan_symbol(symbol, fm.get(symbol, 0.0))
            if r:
                results.append(r)
                print(
                    f"{symbol:10s} {r.get('direction','-'):5s} "
                    f"{r.get('score',0)}/7 {r.get('status','')}"
                )
        except Exception as e:
            print(f"[ERROR] {symbol}: {type(e).__name__}: {e}")

        # Bitget futures-active-buy-sell: documented 1 request/sec/IP.
        if i < len(SYMBOLS) - 1:
            time.sleep(1.05)

    ranked = sorted(
        [r for r in results if r.get("direction") in ("LONG", "SHORT")],
        key=lambda x: (x.get("score", 0), -x.get("stop_dist", 99)),
        reverse=True
    )

    print("\n=== TOP SETUPS ===")
    for r in ranked[:10]:
        print(
            f"{r['symbol']:10s} {r['direction']:5s} {r['score']}/7 "
            f"{r['status']:14s} OI60={fmt_pct(r['oi60'])} "
            f"Funding={r['funding']*100:+.4f}%"
        )

    candidates = [
        r for r in ranked
        if r["score"] >= 6 and r["stop_dist"] <= 0.03
    ]

    fresh = []
    for r in candidates:
        k = alert_key(r)
        if k not in state["alerts"]:
            fresh.append((k, r))

    if not fresh:
        print("No NEW 6/7 or 7/7 alert this run.")
        save_state(state)
        return

    body = ("\n\n" + "=" * 64 + "\n\n").join(
        render_alert(r) for _, r in fresh
    )

    subject = "[PFT-7] " + " | ".join(
        f"{r['symbol']} {r['direction']} {r['score']}/7"
        for _, r in fresh[:4]
    )

    try:
        if send_email(subject, body):
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for k, _ in fresh:
                state["alerts"][k] = now
            print(f"Email sent for {len(fresh)} new signal(s).")
        else:
            print("Signal found, but email is not configured.")
    finally:
        save_state(state)

if __name__ == "__main__":
    main()
