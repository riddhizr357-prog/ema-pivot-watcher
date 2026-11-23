#!/usr/bin/env python3
"""Regression tests for watcher.py. No dependencies.

  python test_watcher.py            offline synthetic tests (Yahoo quirks injected into clean data)
  python test_watcher.py --live     ALSO replay the last ~8 days of live data for every symbol in SYMBOLS
"""
import os, random, sys, tempfile, unittest, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import watcher as w

NY = ZoneInfo("America/New_York")


def ts(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def make_series(kind, seed=1, start=(2026, 11, 15, 17), end=(2026, 11, 23, 9)):
    """Clean random-walk 15m bars; fx = no bars between Fri 17:00 and Sun 17:00 NY (like a real fx market)."""
    rnd, px, bars = random.Random(seed), 100.0, []
    t, tend = ts(*start), ts(*end)
    while t < tend:
        dt = datetime.fromtimestamp(t, NY)
        closed = kind == "fx" and ((dt.weekday() == 4 and dt.hour >= 17) or dt.weekday() == 5
                                   or (dt.weekday() == 6 and dt.hour < 17))
        if not closed:
            o = px
            px = px + rnd.gauss(0, .15)
            bars.append((t, max(o, px) + abs(rnd.gauss(0, .05)), min(o, px) - abs(rnd.gauss(0, .05)), px))
        t += w.BAR_SEC
    return bars


def noisy(bars, kind):
    """The same data plus every quirk seen on Yahoo."""
    last = bars[-1]
    out = list(bars)
    out.append((last[0] + w.BAR_SEC, last[3] + .3, last[3] - .1, last[3] + .2))       # forming candle (real, partial)
    out.append((last[0] + w.BAR_SEC + 700, last[3] + .2, last[3] + .2, last[3] + .2))  # live-quote pseudo-bar
    out += bars[10:14]                                                                  # duplicate timestamps
    if kind == "fx":
        fri = ts(2026, 11, 20, 17, 0)
        out += [(fri, 99.9, 99.9, 99.9), (fri + 3600, 99.9, 99.9, 99.9)]               # flat weekend stub
    random.Random(7).shuffle(out)                                                       # unsorted
    out.append((last[0] - w.BAR_SEC * 5, None, None, None))
    return [b for b in out if None not in b]


class Hygiene(unittest.TestCase):
    def test_noise_never_changes_the_result(self):
        for kind in ("fx", "utc"):
            for seed in range(1, 6):
                clean = make_series(kind, seed)
                now = clean[-1][0] + w.BAR_SEC + 120  # 2 min after the last candle closed
                a, _ = w.find_crosses(clean, kind, 40, now)
                b, _ = w.find_crosses(noisy(clean, kind), kind, 40, now)
                self.assertEqual(a, b, "%s seed %d" % (kind, seed))

    def test_forming_candle_is_never_evaluated(self):
        clean = make_series("fx")
        now = clean[-1][0] + w.BAR_SEC - 5          # last candle has NOT closed yet
        _, used = w.find_crosses(clean, "fx", 4, now)
        self.assertLess(used[-1][0] + w.BAR_SEC, now + 1)
        self.assertEqual(used[-1][0], clean[-2][0])

    def test_weekend_stub_not_used_as_previous_day(self):
        clean = make_series("fx")
        bars = clean + [(ts(2026, 11, 20, 17), 99.9, 99.9, 99.9), (ts(2026, 11, 20, 17, 15), 99.9, 99.9, 99.9)]
        out, _ = w.find_crosses(bars, "fx", 200, clean[-1][0] + w.BAR_SEC + 60)
        for t, name, d, lv, e, c in out:
            self.assertNotEqual(round(lv, 3), 99.9)
        # all 7 levels equal at once is the old bug signature
        per_candle = {}
        for t, name, *_ in out:
            per_candle.setdefault(t, set()).add(name)
        self.assertTrue(all(len(v) < 5 for v in per_candle.values()))

    def test_no_previous_day_means_silence(self):
        bars = make_series("utc", start=(2026, 11, 22, 18), end=(2026, 11, 23, 9))  # only a stub of the previous UTC day
        out, _ = w.find_crosses(bars, "utc", 200, bars[-1][0] + w.BAR_SEC + 60)
        self.assertEqual(out, [])

    def test_stale_previous_day_means_silence(self):
        bars = [b for b in make_series("utc", end=(2026, 11, 23, 9))
                if not (ts(2026, 11, 17, 0) <= b[0] < ts(2026, 11, 22, 19))]  # 5-day hole in the data
        out, _ = w.find_crosses(bars, "utc", 40, bars[-1][0] + w.BAR_SEC + 60)
        self.assertEqual(out, [])

    def test_multi_level_candle_is_one_message(self):
        t0 = ts(2026, 11, 23, 9)
        crosses = [(t0, "P", "UP", 1.0, 1.2, 1.3), (t0, "R1", "UP", 1.1, 1.2, 1.3), (t0, "R2", "UP", 1.15, 1.2, 1.3)]
        msgs = w.build_messages("X", 2, crosses)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0][2], ["P", "R1", "R2"])


class Delivery(unittest.TestCase):
    def test_failed_send_still_saves_state_of_sent_ones_and_no_repeat(self):
        clean = make_series("utc")
        now_patch = clean[-1][0] + w.BAR_SEC + 60
        out, _ = w.find_crosses(clean, "utc", 60, now_patch)
        self.assertTrue(out, "fixture produced no crossings")
        tmp = tempfile.mkdtemp()
        w.STATE = os.path.join(tmp, "state.json")
        sent_msgs, calls = [], {"n": 0}

        def fake_tg(text):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("telegram down")
            sent_msgs.append(text)

        old = (w.fetch, w.telegram, w.SYMBOLS, w.time.time, sys.argv)
        try:
            w.fetch, w.telegram = (lambda sym: clean), fake_tg
            w.SYMBOLS = {"T": ("T", "utc", 2)}
            w.time.time = lambda: now_patch
            sys.argv = ["watcher.py", "-n", "60"]
            w.main()
            first = list(sent_msgs)
            self.assertTrue(json.load(open(w.STATE))["sent"])
            calls["n"] = 99  # telegram healthy again
            calls["n"] = 0
            sent_msgs.clear()
            w.main()
            for m in sent_msgs:
                self.assertNotIn(m, first)  # nothing already delivered is delivered twice
        finally:
            w.fetch, w.telegram, w.SYMBOLS, w.time.time, sys.argv = old


def live_audit():
    """Replay history candle by candle for every configured symbol, the way GitHub Actions would have seen it
    (plus a forming candle and a pseudo-bar at the end). Every alert it would have sent must be a real
    crossing of the final cleaned data, and no real crossing may be missed."""
    import time
    bad = 0
    for label, (sym, kind, dec) in w.SYMBOLS.items():
        raw = w.fetch(sym)
        full, bars = w.find_crosses(raw, kind, 10 ** 6, time.time())
        truth = {(t, n, d) for t, n, d, *_ in full}
        flat = [(t, n) for t, n, d, lv, *_ in full if False]
        start = max(60, len(bars) - 600)
        got = set()
        for i in range(start, len(bars)):
            now = bars[i][0] + w.BAR_SEC + 120
            hist = [b for b in raw if b[0] <= bars[i][0]]
            nxt = bars[i][0] + w.BAR_SEC
            hist += [(nxt, hist[-1][3] + 1, hist[-1][3] - 1, hist[-1][3] + .5),          # forming candle, partial
                     (nxt + 600, hist[-1][3] + .5, hist[-1][3] + .5, hist[-1][3] + .5)]   # live pseudo-bar
            for t, n, d, *_ in w.find_crosses(hist, kind, 1, now)[0]:
                got.add((t, n, d))
        window = {k for k in truth if k[0] >= bars[start][0]}
        spurious, missed = got - truth, window - got
        # level sanity: never more than 4 different levels crossed by one candle (the 7-at-once bug signature)
        per = {}
        for t, n, d in got:
            per.setdefault(t, set()).add(n)
        flat_days = max((len(v) for v in per.values()), default=0)
        ok = not spurious and not missed and flat_days <= 4
        bad += not ok
        print("%-22s replayed %3d candles | real %2d alerts | spurious %d | missed %d | max levels/candle %d | %s" % (
            label, len(bars) - start, len(window), len(spurious), len(missed), flat_days, "OK" if ok else "FAIL"))
        for x in sorted(spurious)[:3]:
            print("   spurious:", datetime.fromtimestamp(x[0] + w.BAR_SEC, w.IST), x[1:])
        for x in sorted(missed)[:3]:
            print("   missed:  ", datetime.fromtimestamp(x[0] + w.BAR_SEC, w.IST), x[1:])
    return bad


if __name__ == "__main__":
    live = "--live" in sys.argv
    if live:
        sys.argv.remove("--live")
    res = unittest.main(exit=False, verbosity=1).result
    rc = 0 if res.wasSuccessful() else 1
    if live:
        print("\nLive replay audit (all symbols):")
        rc |= 1 if live_audit() else 0
    sys.exit(rc)
