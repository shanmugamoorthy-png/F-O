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


def pick(signals, sessions=20, level=30, n=10, cost=0.10):
    """Top n per side per day. Returns (trades, daily table, summary). cost is round-trip percent."""
    days = sorted(signals.Date.unique())[-sessions:]
    data = signals[signals.Date.isin(days)]
    trades = []
    for day, g in data.groupby("Date"):
        for side, sign in (("BULLISH", 1), ("BEARISH", -1)):
            c = g[g.Score * sign >= level].assign(Strength=lambda x: x.Score * sign)
            c = c.sort_values(["Strength", "Rel volume", "Symbol"], ascending=[False, False, True]).head(n)
            if len(c):
                gross = c["Move %"] * sign
                trades.append(c.assign(Side=side, Rank=range(1, len(c) + 1), **{"Gross %": gross, "Net %": gross - cost}))
    if not trades:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    t = pd.concat(trades, ignore_index=True)
    t["Result"] = t["Net %"].map(lambda v: "Win" if v > 0 else "Loss")
    t = t[["Date", "Side", "Rank", "Symbol", "Score", "Rel volume", "Entry", "Close", "Gross %", "Net %", "Result"]]
    market = data.groupby("Date")["Move %"].mean()
    daily = t.pivot_table(index="Date", columns="Side", values="Net %", aggfunc="mean")
    daily.columns = [f"{c.title()} net %" for c in daily.columns]
    daily["All stocks move %"] = market.reindex(daily.index)
    summary = []
    for side, sign in (("BULLISH", 1), ("BEARISH", -1)):
        s = t[t.Side == side]
        if s.empty:
            continue
        per_day = s.groupby("Date")[["Gross %", "Net %"]].mean()
        base = (market.reindex(per_day.index) * sign).mean()      # same days, same direction, every stock
        summary.append({"Side": side, "Days": len(per_day), "Trades": len(s),
                        "Win rate %": (s["Net %"] > 0).mean() * 100,
                        "Avg gross %": s["Gross %"].mean(), "Avg net %": s["Net %"].mean(),
                        "Winning days": int((per_day["Net %"] > 0).sum()),
                        "Edge vs all stocks %": per_day["Gross %"].mean() - base})
    return t.round(2), daily.round(2).reset_index(), pd.DataFrame(summary).round(2)
