#!/usr/bin/env python3
"""EMA9 x daily Fibonacci pivot watcher. Sends a Telegram message when the EMA9 (15m, on close)
crosses a pivot level (P, R1-R3, S1-S3). Standard library only. Meant to run on GitHub Actions.

  python watcher.py                 normal run (sends Telegram if TELEGRAM_* env vars are set)
  python watcher.py --dry-run -n 200   print crossings in the last 200 candles, send nothing
  python watcher.py --test          send one test message to Telegram
"""
import json, os, sys, time, argparse, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# label -> (Yahoo symbol, session kind). "fx" = day rolls at 17:00 New York (like TradingView/OANDA); "utc" = crypto
SYMBOLS = {
    "Gold (GC=F futures)": ("GC=F", "fx"),
    "Oil WTI (CL=F)":      ("CL=F", "fx"),
    "EURUSD": ("EURUSD=X", "fx"),
    "GBPUSD": ("GBPUSD=X", "fx"),
    "BTCUSD": ("BTC-USD", "utc"),
}
EMA_LEN = 9
BAR_SEC = 900  # 15m
NY, IST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def fetch(sym):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=15m&range=10d" % urllib.parse.quote(sym)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res["timestamp"]):
        h, l, c = q["high"][i], q["low"][i], q["close"][i]
        if h is None or l is None or c is None:
            continue
        bars.append((t, h, l, c))
    return bars


def session_day(t, kind):
    dt = datetime.fromtimestamp(t, timezone.utc)
    if kind == "utc":
        return dt.date()
    return (dt.astimezone(NY) + timedelta(hours=7)).date()  # 17:00 NY starts the next day


def levels(h, l, c):
    p, r = (h + l + c) / 3, h - l
    return {"P": p, "R1": p + .382 * r, "R2": p + .618 * r, "R3": p + r,
            "S1": p - .382 * r, "S2": p - .618 * r, "S3": p - r}


def find_crosses(bars, kind, lookback):
    """Crossings of EMA9 over pivot levels in the last `lookback` completed candles."""
    now = time.time()
    if bars and bars[-1][0] + BAR_SEC > now:  # drop the candle still forming
        bars = bars[:-1]
    if len(bars) < 30:
        return [], bars
    days = {}
    for t, h, l, c in bars:
        d = session_day(t, kind)
        if d not in days:
            days[d] = [h, l, c]
        else:
            days[d][0] = max(days[d][0], h)
            days[d][1] = min(days[d][1], l)
            days[d][2] = c
    order = sorted(days)
    a = 2.0 / (EMA_LEN + 1)
    ema, e = [], bars[0][3]
    for _, _, _, c in bars:
        e = a * c + (1 - a) * e
        ema.append(e)
    out = []
    for i in range(max(1, len(bars) - lookback), len(bars)):
        d = session_day(bars[i][0], kind)
        k = order.index(d)
        if k == 0:
            continue  # no previous day available
        for name, lv in levels(*days[order[k - 1]]).items():
            if ema[i] > lv and ema[i - 1] <= lv:
                out.append((bars[i][0], name, "UP", lv, ema[i], bars[i][3]))
            elif ema[i] < lv and ema[i - 1] >= lv:
                out.append((bars[i][0], name, "DOWN", lv, ema[i], bars[i][3]))
    return out, bars


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[no Telegram secrets set] " + text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data, timeout=30).read()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("-n", "--lookback", type=int, default=2)
    args = ap.parse_args()
    if args.test:
        telegram("Watcher test OK. You will get a message here when EMA9 crosses a Fibonacci pivot on the 15m chart.")
        return 0
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {"sent": []}
    sent = set(state.get("sent", []))
    failed = 0
    for label, (sym, kind) in SYMBOLS.items():
        try:
            crosses, _ = find_crosses(fetch(sym), kind, args.lookback)
        except Exception as ex:
            failed += 1
            print("FAILED %s: %s" % (label, ex), file=sys.stderr)
            continue
        for t, name, direction, lv, e, close in crosses:
            key = "%s|%s|%s|%s" % (sym, t, name, direction)
            if key in sent and not args.dry_run:
                continue
            when = datetime.fromtimestamp(t + BAR_SEC, IST).strftime("%d %b %H:%M IST")
            msg = "%s %s: EMA9 crossed %s %s (%s)\nCandle closed %s | close %.5g | EMA9 %.5g\n15m chart, price data may be a few minutes delayed." % (
                "UP" if direction == "UP" else "DOWN", label, "above" if direction == "UP" else "below", name, "%.5g" % lv, when, close, e)
            if args.dry_run:
                print(msg + "\n")
            else:
                telegram(msg)
                sent.add(key)
    if not args.dry_run:
        state["sent"] = sorted(sent)[-500:]
        json.dump(state, open(STATE, "w"), indent=1)
    return 1 if failed == len(SYMBOLS) else 0


if __name__ == "__main__":
    sys.exit(main())
