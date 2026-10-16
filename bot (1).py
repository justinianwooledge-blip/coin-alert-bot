"""Coin alert bot: checks your coins every 5 min and texts you on Telegram.
Sends alerts and suggestions only. It never buys or sells anything."""
import json, os, datetime, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = str(os.environ.get("TG_CHAT_ID", "")).strip()
CFG = json.load(open("coins.json"))
try:
    STATE = json.load(open("state.json"))
except Exception:
    STATE = {}

VERSION = 3
LINE = "━━━━━━━━━━━━━━"
e = html.escape
TP = CFG.get("take_profit_pct", 50)
DIP = CFG.get("rebuy_dip_pct", 30)
PORTION = CFG.get("sell_portion_pct", 25)


# ---------- connections ----------
def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read()


def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}).encode()
    return json.loads(urllib.request.urlopen(
        f"https://api.telegram.org/bot{TOKEN}/{method}", data, timeout=20).read())


def send(text):
    tg("sendMessage", chat_id=CHAT, text=text, parse_mode="HTML", disable_web_page_preview="true")


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


def headlines(query, n=2):
    try:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        return [(i.findtext("title"), i.findtext("link"))
                for i in ET.fromstring(get(url)).findall(".//item")[:n]]
    except Exception:
        return []


# ---------- formatting ----------
def fmt(p):
    return f"${p:.8f}".rstrip("0").rstrip(".") if p < 1 else f"${p:,.4f}"


def arrow(p):
    return "📈" if p > 0.5 else "📉" if p < -0.5 else "➖"


def pct_cell(label, v):
    val = "   —  " if v is None else f"{v:+.1f}%"
    return f"{label:<4}{val:>7}"


def coin_block(r):
    if r.get("missing"):
        return f"❌ <b>{e(r['name'])}</b>  couldn't load (check the CA)"
    dot = "🟢" if r["h24"] > 0.5 else "🔴" if r["h24"] < -0.5 else "⚪"
    up, down = r["up"], r["down"]
    sig = ("💰 Take-profit zone" if up >= TP else "🛒 Rebuy zone" if down <= -DIP
           else "👀 Getting close to take-profit" if up >= TP * 0.7
           else "👀 Getting close to rebuy" if down <= -DIP * 0.7 else "✋ Hold")
    ch = r["changes"]
    grid = "\n".join(pct_cell(a, ch[a]) + "   " + pct_cell(b, ch[b])
                     for a, b in (("30m", "1h"), ("3h", "6h"), ("12h", "24h")))
    return (f"{dot} <b>{e(r['name'])}</b>   {fmt(r['price'])}\n"
            f"▸ Signal:  <b>{sig}</b>\n"
            f"<pre>{grid}</pre>\n"
            f"▸ This week:  low {fmt(r['low'])}  →  high {fmt(r['high'])}")


_stocks = None
def stocks_section():
    global _stocks
    if _stocks is None:
        lines = []
        for sym in CFG.get("stocks", []):
            price, pct = stock_change(sym)
            if price is not None:
                lines.append(f"▸ <b>{sym}</b>  ${price:,.2f}   {arrow(pct)} {pct:+.1f}%")
        _stocks = "📊 <b>Stocks today</b>\n" + "\n".join(lines) if lines else ""
    return _stocks


def news_section():
    parts = []
    for q in CFG.get("news_topics", []):
        items = headlines(q)
        if items:
            parts.append(f"🔹 <b>{e(q)}</b>\n" + "\n".join(
                f'▸ <a href="{e(link or "")}">{e(title or "")}</a>' for title, link in items))
    return "📰 <b>News</b>\n\n" + ("\n\n".join(parts) if parts else "No new headlines today.")


def today_label():
    return datetime.datetime.now(ZoneInfo("America/Chicago")).strftime("%A, %b %-d")


def coins_section(rows):
    return "💰 <b>Your coins</b>\n\n" + "\n\n".join(coin_block(r) for r in rows)


def help_text():
    return ("🤖 <b>What I do</b>\n"
            f"{LINE}\n"
            f"🟢 Text you to <b>take profit</b> when a coin is {TP}% above its lowest price this week\n"
            f"🔻 Text you to <b>rebuy</b> when a coin is {DIP}% below its highest price this week\n"
            "⚡ Warn you about big moves (15%+ in an hour)\n"
            "☀️ Send a morning brief every day\n\n"
            "📋 <b>Commands</b> (tap ☰ Menu)\n"
            "▸ /status  coin prices and signals\n"
            "▸ /news  latest market news\n"
            "▸ /help  this message\n\n"
            "<i>Replies can take up to 5 to 10 minutes.</i>")


def alert(kind, r, c, tip):
    if kind == "sell":
        head = f"🟢 <b>TAKE PROFIT: {e(r['name'])}</b>"
        why = f"📈 <b>{r['up']:.0f}%</b> above this week's low (your target: {TP}%)"
        do = f"Sell about <b>{PORTION}%</b> of your bag"
    else:
        head = f"🔻 <b>REBUY CHANCE: {e(r['name'])}</b>"
        why = f"📉 <b>{abs(r['down']):.0f}%</b> below this week's high (your dip: {DIP}%)"
        do = "Consider buying back in"
    parts = [head, LINE,
             f"💵 Price:  <b>{fmt(r['price'])}</b>", why, "",
             f"✅ <b>Suggestion:</b> {do}", f"💡 {tip}", "",
             f"📊 1h {r['h1']:+.1f}%  |  6h {r['changes']['6h'] or 0:+.1f}%  |  24h {r['h24']:+.1f}%",
             f"💧 Liquidity: ${r['liq']:,.0f}"]
    if r["liq"] and r["liq"] < CFG.get("low_liquidity_usd", 10000):
        parts.append("⚠️ Low liquidity: big trades move the price a lot")
    if c.get("note"):
        parts.append(f"📝 {e(c['note'])}")
    s = stocks_section()
    if s:
        parts += ["", s]
    return "\n".join(parts)


# ---------- main ----------
def main():
    now = datetime.datetime.utcnow()
    rows = []

    for c in CFG["coins"]:
        name, ca = c["name"], c.get("ca", "")
        d = coin_data(ca) if ca else None
        price = float((d or {}).get("priceUsd") or 0)
        if not d or price <= 0:
            rows.append({"name": name, "missing": True})
            continue
        ch = d.get("priceChange") or {}
        st = STATE.setdefault(name, {})
        ts = now.timestamp()

        # Keep 25 hours of prices (one every ~5 min) to work out 30m / 3h / 12h changes
        hist = [h for h in st.get("hist", []) if ts - h[0] <= 25 * 3600]
        hist.append([round(ts), price])
        st["hist"] = hist

        def ago(minutes):
            target = ts - minutes * 60
            best = min(hist[:-1], key=lambda h: abs(h[0] - target), default=None)
            if not best or abs(best[0] - target) > max(600, minutes * 6):
                return None
            return (price / best[1] - 1) * 100

        # Weekly low and high (reset after 7 days)
        for k, better in (("high", price > st.get("high", 0)), ("low", price < st.get("low", 1e18))):
            at = st.get(k + "_at")
            if better or not at or (now - datetime.datetime.fromisoformat(at)).days >= 7:
                st[k], st[k + "_at"] = price, now.isoformat()

        h1, h6, h24 = ch.get("h1"), ch.get("h6"), ch.get("h24")
        r = {"name": name, "price": price, "h1": h1 or 0, "h24": h24 or 0,
             "liq": (d.get("liquidity") or {}).get("usd") or 0,
             "low": st["low"], "high": st["high"],
             "up": (price / st["low"] - 1) * 100, "down": (price / st["high"] - 1) * 100,
             "changes": {"30m": ago(30), "1h": h1 if h1 is not None else ago(60), "3h": ago(180),
                         "6h": h6 if h6 is not None else ago(360), "12h": ago(720),
                         "24h": h24 if h24 is not None else ago(1440)}}
        rows.append(r)

        zone = "sell" if r["up"] >= TP else "buy" if r["down"] <= -DIP else "none"
        if zone != st.get("zone", "none") and zone != "none":
            if zone == "sell":
                tip = ("Momentum is slowing, good time to lock in profit." if r["h1"] < 0
                       else "Still climbing. Sell part, let the rest ride.")
            else:
                tip = ("It's bouncing this hour, a better spot to buy." if r["h1"] >= 0
                       else "Still falling. Wait, or buy a small amount now and more later.")
            send(alert(zone, r, c, tip))
        st["zone"] = zone

        last = st.get("move_at")
        if abs(r["h1"]) >= CFG.get("big_move_pct", 15) and (
                not last or (now - datetime.datetime.fromisoformat(last)).total_seconds() >= 3600):
            emoji = "🚀" if r["h1"] > 0 else "⚠️"
            send(f"⚡ <b>BIG MOVE: {e(name)}</b>\n{LINE}\n{emoji} {r['h1']:+.1f}% in the last hour\n"
                 f"💵 Now {fmt(price)}")
            st["move_at"] = now.isoformat()

    # Commands you send the bot
    try:
        res = tg("getUpdates", offset=STATE.get("tg_offset", 0), timeout=0)
        for u in res.get("result", []):
            STATE["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != CHAT:
                continue
            text = (msg.get("text") or "").strip().lower()
            if text.startswith("/status"):
                send(f"📋 <b>Status check</b>\n<i>{today_label()}</i>\n{LINE}\n\n"
                     + coins_section(rows) + ("\n\n" + stocks_section() if stocks_section() else ""))
            elif text.startswith("/news"):
                send(news_section())
            elif text.startswith("/help") or text.startswith("/start"):
                send(help_text())
    except Exception:
        pass

    # New look: set up the menu and show the new layout once
    if STATE.get("version") != VERSION:
        try:
            tg("setMyCommands", commands=[
                {"command": "status", "description": "Coin prices and signals"},
                {"command": "news", "description": "Latest market news"},
                {"command": "help", "description": "What this bot does"}])
        except Exception:
            pass
        send("✨ <b>Updated!</b> Here's how I work now:\n\n" + help_text())
        send(f"📋 <b>Status check</b>\n<i>{today_label()}</i>\n{LINE}\n\n" + coins_section(rows))
        STATE["version"] = VERSION

    # Morning brief
    today = now.strftime("%Y-%m-%d")
    if now.hour >= CFG.get("daily_brief_hour_utc", 13) and STATE.get("brief_day") != today:
        parts = [f"☀️ <b>Good morning! Daily brief</b>\n<i>{today_label()}</i>\n{LINE}", coins_section(rows)]
        if stocks_section():
            parts.append(stocks_section())
        parts.append(news_section())
        send("\n\n".join(parts))
        STATE["brief_day"] = today

    json.dump(STATE, open("state.json", "w"), indent=2)


if __name__ == "__main__":
    main()
