"""Coin alert bot: checks your coins every 5 min and texts you on Telegram.
Sends alerts only. It never buys or sells anything."""
import json, os, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT_ID"]
CFG = json.load(open("coins.json"))
try:
    STATE = json.load(open("state.json"))
except Exception:
    STATE = {}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read()


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=20)


def coin_data(ca):
    """Find the token on DexScreener by contract address; use its most liquid pair."""
    for url in (f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
                f"https://api.dexscreener.com/latest/dex/search?q={ca}"):
        try:
            pairs = json.loads(get(url)).get("pairs") or []
        except Exception:
            continue
        mine = [p for p in pairs if p.get("baseToken", {}).get("address", "").lower() == ca.lower()]
        pairs = mine or pairs
        if pairs:
            return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    return None


def stock_change(sym):
    try:
        meta = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"))["chart"]["result"][0]["meta"]
        price, prev = meta["regularMarketPrice"], meta["chartPreviousClose"]
        return price, (price - prev) / prev * 100
    except Exception:
        return None, None


def headlines(query, n=3):
    try:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        items = ET.fromstring(get(url)).findall(".//item")[:n]
        return [f"• {i.findtext('title')}\n  {i.findtext('link')}" for i in items]
    except Exception:
        return []


def fmt(p):
    return f"${p:.8f}".rstrip("0").rstrip(".") if p < 1 else f"${p:,.4f}"


def market_line():
    parts = []
    for sym in CFG.get("stocks", []):
        price, pct = stock_change(sym)
        if price is not None:
            parts.append(f"{sym} ${price:,.2f} ({pct:+.1f}%)")
    return "Stocks today: " + ", ".join(parts) if parts else ""


now = datetime.datetime.utcnow()
FIRST_RUN = not STATE
TP = CFG.get("take_profit_pct", 50)
DIP = CFG.get("rebuy_dip_pct", 30)
PORTION = CFG.get("sell_portion_pct", 25)
mkt = None
summary = []

for c in CFG["coins"]:
    name, ca = c["name"], c.get("ca", "")
    d = coin_data(ca) if ca else None
    if not d:
        continue
    price = float(d.get("priceUsd") or 0)
    if price <= 0:
        continue
    ch = d.get("priceChange") or {}
    h1, h24 = ch.get("h1") or 0, ch.get("h24") or 0
    liq = (d.get("liquidity") or {}).get("usd") or 0
    info = f"1h {h1:+.1f}% | 24h {h24:+.1f}% | liquidity ${liq:,.0f}"

    st = STATE.setdefault(name, {})
    # Starting point: the price when the bot first saw the coin (or after the last signal)
    if not st.get("ref"):
        st["ref"] = price
    # Recent high: highest price in the last 7 days
    high_at = st.get("high_at")
    stale = not high_at or (now - datetime.datetime.fromisoformat(high_at)).days >= 7
    if stale or price > st.get("high", 0):
        st["high"], st["high_at"] = price, now.isoformat()

    gain = (price / st["ref"] - 1) * 100
    drop = (price / st["high"] - 1) * 100
    summary.append(f"{name}: {fmt(price)} | {gain:+.0f}% since start | {drop:+.0f}% from 7-day high")

    zone = "sell" if gain >= TP else "buy" if drop <= -DIP else "none"
    if zone != st.get("zone", "none") and zone != "none":
        mkt = mkt if mkt is not None else market_line()
        if zone == "sell":
            tip = ("Momentum is slowing, good time to lock in profit."
                   if h1 < 0 else "Still climbing. Selling part and letting the rest ride is a common move.")
            msg = (f"🟢 TAKE PROFIT? {name} is up {gain:.0f}% (your target: {TP}%)\n"
                   f"Now {fmt(price)}\nSuggestion: sell about {PORTION}% of your bag. {tip}")
        else:
            tip = ("It's bouncing in the last hour, which is a better spot to buy."
                   if h1 >= 0 else "Still falling. Consider waiting, or buy a small amount now and more later.")
            msg = (f"🔻 REBUY? {name} is down {abs(drop):.0f}% from its 7-day high (your dip: {DIP}%)\n"
                   f"Now {fmt(price)}\nSuggestion: {tip}")
        msg += f"\n{info}"
        if liq and liq < CFG.get("low_liquidity_usd", 10000):
            msg += "\n⚠️ Low liquidity: big trades can move the price a lot."
        if c.get("note"):
            msg += f"\nNote: {c['note']}"
        send(msg + (f"\n{mkt}" if mkt else ""))
        st["ref"] = price  # next signal is measured from here
        zone = "none"
    st["zone"] = zone

    # Big-move alert, at most once per hour per coin
    last = st.get("move_at")
    if abs(h1) >= CFG.get("big_move_pct", 15) and (
            not last or (now - datetime.datetime.fromisoformat(last)).total_seconds() >= 3600):
        send(f"⚡ {name} moved {h1:+.1f}% in the last hour. Now {fmt(price)}")
        st["move_at"] = now.isoformat()

# First run: confirm the bot works and show what it found
if FIRST_RUN:
    found = [s.split(":")[0] for s in summary]
    missing = [c["name"] for c in CFG["coins"] if c["name"] not in found]
    msg = "✅ Bot is live. Checking every 5 minutes.\n" + "\n".join(summary)
    if missing:
        msg += "\n❌ Couldn't find: " + ", ".join(missing) + " (check the CA)"
    send(msg)

# Daily brief: prices, stocks, and news
today = now.strftime("%Y-%m-%d")
if now.hour >= CFG.get("daily_brief_hour_utc", 13) and STATE.get("brief_day") != today:
    lines = ["☀️ Daily brief", *summary]
    m = market_line()
    if m:
        lines += ["", m]
    news = []
    for q in CFG.get("news_topics", []):
        news += headlines(q)
    if news:
        lines += ["", "📰 News:", *news[:8]]
    send("\n".join(lines))
    STATE["brief_day"] = today

json.dump(STATE, open("state.json", "w"), indent=2)
