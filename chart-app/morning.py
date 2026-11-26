"""Backtest: pick the top scores at a fixed time each day and hold to that day's last continuous candle."""
import time

import pandas as pd

import analysis as an

CANDLE = 15  # minutes


def day_signals(df, entry="10:15"):
    """One row per past session for one stock. df: 15m candles. Score uses only candles closed by `entry`."""
    if df.empty:
        return pd.DataFrame()
    d = an.add_features(df)
    day = d.index.normalize()
    signal_start = pd.Timestamp(f"2000-01-01 {entry}") - pd.Timedelta(minutes=CANDLE)
    at = (d.index.hour == signal_start.hour) & (d.index.minute == signal_start.minute)
    # Volume up to the signal time against the earlier sessions at the same time
    early = d.volume.where(d.index.hour * 60 + d.index.minute <= signal_start.hour * 60 + signal_start.minute, 0)
    volume = early.groupby(day).sum()
    relative = volume / volume.shift(1).rolling(13, min_periods=2).mean()
    last = d.groupby(day).tail(1)
    last = last[(last.index.hour * 60 + last.index.minute) >= 15 * 60]     # complete sessions only
    exits = pd.Series(last.close.values, index=last.index.normalize())

    rows = d[at & d.pdc2.notna()]
    key = rows.index.normalize()
    out = pd.DataFrame({"Date": key.date, "Score": rows.score.round().astype(int).values,
                        "Rel volume": relative.reindex(key).fillna(0).round(2).values,
                        "Entry": rows.close.values, "Close": exits.reindex(key).values})
    # (open, high, low) of every candle after entry, needed to test stops and targets
    after = {t: [] for t in key}
    for t, g in d.groupby(day):
        if t in after:
            rest = g[g.index > rows.index[key.get_loc(t)]]
            after[t] = list(zip(rest.open, rest.high, rest.low))
    out["Path"] = [after[t] for t in key]
    # for breakout entries: previous day levels, and candles from the signal candle onward
    out["PDH"], out["PDL"] = rows.pdh.values, rows.pdl.values
    bars = {t: [] for t in key}
    for t, g in d.groupby(day):
        if t in bars:
            rest = g[g.index >= rows.index[key.get_loc(t)]]
            bars[t] = list(zip(rest.index.strftime("%H:%M"), rest.open, rest.high, rest.low, rest.close))
    out["Bars"] = [bars[t] for t in key]
    out = out.dropna(subset=["Close"])
    out["Move %"] = (out.Close / out.Entry - 1) * 100
    return out


def collect(symbols, fetch, entry="10:15", progress=None, pause=0.25, max_fails=5):
    """fetch(symbol) -> 15m candles. Returns all stock-day rows plus skipped symbols."""
    frames, failed, streak = [], [], 0
    for i, symbol in enumerate(symbols, 1):
        try:
            rows = day_signals(fetch(symbol), entry)
            streak = 0
            if len(rows):
                frames.append(rows.assign(Symbol=symbol))
            else:
                failed.append(f"{symbol}: not enough data")
        except Exception as e:
            failed.append(f"{symbol}: {str(e)[:150]}")
            streak += 1
            if streak >= max_fails:
                raise RuntimeError(f"Backtest stopped after {max_fails} failures in a row. {failed[-1]}") from e
        if progress:
            progress(i, len(symbols))
        time.sleep(pause)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), failed


def exit_trade(entry, close, path, sign, stop=0.0, target=0.0):
    """Gross % and exit reason. sign +1 long, -1 short; stop/target in percent, 0 = off.
    A gap through a level fills at the candle open. If one candle touches both, the stop counts."""
    for o, h, l in path:
        opened = (o / entry - 1) * 100 * sign
        worst = ((l if sign > 0 else h) / entry - 1) * 100 * sign
        best = ((h if sign > 0 else l) / entry - 1) * 100 * sign
        if stop and opened <= -stop:
            return opened, "Stop"
        if target and opened >= target:
            return opened, "Target"
        if stop and worst <= -stop:
            return -stop, "Stop"
        if target and best >= target:
            return target, "Target"
    return (close / entry - 1) * 100 * sign, "Close"


def pick(signals, sessions=20, level=30, n=10, cost=0.10, stop=0.0, target=0.0, min_rel=0.0):
    """Top n per side per day. Returns (trades, daily table, summary). cost is round-trip percent."""
    days = sorted(signals.Date.unique())[-sessions:]
    data = signals[signals.Date.isin(days)]
    trades = []
    for day, g in data.groupby("Date"):
        for side, sign in (("BULLISH", 1), ("BEARISH", -1)):
            c = g[(g.Score * sign >= level) & (g["Rel volume"] >= min_rel)].assign(Strength=lambda x: x.Score * sign)
            c = c.sort_values(["Strength", "Rel volume", "Symbol"], ascending=[False, False, True]).head(n)
            if len(c):
                done = [exit_trade(r.Entry, r.Close, r.Path, sign, stop, target) for r in c.itertuples()]
                gross = pd.Series([x[0] for x in done], index=c.index)
                trades.append(c.assign(Side=side, Rank=range(1, len(c) + 1), Exit=[x[1] for x in done],
                                       **{"Gross %": gross, "Net %": gross - cost, "Hold %": c["Move %"] * sign,
                                          "All stocks %": g["Move %"].mean() * sign}))
    if not trades:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    t = pd.concat(trades, ignore_index=True)
    t["Result"] = t["Net %"].map(lambda v: "Win" if v > 0 else "Loss")
    t = t[["Date", "Side", "Rank", "Symbol", "Score", "Rel volume", "Entry", "Close", "Exit", "Gross %", "Net %",
           "Result", "Hold %", "All stocks %"]]
    market = data.groupby("Date")["Move %"].mean()
    daily = t.pivot_table(index="Date", columns="Side", values="Net %", aggfunc="mean")
    daily.columns = [f"{c.title()} net %" for c in daily.columns]
    daily["All stocks move %"] = market.reindex(daily.index)
    summary = []
    for side, sign in (("BULLISH", 1), ("BEARISH", -1)):
        s = t[t.Side == side]
        if s.empty:
            continue
        per_day = s.groupby("Date")[["Gross %", "Net %", "Hold %", "All stocks %"]].mean()
        summary.append({"Side": side, "Days": len(per_day), "Trades": len(s),
                        "Win rate %": (s["Net %"] > 0).mean() * 100,
                        "Avg gross %": s["Gross %"].mean(), "Avg net %": s["Net %"].mean(),
                        "Winning days": int((per_day["Net %"] > 0).sum()),
                        "Stopped %": (s.Exit == "Stop").mean() * 100,
                        # selection edge, held to close: picks against every stock, same days and direction
                        "Edge vs all stocks %": (per_day["Hold %"] - per_day["All stocks %"]).mean()})
    return t.round(2), daily.round(2).reset_index(), pd.DataFrame(summary).round(2)


STOPS = (0.0, 0.5, 1.0, 1.5, 2.0)
MIN_REL = (0.0, 1.5, 2.0, 4.0)


def compare(signals, sessions=50, level=30, cost=0.10, target=0.0):
    """Every stop x relative-volume setting, scored separately on the older and newer half of the dates."""
    days = sorted(signals.Date.unique())[-sessions:]
    half = len(days) // 2
    older, newer = signals[signals.Date.isin(days[:half])], signals[signals.Date.isin(days[half:])]
    rows = []
    for stop in STOPS:
        for rel in MIN_REL:
            result = {}
            for name, data in (("Older", older), ("Newer", newer)):
                trades = pick(data, len(days), level, cost=cost, stop=stop, target=target, min_rel=rel)[0]
                result[name] = trades
            for side in ("BULLISH", "BEARISH"):
                row = {"Side": side, "Stop %": stop, "Min rel volume": rel}
                for name, trades in result.items():
                    s = trades[trades.Side == side] if len(trades) else trades
                    row[f"{name} net %"] = s["Net %"].mean() if len(s) else float("nan")
                    row[f"{name} trades"] = len(s)
                rows.append(row)
    out = pd.DataFrame(rows)
    # Judged on both halves; thin samples never qualify
    out["Positive in both"] = ((out["Older net %"] > 0) & (out["Newer net %"] > 0)
                               & (out[["Older trades", "Newer trades"]].min(axis=1) >= 30))
    return out.round(2)


RISK_STOPS = (0.5, 0.75, 1.0, 1.5, 2.0)


def risk_reward(signals, sessions=50, level=30, cost=0.10, ratio=2.0, min_rel=0.0):
    """Stop S with target ratio*S, per side, on all dates and on the older and newer halves."""
    days = sorted(signals.Date.unique())[-sessions:]
    half = len(days) // 2
    parts = (("All", days), ("Older", days[:half]), ("Newer", days[half:]))
    rows = []
    for stop in RISK_STOPS:
        result = {name: pick(signals[signals.Date.isin(d)], len(d), level, cost=cost, stop=stop,
                             target=stop * ratio, min_rel=min_rel)[0] for name, d in parts if len(d)}
        for side in ("BULLISH", "BEARISH"):
            s = {n: (t[t.Side == side] if len(t) else t) for n, t in result.items()}
            a = s.get("All", pd.DataFrame())
            if a.empty:
                continue
            row = {"Side": side, "Stop %": stop, "Target %": stop * ratio, "Trades": len(a),
                   "Target hit %": (a.Exit == "Target").mean() * 100, "Stop hit %": (a.Exit == "Stop").mean() * 100,
                   "Closed %": (a.Exit == "Close").mean() * 100, "Win rate %": (a["Net %"] > 0).mean() * 100,
                   "Avg net %": a["Net %"].mean()}
            for n in ("Older", "Newer"):
                row[f"{n} net %"] = s[n]["Net %"].mean() if n in s and len(s[n]) else float("nan")
            row["Positive in both"] = bool(row["Older net %"] > 0 and row["Newer net %"] > 0)
            rows.append(row)
    return pd.DataFrame(rows).round(2)


def nifty_state(df):
    """{(date, candle start): +1 / -1 / 0}. +1: NIFTY above both its VWAP and the day's open; -1: below both."""
    if df.empty:
        return {}
    d = an.add_features(df)
    day_open = d.open.groupby(d.index.date).transform("first")
    up = (d.close > d.vwap) & (d.close > day_open)
    down = (d.close < d.vwap) & (d.close < day_open)
    return {(t.date(), t.strftime("%H:%M")): int(u) - int(w) for t, u, w in zip(d.index, up, down)}


def breakout_entry(bars, sign, pdh, pdl, date, nifty=None, last_start="13:45"):
    """First candle that closes beyond PDH (long) or PDL (short), with NIFTY agreeing if given.
    Returns (candle start, entry price, later (open, high, low) candles) or None."""
    for i, (start, _, _, _, close) in enumerate(bars):
        if start > last_start:
            break
        broke = close > pdh if sign > 0 else close < pdl
        if broke and (nifty is None or nifty.get((date, start)) == sign):
            return start, close, [(o, h, l) for _, o, h, l, _ in bars[i + 1:]]
    return None


EXITS = ((0.0, 0.0), (0.5, 1.0), (0.75, 1.5), (1.0, 2.0), (1.5, 3.0), (2.0, 4.0))


def breakout_test(signals, nifty, sessions=50, level=30, cost=0.10, min_rel=0.0):
    """Watch the top 10 picked at the signal time; trade only on a breakout. Returns (summary, trades)."""
    days = sorted(signals.Date.unique())[-sessions:]
    half = len(days) // 2
    picks = pick(signals, sessions, level, cost=cost, min_rel=min_rel)[0]
    if picks.empty:
        return pd.DataFrame(), pd.DataFrame()
    watch = picks[["Date", "Side", "Rank", "Symbol", "Score"]].merge(
        signals[["Date", "Symbol", "Close", "PDH", "PDL", "Bars"]], on=["Date", "Symbol"])
    trades = []
    for use_nifty in (False, True):
        entries = []
        for r in watch.itertuples():
            sign = 1 if r.Side == "BULLISH" else -1
            found = breakout_entry(r.Bars, sign, r.PDH, r.PDL, r.Date, nifty if use_nifty else None)
            if found:
                entries.append((r, sign, *found))
        for stop, target in EXITS:
            for r, sign, start, price, path in entries:
                gross, why = exit_trade(price, r.Close, path, sign, stop, target)
                trades.append({"NIFTY filter": "On" if use_nifty else "Off", "Side": r.Side, "Stop %": stop,
                               "Target %": target, "Date": r.Date, "Symbol": r.Symbol, "Entry candle": start,
                               "Entry": price, "Exit": why, "Net %": gross - cost})
    t = pd.DataFrame(trades)
    if t.empty:
        return pd.DataFrame(), t
    rows = []
    for keys, g in t.groupby(["NIFTY filter", "Side", "Stop %", "Target %"]):
        older, newer = g[g.Date.isin(days[:half])], g[g.Date.isin(days[half:])]
        rows.append({**dict(zip(["NIFTY filter", "Side", "Stop %", "Target %"], keys)),
                     "Trades": len(g), "Days traded": g.Date.nunique(),
                     "Target hit %": (g.Exit == "Target").mean() * 100, "Stop hit %": (g.Exit == "Stop").mean() * 100,
                     "Win rate %": (g["Net %"] > 0).mean() * 100, "Avg net %": g["Net %"].mean(),
                     "Older trades": len(older), "Older net %": older["Net %"].mean(),
                     "Newer trades": len(newer), "Newer net %": newer["Net %"].mean()})
    out = pd.DataFrame(rows)
    out["Positive in both"] = ((out["Older net %"] > 0) & (out["Newer net %"] > 0)
                               & (out[["Older trades", "Newer trades"]].min(axis=1) >= 30))
    return out.round(2), t.round(2)
