import os, json, time, requests
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
SESSION.headers.update({"User-Agent": "PFT7-Telegram-Scanner/1.0"})

def api_get(path, params=None, timeout=15):
    r = SESSION.get(BASE + path, params=params or {}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if str(j.get("code")) != "00000":
        raise RuntimeError(f"Bitget API error {path}: {j}")
    return j

def candles(symbol, interval, limit):
    j = api_get("/api/v3/market/candles", {
        "category": CATEGORY, "symbol": symbol,
        "interval": interval, "limit": str(limit), "type": "market",
    })
    rows = [{
        "ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
        "low": float(x[3]), "close": float(x[4]),
        "volume": float(x[5]), "turnover": float(x[6])
    } for x in j.get("data", [])]
    return sorted(rows, key=lambda z: z["ts"])

def closed_only(rows, interval_ms):
    now = int(time.time() * 1000)
    return [x for x in rows if x["ts"] + interval_ms <= now - 5000]

def oi_history(symbol):
    j = api_get("/api/v3/market/open-interest-history", {
        "symbol": symbol, "period": "5m"
    })
    raw = j.get("data", {}).get("list", [])
    rows = [{"ts": int(x["timestamp"]), "oi": float(x["openInterest"])} for x in raw]
    return sorted(rows, key=lambda z: z["ts"])

def funding_map():
    j = api_get("/api/v3/market/current-fund-rate", {"category": CATEGORY})
    return {x["symbol"]: float(x.get("fundingRate") or 0) for x in j.get("data", [])}

def active_buy_sell(symbol):
    j = api_get("/api/v3/market/futures-active-buy-sell", {
        "symbol": symbol, "period": "5m"
    })
    rows = sorted(j.get("data", []), key=lambda x: int(x["ts"]))
    if not rows:
        return 0.5
    x = rows[-1]
    buy = float(x.get("buyVolume") or 0)
    sell = float(x.get("sellVolume") or 0)
    return buy / (buy + sell) if buy + sell else 0.5

def pct(a, b):
    return (b / a - 1) if a else 0.0

def oi_change(rows, n):
    if len(rows) < n + 1:
        return None
    return pct(rows[-n-1]["oi"], rows[-1]["oi"])

def fmt_pct(x):
    return "n/a" if x is None else f"{x*100:+.2f}%"

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
            sig["low"] < prior_low and sig["close"] > prior_low
            and lower_wick / rng >= 0.40 and close_pos >= 0.60
        )
    else:
        prior_high = max(x["high"] for x in prev)
        upper_wick = sig["high"] - max(sig["open"], sig["close"])
        close_pos = (sig["close"] - sig["low"]) / rng
        ok = (
            sig["high"] > prior_high and sig["close"] < prior_high
            and upper_wick / rng >= 0.40 and close_pos <= 0.40
        )
    return ok, sig

def choose_direction(ret6, ret24):
    if ret6 <= -0.05 or ret24 <= -0.08:
        return "LONG"
    if ret6 >= 0.05 or ret24 >= 0.08:
        return "SHORT"
    return None

def scan_symbol(symbol, funding):
    h1 = closed_only(candles(symbol, "1H", 40), 3600000)
    m15 = closed_only(candles(symbol, "15m", 24), 900000)
    if len(h1) < 26 or len(m15) < 8:
        return None

    last = h1[-1]["close"]
    ret6 = pct(h1[-7]["close"], last)
    ret24 = pct(h1[-25]["close"], last)
    direction = choose_direction(ret6, ret24)

    if not direction:
        return {"symbol": symbol, "direction": "-", "score": 0, "status": "NO_SHOCK"}

    oi = oi_history(symbol)
    oi60 = oi_change(oi, 12)
    oi15 = oi_change(oi, 3)
    buy_share = active_buy_sell(symbol)
    auction_ok, sig = failed_auction(h1, direction)

    last15, prev15 = m15[-1], m15[-2]
    ret15 = pct(prev15["close"], last15["close"])

    if direction == "LONG":
        conditions = {
            "Shock move": ret6 <= -0.05 or ret24 <= -0.08,
            "OI crowding": oi60 is not None and oi60 >= 0.05,
            "Funding crowded": funding < 0,
            "Active-flow flip": buy_share >= 0.55,
            "Failed auction": auction_ok,
            "15m confirmation": bool(auction_ok and last15["close"] > sig["high"]),
            "OI unwind": bool(oi15 is not None and oi15 <= -0.02 and ret15 > 0),
        }
        entry = last15["close"]
        stop = sig["low"] * 0.997
        tp1, tp2, tp3 = entry*1.03, entry*1.05, entry*1.08
        stop_dist = (entry-stop)/entry
    else:
        conditions = {
            "Shock move": ret6 >= 0.05 or ret24 >= 0.08,
            "OI crowding": oi60 is not None and oi60 >= 0.05,
            "Funding crowded": funding > 0,
            "Active-flow flip": buy_share <= 0.45,
            "Failed auction": auction_ok,
            "15m confirmation": bool(auction_ok and last15["close"] < sig["low"]),
            "OI unwind": bool(oi15 is not None and oi15 <= -0.02 and ret15 < 0),
        }
        entry = last15["close"]
        stop = sig["high"] * 1.003
        tp1, tp2, tp3 = entry*0.97, entry*0.95, entry*0.92
        stop_dist = (stop-entry)/entry

    score = sum(bool(v) for v in conditions.values())
    valid_risk = stop_dist <= 0.03
    status = "A+ CONFIRMED" if score >= 7 and valid_risk else ("WATCH 6/7" if score >= 6 and valid_risk else "NO_TRADE")

    return {
        "symbol": symbol, "direction": direction, "score": score, "status": status,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "stop_dist": stop_dist, "ret6": ret6, "ret24": ret24,
        "oi60": oi60, "oi15": oi15, "funding": funding,
        "buy_share": buy_share, "conditions": conditions, "signal_ts": sig["ts"]
    }

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("alerts", {})
        return d
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
    return f"{r['symbol']}|{r['direction']}|{r['signal_ts']}|{r['score']}"

def send_telegram(message):
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TG_BOT_TOKEN or TG_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, data={
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True
    }, timeout=25)
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j)
    return True

def maybe_send_test():
    flag = os.getenv("TEST_TELEGRAM", "").strip().lower()
    if flag not in ("1","true","yes","on"):
        return
    vn = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M:%S")
    send_telegram(
        "✅ PFT-7 SCANNER CONNECTED\n\n"
        "Telegram đã kết nối thành công.\n"
        f"Thời gian VN: {vn}\n\n"
        "🟡 6/7 = WATCH\n"
        "🚨 7/7 = A+ CONFIRMED\n\n"
        "Scanner chỉ cảnh báo, không tự đặt lệnh."
    )
    print("Telegram TEST message sent.")

def render_alert(r):
    icon = "🚨" if r["score"] >= 7 else "🟡"
    dicon = "🟢" if r["direction"] == "LONG" else "🔴"
    checks = "\n".join(f"{'✅' if ok else '❌'} {name}" for name, ok in r["conditions"].items())
    return f"""{icon} PFT-7 SIGNAL

{dicon} {r['symbol']} {r['direction']}
Score: {r['score']}/7
Status: {r['status']}

Entry tham khảo: {r['entry']:.10g}
Stop tham khảo: {r['stop']:.10g}
Khoảng Stop: {r['stop_dist']*100:.2f}%

TP1: {r['tp1']:.10g}
TP2: {r['tp2']:.10g}
TP3: {r['tp3']:.10g}

Biến động 6h: {fmt_pct(r['ret6'])}
Biến động 24h: {fmt_pct(r['ret24'])}
OI ~60m: {fmt_pct(r['oi60'])}
OI ~15m: {fmt_pct(r['oi15'])}
Funding: {r['funding']*100:+.4f}%
Active Buy Share 5m: {r['buy_share']*100:.1f}%

{checks}

⚠️ Scanner thử nghiệm, không đảm bảo có lời."""

def main():
    print("=== PFT-7 Telegram Scanner v1.0 ===")
    print(datetime.now(timezone.utc).isoformat())

    maybe_send_test()
    state = load_state()
    fm = funding_map()
    results = []

    for i, symbol in enumerate(SYMBOLS):
        try:
            r = scan_symbol(symbol, fm.get(symbol, 0.0))
            if r:
                results.append(r)
                print(f"{symbol:10s} {r.get('direction','-'):5s} {r.get('score',0)}/7 {r.get('status','')}")
        except Exception as e:
            print(f"[ERROR] {symbol}: {type(e).__name__}: {e}")
        if i < len(SYMBOLS)-1:
            time.sleep(1.05)

    ranked = sorted(
        [r for r in results if r.get("direction") in ("LONG","SHORT")],
        key=lambda x: (x.get("score",0), -x.get("stop_dist",99)),
        reverse=True
    )

    fresh = []
    for r in ranked:
        if r["score"] >= 6 and r["stop_dist"] <= 0.03:
            k = alert_key(r)
            if k not in state["alerts"]:
                fresh.append((k, r))

    if not fresh:
        print("No NEW 6/7 or 7/7 Telegram alert this run.")
        save_state(state)
        return

    now = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    for k, r in fresh:
        try:
            send_telegram(render_alert(r))
            state["alerts"][k] = now
            print(f"Telegram alert sent: {r['symbol']} {r['direction']} {r['score']}/7")
        except Exception as e:
            print(f"[TELEGRAM ERROR] {type(e).__name__}: {e}")

    save_state(state)

if __name__ == "__main__":
    main()
