"""Opening range breakout (ORB) backtest. The rules are fixed in advance and not tuned to the results.

Opening range: the high and low of the first 15 or 30 minutes. Trade: the first 15m candle after it that
closes beyond the range (up = long, down = short), up to 14:00, one trade per stock per day. Ranges narrower
than MIN_RANGE % or wider than MAX_RANGE % of price are skipped."""
import time

import numpy as np
import pandas as pd

from morning import exit_trade

OPEN = 9 * 60 + 15
LAST_START = 13 * 60 + 45
MIN_RANGE, MAX_RANGE = 0.3, 2.0
EXIT_NAMES = ("Hold to close", "Stop at range, no target", "Stop at range, target 2x")


def day_trades(df, nifty=None, ors=(15, 30)):
    """Trades for one stock from 15m candles, one row per (day, opening range, exit rule)."""
    rows = []
    if df.empty:
        return rows
    for day, g in df.groupby(df.index.date):
        minutes = (g.index.hour * 60 + g.index.minute).to_numpy()
        if minutes[0] != OPEN or minutes[-1] < 15 * 60:        # skip partial sessions
            continue
        o, h, l, c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        for width in ors:
            n = width // 15
            if len(g) <= n or minutes[n - 1] != OPEN + 15 * (n - 1):
                continue
            hi, lo = h[:n].max(), l[:n].min()
            if not MIN_RANGE <= (hi - lo) / ((hi + lo) / 2) * 100 <= MAX_RANGE:
                continue
            for i in range(n, len(g)):
                if minutes[i] > LAST_START:
                    break
                sign = 1 if c[i] > hi else -1 if c[i] < lo else 0
                if not sign:
                    continue
                entry, start = c[i], f"{minutes[i] // 60:02d}:{minutes[i] % 60:02d}"
                path = list(zip(o[i + 1:], h[i + 1:], l[i + 1:]))
                risk = abs(entry - (lo if sign > 0 else hi)) / entry * 100
                agree = bool(nifty is not None and nifty.get((day, start)) == sign)
                for name, stop, target in zip(EXIT_NAMES, (0, risk, risk), (0, 0, 2 * risk)):
                    gross, _ = exit_trade(entry, c[-1], path, sign, stop, target)
                    rows.append((day, width, "BULLISH" if sign > 0 else "BEARISH", agree, name, gross, risk))
                break
    return rows


def collect(symbols, fetch, nifty=None, progress=None, pause=0.25, max_fails=5):
    """fetch(symbol) -> 15m candles. Returns (all trades, skipped symbols)."""
    out, failed, streak = [], [], 0
    for i, symbol in enumerate(symbols, 1):
        try:
            rows = day_trades(fetch(symbol), nifty)
            streak = 0
            if rows:
                out += [(symbol, *r) for r in rows]
            else:
                failed.append(f"{symbol}: not enough data")
        except Exception as e:
            failed.append(f"{symbol}: {str(e)[:150]}")
            streak += 1
            if streak >= max_fails:
                raise RuntimeError(f"Test stopped after {max_fails} failures in a row. {failed[-1]}") from e
        if progress:
            progress(i, len(symbols))
        time.sleep(pause)
    cols = ["Symbol", "Date", "Range min", "Side", "NIFTY agrees", "Exit rule", "Gross %", "Risk %"]
    return pd.DataFrame(out, columns=cols), failed


def summarize(trades, cost=0.10, high_cost=0.25):
    """One row per setting. Judged on the older and newer halves of the dates separately."""
    if trades.empty:
        return pd.DataFrame()
    days = sorted(trades.Date.unique())
    older_days = set(days[:len(days) // 2])
    rows = []
    for width in sorted(trades["Range min"].unique()):
        for use_nifty in (False, True):
            for side in ("BULLISH", "BEARISH"):
                for rule in EXIT_NAMES:
                    g = trades[(trades["Range min"] == width) & (trades.Side == side) & (trades["Exit rule"] == rule)]
                    if use_nifty:
                        g = g[g["NIFTY agrees"]]
                    if g.empty:
                        continue
                    net = g["Gross %"] - cost
                    daily = net.groupby(g.Date).mean()
                    is_old = g.Date.isin(older_days)
                    t = daily.mean() / (daily.std() / np.sqrt(len(daily))) if len(daily) > 2 and daily.std() > 0 else np.nan
                    row = {"Range min": width, "NIFTY filter": "On" if use_nifty else "Off", "Side": side,
                           "Exit rule": rule, "Trades": len(g), "Days": g.Date.nunique(),
                           "Win rate %": (net > 0).mean() * 100, "Avg net %": net.mean(),
                           f"Net at {high_cost}% cost": (g["Gross %"] - high_cost).mean(),
                           "Older net %": net[is_old].mean(), "Newer net %": net[~is_old].mean(),
                           "Daily t-stat": t, "Older trades": int(is_old.sum()), "Newer trades": int((~is_old).sum())}
                    row["Passes all checks"] = bool(
                        row["Older net %"] > 0 and row["Newer net %"] > 0 and row[f"Net at {high_cost}% cost"] > 0
                        and t >= 2 and min(row["Older trades"], row["Newer trades"]) >= 100)
                    rows.append(row)
    return pd.DataFrame(rows).round(2)
