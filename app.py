"""F&O stock scanner (research). Same price model as the NIFTY dashboard, applied to every F&O stock.
Step 1: download 5-min history. Step 2: backtest. (Step 3, only if the backtest passes: live Telegram alerts.)"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.dhan.co/v2"
CID = os.getenv("DHAN_CLIENT_ID")
TOKEN = os.getenv("DHAN_ACCESS_TOKEN")
DB = os.getenv("DB_PATH", "fno.db")
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip().strip("\"'").strip()
if TG_TOKEN.lower().startswith("bot"):
    TG_TOKEN = TG_TOKEN[3:]
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip().strip("\"'").strip()
LOCK = threading.Lock()


def db():
    return sqlite3.connect(DB, check_same_thread=False)


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        return False, "TG_TOKEN / TG_CHAT_ID not set in Render"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
        return r.ok, f"HTTP {r.status_code} {'' if r.ok else r.text[:120]}"
    except Exception as e:
        return False, str(e).replace(TG_TOKEN, "***")


@st.cache_resource
def hist_state():
    return {"running": False, "done": 0, "total": 0, "msg": "", "rows": 0, "errs": [], "probe": []}


def hist_post(body, nocid=False, path="/charts/rollingoption"):
    hd = {"access-token": TOKEN, "client-id": CID, "Content-Type": "application/json", "Accept": "application/json"}
    if nocid:
        hd.pop("client-id")
    for _ in range(4):
        r = requests.post(BASE + path, json=body, timeout=40, headers=hd)
        if r.status_code != 429:
            break
        time.sleep(3)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:260]}")
    return r.json()


def fno_ids():
    """NSE equity security ids of every stock that has stock options (F&O), read from Dhan's public instrument list."""
    import csv, re
    r = requests.get("https://images.dhan.co/api-data/api-scrip-master.csv", stream=True, timeout=180)
    rd = csv.reader(x.decode("utf-8", "ignore") for x in r.iter_lines())
    hd = next(rd)
    ix = {n: hd.index(n) for n in ("SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL", "SEM_SERIES")}
    top, fno, eq = max(ix.values()), set(), {}
    for row in rd:
        if len(row) <= top or row[ix["SEM_EXM_EXCH_ID"]] != "NSE":
            continue
        sym = row[ix["SEM_TRADING_SYMBOL"]]
        if row[ix["SEM_INSTRUMENT_NAME"]] == "OPTSTK":
            m = re.match(r"^(.+?)-(\d{1,2}[A-Za-z]{3}\d{4}|[A-Za-z]{3}\d{4})-", sym)
            if m:
                fno.add(m.group(1))
        elif row[ix["SEM_SEGMENT"]] == "E" and row[ix["SEM_SERIES"]] == "EQ":
            eq[sym] = row[ix["SEM_SMST_SECURITY_ID"]]
    return {k: eq[k] for k in sorted(fno) if k in eq}


def fno_job(days):
    """Research download: 5-min closes of every F&O stock, stored compactly (one row per stock per day)."""
    h = hist_state()
    h.update(running=True, done=0, total=0, msg="looking up F&O stocks...", rows=0, errs=[], probe=[])
    try:
        ids = fno_ids()
        h["probe"].append(f"{len(ids)} F&O stocks found, e.g. " + ", ".join(list(ids)[:6]))
        with LOCK:
            c = db()
            c.execute("CREATE TABLE IF NOT EXISTS fnod(sym TEXT, day TEXT, b BLOB, PRIMARY KEY(sym, day))")
            # fnov = the same 5-min bars' HIGH, LOW and VOLUME (3 x 75 float32 per stock per day). Days count as
            # downloaded only when they are in fnov too, so one full run fills volume for the old history.
            c.execute("CREATE TABLE IF NOT EXISTS fnov(sym TEXT, day TEXT, b BLOB, PRIMARY KEY(sym, day))")
            have = {}
            for sy, dd in c.execute("SELECT sym, day FROM fnov"):
                have.setdefault(sy, set()).add(dd)
            c.commit()
            c.close()
        now_ = datetime.now(IST)
        today = now_.date()
        small = days <= 15                                      # daily top-up: fill every missing day, today too once the market is over
        lim = today + timedelta(days=1) if small and now_.hour * 60 + now_.minute >= 935 else today
        a0, wins = today - timedelta(days=days), []
        while a0 <= today:
            wins.append((a0, min(a0 + timedelta(days=85), today + timedelta(days=1))))
            a0 += timedelta(days=85)
        h["total"], bad, n = len(ids) * len(wins), 0, 0
        for sy, sid in ids.items():
            for a, b in wins:
                n += 1
                h["done"], h["msg"] = n, f"{n}/{h['total']} requests ({sy})"
                wd = [x for x in (a + timedelta(days=k) for k in range((b - a).days)) if x.weekday() < 5 and x < lim]
                if wd and sum(x.strftime("%Y-%m-%d") in have.get(sy, ()) for x in wd) >= (0.999 if small else 0.8) * len(wd):
                    continue
                body = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "interval": "5", "oi": False,
                        "fromDate": a.strftime("%Y-%m-%d"), "toDate": b.strftime("%Y-%m-%d")}
                try:
                    j = hist_post(body, path="/charts/intraday")
                    j = j.get("data") or j
                    ts, cl = j.get("timestamp") or [], j.get("close") or []
                    hi, lo, vo = j.get("high") or [], j.get("low") or [], j.get("volume") or []
                    if ts:
                        off = 315532800 if datetime.fromtimestamp(ts[0], IST).year < 2020 else 0
                        dd, dv = {}, {}
                        for i_, (t_, v_) in enumerate(zip(ts, cl)):
                            x = datetime.fromtimestamp(t_ + off, IST)
                            k = (x.hour * 60 + x.minute - 555) // 5
                            if x.weekday() < 5 and 0 <= k < 75:
                                dd.setdefault(f"{x:%Y-%m-%d}", np.full(75, np.nan, np.float32))[k] = v_
                                hv = dv.setdefault(f"{x:%Y-%m-%d}", np.full((3, 75), np.nan, np.float32))
                                hv[0, k] = hi[i_] if i_ < len(hi) and hi[i_] is not None else np.nan
                                hv[1, k] = lo[i_] if i_ < len(lo) and lo[i_] is not None else np.nan
                                hv[2, k] = vo[i_] if i_ < len(vo) and vo[i_] is not None else np.nan
                        rows = [(sy, k_, v_.tobytes()) for k_, v_ in dd.items() if np.isfinite(v_).sum() >= 70]
                        rowv = [(sy, k_, dv[k_].tobytes()) for k_, v_ in dd.items() if np.isfinite(v_).sum() >= 70]
                        with LOCK:
                            c = db()
                            c.executemany("INSERT OR REPLACE INTO fnod VALUES(?,?,?)", rows)
                            c.executemany("INSERT OR REPLACE INTO fnov VALUES(?,?,?)", rowv)
                            c.commit()
                            c.close()
                        h["rows"] += len(rows)
                    bad = 0
                except Exception as e:
                    bad += 1
                    if len(h["errs"]) < 5:
                        h["errs"].append(f"{sy} {a}: {str(e)[:160]}")
                    if "401" in str(e) or "DH-906" in str(e) or bad >= 60:
                        h["msg"] = "stopped: token problem or too many errors in a row"
                        return
                time.sleep(0.3)
        h["msg"] = "finished"
    except Exception as e:
        h["msg"] = f"failed: {e}"
    finally:
        h["running"] = False


def fno_feats(C):
    """The same 12 price features as the NIFTY model, for all days x bars 09:15+12..14:30 at once. C = days x 75 closes."""
    from numpy.lib.stride_tricks import sliding_window_view as swv
    ks = np.arange(12, 64)
    dif = np.diff(C, axis=1, prepend=C[:, :1]) / C * 1e4
    sw = swv(dif, 12, axis=1).std(axis=-1)
    rmax, rmin = np.maximum.accumulate(C, axis=1), np.minimum.accumulate(C, axis=1)
    lag = lambda a: np.concatenate([[0.0], a[:-1]])
    pr, prr = lag((C[:, -1] / C[:, 0] - 1) * 1e4), lag((C.max(1) - C.min(1)) / C[:, 0] * 1e4)
    gap = np.concatenate([[0.0], (C[1:, 0] / C[:-1, -1] - 1) * 1e4])
    er = (C[:, :10].max(1) - C[:, :10].min(1)) / C[:, 0] * 1e4
    sh = (C.shape[0], len(ks))
    X = [(C[:, ks] / C[:, ks - n] - 1) * 1e4 for n in (1, 3, 6, 12)]
    X += [(C[:, ks] / C[:, :1] - 1) * 1e4, (C[:, ks] - rmin[:, ks]) / (rmax[:, ks] - rmin[:, ks] + 1e-9), sw[:, ks - 11],
          np.broadcast_to(er[:, None], sh), np.broadcast_to(5 * ks / 375, sh), np.broadcast_to(pr[:, None], sh),
          np.broadcast_to(prr[:, None], sh), np.broadcast_to(gap[:, None], sh)]
    return np.stack(X, -1), (C[:, -1:] / C[:, ks] - 1) * 1e4, (C[:, -1:] / C[:, ks + 1] - 1) * 1e4


def fno_bt_job(symbol):
    """Backtest of the NIFTY price model on every downloaded F&O stock: one trade per day per stock, walk-forward by year."""
    h = hist_state()
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        if len(syms) < 5:
            h["analysis"] = "Not enough F&O data yet. Run 'Download all F&O stocks' first (Days back 1800)."
            return
        rg, REP = np.random.default_rng(3), 5
        res = [[] for _ in range(REP + 1)]                         # [0] = real, [1..] = scrambled outcomes (luck)
        per = {}
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Running F&O backtest... stock {n_ + 1}/{len(syms)} ({sy})"
            with LOCK:
                c = db()
                rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
                c.close()
            days = np.array([r[0] for r in rows])
            C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
            ix = np.where(np.isnan(C), 0, np.arange(75))
            C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
            ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
            C, days = C[ok], days[ok]
            yr = np.array([x[:4] for x in days])
            if len(set(yr)) < 3:
                continue
            X3, Y3, Y1 = fno_feats(C)
            for rep in range(REP + 1):
                Yr = Y3 if rep == 0 else Y3[rg.permutation(len(Y3))]
                for Z in sorted(set(yr))[2:]:
                    tr, te = yr < Z, yr == Z
                    Xt, mu = X3[tr].reshape(-1, 12), X3[tr].reshape(-1, 12).mean(0)
                    sd = Xt.std(0) + 1e-9
                    A = (Xt - mu) / sd
                    yv = Yr[tr].reshape(-1)
                    w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(12), A.T @ (yv - yv.mean()))
                    thr = np.percentile(np.abs(A @ w), 90)
                    pred = ((X3[te] - mu) / sd) @ w
                    m = np.abs(pred) >= thr
                    has = m.any(1)
                    f = m.argmax(1)
                    r_ = np.arange(len(f))
                    sgn, y_ = np.sign(pred[r_, f]), Yr[te][r_, f]
                    rat = np.abs(pred[r_, f]) / thr
                    yd = Y1[te][r_, f] if rep == 0 else y_
                    px = np.full(len(f), np.nan)
                    stp = np.full((len(f), 3), np.nan)
                    if rep == 0:                                  # stop-loss test: path of each confident trade (5-min closes)
                        Cte = C[te]
                        px = Cte[r_, f + 12]
                        for i in np.where(has & (rat >= 1.7))[0]:
                            R = sgn[i] * (Cte[i, f[i] + 13:] / px[i] - 1) * 1e4
                            for q, S in enumerate((100, 150, 200)):
                                hit = R <= -S
                                stp[i, q] = R[hit.argmax()] if hit.any() else R[-1]
                    for dd, s_, yy, rr, y1, k_, p_, q_ in zip(days[te][has], sgn[has], y_[has], rat[has], yd[has], f[has] + 12, px[has], stp[has]):
                        res[rep].append((dd, s_ * yy, s_, yy, sy, rr, s_ * y1, k_, p_, q_[0], q_[1], q_[2]))
        def stat(L):
            a = pd.DataFrame(L, columns=["day", "pnl", "side", "raw", "sym", "rat", "pnl1", "k", "px", "s1", "s2", "s3"])
            g = a.groupby("day").pnl.mean()
            return a, g.mean(), g.mean() / (g.std() / np.sqrt(len(g)) + 1e-9)
        a, m0, t0 = stat(res[0])
        nf = [stat(res[k])[0] for k in range(1, REP + 1)]
        nulls = [stat(res[k])[1] for k in range(1, REP + 1)]
        a["yr"] = a.day.str[:4]
        ps = a.groupby("sym").pnl.agg(["mean", "count", "std"])
        ps["t"] = ps["mean"] / (ps["std"] / np.sqrt(ps["count"]) + 1e-9)
        out = [f"F&O STOCK BACKTEST: same price model, one trade per stock per day, hold to close, walk-forward (test years {', '.join(sorted(set(a.yr)))})",
               f"  {len(syms)} stocks with 450+ days, {len(a):,} trades on {a.day.nunique()} days",
               f"  average per trade: {a.pnl.mean():+.2f} bp of the stock price, right {(a.pnl > 0).mean() * 100:.0f}%",
               f"  per DAY (all stocks that day averaged, so a market-wide day counts once): {m0:+.2f} bp, t-stat {t0:+.1f}",
               f"  same pipeline on SCRAMBLED outcomes (luck), 5 runs: {np.mean(nulls):+.2f} bp average, highest {max(nulls):+.2f}",
               f"  buys {(a.side > 0).mean() * 100:.0f}% of the time; always buying at the same moments would give {a.raw.mean():+.2f} bp",
               "  by year: " + "  ".join(f"{y}: {a[a.yr == y].pnl.mean():+.2f} bp (n={int((a.yr == y).sum())})" for y in sorted(set(a.yr))),
               f"  after an ASSUMED 10 bp round-trip cost (stock option spread + charges, unverified): {a.pnl.mean() - 10:+.2f} bp",
               f"  stocks with positive average: {int((ps['mean'] > 0).sum())} of {len(ps)};  stocks with t above 2: {int((ps.t > 2).sum())} (pure chance gives about {0.023 * len(ps):.0f})",
               "  best 5 by t-stat (picked after the fact, so flattering): " + ", ".join(f"{k} {v.t:+.1f}" for k, v in ps.sort_values("t", ascending=False).head(5).iterrows()),
               f"  BUY trades: stock itself moved {a[a.side > 0].raw.mean():+.2f} bp;  SELL trades: stock moved {a[a.side < 0].raw.mean():+.2f} bp (the model gains {-a[a.side < 0].raw.mean():+.2f} bp by selling)",
               "  by confidence (signal strength vs the entry threshold): " + "  ".join(
                   f"{lo}-{hi}x: {a[(a.rat >= lo) & (a.rat < hi)].pnl.mean():+.1f} bp (n={int(((a.rat >= lo) & (a.rat < hi)).sum())})"
                   for lo, hi in ((1, 1.3), (1.3, 1.7), (1.7, 2.5), (2.5, 99))),
               f"  if you could only ENTER ONE BAR (5 min) LATER on the same signals: {a.pnl1.mean():+.2f} bp",
               "  net average per trade after a round-trip cost of:  " + "   ".join(f"{k} bp: {a.pnl.mean() - k:+.1f}" for k in (3, 5, 8, 10)),
               "",
               "  Real only if the per-day average clearly beats the scrambled runs, is positive in every year,",
               "  t-stat is above 3, and it stays positive after realistic costs. Not advice."]
        for lo in (1.7, 2.5):
            b = a[a.rat >= lo]
            if len(b) < 50:
                continue
            g = b.groupby("day").pnl.mean()
            tp = b.groupby("sym").size().sort_values(ascending=False)
            dsum = b.groupby("day").pnl.sum().sort_values(ascending=False)
            keep = g.sort_values().iloc[:int(len(g) * 0.95)]
            nl = [x[x.rat >= lo].pnl.mean() for x in nf]
            out += ["", f"HIGH-CONFIDENCE ONLY (signal at least {lo}x the entry threshold):",
                    f"  {len(b):,} trades on {len(g)} days = about {len(b) / len(g):.1f} signals per day across all stocks",
                    f"  average per trade {b.pnl.mean():+.1f} bp, right {(b.pnl > 0).mean() * 100:.0f}%, median trade {b.pnl.median():+.1f} bp",
                    f"  per DAY average {g.mean():+.1f} bp, t-stat {g.mean() / (g.std() / np.sqrt(len(g)) + 1e-9):+.1f};  scrambled outcomes (luck): {np.nanmean(nl):+.1f} bp, highest {np.nanmax(nl):+.1f}",
                    "  by year: " + "  ".join(f"{y}: {b[b.yr == y].pnl.mean():+.1f} bp (n={int((b.yr == y).sum())})" for y in sorted(set(b.yr))),
                    f"  entered one bar (5 min) later: {b.pnl1.mean():+.1f} bp;  BUY trades {b[b.side > 0].pnl.mean():+.1f} bp, SELL trades {b[b.side < 0].pnl.mean():+.1f} bp",
                    "  net per trade after round-trip cost:  " + "   ".join(f"{k} bp: {b.pnl.mean() - k:+.1f}" for k in (5, 8, 10, 15)),
                    f"  concentration: top 10 stocks make {tp.head(10).sum() / len(b) * 100:.0f}% of the trades;  best 5 days make {dsum.head(5).sum() / dsum.sum() * 100:.0f}% of total profit;  average of the worse 95% of days: {keep.mean():+.1f} bp",
                    "  by entry time: " + "  ".join(f"{hh}h: {b[(555 + 5 * b.k) // 60 == hh].pnl.mean():+.1f} bp (n={int(((555 + 5 * b.k) // 60 == hh).sum())})" for hh in (10, 11, 12, 13, 14))]
        bq = a[(a.rat >= 1.7) & a.px.notna()]
        if len(bq) >= 50:
            cols = [("no stop", "pnl"), ("stop 100", "s1"), ("stop 150", "s2"), ("stop 200", "s3")]
            out += ["", "STOP-LOSS AND PRICE FILTER TEST (signals at least 1.7x; 4 price filters x 4 exit rules = 16 fixed combinations, none picked after the fact):",
                    "  stop-loss is checked on 5-min closes only (a real stop fills worse in fast moves, and no volume data is stored, so the filter is by share price).",
                    "  numbers = average bp per trade: before cost / after a 10 bp round-trip cost"]
            for pmin in (0, 200, 500, 1000):
                bb = bq[bq.px >= pmin]
                if len(bb) < 30:
                    continue
                out.append(f"  share price above Rs {pmin}: {len(bb):,} trades ({len(bb) / bb.day.nunique():.1f} per day)")
                out.append("    " + "   ".join(f"{nm}: {bb[c].mean():+.1f} / {bb[c].mean() - 10:+.1f}" for nm, c in cols))
                out.append("    worst single trade:  " + "   ".join(f"{nm}: {bb[c].min():+.0f}" for nm, c in cols) +
                           f";  stopped out: 100bp {((bb.s1 - bb.pnl).abs() > 1e-6).mean() * 100:.0f}%, 150bp {((bb.s2 - bb.pnl).abs() > 1e-6).mean() * 100:.0f}%, 200bp {((bb.s3 - bb.pnl).abs() > 1e-6).mean() * 100:.0f}%")
                for nm, c in (("no stop", "pnl"), ("stop 150", "s2")):
                    out.append(f"    by year, {nm}: " + "  ".join(f"{y}: {bb[bb.yr == y][c].mean():+.1f} (n={int((bb.yr == y).sum())})" for y in sorted(set(bb.yr))))
            out.append("  Judge by: better than 'no stop' in EVERY year, not just on average. With 16 combinations one can look good by chance.")
        out += ["", "  (Strong signals earn more, which is what a real effect looks like. Still check: circuit-limit stocks, wide spreads, F&O ban list.)"]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"F&O backtest failed: {e}"



def fno_swing_job():
    """SWING backtest: one price model for all F&O stocks, decided at the close, enter next morning (09:20 price),
    hold 5 trading days, exit at the close of the 5th day. Walk-forward by year, one open trade per stock at a time."""
    h = hist_state()
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        if len(syms) < 5:
            h["analysis"] = "Not enough F&O data yet. Run 'Download all F&O stocks' first."
            return
        F, YN, YR, DY, SY, TT = [], [], [], [], [], []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Running swing backtest... loading stock {n_ + 1}/{len(syms)} ({sy})"
            with LOCK:
                c = db()
                rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
                c.close()
            days = np.array([r[0] for r in rows])
            C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
            ix = np.where(np.isnan(C), 0, np.arange(75))
            C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
            ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
            C, days = C[ok], days[ok]
            n = len(days)
            if n < 150:
                continue
            O, D = C[:, 0], C[:, -1]
            sh = lambda a, k: np.concatenate([np.full(k, np.nan), a[:-k]])
            dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
            sD = pd.Series(D)
            vs = pd.Series(dr).rolling(20).std().to_numpy()
            mx, mn, ma = sD.rolling(20).max().to_numpy(), sD.rolling(20).min().to_numpy(), sD.rolling(20).mean().to_numpy()
            v = np.maximum(vs, 1e-4)
            feats = [(D / sh(D, k) - 1) / v for k in (1, 3, 5, 10, 20)]
            feats += [(D - mn) / (mx - mn + 1e-9), (D / ma - 1) / v, (O / sh(D, 1) - 1) / v, (D / O - 1) / v,
                      (C[:, -1] / C[:, -13] - 1) / v, np.log(v)]
            fwd = np.full(n, np.nan)
            fwd[:n - 5] = D[5:] / O[1:n - 4] - 1                    # enter next day 09:20, exit close of day t+5
            dd = days.astype("datetime64[D]")
            span = np.full(n, 99.0)
            span[:n - 5] = (dd[5:] - dd[:-5]).astype(float)
            valid = np.isfinite(fwd) & np.isfinite(vs) & np.isfinite(mx) & (span <= 11) & (np.arange(n) >= 20)
            X = np.stack(feats, 1)
            valid &= np.isfinite(X).all(1)
            F.append(np.clip(X[valid], -8, 8))
            YN.append(np.clip(fwd[valid] / v[valid], -8, 8))
            YR.append(fwd[valid] * 1e4)
            DY.append(days[valid])
            SY.append(np.full(valid.sum(), n_))
            TT.append(np.where(valid)[0])
        F, YN, YR = np.concatenate(F), np.concatenate(YN), np.concatenate(YR)
        DY, SY, TT = np.concatenate(DY), np.concatenate(SY), np.concatenate(TT)
        yr = np.array([x[:4] for x in DY])
        years = sorted(set(yr))[2:]
        mkt = pd.Series(YR).groupby(DY).mean()                                # average 5-day move of ALL stocks from each day

        def run(yn, yr_raw):
            res = []
            for Z in years:
                tr, te = yr < Z, yr == Z
                mu, sd = F[tr].mean(0), F[tr].std(0) + 1e-9
                A = (F[tr] - mu) / sd
                w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(A.shape[1]), A.T @ (yn[tr] - yn[tr].mean()))
                thr = np.percentile(np.abs(A @ w), 90)
                pred = ((F[te] - mu) / sd) @ w
                gi = np.where(te)[0]
                last, sel = {}, []
                for j in np.where(np.abs(pred) >= thr)[0]:
                    g = gi[j]
                    if SY[g] in last and TT[g] < last[SY[g]] + 5:
                        continue                                            # that stock's previous trade is still open
                    last[SY[g]] = TT[g]
                    sel.append((DY[g], SY[g], np.sign(pred[j]), abs(pred[j]) / thr, yr_raw[g]))
                res += sel
            a = pd.DataFrame(res, columns=["day", "s", "side", "rat", "raw"])
            a["pnl"] = a.side * a.raw
            return a

        rg = np.random.default_rng(5)
        nulls = []
        for k in range(3):
            h["analysis"] = f"Running swing backtest... luck check {k + 1}/3"
            p = rg.permutation(len(YN))
            nulls.append(run(YN[p], YR[p]).pnl.mean())
        h["analysis"] = "Running swing backtest... real run"
        a = run(YN, YR)
        a["yr"] = a.day.str[:4]
        a["wk"] = pd.to_datetime(a.day).dt.strftime("%G-%V")
        a["mk"] = a.day.map(mkt)
        a["ex"] = a.side * (a.raw - a.mk)                                    # gain over simply holding the average F&O stock
        gw = a.groupby("wk").pnl.mean()
        tw = gw.mean() / (gw.std() / np.sqrt(len(gw)) + 1e-9)
        out = [f"F&O SWING BACKTEST: signal at the close, enter next morning (09:20 price), hold 5 trading days, exit at the close. Test years {', '.join(years)}",
               f"  {len(syms)} stocks, one price model for all (moves scaled by each stock's own volatility), walk-forward by year, one open trade per stock",
               f"  {len(a):,} trades over {a.wk.nunique()} weeks = about {len(a) / a.wk.nunique():.0f} new trades per week",
               f"  average per trade: {a.pnl.mean():+.1f} bp, right {(a.pnl > 0).mean() * 100:.0f}%, median {a.pnl.median():+.1f} bp",
               f"  per WEEK average {gw.mean():+.1f} bp, t-stat {tw:+.1f} (each week counted once);  scrambled outcomes (luck), 3 runs: {np.mean(nulls):+.1f} bp, highest {max(nulls):+.1f}",
               "  by year: " + "  ".join(f"{y}: {a[a.yr == y].pnl.mean():+.1f} bp (n={int((a.yr == y).sum())})" for y in years),
               f"  BUY trades: {a[a.side > 0].pnl.mean():+.1f} bp (n={int((a.side > 0).sum())});  SELL trades: {a[a.side < 0].pnl.mean():+.1f} bp (n={int((a.side < 0).sum())})",
               f"  market check: the average F&O stock itself gained {mkt.mean():+.1f} bp per 5-day window; the model's gain OVER the market (excess) is {a.ex.mean():+.1f} bp per trade",
               "  by confidence: " + "  ".join(f"{lo}-{hi}x: {a[(a.rat >= lo) & (a.rat < hi)].pnl.mean():+.1f} bp (n={int(((a.rat >= lo) & (a.rat < hi)).sum())})"
                                              for lo, hi in ((1, 1.3), (1.3, 1.7), (1.7, 2.5), (2.5, 99))),
               "  net per trade after a round-trip cost of:  " + "   ".join(f"{k} bp: {a.pnl.mean() - k:+.1f}" for k in (5, 10, 20, 30)),
               f"  worst trade {a.pnl.min():+.0f} bp, best {a.pnl.max():+.0f} bp;  best 5 weeks make {gw.sort_values(ascending=False).head(5).sum() / gw.sum() * 100:.0f}% of total profit",
               f"  concentration: top 10 stocks make {a.groupby('s').size().sort_values(ascending=False).head(10).sum() / len(a) * 100:.0f}% of the trades"]
        for lo in (1.7, 2.5):
            b = a[a.rat >= lo]
            if len(b) < 50:
                continue
            gb = b.groupby("wk").pnl.mean()
            out += ["", f"STRONG SIGNALS ONLY (at least {lo}x):",
                    f"  {len(b):,} trades, about {len(b) / b.wk.nunique():.1f} per week on {b.wk.nunique()} weeks;  average {b.pnl.mean():+.1f} bp, right {(b.pnl > 0).mean() * 100:.0f}%",
                    f"  per WEEK average {gb.mean():+.1f} bp, t-stat {gb.mean() / (gb.std() / np.sqrt(len(gb)) + 1e-9):+.1f};  over the market: {b.ex.mean():+.1f} bp",
                    "  by year: " + "  ".join(f"{y}: {b[b.yr == y].pnl.mean():+.1f} bp (n={int((b.yr == y).sum())})" for y in years),
                    f"  BUY {b[b.side > 0].pnl.mean():+.1f} bp,  SELL {b[b.side < 0].pnl.mean():+.1f} bp;  after 10 bp cost {b.pnl.mean() - 10:+.1f}, after 20 bp {b.pnl.mean() - 20:+.1f}"]
        out += ["", "  Real only if: per-week average clearly beats the scrambled runs, the gain OVER the market is positive,",
                "  every year is positive and it stays positive after realistic costs (futures about 10 bp, delivery shares more). Not advice."]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"Swing backtest failed: {e}"


def fno_swing2_job(K=10, minn=40):
    """SWING v2 (one fixed design, not tuned): every week, rank all F&O stocks by a model that predicts the move OVER the
    market; long the top K, short the bottom K; enter next morning, exit the close of the 5th day. Market-neutral by design."""
    h = hist_state()
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        if len(syms) < 5:
            h["analysis"] = "Not enough F&O data yet. Run 'Download all F&O stocks' first."
            return
        F, FW, V, DY = [], [], [], []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Running swing v2... loading stock {n_ + 1}/{len(syms)} ({sy})"
            with LOCK:
                c = db()
                rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
                c.close()
            days = np.array([r[0] for r in rows])
            C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
            ix = np.where(np.isnan(C), 0, np.arange(75))
            C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
            ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
            C, days = C[ok], days[ok]
            n = len(days)
            if n < 200:
                continue
            O, D = C[:, 0], C[:, -1]
            sh = lambda a, k: np.concatenate([np.full(k, np.nan), a[:-k]])
            dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
            sD = pd.Series(D)
            vs = pd.Series(dr).rolling(20).std().to_numpy()
            mx, mn, ma = sD.rolling(20).max().to_numpy(), sD.rolling(20).min().to_numpy(), sD.rolling(20).mean().to_numpy()
            v = np.maximum(vs, 1e-4)
            feats = [(D / sh(D, k) - 1) / v for k in (1, 3, 5, 10, 20, 60)]
            feats += [(sh(D, 20) / sh(D, 120) - 1) / v,                      # 6-month momentum skipping the last month
                      (D - mn) / (mx - mn + 1e-9), (D / ma - 1) / v, (O / sh(D, 1) - 1) / v, (D / O - 1) / v,
                      (C[:, -1] / C[:, -13] - 1) / v, np.log(v)]
            fwd = np.full(n, np.nan)
            fwd[:n - 5] = D[5:] / O[1:n - 4] - 1
            dd = days.astype("datetime64[D]")
            span = np.full(n, 99.0)
            span[:n - 5] = (dd[5:] - dd[:-5]).astype(float)
            X = np.stack(feats, 1)
            valid = np.isfinite(fwd) & np.isfinite(vs) & (span <= 11) & (np.arange(n) >= 120) & np.isfinite(X).all(1)
            F.append(np.clip(X[valid], -8, 8))
            FW.append(fwd[valid])
            V.append(v[valid])
            DY.append(days[valid])
        F, FW, V, DY = np.concatenate(F), np.concatenate(FW), np.concatenate(V), np.concatenate(DY)
        F = (pd.DataFrame(F) - pd.DataFrame(F).groupby(DY).transform("mean")).to_numpy()     # each feature relative to that day's average stock
        mk = pd.Series(FW).groupby(DY).transform("mean").to_numpy()
        yx = np.clip((FW - mk) / V, -8, 8)                                  # target: move over the market, in the stock's own volatility units
        yr = np.array([x[:4] for x in DY])
        years = sorted(set(yr))[2:]
        du = pd.DataFrame({"d": sorted(set(DY))})
        du["wk"] = pd.to_datetime(du.d).dt.strftime("%G-%V")
        wmask = np.isin(DY, du.groupby("wk").d.max().to_numpy())             # last trading day of each week = decision day
        weeks = []
        for Z in years:
            tr, te = yr < Z, (yr == Z) & wmask
            mu, sd = F[tr].mean(0), F[tr].std(0) + 1e-9
            A = (F[tr] - mu) / sd
            w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(A.shape[1]), A.T @ (yx[tr] - yx[tr].mean()))
            gi = np.where(te)[0]
            pred = ((F[gi] - mu) / sd) @ w
            dte = DY[gi]
            for d in sorted(set(dte)):
                m = dte == d
                if m.sum() < minn:
                    continue
                p, r = pred[m], FW[gi[m]] * 1e4
                o = np.argsort(p)
                weeks.append((d, Z, o[-K:], o[:K], r))
        if len(weeks) < 20:
            h["analysis"] = "Swing v2: too few test weeks."
            return
        L = np.array([r[t].mean() for _, _, t, b, r in weeks])
        S = np.array([-r[b].mean() for _, _, t, b, r in weeks])
        M = np.array([r.mean() for _, _, t, b, r in weeks])
        Pn = (L + S) / 2                                                    # average gain per trade (long and short legs together)
        wy = np.array([z for _, z, _, _, _ in weeks])
        rg = np.random.default_rng(11)
        nul = []
        for _ in range(400):
            x = []
            for _, _, t, b, r in weeks:
                rp = r[rg.permutation(len(r))]
                x.append((rp[t].mean() - rp[b].mean()) / 2)
            nul.append(np.mean(x))
        tst = lambda a: a.mean() / (a.std() / np.sqrt(len(a)) + 1e-9)
        out = [f"F&O SWING v2 (market-neutral long/short): every week long the top {K}, short the bottom {K} of ~{int(np.mean([len(r) for *_, r in weeks]))} stocks",
               f"  decide at the close of the week's last trading day, enter next morning 09:20, exit the close of the 5th day. Test years {', '.join(years)}, {len(weeks)} weeks",
               "  model: ridge on 13 price features (1/3/5/10/20/60-day moves, 6-month momentum, range position, gap, intraday), all relative to that day's average stock",
               f"  average gain per trade (long and short together): {Pn.mean():+.1f} bp, weekly t-stat {tst(Pn):+.1f}, weeks positive {np.mean(Pn > 0) * 100:.0f}%",
               f"  luck check (same picks, outcomes shuffled among stocks, 400 runs): average {np.mean(nul):+.1f} bp, 95th percentile {np.percentile(nul, 95):+.1f};  real result beats {np.mean(np.array(nul) < Pn.mean()) * 100:.0f}% of them",
               f"  long leg: {L.mean():+.1f} bp per trade (the average stock that week: {M.mean():+.1f}, so over the market {L.mean() - M.mean():+.1f});  short leg: {S.mean():+.1f} bp per trade",
               "  by year: " + "  ".join(f"{y}: {Pn[wy == y].mean():+.1f} bp (weeks {int((wy == y).sum())})" for y in years),
               "  net per trade after a round-trip cost of:  " + "   ".join(f"{k} bp: {Pn.mean() - k:+.1f}" for k in (5, 10, 20)),
               f"  worst week {Pn.min():+.0f} bp, best {Pn.max():+.0f} bp;  best 5 weeks make {np.sort(Pn)[::-1][:5].sum() / Pn.sum() * 100:.0f}% of total profit",
               "",
               "  Real only if: it beats the luck check (above about 95%), weekly t-stat above 3, every year positive, positive after 10 bp.",
               "  Shorting needs stock futures (margin). This is ONE fixed design, so a lucky result is still possible. Not advice."]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"Swing v2 failed: {e}"


def fno_swing3_job(K=10, minn=40):
    """SWING v2 weekly lists, same entry, but compare exit rules: hold 5 days vs take profit at +3%, +4%, +5% (whichever comes first)."""
    h = hist_state()
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        if len(syms) < 5:
            h["analysis"] = "Not enough F&O data yet."
            return

        def clean(sy):
            with LOCK:
                c = db()
                rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
                c.close()
            days = np.array([r[0] for r in rows])
            C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
            ix = np.where(np.isnan(C), 0, np.arange(75))
            C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
            ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
            return days[ok], C[ok]

        F, FW, V, DY, TT, SI = [], [], [], [], [], []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Running swing exit test... loading stock {n_ + 1}/{len(syms)} ({sy})"
            days, C = clean(sy)
            n = len(days)
            if n < 200:
                continue
            O, D = C[:, 0], C[:, -1]
            sh = lambda a, k: np.concatenate([np.full(k, np.nan), a[:-k]])
            dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
            sD = pd.Series(D)
            vs = pd.Series(dr).rolling(20).std().to_numpy()
            mx, mn, ma = sD.rolling(20).max().to_numpy(), sD.rolling(20).min().to_numpy(), sD.rolling(20).mean().to_numpy()
            v = np.maximum(vs, 1e-4)
            feats = [(D / sh(D, k) - 1) / v for k in (1, 3, 5, 10, 20, 60)]
            feats += [(sh(D, 20) / sh(D, 120) - 1) / v, (D - mn) / (mx - mn + 1e-9), (D / ma - 1) / v, (O / sh(D, 1) - 1) / v,
                      (D / O - 1) / v, (C[:, -1] / C[:, -13] - 1) / v, np.log(v)]
            fwd = np.full(n, np.nan)
            fwd[:n - 5] = D[5:] / O[1:n - 4] - 1
            dd = days.astype("datetime64[D]")
            span = np.full(n, 99.0)
            span[:n - 5] = (dd[5:] - dd[:-5]).astype(float)
            X = np.stack(feats, 1)
            valid = np.isfinite(fwd) & np.isfinite(vs) & (span <= 11) & (np.arange(n) >= 120) & np.isfinite(X).all(1)
            F.append(np.clip(X[valid], -8, 8))
            FW.append(fwd[valid])
            V.append(v[valid])
            DY.append(days[valid])
            TT.append(np.where(valid)[0])
            SI.append(np.full(valid.sum(), n_))
        F, FW, V, DY, TT, SI = (np.concatenate(x) for x in (F, FW, V, DY, TT, SI))
        F = (pd.DataFrame(F) - pd.DataFrame(F).groupby(DY).transform("mean")).to_numpy()
        mk = pd.Series(FW).groupby(DY).transform("mean").to_numpy()
        yx = np.clip((FW - mk) / V, -8, 8)
        yr = np.array([x[:4] for x in DY])
        years = sorted(set(yr))[2:]
        du = pd.DataFrame({"d": sorted(set(DY))})
        du["wk"] = pd.to_datetime(du.d).dt.strftime("%G-%V")
        wmask = np.isin(DY, du.groupby("wk").d.max().to_numpy())
        trades = []                                                        # (week no, year, side, row)
        wn = 0
        for Z in years:
            tr, te = yr < Z, (yr == Z) & wmask
            mu, sd = F[tr].mean(0), F[tr].std(0) + 1e-9
            A = (F[tr] - mu) / sd
            w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(A.shape[1]), A.T @ (yx[tr] - yx[tr].mean()))
            gi = np.where(te)[0]
            pred = ((F[gi] - mu) / sd) @ w
            dte = DY[gi]
            for d in sorted(set(dte)):
                m = dte == d
                if m.sum() < minn:
                    continue
                gm, o = gi[m], np.argsort(pred[m])
                trades += [(wn, Z, 1, g) for g in gm[o[-K:]]] + [(wn, Z, -1, g) for g in gm[o[:K]]]
                wn += 1
        if wn < 20:
            h["analysis"] = "Swing exit test: too few test weeks."
            return
        TPS = (300, 400, 500)
        rec = []
        bysym = {}
        for wk_, Z, sd_, g in trades:
            bysym.setdefault(SI[g], []).append((wk_, Z, sd_, g))
        for k_, (si, lst) in enumerate(bysym.items()):
            h["analysis"] = f"Running swing exit test... paths {k_ + 1}/{len(bysym)}"
            days, C = clean(syms[si])
            for wk_, Z, sd_, g in lst:
                t = TT[g]
                ent = C[t + 1, 0]
                R = sd_ * (C[t + 1:t + 6].ravel()[1:] / ent - 1) * 1e4
                row = [wk_, Z, sd_, R[-1]]
                for tp in TPS:
                    hit = R >= tp
                    row += [float(tp) if hit.any() else R[-1], bool(hit.any()), int(hit.argmax() // 75 + 1) if hit.any() else 5]
                hs = R <= -300                                              # stop-loss at -3%
                row += [-300.0 if hs.any() else R[-1], R[hs.argmax()] if hs.any() else R[-1], bool(hs.any()), int(hs.argmax() // 75 + 1) if hs.any() else 5]
                rec.append(row)
        a = pd.DataFrame(rec, columns=["wk", "yr", "side", "hold", "p3", "h3", "d3", "p4", "h4", "d4", "p5", "h5", "d5", "sl", "slr", "hsl", "dsl"])
        rules = [("hold 5 days", "hold", None, None), ("take profit +3%", "p3", "h3", "d3"), ("take profit +4%", "p4", "h4", "d4"), ("take profit +5%", "p5", "h5", "d5"),
                 ("STOP-LOSS -3%, filled at -3% (ideal)", "sl", "hsl", "dsl"), ("STOP-LOSS -3%, filled at the 5-min close that breaks it (more realistic)", "slr", "hsl", "dsl")]
        wkh = a.groupby("wk").hold.mean()
        out = [f"F&O SWING EXIT TEST: same weekly long {K} / short {K} lists as SWING v2, enter next morning 09:20; only the exit differs",
               f"  {len(a):,} trades over {wn} weeks, test years {', '.join(years)}. A target is checked on 5-min closes and filled AT the target (an ideal limit order).",
               ""]
        tt = lambda s: s.mean() / (s.std() / np.sqrt(len(s)) + 1e-9)
        for nm, pc, hc, dc in rules:
            wk = a.groupby("wk")[pc].mean()
            line = (f"  {nm}: {a[pc].mean():+.1f} bp per trade, weekly t {tt(wk):+.1f}, weeks positive {(wk > 0).mean() * 100:.0f}%, "
                    f"after 10 bp cost {a[pc].mean() - 10:+.1f}")
            if hc:
                line += f";  triggered on {a[hc].mean() * 100:.0f}% of trades (on average day {a.loc[a[hc], dc].mean():.1f} of 5)"
            out.append(line)
            out.append("     by year: " + "  ".join(f"{y}: {a[a.yr == y][pc].mean():+.1f}" for y in years) +
                       f";  long {a[a.side > 0][pc].mean():+.1f}, short {a[a.side < 0][pc].mean():+.1f};  best 5 weeks make {np.sort(wk.to_numpy())[::-1][:5].sum() / wk.sum() * 100:.0f}% of profit;  worst trade {a[pc].min():+.0f} bp")
        out.append("")
        for nm, pc, hc, dc in rules[1:]:
            df = a.groupby("wk")[pc].mean() - wkh
            yrs = a.groupby("yr").apply(lambda x: x[pc].mean() - x.hold.mean())
            out.append(f"  {nm} minus hold: {df.mean():+.1f} bp per trade per week, t {tt(df):+.1f};  by year: " +
                       "  ".join(f"{y}: {yrs[y]:+.1f}" for y in years))
        out += ["", "  Worth using only if the target beats 'hold' in EVERY year, the difference t-stat is above 2, and it stays positive after 10 bp.",
                "  Four exit rules tested, so a small gain can be chance. Ideal fills are at the exact level; real fills can be worse (see the 'more realistic' stop line). Not advice."]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"Swing exit test failed: {e}"


def fno_xs_job(K=10, minn=40):
    """INTRADAY MARKET-NEUTRAL RANKING (fixed rules, nothing fitted): at 10:15 rank every F&O stock by its move since 09:20
    relative to the average stock, scaled by its own volatility. Long the top K, short the bottom K, enter 10:20, exit at the close.
    A negative result means the opposite (reversal) is the effect."""
    h = hist_state()
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        if len(syms) < 5:
            h["analysis"] = "Not enough F&O data yet."
            return
        DY, RM, VV, RT, R0 = [], [], [], [], []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Running intraday ranking test... loading stock {n_ + 1}/{len(syms)} ({sy})"
            with LOCK:
                c = db()
                rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
                c.close()
            days = np.array([r[0] for r in rows])
            C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
            ix = np.where(np.isnan(C), 0, np.arange(75))
            C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
            ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
            C, days = C[ok], days[ok]
            if len(days) < 30:
                continue
            D = C[:, -1]
            dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
            vs = pd.Series(dr).rolling(20).std().shift(1).to_numpy()          # volatility known before today
            good = np.isfinite(vs) & (vs > 1e-4)
            DY.append(days[good])
            RM.append((C[good, 11] / C[good, 0] - 1))                          # 09:20 -> 10:15 move
            VV.append(vs[good])
            RT.append((C[good, -1] / C[good, 12] - 1) * 1e4)                   # enter 10:20, exit at the close
            R0.append((C[good, -1] / C[good, 11] - 1) * 1e4)                   # no-delay version (enter 10:15), diagnostic only
        DY, RM, VV, RT, R0 = (np.concatenate(x) for x in (DY, RM, VV, RT, R0))
        sc = (RM - pd.Series(RM).groupby(DY).transform("mean").to_numpy()) / VV
        order = np.argsort(DY, kind="stable")
        DY, sc, RT, R0 = DY[order], sc[order], RT[order], R0[order]
        ud, st_ = np.unique(DY, return_index=True)
        en = list(st_[1:]) + [len(DY)]
        recs = []
        for d, s0, e0 in zip(ud, st_, en):
            if e0 - s0 < minn:
                continue
            o = np.argsort(sc[s0:e0])
            r, r0 = RT[s0:e0], R0[s0:e0]
            recs.append((d, o[-K:], o[:K], r, r0))
        if len(recs) < 50:
            h["analysis"] = "Intraday ranking test: too few days."
            return
        L = np.array([r[t].mean() for _, t, b, r, _ in recs])
        S = np.array([-r[b].mean() for _, t, b, r, _ in recs])
        M = np.array([r.mean() for *_, r, _ in recs])
        P = (L + S) / 2
        P0 = np.array([(r0[t].mean() - r0[b].mean()) / 2 for _, t, b, _, r0 in recs])
        yy = np.array([d[:4] for d, *_ in recs])
        yrs = [y for y in sorted(set(yy)) if (yy == y).sum() >= 20]
        rg = np.random.default_rng(17)
        nul = []
        for _ in range(400):
            x = 0.0
            for _, t, b, r, _ in recs:
                rp = r[rg.permutation(len(r))]
                x += (rp[t].mean() - rp[b].mean()) / 2
            nul.append(x / len(recs))
        nul = np.array(nul)
        tt = lambda a: a.mean() / (a.std() / np.sqrt(len(a)) + 1e-9)
        out = [f"INTRADAY MARKET-NEUTRAL RANKING: at 10:15 rank ~{int(np.mean([len(r) for *_, r, _ in recs]))} F&O stocks by their 09:20-10:15 move vs the average stock (scaled by own volatility)",
               f"  long the top {K}, short the bottom {K}, enter 10:20, exit at the close.  {len(recs)} days, years {', '.join(yrs)}. Nothing is fitted, so every year is out-of-sample.",
               f"  average per trade (long and short together): {P.mean():+.1f} bp, daily t-stat {tt(P):+.1f}, days positive {(P > 0).mean() * 100:.0f}%",
               f"  (a NEGATIVE number means the opposite works: buy the morning losers, sell the morning winners = reversal)",
               f"  luck check (outcomes shuffled among stocks each day, 400 runs): average {nul.mean():+.1f} bp, middle 95% {np.percentile(nul, 2.5):+.1f} to {np.percentile(nul, 97.5):+.1f};  "
               f"real result is outside the luck range: {'YES' if (P.mean() > np.percentile(nul, 97.5) or P.mean() < np.percentile(nul, 2.5)) else 'no'}",
               f"  long leg (morning winners): {L.mean():+.1f} bp vs the average stock {M.mean():+.1f} (so {L.mean() - M.mean():+.1f} over the market);  short leg (morning losers): {S.mean():+.1f} bp",
               "  by year: " + "  ".join(f"{y}: {P[yy == y].mean():+.1f} bp (t {tt(P[yy == y]):+.1f}, {int((yy == y).sum())} days)" for y in yrs),
               "  after a round-trip cost per trade of:  " + "   ".join(f"{k} bp: {abs(P.mean()) - k:+.1f}" for k in (5, 10, 15)) + "   (using the stronger direction)",
               f"  diagnostic, entering at 10:15 with no delay: {P0.mean():+.1f} bp (if this is much bigger, the effect fades within 5 minutes)",
               f"  worst day {P.min():+.0f} bp, best day {P.max():+.0f} bp;  best 10 days make {np.sort(P * np.sign(P.mean()))[::-1][:10].sum() / abs(P.sum()) * 100:.0f}% of the total",
               "",
               "  Real only if: outside the luck range, |daily t-stat| above 3, the same sign in EVERY year, and still positive after 10 bp.",
               "  Shorting intraday is allowed in cash (MIS) or futures. One fixed design, not tuned. Not advice."]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"Intraday ranking test failed: {e}"


def intraday_panel(h, label):
    """One row per stock per day: 09:20->10:15 move, trailing volatility, relative morning volume, 10:20->close outcome, sector."""
    sm = sector_map()
    with LOCK:
        c = db()
        syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
        c.close()
    out = []
    for n_, sy in enumerate(syms):
        h["analysis"] = f"Running {label}... loading stock {n_ + 1}/{len(syms)} ({sy})"
        with LOCK:
            c = db()
            rows = c.execute("SELECT d.day, d.b, v.b FROM fnod d JOIN fnov v ON v.sym = d.sym AND v.day = d.day WHERE d.sym=? ORDER BY d.day", (sy,)).fetchall()
            c.close()
        if len(rows) < 60:
            continue
        days = np.array([r[0] for r in rows])
        C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
        HV = np.array([np.frombuffer(r[2], np.float32).reshape(3, 75) for r in rows], float)
        ix = np.where(np.isnan(C), 0, np.arange(75))
        C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
        ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
        C, days, HV = C[ok], days[ok], HV[ok]
        D = C[:, -1]
        dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
        vs = pd.Series(dr).rolling(20).std().shift(1).to_numpy()
        vm = HV[:, 2, :12]
        mv = np.where(np.isfinite(vm).sum(1) >= 10, np.nansum(vm, 1), np.nan)            # 09:15-10:15 volume
        usual = pd.Series(mv).rolling(20, min_periods=15).mean().shift(1).to_numpy()     # usual for that hour, known before today
        good = np.isfinite(vs) & (vs > 1e-4) & np.isfinite(usual) & (usual > 0) & np.isfinite(mv)
        out.append(pd.DataFrame({"day": days[good], "sym": sy, "rm": C[good, 11] / C[good, 0] - 1, "vs": vs[good],
                                 "rv": mv[good] / usual[good], "ret": (C[good, -1] / C[good, 12] - 1) * 1e4,
                                 "ind": sm.get(sy, "")}))
    a = pd.concat(out, ignore_index=True)
    a["sc"] = (a.rm - a.groupby("day").rm.transform("mean")) / a.vs
    return a


def _xs_report(title, rules, recs, h, extra):
    """recs: list of (day, long outcomes array, short outcomes array, all outcomes that day). Prints the standard verdict block."""
    L = np.array([l.mean() for _, l, s, _ in recs])
    S = np.array([-s.mean() for _, l, s, _ in recs])
    M = np.array([r.mean() for *_, r in recs])
    P = (L + S) / 2
    yy = np.array([d[:4] for d, *_ in recs])
    yrs = [y for y in sorted(set(yy)) if (yy == y).sum() >= 20]
    rg = np.random.default_rng(23)
    nul = []
    for k in range(400):
        if k % 50 == 0:
            h["analysis"] = f"Running {title}... luck check {k}/400"
        x = 0.0
        for _, l, s, r in recs:
            p = rg.permutation(len(r))
            x += (r[p[:len(l)]].mean() - r[p[len(l):len(l) + len(s)]].mean()) / 2
        nul.append(x / len(recs))
    nul = np.array(nul)
    tt = lambda v: v.mean() / (v.std() / np.sqrt(len(v)) + 1e-9)
    lo, hi = np.percentile(nul, 2.5), np.percentile(nul, 97.5)
    return [title, rules,
            f"  {len(recs)} days, years {', '.join(yrs)}. Nothing is fitted, so every year is out-of-sample.",
            f"  average per trade (long and short together): {P.mean():+.1f} bp, daily t-stat {tt(P):+.1f}, days positive {(P > 0).mean() * 100:.0f}%",
            f"  luck check (same number of stocks picked at random each day, 400 runs): middle 95% {lo:+.1f} to {hi:+.1f};  outside the luck range: {'YES' if (P.mean() > hi or P.mean() < lo) else 'no'}",
            f"  long leg {L.mean():+.1f} bp vs the average stock {M.mean():+.1f} (so {L.mean() - M.mean():+.1f} over the market);  short leg {S.mean():+.1f} bp",
            "  by year: " + "  ".join(f"{y}: {P[yy == y].mean():+.1f} bp (t {tt(P[yy == y]):+.1f}, {int((yy == y).sum())} days)" for y in yrs),
            "  after a round-trip cost per trade of:  " + "   ".join(f"{k} bp: {P.mean() - k:+.1f}" for k in (5, 10, 15)),
            f"  worst day {P.min():+.0f} bp, best day {P.max():+.0f} bp;  best 10 days make {np.sort(P)[::-1][:10].sum() / P.sum() * 100:.0f}% of the total"] + extra + [
            "", "  Real only if: outside the luck range, daily t-stat above 3, positive in EVERY year, and still positive after 10 bp.",
            "  (A clearly NEGATIVE result would mean the opposite trade works.) One fixed design, not tuned. Not advice."]


def fno_vol_job(K=5, need=10):
    """VOLUME-CONFIRMED MORNING MOVE: among stocks trading at least 2x their usual 09:15-10:15 volume, long the K strongest
    and short the K weakest 09:20-10:15 movers (vs the average stock, scaled by volatility). Enter 10:20, exit at the close."""
    h = hist_state()
    try:
        a = intraday_panel(h, "volume test")
        recs, ctl, allk, nh = [], [], [], []
        for d, g in a.groupby("day", sort=True):
            hv = g[g.rv >= 2]
            lw = g[g.rv < 1]
            if len(hv) >= need:
                o = hv.sort_values("sc")
                recs.append((d, o.ret.to_numpy()[-K:], o.ret.to_numpy()[:K], g.ret.to_numpy()))
                nh.append(len(hv))
                if len(lw) >= need:
                    q = lw.sort_values("sc").ret.to_numpy()
                    ctl.append((q[-K:].mean() - q[:K].mean()) / 2)
                q = g.sort_values("sc").ret.to_numpy()
                allk.append((q[-K:].mean() - q[:K].mean()) / 2)
        if len(recs) < 50:
            h["analysis"] = f"Volume test: only {len(recs)} days had {need}+ high-volume stocks."
            return
        extra = ["",
                 f"  comparison (same days, same K={K}): ignoring volume {np.mean(allk):+.1f} bp;  LOW-volume stocks only (under 1x usual) {np.mean(ctl):+.1f} bp",
                 f"  high-volume stocks per day: average {np.mean(nh):.0f} (days with fewer than {need} were skipped: {a.day.nunique() - len(recs)} of {a.day.nunique()})",
                 "  If volume matters, the high-volume line (top) should be clearly better than both comparison numbers."]
        h["analysis"] = "\n".join(_xs_report(
            "VOLUME-CONFIRMED MORNING MOVE (market-neutral, intraday)",
            f"  at 10:15, among stocks with 09:15-10:15 volume at least 2x their 20-day usual: long the {K} strongest, short the {K} weakest movers; enter 10:20, exit at the close",
            recs, h, extra))
    except Exception as e:
        h["analysis"] = f"Volume test failed: {e}"


def fno_sector_job(K=3, need=5):
    """STRONGEST STOCKS IN THE STRONGEST SECTOR: at 10:15 find the sector that moved most vs the market (and the weakest);
    long the K strongest stocks of the strong sector, short the K weakest of the weak sector. Enter 10:20, exit at the close."""
    h = hist_state()
    try:
        a = intraday_panel(h, "sector test")
        a = a[a.ind != ""]
        recs, whole = [], []
        for d, g in a.groupby("day", sort=True):
            cnt = g.groupby("ind").size()
            ok = cnt[cnt >= need].index
            gg = g[g.ind.isin(ok)]
            if len(ok) < 4:
                continue
            sec = gg.groupby("ind").sc.mean().sort_values()
            top, bot = gg[gg.ind == sec.index[-1]].sort_values("sc"), gg[gg.ind == sec.index[0]].sort_values("sc")
            recs.append((d, top.ret.to_numpy()[-K:], bot.ret.to_numpy()[:K], g.ret.to_numpy()))
            whole.append((top.ret.mean() - bot.ret.mean()) / 2)
        if len(recs) < 50:
            h["analysis"] = f"Sector test: only {len(recs)} usable days."
            return
        extra = ["", f"  comparison: the WHOLE strongest sector vs the WHOLE weakest sector (all their stocks): {np.mean(whole):+.1f} bp",
                 f"  sectors used: those with at least {need} stocks with data that day ({a.ind.nunique()} industries in the list)"]
        h["analysis"] = "\n".join(_xs_report(
            "STRONGEST STOCKS IN THE STRONGEST SECTOR (market-neutral, intraday)",
            f"  at 10:15: strongest sector vs the market -> long its {K} strongest stocks; weakest sector -> short its {K} weakest; enter 10:20, exit at the close",
            recs, h, extra))
    except Exception as e:
        h["analysis"] = f"Sector test failed: {e}"


def sector_map():
    """Stock -> industry, from NSE's Nifty 500 list saved in the repo as sectors.csv (next to app.py)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sectors.csv")
    if not os.path.exists(p):
        return {}
    d = pd.read_csv(p)
    return dict(zip(d["Symbol"].astype(str).str.strip(), d["Industry"].astype(str).str.strip()))


# ======================= Weekly swing paper list (forward test of SWING v2) =======================
SW_K = 10          # stocks long and stocks short each week
SW_MIN = 40        # need at least this many stocks with data on the decision day


def swing_build(syms):
    """Features for every stock-day (same as SWING v2) plus the target where the 5-day outcome is already known."""
    F, FW, V, DY, SY, DC = [], [], [], [], [], []
    for sy in syms:
        with LOCK:
            c = db()
            rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
            c.close()
        days = np.array([r[0] for r in rows])
        C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
        if not len(C):
            continue
        ix = np.where(np.isnan(C), 0, np.arange(75))
        C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
        ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
        C, days = C[ok], days[ok]
        n = len(days)
        if n < 200:
            continue
        O, D = C[:, 0], C[:, -1]
        sh = lambda a, k: np.concatenate([np.full(k, np.nan), a[:-k]])
        dr = np.concatenate([[np.nan], D[1:] / D[:-1] - 1])
        sD = pd.Series(D)
        vs = pd.Series(dr).rolling(20).std().to_numpy()
        mx, mn, ma = sD.rolling(20).max().to_numpy(), sD.rolling(20).min().to_numpy(), sD.rolling(20).mean().to_numpy()
        v = np.maximum(vs, 1e-4)
        feats = [(D / sh(D, k) - 1) / v for k in (1, 3, 5, 10, 20, 60)]
        feats += [(sh(D, 20) / sh(D, 120) - 1) / v, (D - mn) / (mx - mn + 1e-9), (D / ma - 1) / v, (O / sh(D, 1) - 1) / v,
                  (D / O - 1) / v, (C[:, -1] / C[:, -13] - 1) / v, np.log(v)]
        fwd = np.full(n, np.nan)
        fwd[:n - 5] = D[5:] / O[1:n - 4] - 1
        dd = days.astype("datetime64[D]")
        span = np.full(n, 99.0)
        span[:n - 5] = (dd[5:] - dd[:-5]).astype(float)
        X = np.stack(feats, 1)
        okf = np.isfinite(vs) & (np.arange(n) >= 120) & np.isfinite(X).all(1)
        F.append(np.clip(X[okf], -8, 8))
        FW.append(np.where(span <= 11, fwd, np.nan)[okf])
        V.append(v[okf])
        DY.append(days[okf])
        DC.append(D[okf])
        SY.append(np.array([sy] * int(okf.sum()), dtype=object))
    F, FW, V, DY, SY, DC = (np.concatenate(x) for x in (F, FW, V, DY, SY, DC))
    F = (pd.DataFrame(F) - pd.DataFrame(F).groupby(DY).transform("mean")).to_numpy()
    mk = pd.Series(FW).groupby(DY).transform("mean").to_numpy()
    return F, np.clip((FW - mk) / V, -8, 8), DY, SY, DC


def swing_db():
    with LOCK:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS swingpaper(wk_day TEXT, sym TEXT, side TEXT, ref REAL, entry_day TEXT, entry REAL,
                     exit_day TEXT, exit REAL, bp REAL, PRIMARY KEY(wk_day, sym))""")
        c.commit()
        c.close()


def swing_list(now, require_today=True):
    """Rank every F&O stock with the SWING v2 model; long the top, short the bottom; save and send on Telegram."""
    ls = live_state()
    swing_db()
    with LOCK:
        c = db()
        syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
        c.close()
    F, yx, DY, SY, DC = swing_build(syms)
    d_last = str(np.unique(DY)[-1])
    if require_today and d_last != now.strftime("%Y-%m-%d"):
        ls["note"] = f"weekly list: today's data not stored yet (latest {d_last}), will retry"
        return False
    tr = np.isfinite(yx)
    mu, sd = F[tr].mean(0), F[tr].std(0) + 1e-9
    A = (F[tr] - mu) / sd
    w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(A.shape[1]), A.T @ (yx[tr] - yx[tr].mean()))
    te = np.where(DY == d_last)[0]
    if len(te) < SW_MIN:
        ls["note"] = f"weekly list: only {len(te)} stocks have data for {d_last}"
        return False
    p = ((F[te] - mu) / sd) @ w
    o = np.argsort(p)
    longs, shorts = te[o[-SW_K:]][::-1], te[o[:SW_K]]
    with LOCK:
        c = db()
        for g, side in [(g, "BUY") for g in longs] + [(g, "SELL") for g in shorts]:
            c.execute("INSERT OR IGNORE INTO swingpaper(wk_day, sym, side, ref) VALUES(?,?,?,?)", (d_last, SY[g], side, float(DC[g])))
        c.commit()
        c.close()
    send_telegram(f"WEEKLY PAPER LIST (data of {d_last}, {len(te)} stocks ranked)\nLONG: {', '.join(SY[longs])}\nSHORT: {', '.join(SY[shorts])}\n"
                  "Enter next trading morning 09:20, exit next Friday close. Paper only. Research, not advice.")
    ls["note"] = f"weekly list saved for {d_last}"
    return True


def swing_enter(now):
    """Next trading morning: record the 09:20 price as the entry of each listed stock. Returns how many are still waiting."""
    swing_db()
    d_s = now.strftime("%Y-%m-%d")
    with LOCK:
        c = db()
        pend = c.execute("SELECT wk_day, sym FROM swingpaper WHERE entry IS NULL AND wk_day < ?", (d_s,)).fetchall()
        c.close()
    if not pend:
        return 0
    ls = live_state()
    if not ls["ids"]:
        ls["ids"] = fno_ids()
    left = 0
    for wk, sy in pend:
        try:
            arr = day_bars(ls["ids"][sy], now.date())
            time.sleep(0.12)
            if np.isnan(arr[0]):
                left += 1
                continue
            with LOCK:
                c = db()
                c.execute("UPDATE swingpaper SET entry_day=?, entry=? WHERE wk_day=? AND sym=?", (d_s, float(arr[0]), wk, sy))
                c.commit()
                c.close()
        except Exception as e:
            left += 1
            ls["note"] = f"swing entry error {sy}: {str(e)[:100]}"
    return left


def swing_exit(now):
    """Friday after the close: exit last week's list at today's close and send the week's result."""
    swing_db()
    d_s = now.strftime("%Y-%m-%d")
    ls = live_state()
    with LOCK:
        c = db()
        op = c.execute("SELECT wk_day, sym, side, entry FROM swingpaper WHERE entry IS NOT NULL AND exit IS NULL AND wk_day < ?", (d_s,)).fetchall()
        c.close()
    if not op:
        return
    if not ls["ids"]:
        ls["ids"] = fno_ids()
    done = {}
    for wk, sy, side, entry in op:
        try:
            with LOCK:
                c = db()
                r = c.execute("SELECT b FROM fnod WHERE sym=? AND day=?", (sy, d_s)).fetchone()
                c.close()
            arr = np.frombuffer(r[0], np.float32).astype(float) if r else day_bars(ls["ids"][sy], now.date())
            ok = arr[~np.isnan(arr)]
            if not len(ok):
                continue
            bp = (1 if side == "BUY" else -1) * (ok[-1] / entry - 1) * 1e4
            with LOCK:
                c = db()
                c.execute("UPDATE swingpaper SET exit_day=?, exit=?, bp=? WHERE wk_day=? AND sym=?", (d_s, float(ok[-1]), float(bp), wk, sy))
                c.commit()
                c.close()
            done.setdefault(wk, []).append((side, bp))
        except Exception as e:
            ls["note"] = f"swing exit error {sy}: {str(e)[:100]}"
    for wk, lst in done.items():
        lg = [b for s, b in lst if s == "BUY"]
        sh_ = [b for s, b in lst if s == "SELL"]
        send_telegram(f"WEEKLY PAPER RESULT (list of {wk}): per trade {np.mean([b for _, b in lst]):+.0f} bp; "
                      f"long avg {np.mean(lg) if lg else 0:+.0f} bp ({len(lg)}), short avg {np.mean(sh_) if sh_ else 0:+.0f} bp ({len(sh_)}). Research, not advice.")


# ======================= Live paper alerts =======================
SIG_MIN = float(os.environ.get("SIG_MIN", "1.7"))      # alert only when the signal is at least this many times the entry threshold
STRONG = 2.5


@st.cache_resource
def live_state():
    return {"models": {}, "ids": {}, "fit_day": "", "scanned": -1, "closed": "", "hist": "", "note": "starting...",
            "last_scan": "", "thread": None, "busy": ""}


def day_bars(sid, day):
    """Today's 5-min closes of one stock, as an array of 75 (nan where missing)."""
    body = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "interval": "5", "oi": False,
            "fromDate": day.strftime("%Y-%m-%d"), "toDate": (day + timedelta(days=1)).strftime("%Y-%m-%d")}
    j = hist_post(body, path="/charts/intraday")
    j = j.get("data") or j
    ts, cl = j.get("timestamp") or [], j.get("close") or []
    arr = np.full(75, np.nan)
    if ts:
        off = 315532800 if datetime.fromtimestamp(ts[0], IST).year < 2020 else 0
        for t_, v_ in zip(ts, cl):
            x = datetime.fromtimestamp(t_ + off, IST)
            k = (x.hour * 60 + x.minute - 555) // 5
            if x.date() == day and 0 <= k < 75:
                arr[k] = v_
    return arr


def live_fit():
    """Train one model per stock on all stored history (same model as the backtest)."""
    ls = live_state()
    ls["busy"] = "training models..."
    ids = fno_ids()
    with LOCK:
        c = db()
        have = {r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450")}
        c.close()
    models = {}
    for sy, sid in ids.items():
        if sy not in have:
            continue
        with LOCK:
            c = db()
            rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
            c.close()
        C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
        C = np.take_along_axis(C, np.maximum.accumulate(np.where(np.isnan(C), 0, np.arange(75)), axis=1), axis=1)
        ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
        C, days = C[ok], [r[0] for r, o in zip(rows, ok) if o]
        if len(C) < 450:
            continue
        X3, Y3, _ = fno_feats(C)
        Xt = X3.reshape(-1, 12)
        mu, sd = Xt.mean(0), Xt.std(0) + 1e-9
        A = (Xt - mu) / sd
        yv = Y3.reshape(-1)
        w = np.linalg.solve(A.T @ A + 0.1 * len(A) * np.eye(12), A.T @ (yv - yv.mean()))
        models[sy] = dict(mu=mu, sd=sd, w=w, thr=float(np.percentile(np.abs(A @ w), 90)), prev=C[-1], prevday=days[-1], id=sid)
    ls["models"], ls["ids"] = models, ids
    ls["busy"] = ""
    ls["note"] = f"{len(models)} stock models ready"


def paper_db():
    with LOCK:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS fnopaper(day TEXT, sym TEXT, t_in TEXT, side TEXT, entry REAL, ratio REAL,
                     t_out TEXT, exit REAL, bp REAL, PRIMARY KEY(day, sym))""")
        c.commit()
        c.close()


def live_scan(now):
    """Look at every stock's finished 5-min bars; the first confident moment of the day makes one paper trade + Telegram alert."""
    ls = live_state()
    day = now.date()
    d_s = day.strftime("%Y-%m-%d")
    kmax = (now.hour * 60 + now.minute - 555) // 5 - 1          # last FINISHED bar (the forming bar is ignored)
    if kmax < 12:
        return 0
    paper_db()
    with LOCK:
        c = db()
        done = {r[0] for r in c.execute("SELECT sym FROM fnopaper WHERE day=?", (d_s,))}
        c.close()
    n = 0
    for sy, m in ls["models"].items():
        if sy in done:
            continue
        try:
            if (day - datetime.strptime(m["prevday"], "%Y-%m-%d").date()).days > 6:
                continue                                          # yesterday missing: skip this stock today
            arr = day_bars(m["id"], day)
            time.sleep(0.12)
            row = arr[:kmax + 1]
            if np.isnan(row[0]):
                continue
            row = np.where(np.isnan(row), 0, row)
            row = np.take_along_axis(row[None], np.maximum.accumulate(np.where(row == 0, 0, np.arange(len(row))))[None], axis=1)[0]
            row = np.concatenate([row, np.full(75 - len(row), row[-1])])
            X = fno_feats(np.stack([m["prev"], row]))[0][1]      # today's features for bars 12..63
            top = min(kmax, 63) - 12
            if top < 0:
                continue
            ratio = np.abs(((X[:top + 1] - m["mu"]) / m["sd"]) @ m["w"]) / m["thr"]
            hit = np.where(ratio >= SIG_MIN)[0]
            if not len(hit):
                continue
            j = int(hit[0])
            k = j + 12
            if k < kmax - 2:
                continue                                          # signal was long ago (service started late): do not chase
            side = "BUY" if (((X[j] - m["mu"]) / m["sd"]) @ m["w"]) > 0 else "SELL"
            t_in = f"{(555 + 5 * k + 5) // 60:02d}:{(555 + 5 * k + 5) % 60:02d}"
            with LOCK:
                c = db()
                c.execute("INSERT OR IGNORE INTO fnopaper(day,sym,t_in,side,entry,ratio) VALUES(?,?,?,?,?,?)",
                          (d_s, sy, t_in, side, float(row[k]), float(ratio[j])))
                c.commit()
                c.close()
            send_telegram(f"PAPER {side} {sy} at {t_in}, price {row[k]:,.2f}. Signal {ratio[j]:.1f}x"
                          f"{' (STRONG)' if ratio[j] >= STRONG else ''}. Paper trade, exit about 15:25. Research, not advice.")
            n += 1
        except Exception as e:
            ls["note"] = f"scan error {sy}: {str(e)[:100]}"
            if "401" in str(e) or "DH-906" in str(e):
                ls["note"] = "Dhan token expired: update DHAN_ACCESS_TOKEN in Render"
                return n
    ls["last_scan"] = f"{now:%H:%M} bar {kmax}, {n} new"
    return n


def live_close(now):
    """After the close: record each paper trade's exit (15:25 bar) and send a one-line summary."""
    paper_db()
    d_s = now.strftime("%Y-%m-%d")
    ls = live_state()
    with LOCK:
        c = db()
        op = c.execute("SELECT sym, side, entry FROM fnopaper WHERE day=? AND exit IS NULL", (d_s,)).fetchall()
        c.close()
    bps = []
    if not ls["ids"]:
        ls["ids"] = fno_ids()
    for sy, side, entry in op:
        try:
            arr = day_bars(ls["ids"][sy], now.date())
            time.sleep(0.12)
            ok = arr[~np.isnan(arr)]
            if not len(ok):
                ls["note"] = f"close: no bars for {sy}, will retry"
                print("close: no bars", sy, flush=True)
                continue
            bp = (1 if side == "BUY" else -1) * (ok[-1] / entry - 1) * 1e4
            with LOCK:
                c = db()
                c.execute("UPDATE fnopaper SET t_out=?, exit=?, bp=? WHERE day=? AND sym=?", ("15:25", float(ok[-1]), float(bp), d_s, sy))
                c.commit()
                c.close()
            bps.append(bp)
        except Exception as e:
            ls["note"] = f"close error {sy}: {str(e)[:100]}"
            print("close error", sy, e, flush=True)
    if bps:
        send_telegram(f"PAPER RESULTS today: {len(bps)} trades, average {np.mean(bps):+.1f} bp, best {max(bps):+.0f}, worst {min(bps):+.0f}. Research, not advice.")
    return len(op) - len(bps)                                     # trades still open


def live_loop():
    ls = live_state()
    while True:
        try:
            now = datetime.now(IST).replace(tzinfo=None)
            mnow, d_s = now.hour * 60 + now.minute, now.strftime("%Y-%m-%d")
            if now.weekday() < 5:
                if ls["fit_day"] != d_s and mnow >= 525 and CID and TOKEN:      # from 08:45: top up history, then train
                    ls["fit_day"] = d_s
                    ls["busy"] = "topping up history..."
                    fno_job(10)
                    live_fit()
                if ls["models"] and 555 + 5 * 13 <= mnow < 14 * 60 + 40 and ((mnow - 555) % 5 > 0 or now.second >= 25):
                    kmax = (mnow - 555) // 5 - 1
                    if kmax > ls["scanned"] or ls.get("scanned_day") != d_s:
                        ls["scanned"], ls["scanned_day"] = kmax, d_s
                        live_scan(now)
                if mnow >= 15 * 60 + 31 and ls["closed"] != d_s and (mnow - ls.get("close_try", 0) >= 3):
                    ls["close_try"] = mnow
                    left = live_close(now)                                  # retry every 3 min until every exit is saved
                    if not left or mnow >= 18 * 60:
                        ls["closed"] = d_s
                if mnow >= 15 * 60 + 45 and ls["hist"] != d_s and CID and TOKEN:
                    ls["hist"] = d_s
                    fno_job(5)
                if (9 * 60 + 25 <= mnow < 11 * 60 and CID and TOKEN and ls.get("swing_e_day") != d_s
                        and mnow - ls.get("swing_e", -99) >= 5):                    # weekly list: record entry prices
                    ls["swing_e"] = mnow
                    if swing_enter(now) == 0:
                        ls["swing_e_day"] = d_s
                if (now.weekday() == 4 and mnow >= 15 * 60 + 50 and ls["hist"] == d_s and ls.get("swing") != d_s
                        and mnow - ls.get("swing_t", -99) >= 10):                   # Friday: close last week's list, make the next
                    ls["swing_t"] = mnow
                    swing_exit(now)
                    if swing_list(now):
                        ls["swing"] = d_s
        except Exception as e:
            ls["note"] = f"loop error: {str(e)[:120]}"
        time.sleep(20)


def ensure_live():
    ls = live_state()
    if not (CID and TOKEN):
        return
    if ls["thread"] is None or not ls["thread"].is_alive():
        ls["thread"] = threading.Thread(target=live_loop, daemon=True)
        ls["thread"].start()


# ======================= Page =======================
st.set_page_config(page_title="F&O stock scanner (research)", layout="wide")
st.title("F&O stock scanner (research)")
st.caption("Same price model as the NIFTY dashboard, applied to every F&O stock. Research only, not advice.")
_hs = hist_state()
if not (CID and TOKEN):
    st.error("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Render (Environment tab), then redeploy.")
_d = st.number_input("Days back (calendar)", 30, 1800, 1800, step=30, help="1800 = about 5 years. Already downloaded days are skipped.")
if st.button("1) Download all F&O stocks 5-min (long, run after 15:30)", disabled=_hs["running"]):
    threading.Thread(target=fno_job, args=(int(_d),), daemon=True).start()
    _hs["msg"] = "starting..."
    _hs["running"] = True
if st.button("2) Run backtest"):
    threading.Thread(target=fno_bt_job, args=("NIFTY",), daemon=True).start()
    _hs["analysis"] = "Running F&O backtest... (a few minutes)"
if st.button("3) Run SWING backtest (hold 1 week)"):
    threading.Thread(target=fno_swing_job, daemon=True).start()
    _hs["analysis"] = "Running swing backtest... (a few minutes)"
if st.button("4) Run SWING v2 (market-neutral, long top 10 / short bottom 10)"):
    threading.Thread(target=fno_swing2_job, daemon=True).start()
    _hs["analysis"] = "Running swing v2... (a few minutes)"
if st.button("5) Run SWING exit test (hold vs take profit 3/4/5% vs stop-loss 3%)"):
    threading.Thread(target=fno_swing3_job, daemon=True).start()
    _hs["analysis"] = "Running swing exit test... (a few minutes)"
if st.button("6) Run INTRADAY ranking test (market-neutral, long top 10 / short bottom 10 at 10:15)"):
    threading.Thread(target=fno_xs_job, daemon=True).start()
    _hs["analysis"] = "Running intraday ranking test... (a few minutes)"
if st.button("7) Run VOLUME test (morning move confirmed by 2x volume)"):
    threading.Thread(target=fno_vol_job, daemon=True).start()
    _hs["analysis"] = "Running volume test... (a few minutes)"
if st.button("8) Run SECTOR test (strongest stocks in the strongest sector)"):
    threading.Thread(target=fno_sector_job, daemon=True).start()
    _hs["analysis"] = "Running sector test... (a few minutes)"


@st.fragment(run_every=5)
def status():
    st.caption(f"{'Running' if _hs['running'] else 'Idle'} - {_hs['msg'] or 'not started'} - {_hs['rows']:,} stock-days saved")
    for e in _hs["errs"]:
        st.caption("error: " + e)
    for e in _hs["probe"]:
        st.caption(e)
    if _hs.get("analysis"):
        st.code(_hs["analysis"], language=None)


status()
ensure_live()
try:
    _sm = sector_map()
    with LOCK:
        _c = db()
        _have = [r[0] for r in _c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450")]
        _c.close()
    if not _sm:
        st.caption("Sectors: sectors.csv not found in the repo.")
    else:
        _miss = [s for s in _have if s not in _sm]
        st.caption(f"Sectors: {len(_have) - len(_miss)} of {len(_have)} stocks matched to {len({_sm[s] for s in _have if s in _sm})} industries."
                   + (f" Not in the list: {', '.join(_miss[:30])}" if _miss else ""))
except Exception as _e:
    st.caption(f"Sectors: could not read ({_e})")


@st.cache_data(ttl=600)
def data_check():
    with LOCK:
        c = db()
        a = c.execute("SELECT COUNT(*), COUNT(DISTINCT sym), MIN(day), MAX(day) FROM fnod").fetchone()
        try:
            b = c.execute("SELECT COUNT(*), COUNT(DISTINCT sym), MIN(day), MAX(day) FROM fnov").fetchone()
            r = c.execute("SELECT b FROM fnov ORDER BY day DESC LIMIT 1").fetchone()
            vtot = float(np.nansum(np.frombuffer(r[0], np.float32).reshape(3, 75)[2])) if r else 0.0
        except Exception:
            b, vtot = (0, 0, "-", "-"), 0.0
        c.close()
    return a, b, vtot


try:
    _a, _b, _v = data_check()
    st.caption(f"Stored data: prices {_a[0]:,} stock-days ({_a[1]} stocks, {_a[2]} to {_a[3]});  "
               f"high/low/volume {_b[0]:,} stock-days ({_b[1]} stocks, {_b[2]} to {_b[3]}); latest day's volume of one stock: {_v:,.0f}  (refreshes every 10 min)")
except Exception as _e:
    st.caption(f"Stored data: could not read ({_e})")


@st.fragment(run_every=30)
def live_panel():
    ls = live_state()
    st.subheader("Live paper alerts")
    st.caption(f"Signal strength at least {SIG_MIN}x. {ls['note']} {ls['busy']} Last scan: {ls['last_scan'] or 'none yet'}. "
               "From 08:45 it tops up data and trains; scans every 5 min from 10:19; exits at 15:25. Paper only, no money.")
    try:
        with LOCK:
            c = db()
            t = pd.read_sql("SELECT * FROM fnopaper ORDER BY day DESC, t_in DESC LIMIT 300", c)
            c.close()
    except Exception:
        t = pd.DataFrame()
    if t.empty:
        st.caption("No paper trades yet.")
        return
    dn = t[t.bp.notna()]
    if len(dn):
        st.write(f"Closed: {len(dn)} trades on {dn.day.nunique()} days, right {(dn.bp > 0).mean() * 100:.0f}%, "
                 f"average {dn.bp.mean():+.1f} bp, after 10 bp cost {dn.bp.mean() - 10:+.1f} bp")
    st.dataframe(t, hide_index=True)


live_panel()


@st.fragment(run_every=60)
def swing_panel():
    st.subheader("Weekly swing paper list (forward test of SWING v2)")
    st.caption(f"Every Friday after 15:50: long the top {SW_K} and short the bottom {SW_K} stocks by the market-neutral model, "
               "entry next trading morning 09:20, exit next Friday close. Paper only, no money.")
    try:
        swing_db()
        with LOCK:
            c = db()
            t = pd.read_sql("SELECT * FROM swingpaper ORDER BY wk_day DESC, side, sym LIMIT 120", c)
            c.close()
    except Exception:
        t = pd.DataFrame()
    if t.empty:
        st.caption("No weekly list yet. The first one appears on a Friday after 15:50, or press the button below.")
        return
    dn = t[t.bp.notna()]
    if len(dn):
        wkr = dn.groupby("wk_day").bp.mean()
        st.write(f"Finished weeks: {len(wkr)}, average per trade {dn.bp.mean():+.1f} bp, after 10 bp cost {dn.bp.mean() - 10:+.1f} bp, "
                 f"weeks positive {(wkr > 0).mean() * 100:.0f}%")
    st.dataframe(t, hide_index=True)


swing_panel()
if st.button("Make the weekly list now (uses the latest stored day)"):
    with st.spinner("Ranking all stocks..."):
        try:
            _ok = swing_list(datetime.now(IST).replace(tzinfo=None), require_today=False)
            st.info(f"{'Saved' if _ok else 'Not saved'}: {live_state()['note']}")
        except Exception as _e:
            st.error(f"Failed: {_e}")
if st.button("Send test Telegram"):
    ok, info = send_telegram("Test from F&O scanner: Telegram works.")
    if ok:
        st.success("Test sent")
    else:
        st.error(f"Failed: {info}")
if st.button("Close today's open paper trades now (use after 15:30)"):
    left = live_close(datetime.now(IST).replace(tzinfo=None))
    st.info(f"Done. Still open: {left}. {live_state()['note']}")
st.caption("If the box does not update, reload the page. The work continues on the server.")

# === END OF app.py (if you do not see this line at the bottom, the paste was cut off) ===
