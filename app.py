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
            have = {}
            for sy, dd in c.execute("SELECT sym, day FROM fnod"):
                have.setdefault(sy, set()).add(dd)
            c.commit()
            c.close()
        today = datetime.now(IST).date()
        a0, wins = today - timedelta(days=days), []
        while a0 <= today:
            wins.append((a0, min(a0 + timedelta(days=85), today + timedelta(days=1))))
            a0 += timedelta(days=85)
        h["total"], bad, n = len(ids) * len(wins), 0, 0
        for sy, sid in ids.items():
            for a, b in wins:
                n += 1
                h["done"], h["msg"] = n, f"{n}/{h['total']} requests ({sy})"
                wd = [x for x in (a + timedelta(days=k) for k in range((b - a).days)) if x.weekday() < 5 and x < today]
                if wd and sum(x.strftime("%Y-%m-%d") in have.get(sy, ()) for x in wd) >= 0.8 * len(wd):
                    continue
                body = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "interval": "5", "oi": False,
                        "fromDate": a.strftime("%Y-%m-%d"), "toDate": b.strftime("%Y-%m-%d")}
                try:
                    j = hist_post(body, path="/charts/intraday")
                    j = j.get("data") or j
                    ts, cl = j.get("timestamp") or [], j.get("close") or []
                    if ts:
                        off = 315532800 if datetime.fromtimestamp(ts[0], IST).year < 2020 else 0
                        dd = {}
                        for t_, v_ in zip(ts, cl):
                            x = datetime.fromtimestamp(t_ + off, IST)
                            k = (x.hour * 60 + x.minute - 555) // 5
                            if x.weekday() < 5 and 0 <= k < 75:
                                dd.setdefault(f"{x:%Y-%m-%d}", np.full(75, np.nan, np.float32))[k] = v_
                        rows = [(sy, k_, v_.tobytes()) for k_, v_ in dd.items() if np.isfinite(v_).sum() >= 70]
                        with LOCK:
                            c = db()
                            c.executemany("INSERT OR REPLACE INTO fnod VALUES(?,?,?)", rows)
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
                    for dd, s_, yy, rr, y1, k_ in zip(days[te][has], sgn[has], y_[has], rat[has], yd[has], f[has] + 12):
                        res[rep].append((dd, s_ * yy, s_, yy, sy, rr, s_ * y1, k_))
        def stat(L):
            a = pd.DataFrame(L, columns=["day", "pnl", "side", "raw", "sym", "rat", "pnl1", "k"])
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
        out += ["", "  (Strong signals earn more, which is what a real effect looks like. Still check: circuit-limit stocks, wide spreads, F&O ban list.)"]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"F&O backtest failed: {e}"




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
if st.button("Send test Telegram"):
    ok, info = send_telegram("Test from F&O scanner: Telegram works.")
    if ok:
        st.success("Test sent")
    else:
        st.error(f"Failed: {info}")
st.caption("If the box does not update, reload the page. The work continues on the server.")

# === END OF app.py (if you do not see this line at the bottom, the paste was cut off) ===
