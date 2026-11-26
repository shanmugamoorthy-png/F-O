"""Price action analysis. Every score uses only data available at that candle (no look-ahead)."""
import numpy as np
import pandas as pd

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample(df, minutes):
    """Build N-minute candles anchored at 09:15 for each session."""
    if df.empty:
        return df
    since_open = (df.index.hour * 60 + df.index.minute) - (9 * 60 + 15)
    start = df.index.normalize() + pd.to_timedelta(9 * 60 + 15 + since_open // minutes * minutes, unit="min")
    return df.groupby(start).agg(AGG)


def pivot_levels(high, low, close):
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * pivot - bc
    return dict(pivot=pivot, cpr_low=np.minimum(bc, tc), cpr_high=np.maximum(bc, tc),
                r1=2 * pivot - low, s1=2 * pivot - high)


def session_closed(last_candle, now=None, end=(15, 30)):
    """True when the session of the latest candle has ended, so levels apply to the next session."""
    now = now or pd.Timestamp.now(tz="Asia/Kolkata")
    return last_candle.date() < now.date() or (now.hour, now.minute) >= end


def current_view(d, now=None, end=(15, 30)):
    """Copy of d for the live estimate. After the close, the last row gets next-session levels.
    Historical rows are untouched. Needs at least two sessions."""
    closed = session_closed(d.index[-1], now, end)
    view = d.copy()
    if closed:
        s = d.groupby(d.index.date).agg(AGG)
        a, b = s.iloc[-2], s.iloc[-1]
        new = dict(pdh=b.high, pdl=b.low, pdc=b.close, pdh2=a.high, pdl2=a.low, pdc2=a.close,
                   **pivot_levels(b.high, b.low, b.close))
        for name, value in new.items():
            view.loc[view.index[-1], name] = value
        view["score"] = score(view)
    return view, closed


def add_features(df):
    d = df.copy()
    day = pd.Series(d.index.date, index=d.index)
    c, h, l, o = d.close, d.high, d.low, d.open
    d["ema9"] = c.ewm(span=9, adjust=False).mean()
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = (100 * up / (up + dn)).fillna(50)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    # VWAP; indices have no volume, so use the running average of typical price instead
    tp = (h + l + c) / 3
    vol = d.volume if d.volume.sum() > 0 else pd.Series(1.0, index=d.index)
    d["vwap"] = (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum().replace(0, np.nan)

    # Levels from the previous two sessions
    s = d.groupby(day).agg(AGG)
    lv = pd.DataFrame({"pdh": s.high.shift(1), "pdl": s.low.shift(1), "pdc": s.close.shift(1),
                       "pdc2": s.close.shift(2), "pdh2": s.high.shift(2), "pdl2": s.low.shift(2)})
    for name, value in pivot_levels(lv.pdh, lv.pdl, lv.pdc).items():
        lv[name] = value
    for col in lv.columns:
        d[col] = day.map(lv[col])

    # Candle patterns
    body, rng = (c - o).abs(), (h - l).replace(0, np.nan)
    upper, lower = h - np.maximum(o, c), np.minimum(o, c) - l
    po, pc = o.shift(), c.shift()
    d["pattern"] = np.select(
        [(c > o) & (pc < po) & (c >= po) & (o <= pc),
         (c < o) & (pc > po) & (c <= po) & (o >= pc),
         (lower > 2 * body) & (upper < body) & (body > 0),
         (upper > 2 * body) & (lower < body) & (body > 0),
         body / rng < 0.1,
         body / rng > 0.8],
        ["Bullish engulfing", "Bearish engulfing", "Hammer", "Shooting star", "Doji", "Marubozu"], default="")
    d["score"] = score(d)
    return d


def factors(d):
    """Each factor is -1, 0 or +1. Weights add up to 100."""
    c = d.close
    mom = (c - c.shift(3)) / d.atr
    two = np.sign(d.pdc - d.pdc2) + np.sign((d.pdh - d.pdh2) + (d.pdl - d.pdl2))
    return {
        "EMA 9 vs EMA 20": (20, np.sign(d.ema9 - d.ema20)),
        "Price vs VWAP": (20, np.sign(c - d.vwap)),
        "Previous day high/low break": (15, np.where(c > d.pdh, 1, np.where(c < d.pdl, -1, 0))),
        "Price vs pivot": (10, np.sign(c - d["pivot"])),
        "Last 2 sessions structure": (15, np.sign(two)),
        "RSI zone": (10, np.where(d.rsi > 55, 1, np.where(d.rsi < 45, -1, 0))),
        "3-candle momentum": (10, np.where(mom > 0.5, 1, np.where(mom < -0.5, -1, 0))),
    }


def score(d):
    total = sum(w * np.nan_to_num(np.asarray(v, dtype=float)) for w, v in factors(d).values())
    return pd.Series(total, index=d.index)


def bias_of(value, threshold=30):
    return "BULLISH" if value >= threshold else "BEARISH" if value <= -threshold else "SIDEWAYS"


def backtest(d, horizon=3, threshold=30):
    """How often did the signal's direction match the close `horizon` candles later (same session only)?"""
    day = pd.Series(d.index.date, index=d.index)
    move = d.close.groupby(day).shift(-horizon) - d.close
    valid = move.notna() & d.pdc2.notna()
    out = {}
    for name, mask, sign in (("BULLISH", d.score >= threshold, 1), ("BEARISH", d.score <= -threshold, -1)):
        m = move[valid & mask] * sign
        base = (move[valid] * sign > 0).mean() if valid.any() else np.nan
        out[name] = dict(signals=int(len(m)), hit=float((m > 0).mean()) if len(m) else np.nan,
                         base=float(base), avg=float(m.mean()) if len(m) else np.nan)
    return out


def session_summary(g):
    o, h, l, c = g.open.iloc[0], g.high.max(), g.low.min(), g.close.iloc[-1]
    pos = (c - l) / (h - l) if h > l else 0.5
    first = g[g.index < g.index[0] + pd.Timedelta(minutes=60)]
    kind = "trend up" if c > o and pos > 0.7 else "trend down" if c < o and pos < 0.3 else "range / two-sided"
    broke = ("broke above" if c > first.high.max() else "broke below" if c < first.low.min() else "stayed inside")
    return (f"**{g.index[0]:%a %d %b}** — {kind}. O {o:,.2f} · H {h:,.2f} · L {l:,.2f} · C {c:,.2f} "
            f"({(c / o - 1) * 100:+.2f}% from open). Closed at {pos:.0%} of the day's range and {broke} "
            f"the first-hour range.")


def report(d, horizon=3, threshold=30, now=None, end=(15, 30)):
    """Commentary, scenarios and an illustrative projected path from the latest candle."""
    view, closed = current_view(d, now, end)
    last = view.iloc[-1]
    day = pd.Series(d.index.date, index=d.index)
    sessions = [g for _, g in d.groupby(day)]
    if len(sessions) < 3:
        raise ValueError("Need at least 3 sessions of intraday data.")
    prev = sessions[-2:] if closed else sessions[-3:-1]
    c, atr, val = last.close, last.atr, float(last.score)
    bias = bias_of(val, threshold)

    lines = ["#### Previous 2 intraday sessions"] + [f"- {session_summary(g)}" for g in prev]
    a, b = prev
    shape = ("higher high and higher low (up structure)" if b.high.max() > a.high.max() and b.low.min() > a.low.min()
             else "lower high and lower low (down structure)" if b.high.max() < a.high.max() and b.low.min() < a.low.min()
             else "inside day (compression)" if b.high.max() <= a.high.max() and b.low.min() >= a.low.min()
             else "outside / mixed day")
    lines.append(f"- Day-over-day: {shape}.")

    where = ("above the previous high" if c > last.pdh else "below the previous low" if c < last.pdl
             else "inside the previous range")
    if closed:
        lines += [f"#### Setup for the next session",
                  f"- Levels below come from {d.index[-1]:%d %b}. VWAP, EMA, RSI and patterns are as of that close. "
                  "An opening gap is not predicted."]
    else:
        lines += [f"#### Live session {d.index[-1]:%d %b}", f"- {session_summary(sessions[-1])}"]
    lines += [f"- Last price **{c:,.2f}** is {where}, {'above' if c > last.vwap else 'below'} VWAP ({last.vwap:,.2f}).",
              f"- EMA9 {'>' if last.ema9 > last.ema20 else '<'} EMA20 · RSI {last.rsi:.0f} · ATR {atr:,.2f}.",
              f"- Levels: R1 {last.r1:,.2f} · CPR {last.cpr_low:,.2f}–{last.cpr_high:,.2f} · S1 {last.s1:,.2f} · "
              f"PDH {last.pdh:,.2f} · PDL {last.pdl:,.2f}."]
    recent = d[d.pattern != ""].tail(3)
    if len(recent):
        lines.append("- Recent patterns: " + ", ".join(f"{p} ({t:%d %b %H:%M})" for t, p in recent.pattern.items()))

    levels = sorted({float(x) for x in (last.pdh, last.pdl, last.pdh2, last.pdl2, last["pivot"], last.r1, last.s1,
                                        sessions[-1].high.max(), sessions[-1].low.min(),
                                        np.nan if closed else last.vwap)      # VWAP resets at the next open
              if pd.notna(x)})
    res = next((x for x in levels if x > c + 0.25 * atr), c + atr)
    sup = next((x for x in reversed(levels) if x < c - 0.25 * atr), c - atr)
    res2 = next((x for x in levels if x > res + 0.25 * atr), res + atr)
    sup2 = next((x for x in reversed(levels) if x < sup - 0.25 * atr), sup - atr)
    lines += [f"#### Next move estimate ({'first' if closed else 'next'} {horizon} candles): **{bias}** (score {val:+.0f} of ±100)",
              f"- Up case: candle close above {res:,.2f} → {res2:,.2f}; invalid below {sup:,.2f}.",
              f"- Down case: candle close below {sup:,.2f} → {sup2:,.2f}; invalid above {res:,.2f}.",
              f"- Range case: while between {sup:,.2f} and {res:,.2f}, no breakout is confirmed."]

    # Projected path: capped at the nearest level, size scales with score. Illustration only.
    drift = np.clip(val / 100, -1, 1) * atr * 0.5 if bias != "SIDEWAYS" else 0.0
    path, p = [], c
    for _ in range(horizon):
        q = min(max(p + drift, sup), res)
        path.append((p, max(p, q) + atr * 0.35, min(p, q) - atr * 0.35, q))
        p = q
    table = pd.DataFrame([(n, w, int(np.nan_to_num(np.asarray(v, dtype=float))[-1])) for n, (w, v) in factors(view).items()],
                         columns=["Factor", "Weight", "Signal"])
    return dict(closed=closed, levels={k: float(last[k]) for k in ("pdh", "pdl", "pivot")}, bias=bias, score=val, text="\n".join(lines), path=path, factors=table,
                support=sup, resistance=res, backtest=backtest(d, horizon, threshold))
