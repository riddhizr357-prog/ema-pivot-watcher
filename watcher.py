#!/usr/bin/env python3
"""EMA9 x daily Fibonacci pivot watcher. Sends a Telegram message when the EMA9 (15m, on close)
crosses a pivot level (P, R1-R3, S1-S3). Standard library only. Meant to run on GitHub Actions.

  python watcher.py                 normal run (sends Telegram if TELEGRAM_* env vars are set)
  python watcher.py --dry-run -n 200   print crossings in the last 200 candles, send nothing
  python watcher.py --test          send one test message to Telegram

Data hygiene (applies to EVERY symbol, see clean_bars / daily_ranges):
  * duplicate / unsorted / null bars are removed
  * Yahoo's live-quote pseudo-bar (odd timestamp, high=low=close) is removed
  * only fully CLOSED 15m candles are ever evaluated (the forming candle is never used)
  * fx-style symbols drop the stray bars Yahoo puts after the Friday close (fake flat "weekend day")
  * a "previous day" must be a real session (>= MIN_DAY_BARS bars, real range, recent) or no alert is sent
"""
import json, os, sys, time, argparse, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# label -> (Yahoo symbol, session kind, price decimals).
# "fx" = day rolls at 17:00 New York (like TradingView/OANDA/CME), closed on weekends; "utc" = 24/7 crypto.
SYMBOLS = {
    "Gold (Dec 2026 futures)": ("GCZ26.CMX", "fx", 1),  # pinned: GC=F rolled to Feb on 25 Nov (+$28 jump). Dec trades to ~29 Dec; revisit then.
    "Oil WTI (CL=F)":      ("CL=F", "fx", 2),
    "EURUSD":              ("EURUSD=X", "fx", 5),
    "GBPUSD":              ("GBPUSD=X", "fx", 5),
    "BTCUSD":              ("BTC-USD", "utc", 0),
}
EMA_LEN = 9
BAR_SEC = 900          # 15m
MIN_DAY_BARS = 20      # a session with fewer bars is a stub (weekend/holiday remnant), never a "previous day"
MAX_PREV_GAP_DAYS = 5  # previous day older than this => data gap, levels untrustworthy, stay silent
STALE_HOURS = 8        # warn (stderr only) if the newest closed candle is older than this on a weekday
KEEP_SENT = 400
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


def clean_bars(bars, kind, now):
    """Return only trustworthy, fully closed 15m candles, oldest first."""
    seen = {}
    for t, h, l, c in bars:
        if t % BAR_SEC:                     # live-quote pseudo-bar / misaligned bar
            continue
        if not (h >= l and l <= c <= h):    # corrupt candle
            continue
        seen[t] = (t, h, l, c)              # duplicate timestamps: last one wins
    out = [seen[t] for t in sorted(seen)]
    if kind == "fx":                        # stray bars after Friday's close form a fake flat "Saturday"
        out = [b for b in out if session_day(b[0], kind).weekday() < 5]
    return [b for b in out if b[0] + BAR_SEC <= now]   # never the candle still forming


def daily_ranges(bars, kind):
    """session date -> [high, low, close, n_bars]"""
    days = {}
    for t, h, l, c in bars:
        d = session_day(t, kind)
        if d not in days:
            days[d] = [h, l, c, 1]
        else:
            x = days[d]
            x[0], x[1], x[2], x[3] = max(x[0], h), min(x[1], l), c, x[3] + 1
    return days


def find_crosses(bars, kind, lookback, now=None):
    """Crossings of EMA9 over pivot levels in the last `lookback` closed candles.
    Returns ([(t, name, direction, level, ema, close)], clean_bars)."""
    now = time.time() if now is None else now
    bars = clean_bars(bars, kind, now)
    if len(bars) < 30:
        return [], bars
    days = daily_ranges(bars, kind)
    valid = [d for d in sorted(days) if days[d][3] >= MIN_DAY_BARS and days[d][0] > days[d][1]]
    a = 2.0 / (EMA_LEN + 1)
    ema, e = [], bars[0][3]
    for _, _, _, c in bars:
        e = a * c + (1 - a) * e
        ema.append(e)
    out = []
    for i in range(max(1, len(bars) - lookback), len(bars)):
        d = session_day(bars[i][0], kind)
        prev = [x for x in valid if x < d]
        if not prev or (d - prev[-1]).days > MAX_PREV_GAP_DAYS:
            continue  # no trustworthy previous day -> no levels -> no alert
        pd = days[prev[-1]]
        for name, lv in levels(pd[0], pd[1], pd[2]).items():
            if ema[i] > lv and ema[i - 1] <= lv:
                out.append((bars[i][0], name, "UP", lv, ema[i], bars[i][3]))
            elif ema[i] < lv and ema[i - 1] >= lv:
                out.append((bars[i][0], name, "DOWN", lv, ema[i], bars[i][3]))
    return out, bars


def build_messages(label, dec, crosses):
    """One message per (candle, direction) - several levels crossed in one candle are listed together."""
    groups = {}
    for t, name, direction, lv, e, close in crosses:
        groups.setdefault((t, direction), []).append((name, lv, e, close))
    msgs = []
    for (t, direction), items in sorted(groups.items()):
        when = datetime.fromtimestamp(t + BAR_SEC, IST).strftime("%d %b %H:%M IST")
        lvls = ", ".join("%s (%.*f)" % (n, dec, lv) for n, lv, _, _ in items)
        e, close = items[0][2], items[0][3]
        msgs.append((t, direction, [n for n, _, _, _ in items],
                     "%s %s: EMA9 crossed %s %s\nCandle closed %s | close %.*f | EMA9 %.*f\n15m chart, price data may be a few minutes delayed." % (
                         direction, label, "above" if direction == "UP" else "below", lvls, when, dec, close, dec, e)))
    return msgs


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[no Telegram secrets set] " + text)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tok, data, timeout=30).read()
    return True


def save_state(sent_list):
    json.dump({"sent": sent_list[-KEEP_SENT:]}, open(STATE, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("-n", "--lookback", type=int, default=12)
    args = ap.parse_args()
    if args.test:
        telegram("Watcher test OK. You will get a message here when EMA9 crosses a Fibonacci pivot on the 15m chart.")
        return 0
    try:
        sent_list = list(json.load(open(STATE)).get("sent", []))
    except Exception:
        sent_list = []
    sent = set(sent_list)
    failed = 0
    try:
        for label, (sym, kind, dec) in SYMBOLS.items():
            try:
                crosses, bars = find_crosses(fetch(sym), kind, args.lookback)
            except Exception as ex:
                failed += 1
                print("FAILED %s: %s" % (label, ex), file=sys.stderr)
                continue
            if bars and kind == "fx" and time.time() - bars[-1][0] > STALE_HOURS * 3600 \
                    and datetime.now(NY).weekday() < 4:
                print("WARNING %s: newest closed candle is old (%s)" % (
                    label, datetime.fromtimestamp(bars[-1][0], IST)), file=sys.stderr)
            for t, direction, names, msg in build_messages(label, dec, crosses):
                keys = ["%s|%s|%s|%s" % (sym, t, n, direction) for n in names]
                if not args.dry_run and all(k in sent for k in keys):
                    continue
                if args.dry_run:
                    print(msg + "\n")
                    continue
                try:
                    telegram(msg)
                except Exception as ex:  # one failed send must not lose the state of the ones already sent
                    print("TELEGRAM FAILED %s: %s" % (label, ex), file=sys.stderr)
                    failed += 1
                    continue
                for k in keys:
                    if k not in sent:
                        sent.add(k)
                        sent_list.append(k)
    finally:
        if not args.dry_run:
            save_state(sent_list)
    return 1 if failed >= len(SYMBOLS) else 0


if __name__ == "__main__":
    sys.exit(main())
