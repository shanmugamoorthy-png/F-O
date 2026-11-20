"""F&O price-model alerts (paper only). Intraday: the NIFTY dashboard's price model on every F&O stock, Telegram alert at
signal strength 1.7x+, exit 15:25. Weekly: market-neutral swing list (long top 10 / short bottom 10), Friday to Friday.
Research tests were removed after review; a full research copy is kept separately."""
import gc
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


def sector_map():
    """Stock -> industry, from NSE's Nifty 500 list saved in the repo as sectors.csv (next to app.py)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sectors.csv")
    if not os.path.exists(p):
        return {}
    d = pd.read_csv(p)
    return dict(zip(d["Symbol"].astype(str).str.strip(), d["Industry"].astype(str).str.strip()))


# ======================= Research: Donchian channel backtests (run after market hours) =======================
DC_N, DC_EXIT = 20, 10          # channel length and exit-channel length (classic turtle rules), fixed in advance


def _stock_hlc(sy):
    """5-min high, low, close of one stock for all stored days (days x 75), only days with both prices and high/low."""
    with LOCK:
        c = db()
        rows = c.execute("SELECT d.day, d.b, v.b FROM fnod d JOIN fnov v ON v.sym = d.sym AND v.day = d.day "
                         "WHERE d.sym=? ORDER BY d.day", (sy,)).fetchall()
        c.close()
    if not rows:
        return None
    days = np.array([r[0] for r in rows])
    C = np.array([np.frombuffer(r[1], np.float32) for r in rows], float)
    HV = np.array([np.frombuffer(r[2], np.float32).reshape(3, 75)[:2] for r in rows], float)
    ix = np.where(np.isnan(C), 0, np.arange(75))
    C = np.take_along_axis(C, np.maximum.accumulate(ix, axis=1), axis=1)
    ok = ~np.isnan(C[:, 0]) & (C.min(1) > 0)
    C, H, L, days = C[ok], HV[ok, 0], HV[ok, 1], days[ok]
    H = np.where(np.isfinite(H) & (H >= C), H, C)
    L = np.where(np.isfinite(L) & (L <= C) & (L > 0), L, C)
    return days, H, L, C


def _roll(a, n, fn):
    """Value of fn over the previous n items (excluding the current one); NaN where not enough history."""
    from numpy.lib.stride_tricks import sliding_window_view as swv
    out = np.full(len(a), np.nan)
    if len(a) > n:
        out[n:] = fn(swv(a[:-1], n), axis=-1)
    return out


def _dc_report(title, rules, a, unit_key, extra):
    tt = lambda x: x.mean() / (x.std() / np.sqrt(len(x)) + 1e-9) if len(x) > 2 else 0.0
    g = a.groupby(unit_key).pnl.mean()
    yrs = sorted(set(a.yr))
    out = [title, rules,
           f"  {len(a):,} trades on {a.sym.nunique()} stocks, {g.size} {'days' if unit_key == 'day' else 'weeks'}, years {', '.join(yrs)}",
           f"  average per trade {a.pnl.mean():+.1f} bp, right {(a.pnl > 0).mean() * 100:.0f}%, median {a.pnl.median():+.1f} bp",
           f"  per {'DAY' if unit_key == 'day' else 'WEEK'} (all trades averaged, each counted once): {g.mean():+.1f} bp, t-stat {tt(g):+.1f}, positive {(g > 0).mean() * 100:.0f}%",
           "  by year: " + "  ".join(f"{y}: {a[a.yr == y].pnl.mean():+.1f} bp (n={int((a.yr == y).sum())})" for y in yrs),
           f"  BUY (upper breakout) {a[a.side > 0].pnl.mean():+.1f} bp (n={int((a.side > 0).sum())});  SELL (lower breakdown) {a[a.side < 0].pnl.mean():+.1f} bp (n={int((a.side < 0).sum())})",
           "  after a round-trip cost of:  " + "   ".join(f"{k} bp: {a.pnl.mean() - k:+.1f}" for k in (5, 10, 20)),
           f"  best 10 {'days' if unit_key == 'day' else 'weeks'} make {np.sort(g.to_numpy())[::-1][:10].sum() / g.sum() * 100 if g.sum() else 0:.0f}% of the total;  worst trade {a.pnl.min():+.0f} bp"]
    out += extra
    out += ["", "  Real only if: t-stat above 3, positive in EVERY year, and still positive after a realistic cost "
            "(about 10 bp intraday, 10-20 bp for a week). One fixed design, not tuned. Survivorship: today's F&O list. Not advice."]
    return "\n".join(out)


# ======================= Research: monthly momentum (fixed rules, set before testing) =======================
MOM_TOP = 20             # stocks held
MOM_COST = 25.0          # bp per 100% of the portfolio replaced (delivery: STT 0.1% x2, stamp, exchange, GST, DP)


def _daily_panel(progress=None):
    """Daily 09:20 price and 15:25 close of every stock (days x stocks) from the stored 5-min history."""
    with LOCK:
        c = db()
        syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 200 ORDER BY sym")]
        c.close()
    op, cl = {}, {}
    for n_, sy in enumerate(syms):
        if progress and n_ % 10 == 0:
            progress(f"Monthly momentum: loading {n_ + 1}/{len(syms)}")
        with LOCK:
            c = db()
            rows = c.execute("SELECT day, b FROM fnod WHERE sym=? ORDER BY day", (sy,)).fetchall()
            c.close()
        o_, c_ = {}, {}
        for d, b in rows:
            a = np.frombuffer(b, np.float32)
            v = a[np.isfinite(a) & (a > 0)]
            if len(v) >= 30:
                o_[d], c_[d] = float(v[0]), float(v[-1])
        op[sy], cl[sy] = pd.Series(o_), pd.Series(c_)
    O, C = pd.DataFrame(op).sort_index(), pd.DataFrame(cl).sort_index()
    good = C.notna().sum(1) >= 0.5 * C.shape[1]          # drop thin days (holidays / partial downloads)
    return O[good], C[good]


def momentum_job():
    """Classic 12-1 momentum, monthly. At each month-end rank stocks by the return from 12 months ago to 1 month ago,
    buy the top 20 equally at the next trading day's 09:20 price, hold to the next month's first 09:20 price.
    Compared with holding all F&O stocks equally. Also shown: 6-month momentum and a long-short (top 20 vs bottom 20)."""
    h = hist_state()
    h.update(an_running=True, analysis="Monthly momentum: starting...")
    try:
        O, C = _daily_panel(lambda m: h.update(analysis=m))
        days = pd.to_datetime(C.index)
        mon = days.to_period("M")
        last_ix = [np.where(mon == m)[0][-1] for m in mon.unique()]       # last trading day of each month
        rows = []
        for a_, b_ in zip(last_ix[:-1], last_ix[1:]):
            t, e, x = a_, a_ + 1, b_ + 1                                     # signal close, entry day, exit day
            if x >= len(C) or t < 252:
                continue
            ent, ext = O.iloc[e], O.iloc[x]
            r = (ext / ent - 1) * 1e4
            m12 = C.iloc[t - 21] / C.iloc[t - 252] - 1
            m6 = C.iloc[t] / C.iloc[t - 126] - 1
            ok = r.notna() & m12.notna() & m6.notna()
            if ok.sum() < 60:
                continue
            rr, s12, s6 = r[ok].clip(-5000, 5000), m12[ok], m6[ok]
            top12 = s12.nlargest(MOM_TOP).index
            bot12 = s12.nsmallest(MOM_TOP).index
            top6 = s6.nlargest(MOM_TOP).index
            rows.append({"month": str(mon[e]), "n": int(ok.sum()), "mkt": rr.mean(), "top12": rr[top12].mean(),
                         "bot12": rr[bot12].mean(), "top6": rr[top6].mean(), "set12": set(top12), "set6": set(top6)})
        if len(rows) < 12:
            h["analysis"] = f"Monthly momentum: only {len(rows)} months testable (needs 12 months of history first)."
            return
        a = pd.DataFrame(rows)
        for k in ("12", "6"):                                                # turnover -> cost each month
            prev, tc = None, []
            for st_ in a[f"set{k}"]:
                tc.append(1.0 if prev is None else len(st_ - prev) / MOM_TOP)
                prev = st_
            a[f"to{k}"] = tc
        a["net12"] = a.top12 - a.to12 * MOM_COST
        a["net6"] = a.top6 - a.to6 * MOM_COST
        a["ls"] = a.top12 - a.bot12 - 2 * a.to12 * MOM_COST
        a["yr"] = a.month.str[:4]
        tt = lambda x: x.mean() / (x.std() / np.sqrt(len(x)) + 1e-9) if len(x) > 2 else 0.0

        def grow(x):
            eq = (1 + x / 1e4).cumprod()
            yrs = len(x) / 12
            return (eq.iloc[-1] ** (1 / yrs) - 1) * 100, ((eq / eq.cummax()) - 1).min() * 100

        def line(name, col):
            ex = a[col] - a.mkt
            cg, dd = grow(a[col])
            return (f"  {name}: {a[col].mean():+.0f} bp a month ({cg:+.1f}% a year), beat the market by {ex.mean():+.0f} bp a month, "
                    f"t-stat {tt(ex):+.2f}, beat it in {(ex > 0).mean() * 100:.0f}% of months, worst fall {dd:.0f}%")
        mg, mdd = grow(a.mkt)
        ex12 = a.net12 - a.mkt
        yr_ex = ex12.groupby(a.yr).mean()
        yr_n = a.groupby("yr").size()
        full = yr_ex[yr_n >= 10]                                             # years with at least 10 tested months
        out = ["MONTHLY MOMENTUM (F&O stocks, fixed rules set before testing)",
               f"  Rank by the return from 12 months ago to 1 month ago; buy the top {MOM_TOP} equally at the next trading day's 09:20 "
               "price; hold one month; repeat.",
               f"  {len(a)} months tested ({a.month.iloc[0]} to {a.month.iloc[-1]}), about {a.n.mean():.0f} stocks ranked each month.",
               f"  Costs: {MOM_COST:.0f} bp for each 100% of the portfolio replaced; average replaced per month: "
               f"{a.to12.iloc[1:].mean() * 100:.0f}% (12-1), {a.to6.iloc[1:].mean() * 100:.0f}% (6-month).",
               "",
               f"  Market (all F&O stocks equally, no costs): {a.mkt.mean():+.0f} bp a month ({mg:+.1f}% a year), worst fall {mdd:.0f}%",
               line("MAIN  12-1 momentum, top 20, after costs", "net12"),
               line("      6-month momentum, top 20, after costs", "net6"),
               f"  Long-short check (buy top 20, sell bottom 20, after costs): {a.ls.mean():+.0f} bp a month, t-stat {tt(a.ls):+.2f}",
               "",
               "  MAIN by year (gain over the market per month, after costs):  "
               + "  ".join(f"{y}: {v:+.0f} bp (n={int((a.yr == y).sum())})" for y, v in yr_ex.items()),
               (f"  best 3 months make {np.sort(ex12.to_numpy())[::-1][:3].sum() / ex12.sum() * 100:.0f}% of the total gain over the market"
                if ex12.sum() > 0 else "  no total gain over the market"),
               "",
               "  Last 6 months (MAIN, gain over market): " + "  ".join(f"{m}: {v:+.0f}" for m, v in zip(a.month.iloc[-6:], ex12.iloc[-6:])),
               "",
               "  PASS RULE (set in advance): MAIN beats the market after costs on average, t-stat above 2, and beats it in at least "
               "3 of the full years (10+ months tested).",
               "  Verdict: " + ("PASSES - worth a forward paper test (one list a month)." if ex12.mean() > 0 and tt(ex12) > 2
                                 and (full > 0).sum() >= min(3, len(full)) else "DOES NOT PASS."),
               "",
               "  Caution: today's F&O list only (stocks that did well were added to F&O, which flatters momentum and the market "
               "line alike). Prices: 5-min history, entry and exit at 09:20. Research only, not advice."]
        h["analysis"] = "\n".join(out)
    except Exception as e:
        h["analysis"] = f"Monthly momentum failed: {e}"
    finally:
        h["an_running"] = False
        gc.collect()


def donchian_intraday_job():
    """5-min Donchian breakout per stock: close above the highest high of the previous 20 candles = BUY (below the lowest
    low = SELL), first signal per stock per day between 09:45 and 14:45, enter at that close; exit when a close crosses the
    10-candle opposite channel, else at the 15:25 close."""
    h = hist_state()
    h.update(an_running=True, analysis="Donchian intraday: starting...")
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        rec = []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Donchian intraday: stock {n_ + 1}/{len(syms)} ({sy})"
            got = _stock_hlc(sy)
            if got is None or len(got[0]) < 60:
                continue
            days, H, L, C = got
            nd = len(days)
            hf, lf, cf = H.ravel(), L.ravel(), C.ravel()
            up, dn = _roll(hf, DC_N, np.max).reshape(nd, 75), _roll(lf, DC_N, np.min).reshape(nd, 75)
            xl, xh = _roll(lf, DC_EXIT, np.min).reshape(nd, 75), _roll(hf, DC_EXIT, np.max).reshape(nd, 75)
            ks = np.arange(6, 67)                                          # signal candles closing 09:50 ... 14:50
            sig = np.where(C[:, ks] > up[:, ks], 1, np.where(C[:, ks] < dn[:, ks], -1, 0))
            has = (sig != 0).any(1)
            for d in np.where(has)[0]:
                j = int(np.argmax(sig[d] != 0))
                k0, sd = ks[j], int(sig[d, j])
                e = C[d, k0]
                rest = C[d, k0 + 1:]
                hit = (rest < xl[d, k0 + 1:]) if sd == 1 else (rest > xh[d, k0 + 1:])
                x = rest[hit.argmax()] if hit.any() else C[d, -1]
                e1 = C[d, k0 + 1]                                          # diagnostic: one candle later
                rest1 = C[d, k0 + 2:]
                hit1 = (rest1 < xl[d, k0 + 2:]) if sd == 1 else (rest1 > xh[d, k0 + 2:])
                x1 = rest1[hit1.argmax()] if hit1.any() else C[d, -1]
                rec.append((days[d], sy, sd, sd * (x / e - 1) * 1e4, sd * (x1 / e1 - 1) * 1e4, sd * (C[d, -1] / e - 1) * 1e4))
        a = pd.DataFrame(rec, columns=["day", "sym", "side", "pnl", "pnl_delay", "pnl_close"])
        if a.empty:
            h["analysis"] = "Donchian intraday: no trades (is the high/low/volume history downloaded?)"
            return
        a["yr"] = a.day.str[:4]
        extra = ["", f"  entered one candle (5 min) later: {a.pnl_delay.mean():+.1f} bp;  "
                     f"same entries held to the 15:25 close (no exit rule): {a.pnl_close.mean():+.1f} bp"]
        h["analysis"] = _dc_report("DONCHIAN INTRADAY (5-min, F&O stocks)",
                                   f"  BUY on a close above the {DC_N}-candle high, SELL below the {DC_N}-candle low; first signal per stock per day "
                                   f"09:45-14:45; exit on a close through the {DC_EXIT}-candle opposite channel or at 15:25", a, "day", extra)
    except Exception as e:
        h["analysis"] = f"Donchian intraday failed: {e}"
    finally:
        h["an_running"] = False
        gc.collect()


def donchian_weekly_job():
    """Daily Donchian breakout per stock: close above the highest high of the previous 20 days = BUY (below the lowest low =
    SELL). Enter next morning (09:20 price). Main exit: close of the 5th trading day. Second line: turtle exit (close through
    the 10-day opposite channel, at most 20 days). One open trade per stock at a time."""
    h = hist_state()
    h.update(an_running=True, analysis="Donchian weekly: starting...")
    try:
        with LOCK:
            c = db()
            syms = [r[0] for r in c.execute("SELECT sym FROM fnod GROUP BY sym HAVING COUNT(*) >= 450 ORDER BY sym")]
            c.close()
        rec, allfwd = [], []
        for n_, sy in enumerate(syms):
            h["analysis"] = f"Donchian weekly: stock {n_ + 1}/{len(syms)} ({sy})"
            got = _stock_hlc(sy)
            if got is None or len(got[0]) < 60:
                continue
            days, H, L, C = got
            D, DH, DL, O1 = C[:, -1], H.max(1), L.min(1), C[:, 0]
            n = len(D)
            up, dn = _roll(DH, DC_N, np.max), _roll(DL, DC_N, np.min)
            xl, xh = _roll(DL, DC_EXIT, np.min), _roll(DH, DC_EXIT, np.max)
            dd = days.astype("datetime64[D]")
            fwd = np.full(n, np.nan)
            fwd[:n - 5] = D[5:] / O1[1:n - 4] - 1                          # enter next morning, exit 5th day close
            span = np.full(n, 99.0)
            span[:n - 5] = (dd[5:] - dd[:-5]).astype(float)
            ok = np.isfinite(fwd) & (span <= 11)
            allfwd.append(pd.DataFrame({"day": days[ok], "f": fwd[ok] * 1e4}))
            t = DC_N
            while t < n - 5:
                sd = 1 if D[t] > up[t] else (-1 if D[t] < dn[t] else 0)
                if sd == 0 or not ok[t]:
                    t += 1
                    continue
                e = O1[t + 1]
                u = t + 1
                while u < min(n - 1, t + 20) and not ((D[u] < xl[u]) if sd == 1 else (D[u] > xh[u])):
                    u += 1
                rec.append((days[t], sy, sd, sd * fwd[t] * 1e4, fwd[t] * 1e4, sd * (D[u] / e - 1) * 1e4, u - t))
                t += 5                                                     # the 5-day trade is still open until then
        a = pd.DataFrame(rec, columns=["day", "sym", "side", "pnl", "raw", "pnl_turtle", "turtle_days"])
        if a.empty:
            h["analysis"] = "Donchian weekly: no trades."
            return
        mk = pd.concat(allfwd).groupby("day").f.mean()
        a["mk"] = a.day.map(mk)
        a["ex"] = a.side * (a.raw - a.mk)
        a["yr"] = a.day.str[:4]
        a["wk"] = pd.to_datetime(a.day).dt.strftime("%G-%V")
        extra = ["", f"  market check: the average F&O stock moved {mk.mean():+.1f} bp over the same 5-day windows; "
                     f"gain OVER the market per trade {a.ex.mean():+.1f} bp (BUY {a[a.side > 0].ex.mean():+.1f}, SELL {a[a.side < 0].ex.mean():+.1f})",
                 f"  turtle exit instead ({DC_EXIT}-day opposite channel, max 20 days): {a.pnl_turtle.mean():+.1f} bp per trade, "
                 f"held {a.turtle_days.mean():.1f} days on average;  by year: "
                 + "  ".join(f"{y}: {a[a.yr == y].pnl_turtle.mean():+.1f}" for y in sorted(set(a.yr)))]
        h["analysis"] = _dc_report("DONCHIAN WEEKLY (daily candles, F&O stocks)",
                                   f"  BUY on a daily close above the {DC_N}-day high, SELL below the {DC_N}-day low; enter next morning 09:20; "
                                   "exit at the close of the 5th trading day", a, "wk", extra)
    except Exception as e:
        h["analysis"] = f"Donchian weekly failed: {e}"
    finally:
        h["an_running"] = False
        gc.collect()


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


BIG_SIGMA = 3.0    # a day's move counts as "big" when it is more than 3x the stock's normal daily move (std of the 20 days before)


def big_move_tag(D, today_px=None, last=1):
    """Text tag when a stock just made an unusually big move (results, news). D = daily closes, oldest first.
    last=1 checks yesterday (intraday alerts); last=5 checks the last 5 days (weekly list)."""
    D = np.asarray(D, float)
    D = D[np.isfinite(D) & (D > 0)]
    if len(D) < 21 + last:
        return ""
    dr = D[1:] / D[:-1] - 1
    sd = float(np.std(dr[-(20 + last):-last]))
    if sd <= 0:
        return ""
    parts = [f"{r * 100:+.1f}%" for r in dr[-last:] if abs(r) > BIG_SIGMA * sd]
    if parts:
        parts = [("yesterday " if last == 1 else "") + ", ".join(parts) + ("" if last == 1 else " in the last 5 days")]
    if today_px:
        t = today_px / D[-1] - 1
        if abs(t) > BIG_SIGMA * sd:
            parts.append(f"today {t * 100:+.1f}%")
    return ("after big move: " + "; ".join(parts)) if parts else ""


def daily_closes(sym, upto, n=30, include=True):
    """Last n daily closes of a stock up to a day (the day itself included or not)."""
    with LOCK:
        c = db()
        rows = c.execute(f"SELECT b FROM fnod WHERE sym=? AND day {'<=' if include else '<'} ? ORDER BY day DESC LIMIT ?",
                         (sym, upto, n)).fetchall()
        c.close()
    out = []
    for (b,) in rows[::-1]:
        a = np.frombuffer(b, np.float32)
        a = a[np.isfinite(a)]
        if len(a):
            out.append(float(a[-1]))
    return out


def _add_col(table, col):
    with LOCK:
        c = db()
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            c.commit()
        c.close()


def backfill_tags():
    """Give old rows (made before tagging existed) their big-move tag. Rows keep '' when there was no big move."""
    for table, key, last in (("fnopaper", "day", 1), ("swingpaper", "wk_day", 5)):
        with LOCK:
            c = db()
            try:
                rows = c.execute(f"SELECT {key}, sym, {'entry' if table == 'fnopaper' else 'NULL'} FROM {table} WHERE tag IS NULL").fetchall()
            except Exception:
                rows = []
            c.close()
        for d_, sy, px in rows:
            tg = big_move_tag(daily_closes(sy, d_, include=(table == "swingpaper")), px, last=last)
            with LOCK:
                c = db()
                c.execute(f"UPDATE {table} SET tag=? WHERE {key}=? AND sym=?", (tg, d_, sy))
                c.commit()
                c.close()


def swing_db():
    with LOCK:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS swingpaper(wk_day TEXT, sym TEXT, side TEXT, ref REAL, entry_day TEXT, entry REAL,
                     exit_day TEXT, exit REAL, bp REAL, PRIMARY KEY(wk_day, sym))""")
        c.commit()
        c.close()
    _add_col("swingpaper", "tag")


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
    tags = {SY[g]: big_move_tag(daily_closes(SY[g], d_last), last=5) for g in list(longs) + list(shorts)}
    with LOCK:
        c = db()
        for g, side in [(g, "BUY") for g in longs] + [(g, "SELL") for g in shorts]:
            c.execute("INSERT OR IGNORE INTO swingpaper(wk_day, sym, side, ref, tag) VALUES(?,?,?,?,?)",
                      (d_last, SY[g], side, float(DC[g]), tags[SY[g]]))
        c.commit()
        c.close()
    nm = lambda g: SY[g] + (" ⚠" if tags[SY[g]] else "")
    warn = "".join(f"\n⚠ {s_}: {t_}" for s_, t_ in tags.items() if t_)
    send_telegram(f"WEEKLY PAPER LIST (data of {d_last}, {len(te)} stocks ranked)\nLONG: {', '.join(nm(g) for g in longs)}\n"
                  f"SHORT: {', '.join(nm(g) for g in shorts)}{warn}\n"
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
        models[sy] = dict(mu=mu, sd=sd, w=w, thr=float(np.percentile(np.abs(A @ w), 90)), prev=C[-1], prevday=days[-1], id=sid,
                           dc=C[-30:, -1].copy())
    ls["models"], ls["ids"] = models, ids
    gc.collect()
    ls["busy"] = ""
    ls["note"] = f"{len(models)} stock models ready"


def paper_db():
    with LOCK:
        c = db()
        c.execute("""CREATE TABLE IF NOT EXISTS fnopaper(day TEXT, sym TEXT, t_in TEXT, side TEXT, entry REAL, ratio REAL,
                     t_out TEXT, exit REAL, bp REAL, PRIMARY KEY(day, sym))""")
        c.commit()
        c.close()
    _add_col("fnopaper", "tag")


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
            tag = big_move_tag(m.get("dc", []), float(row[k]), last=1)
            with LOCK:
                c = db()
                c.execute("INSERT OR IGNORE INTO fnopaper(day,sym,t_in,side,entry,ratio,tag) VALUES(?,?,?,?,?,?,?)",
                          (d_s, sy, t_in, side, float(row[k]), float(ratio[j]), tag))
                c.commit()
                c.close()
            send_telegram(f"PAPER {side} {sy} at {t_in}, price {row[k]:,.2f}. Signal {ratio[j]:.1f}x"
                          f"{' (STRONG)' if ratio[j] >= STRONG else ''}. Paper trade, exit about 15:25."
                          + (f"\n⚠ {tag}: the model was not built for such days." if tag else "") + " Research, not advice.")
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
                    ls["models"] = {}                                       # not needed until Monday's 08:45 training: free memory
                    gc.collect()
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
st.set_page_config(page_title="F&O price-model alerts", page_icon="📈", layout="wide")
_hs = hist_state()
ensure_live()


@st.cache_resource(show_spinner=False)
def _tables_ready():
    """Once per server start: make sure both paper tables have the 'tag' column, and tag rows made before tagging existed."""
    paper_db()
    swing_db()
    try:
        backfill_tags()
    except Exception:
        pass
    return True


_tables_ready()


@st.cache_data(ttl=86400, show_spinner=False)
def ids_cached():
    return fno_ids()


@st.cache_data(ttl=3600, show_spinner=False)
def sectors_cached():
    return sector_map()


TIMEFRAMES = {"5 min": 5, "15 min": 15, "30 min": 30, "1 hour": 60, "1 day": "D"}


@st.cache_data(ttl=120, show_spinner=False)
def range_5m(sid, from_s, to_s):
    """One stock's 5-min candles (open, high, low, close, volume) from Dhan for a date range (fetched when a chart is opened)."""
    d0, d1 = datetime.strptime(from_s, "%Y-%m-%d").date(), datetime.strptime(to_s, "%Y-%m-%d").date()
    rows, a = [], d0
    while a <= d1:
        b = min(a + timedelta(days=80), d1)
        body = {"securityId": sid, "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "interval": "5", "oi": False,
                "fromDate": a.strftime("%Y-%m-%d"), "toDate": (b + timedelta(days=1)).strftime("%Y-%m-%d")}
        j = hist_post(body, path="/charts/intraday")
        j = j.get("data") or j
        ts = j.get("timestamp") or []
        if ts:
            off = 315532800 if datetime.fromtimestamp(ts[0], IST).year < 2020 else 0
            vol = j.get("volume") or [0] * len(ts)
            for i, t_ in enumerate(ts):
                x = datetime.fromtimestamp(t_ + off, IST).replace(tzinfo=None)
                if a <= x.date() <= b and 555 <= x.hour * 60 + x.minute < 930:
                    rows.append((x, j["open"][i], j["high"][i], j["low"][i], j["close"][i], vol[i]))
        a = b + timedelta(days=1)
    df = pd.DataFrame(rows, columns=["dt", "o", "h", "l", "c", "v"])
    for k in ("o", "h", "l", "c", "v"):
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["v"] = df.v.fillna(0)
    df = df.dropna(subset=["o", "h", "l", "c"])
    return df.drop_duplicates("dt").sort_values("dt").reset_index(drop=True)


def bar_key(ts, tf):
    """Which candle (of timeframe tf) a 5-min bar starting at ts belongs to, as sortable text."""
    if tf == "D":
        return ts.strftime("%Y-%m-%d")
    m = (ts.hour * 60 + ts.minute - 555) // tf * tf + 555
    return f"{ts:%Y-%m-%d} {m // 60:02d}:{m % 60:02d}"


def resample(df, tf):
    k = df.dt.map(lambda x: bar_key(x, tf))
    out = df.groupby(k, sort=True).agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index(names="key")
    multi = df.dt.dt.date.nunique() > 1
    out["lbl"] = out.key.str[5:10] if tf == "D" else (out.key.str[5:] if multi else out.key.str[11:])
    out["lbl"] = out.lbl.str.replace("-", "/", regex=False)
    return out


def candles(out, marks, hline=None):
    """Candlestick (no gaps for nights or weekends) + volume. marks: list of dicts with key, p, text, shape."""
    import altair as alt
    out = out.copy()
    out["dir"] = np.where(out.c >= out.o, "up", "down")
    col = alt.Color("dir:N", scale=alt.Scale(domain=["up", "down"], range=["#16a34a", "#dc2626"]), legend=None)
    x = alt.X("lbl:O", sort=list(out.lbl), title=None, axis=alt.Axis(labelOverlap="greedy", labelAngle=0 if len(out) <= 30 else -40))
    tip = [alt.Tooltip("lbl:O", title="Bar"), alt.Tooltip("o:Q", title="Open", format=",.2f"), alt.Tooltip("h:Q", title="High", format=",.2f"),
           alt.Tooltip("l:Q", title="Low", format=",.2f"), alt.Tooltip("c:Q", title="Close", format=",.2f"), alt.Tooltip("v:Q", title="Volume", format=",.0f")]
    base = alt.Chart(out).encode(x=x)
    layers = [base.mark_rule().encode(y=alt.Y("l:Q", scale=alt.Scale(zero=False), title=None), y2="h:Q", color=col, tooltip=tip),
              base.mark_bar().encode(y="o:Q", y2="c:Q", color=col, tooltip=tip)]
    if hline:
        layers.append(alt.Chart(pd.DataFrame({"p": [float(hline)]})).mark_rule(strokeDash=[4, 4], color="#2563eb").encode(y="p:Q"))
    lab = dict(zip(out.key, out.lbl))
    mk = [{"lbl": lab[m["key"]], "p": float(m["p"]), "text": m["text"], "sh": m["shape"]} for m in marks if m["key"] in lab and pd.notna(m["p"])]
    if mk:
        m = alt.Chart(pd.DataFrame(mk)).encode(x=x, y="p:Q")
        layers += [m.mark_point(filled=True, size=170, color="#2563eb", opacity=1).encode(shape=alt.Shape("sh:N", scale=None)),
                   m.mark_text(dy=-16, fontWeight="bold", color="#2563eb").encode(text="text:N")]
    price = alt.layer(*layers).properties(height=340)
    vol = (alt.Chart(out).mark_bar(opacity=0.6)                               # no date labels here: they squeezed the bars flat
           .encode(x=alt.X("lbl:O", sort=list(out.lbl), axis=None), y=alt.Y("v:Q", title="Volume", axis=alt.Axis(format="~s", tickCount=3)),
                   color=col, tooltip=tip).properties(height=170))
    return price, vol


def show_candles(out, marks, hline=None):
    pch, vch = candles(out, marks, hline)
    st.altair_chart(pch, use_container_width=True)
    if out.v.sum() > 0:
        st.altair_chart(vch, use_container_width=True)
    else:
        st.caption("No volume data returned for this range.")


def _color_bp(v):
    """Green for a gain, red for a loss. Works on numbers and on already formatted text like '+12.3'."""
    try:
        x = float(str(v).replace(",", "").replace("x", ""))
    except ValueError:
        return ""
    if pd.isna(x) or x == 0:
        return ""
    return "color: #16a34a; font-weight: 600" if x > 0 else "color: #dc2626; font-weight: 600"


def txt(s, f, empty="-"):
    """Numbers to text so empty cells show '-' (Streamlit shows 'None' for empty number cells)."""
    return pd.to_numeric(s, errors="coerce").map(lambda v: empty if pd.isna(v) else f.format(v))


def read_table(sql, args=()):
    try:
        with LOCK:
            c = db()
            t = pd.read_sql(sql, c, params=args)
            c.close()
        return t
    except Exception:
        return pd.DataFrame()


st.title("📈 F&O price-model alerts")
st.caption("Paper trading only, no real orders. Intraday alerts on every F&O stock plus a weekly market-neutral swing list. Research, not advice.")
if not (CID and TOKEN):
    st.error("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in Render (Environment tab), then redeploy.")


def _version():
    """A cheap fingerprint of everything the page shows; it changes when a trade, exit, scan or weekly list changes."""
    ls = live_state()
    try:
        with LOCK:
            c = db()
            a = c.execute("SELECT COUNT(*), COUNT(bp) FROM fnopaper").fetchone()
            try:
                b = c.execute("SELECT COUNT(*), COUNT(entry), COUNT(bp) FROM swingpaper").fetchone()
            except Exception:
                b = ()
            c.close()
    except Exception:
        a, b = (), ()
    h = hist_state()
    return (a, b, ls["last_scan"], ls["note"], ls["busy"], ls["thread"] is not None and ls["thread"].is_alive(),
            h["running"], h["msg"].split(" (")[0] if h["running"] else h["msg"])


@st.fragment(run_every=60)
def auto_refresh():
    """The only timer on the page: once a minute, reload the page if anything new happened (selections are kept)."""
    v = _version()
    if st.session_state.get("_ver") is None:
        st.session_state["_ver"] = v
    elif st.session_state["_ver"] != v:
        st.session_state["_ver"] = v
        st.rerun()


auto_refresh()
tab1, tab2, tab3 = st.tabs(["Intraday alerts", "Weekly swing list", "Settings & data"])


@st.fragment
def intraday_tab():
    ls = live_state()
    alive = ls["thread"] is not None and ls["thread"].is_alive()
    st.caption(f"{'🟢 Worker running' if alive else '🔴 Worker stopped: reload this page'}  ·  {ls['note']} {ls['busy']}  ·  "
               f"Last scan: {ls['last_scan'] or 'none yet'}  ·  signal at least {SIG_MIN}x, scans 10:20-14:40, exit 15:25")
    t = read_table("SELECT * FROM fnopaper ORDER BY day DESC, t_in")
    if t.empty:
        st.info("No paper trades yet. Alerts start after 10:20 on a market day.")
        return
    dn = t[t.bp.notna()]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Closed trades", f"{len(dn)}")
    c2.metric("Trading days", f"{dn.day.nunique()}")
    c3.metric("Win rate", f"{(dn.bp > 0).mean() * 100:.0f}%" if len(dn) else "-")
    c4.metric("Average per trade", f"{dn.bp.mean():+.1f} bp" if len(dn) else "-")
    c5.metric("After 10 bp cost", f"{dn.bp.mean() - 10:+.1f} bp" if len(dn) else "-")
    if len(dn):
        bg = dn.tag.fillna("") != ""
        st.caption(f"Normal trades: {int((~bg).sum())}, average {dn[~bg].bp.mean() if (~bg).any() else float('nan'):+.1f} bp  ·  "
                   f"⚠ after a big move: {int(bg.sum())}, average {dn[bg].bp.mean() if bg.any() else float('nan'):+.1f} bp "
                   f"(a move over {BIG_SIGMA:g}x the stock's normal daily move, e.g. results day)")
    days = list(dict.fromkeys(t.day))
    dsel = st.selectbox("Day", days, index=0, key="i_day")
    d = t[t.day == dsel].copy()
    sm = sectors_cached()
    tg = d.tag.fillna("")
    show = pd.DataFrame({"Time": d.t_in, "Stock": d.sym + np.where(tg != "", " ⚠", ""), "Sector": d.sym.map(sm).fillna(""), "Side": d.side,
                         "Entry": txt(d.entry, "{:,.2f}"), "Strength": txt(d.ratio, "{:.1f}x"), "Exit": txt(d.exit, "{:,.2f}", "open"),
                         "Result (bp)": txt(d.bp, "{:+.1f}", "open"), "Note": tg.str.replace("after big move: ", "big move: ", regex=False)})
    sty = (show.style
           .map(_color_bp, subset=["Result (bp)"])
           .map(lambda v: "color: #16a34a" if v == "BUY" else "color: #dc2626", subset=["Side"]))
    st.dataframe(sty, hide_index=True, use_container_width=True)
    if d.bp.notna().any():
        st.caption(f"{dsel}: {int(d.bp.notna().sum())} closed, average {d.bp.mean():+.1f} bp, right {(d.bp.dropna() > 0).mean() * 100:.0f}%")
    cc1, cc2 = st.columns([2, 3])
    sym = cc1.selectbox("Candle chart", list(d.sym), key="i_sym")
    tfn = cc2.radio("Timeframe", ["5 min", "15 min", "30 min"], horizontal=True, key="i_tf")
    r = d[d.sym == sym].iloc[0]
    try:
        ids = ls["ids"] or ids_cached()
        raw = range_5m(ids[sym], dsel, dsel)
        if raw.empty:
            st.caption("No candles returned for this day yet.")
        else:
            tf = TIMEFRAMES[tfn]
            out = resample(raw, tf)
            ent = datetime.strptime(f"{dsel} {r.t_in}", "%Y-%m-%d %H:%M") - timedelta(minutes=5)   # the bar that closed at the entry time
            marks = [{"key": bar_key(ent, tf), "p": r.entry, "text": f"{r.side} {r.entry:,.2f}", "shape": "triangle-up" if r.side == "BUY" else "triangle-down"},
                     {"key": out.key.iloc[-1], "p": r.exit, "text": f"EXIT {r.exit:,.2f}" if pd.notna(r.exit) else "", "shape": "square"}]
            show_candles(out, marks, r.entry)
            st.caption(f"{sym}: {r.side} at {r.t_in} ({r.entry:,.2f}), signal {r.ratio:.1f}x"
                       + (f", exit 15:25 at {r.exit:,.2f} = {r.bp:+.1f} bp" if pd.notna(r.bp) else ", still open")
                       + (f"  ·  ⚠ {r.tag}" if isinstance(r.tag, str) and r.tag else ""))
    except Exception as e:
        st.caption(f"Chart not available: {str(e)[:150]}")
    with st.expander("History by day"):
        if len(dn):
            g = dn.groupby("day").agg(trades=("bp", "size"), right=("bp", lambda x: f"{(x > 0).mean() * 100:.0f}%"), avg_bp=("bp", "mean"),
                                      best=("bp", "max"), worst=("bp", "min")).sort_index(ascending=False).reset_index()
            st.dataframe(g.style.format({"avg_bp": "{:+.1f}", "best": "{:+.0f}", "worst": "{:+.0f}"}).map(_color_bp, subset=["avg_bp"]),
                         hide_index=True, use_container_width=True)
        st.download_button("Download all intraday paper trades (CSV)", t.to_csv(index=False), "intraday_paper.csv", "text/csv")


@st.fragment
def swing_tab():
    st.caption("Every Friday after 15:50 the market-neutral model ranks all F&O stocks: long the top 10, short the bottom 10. "
               "Entry next trading morning 09:20, exit the following Friday close. Telegram sends the list and the result.")
    swing_db()
    t = read_table("SELECT * FROM swingpaper ORDER BY wk_day DESC, side, sym")
    if t.empty:
        st.info("No weekly list yet. The first one is made on Friday after 15:50 (or use Settings & data > Make the weekly list now).")
        return
    dn = t[t.bp.notna()]
    wk = dn.groupby("wk_day").bp.mean()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Finished weeks", f"{len(wk)}")
    c2.metric("Average per trade", f"{dn.bp.mean():+.1f} bp" if len(dn) else "-")
    c3.metric("Weeks positive", f"{(wk > 0).mean() * 100:.0f}%" if len(wk) else "-")
    c4.metric("After 10 bp cost", f"{dn.bp.mean() - 10:+.1f} bp" if len(dn) else "-")
    weeks = list(dict.fromkeys(t.wk_day))
    wsel = st.selectbox("List decided on", weeks, index=0, key="s_wk")
    w = t[t.wk_day == wsel]
    sm = sectors_cached()
    cl, cs = st.columns(2)
    for col, side, ttl in ((cl, "BUY", "Long (buy)"), (cs, "SELL", "Short (sell)")):
        x = w[w.side == side]
        show = pd.DataFrame({"Stock": x.sym + np.where(x.tag.fillna("") != "", " ⚠", ""), "Sector": x.sym.map(sm).fillna(""),
                             "Entry": txt(x.entry, "{:,.2f}"),
                             "Exit": txt(x.exit, "{:,.2f}"), "Result (bp)": txt(x.bp, "{:+.1f}")})
        col.markdown(f"**{ttl}**")
        col.dataframe(show.style.map(_color_bp, subset=["Result (bp)"]), hide_index=True, use_container_width=True)
    for _, rr in w[w.tag.fillna("") != ""].iterrows():
        st.caption(f"⚠ {rr.sym} ({'long' if rr.side == 'BUY' else 'short'}): {rr.tag}. The model only sees prices, not results or news.")
    if w.bp.notna().any():
        st.caption(f"Week of {wsel}: per trade {w.bp.mean():+.1f} bp (long {w[w.side == 'BUY'].bp.mean():+.1f}, short {w[w.side == 'SELL'].bp.mean():+.1f})")
    elif w.entry.notna().any():
        st.caption("Open: entered, exits at next Friday's close.")
    else:
        st.caption("Waiting for entry at the next trading morning 09:20.")
    st.markdown("**Candle chart**")
    w1, w2, w3 = st.columns([2, 4, 3])
    sym = w1.selectbox("Stock", list(w.sym), key="s_sym", format_func=lambda s_: f"{s_} ({'long' if (w.side[w.sym == s_] == 'BUY').any() else 'short'})")
    tfn = w2.radio("Timeframe", list(TIMEFRAMES), index=3, horizontal=True, key="s_tf")
    back = w3.select_slider("Days before the decision", [5, 10, 20, 40, 60, 90], value=10, key="s_back")
    r = w[w.sym == sym].iloc[0]
    try:
        ids = live_state()["ids"] or ids_cached()
        d0 = datetime.strptime(wsel, "%Y-%m-%d").date() - timedelta(days=back)
        d1 = datetime.strptime(r.exit_day, "%Y-%m-%d").date() if isinstance(r.exit_day, str) else datetime.now(IST).date()
        raw = range_5m(ids[sym], d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d"))
        if raw.empty:
            st.caption("No candles returned.")
        else:
            tf = TIMEFRAMES[tfn]
            out = resample(raw, tf)
            day_last = lambda ds: bar_key(raw[raw.dt.dt.strftime("%Y-%m-%d") == ds].dt.max(), tf) if (raw.dt.dt.strftime("%Y-%m-%d") == ds).any() else ""
            day_first = lambda ds: bar_key(raw[raw.dt.dt.strftime("%Y-%m-%d") == ds].dt.min(), tf) if (raw.dt.dt.strftime("%Y-%m-%d") == ds).any() else ""
            ent, ext = pd.to_numeric(r.entry, errors="coerce"), pd.to_numeric(r.exit, errors="coerce")
            marks = [{"key": day_last(wsel), "p": r.ref, "text": "DECIDED", "shape": "circle"}]
            if pd.notna(ent):
                marks.append({"key": day_first(r.entry_day), "p": ent, "text": f"{r.side} {ent:,.2f}", "shape": "triangle-up" if r.side == "BUY" else "triangle-down"})
            if pd.notna(ext):
                marks.append({"key": day_last(r.exit_day), "p": ext, "text": f"EXIT {ext:,.2f}", "shape": "square"})
            show_candles(out, marks, ent if pd.notna(ent) else None)
            if pd.notna(ext):
                st.caption(f"{sym}: {r.side} {ent:,.2f} on {r.entry_day}, exit {ext:,.2f} on {r.exit_day} = {r.bp:+.1f} bp")
            elif pd.notna(ent):
                now_bp = (1 if r.side == "BUY" else -1) * (out.c.iloc[-1] / ent - 1) * 1e4
                st.caption(f"{sym}: {r.side} {ent:,.2f} on {r.entry_day}, open. Now {out.c.iloc[-1]:,.2f} = {now_bp:+.1f} bp so far")
            else:
                st.caption(f"{sym}: decided {wsel} at close {r.ref:,.2f}; entry at the next trading morning 09:20")
    except Exception as e:
        st.caption(f"Chart not available: {str(e)[:150]}")
    with st.expander("History by week"):
        if len(dn):
            g = dn.groupby("wk_day").apply(lambda x: pd.Series({"trades": len(x), "per_trade_bp": x.bp.mean(),
                                                                "long_bp": x[x.side == "BUY"].bp.mean(), "short_bp": x[x.side == "SELL"].bp.mean()}))
            st.dataframe(g.sort_index(ascending=False).reset_index().style.format({"trades": "{:.0f}", "per_trade_bp": "{:+.1f}", "long_bp": "{:+.1f}", "short_bp": "{:+.1f}"})
                         .map(_color_bp, subset=["per_trade_bp"]), hide_index=True, use_container_width=True)
        st.download_button("Download all weekly paper trades (CSV)", t.to_csv(index=False), "weekly_paper.csv", "text/csv")


@st.cache_data(ttl=600, show_spinner=False)
def data_check():
    with LOCK:
        c = db()
        a = c.execute("SELECT COUNT(*), COUNT(DISTINCT sym), MIN(day), MAX(day) FROM fnod").fetchone()
        try:
            b = c.execute("SELECT COUNT(*), MAX(day) FROM fnov").fetchone()
        except Exception:
            b = (0, "-")
        c.close()
    return a, b


def download_status():
    if _hs["running"] or _hs["msg"]:
        st.caption(f"{'Running' if _hs['running'] else 'Idle'} - {_hs['msg'] or 'not started'} - {_hs['rows']:,} stock-days saved")
        for e in _hs["errs"]:
            st.caption("note: " + e)


def settings_tab():
    try:
        a, b = data_check()
        sm = sectors_cached()
        st.markdown(f"**Stored history:** {a[0]:,} stock-days of 5-min prices for {a[1]} stocks ({a[2]} to {a[3]}); "
                    f"high/low/volume {b[0]:,} stock-days (latest {b[1]}). Sector list: {len(sm)} stocks.")
    except Exception as e:
        st.caption(f"Data check failed: {e}")
    st.caption("Daily routine (automatic): 08:45 top up history and train; 10:20-14:40 scan; 15:31 close intraday trades; "
               "15:45 save the day; Friday 15:50 close last week's list and make the new one. Keep DHAN_ACCESS_TOKEN fresh every morning.")
    b1, b2, b3 = st.columns(3)
    if b1.button("Send test Telegram", use_container_width=True):
        ok, info = send_telegram("Test from F&O price-model alerts: Telegram works.")
        if ok:
            st.success("Test sent")
        else:
            st.error(f"Failed: {info}")
    if b2.button("Close today's open intraday trades (after 15:30)", use_container_width=True):
        left = live_close(datetime.now(IST).replace(tzinfo=None))
        st.info(f"Done. Still open: {left}. {live_state()['note']}")
    if b3.button("Make the weekly list now (latest stored day)", use_container_width=True):
        with st.spinner("Ranking all stocks..."):
            try:
                ok = swing_list(datetime.now(IST).replace(tzinfo=None), require_today=False)
                st.info(f"{'Saved' if ok else 'Not saved'}: {live_state()['note']}")
            except Exception as e:
                st.error(f"Failed: {e}")
    with st.expander("Research backtests (run after 15:45 or at the weekend)"):
        st.caption(f"Classic Donchian/turtle rules, fixed in advance: {DC_N}-candle breakout, {DC_EXIT}-candle opposite-channel exit. "
                   "Uses the stored 5 years. Takes a few minutes; the live alerts keep running.")
        _hs.setdefault("an_running", False)
        r1, r2 = st.columns(2)
        if r1.button("Run intraday Donchian", use_container_width=True, disabled=_hs["an_running"]):
            _hs["an_running"] = True
            threading.Thread(target=donchian_intraday_job, daemon=True).start()
        if r2.button("Run weekly Donchian", use_container_width=True, disabled=_hs["an_running"]):
            _hs["an_running"] = True
            threading.Thread(target=donchian_weekly_job, daemon=True).start()
        if st.button("Run monthly momentum (12-1, top 20)", use_container_width=True, disabled=_hs["an_running"]):
            _hs["an_running"] = True
            threading.Thread(target=momentum_job, daemon=True).start()
        if _hs.get("analysis"):
            st.code(_hs["analysis"], language=None)
            if _hs["an_running"]:
                st.button("🔄 Refresh result", use_container_width=True)
    with st.expander("Repair history (only if data is missing)"):
        dd = st.number_input("Days back (calendar)", 30, 1800, 30, step=30, help="Already saved days are skipped. Run after 15:30.")
        if st.button("Download missing 5-min history", disabled=_hs["running"]):
            threading.Thread(target=fno_job, args=(int(dd),), daemon=True).start()
            _hs["msg"] = "starting..."
            _hs["running"] = True
        download_status()


with tab1:
    intraday_tab()
with tab2:
    swing_tab()
with tab3:
    settings_tab()

# === END OF app.py (if you do not see this line at the bottom, the paste was cut off) ===
