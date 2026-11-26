"""Rank F&O stocks by the same price action score used on the chart."""
import time

import pandas as pd

import analysis as an
import dhan


def score_stock(df, threshold=30, now=None):
    """One result row from intraday candles, or None if there is too little history."""
    if df.empty or len(set(df.index.date)) < 3:
        return None
    view, _ = an.current_view(an.add_features(df), now, dhan.SESSION_END["NSE_EQ"])
    last = view.iloc[-1]
    days = pd.Series(df.index.date, index=df.index)
    closes = df.close.groupby(days).last()
    # Volume of the latest session against earlier sessions up to the same time of day
    upto = df[(df.index.hour * 60 + df.index.minute) <= (df.index[-1].hour * 60 + df.index[-1].minute)]
    volume = upto.volume.groupby(upto.index.date).sum()
    usual = volume.iloc[:-1].mean()
    return {"Score": int(round(last.score)), "Bias": an.bias_of(last.score, threshold),
            "Close": round(float(last.close), 2),
            "Day %": round(float(closes.iloc[-1] / closes.iloc[-2] - 1) * 100, 2),
            "Rel volume": round(float(volume.iloc[-1] / usual), 2) if usual > 0 else 0.0,
            "PDH": round(float(last.pdh), 2), "PDL": round(float(last.pdl), 2),
            "As of": df.index[-1]}


def scan(symbols, fetch, threshold=30, progress=None, pause=0.25, max_fails=5):
    """fetch(symbol) -> candles. pause keeps requests under Dhan's per-second limit."""
    rows, failed, streak = [], [], 0
    for i, symbol in enumerate(symbols, 1):
        try:
            row = score_stock(fetch(symbol), threshold)
            streak = 0
            if row:
                rows.append({"Symbol": symbol, **row})
            else:
                failed.append(f"{symbol}: not enough data")
        except Exception as e:
            failed.append(f"{symbol}: {str(e)[:150]}")
            streak += 1
            if streak >= max_fails:
                raise RuntimeError(f"Scan stopped after {max_fails} failures in a row. {failed[-1]}") from e
        if progress:
            progress(i, len(symbols))
        time.sleep(pause)
    table = pd.DataFrame(rows)
    if len(table):
        latest = table["As of"].dt.date.max()
        stale = table["As of"].dt.date < latest
        failed += [f"{s}: no candles for {latest:%d %b}" for s in table[stale].Symbol]
        table = table[~stale]
    return table, failed


def top(table, bias, n=10):
    """Strongest scores first; ties go to the higher relative volume."""
    if table.empty:
        return table
    picked = table[table.Bias == bias]
    picked = picked.sort_values(["Score", "Rel volume"], ascending=[bias == "BEARISH", False]).head(n)
    out = picked.drop(columns=["As of", "Bias"])
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out
