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
    days = list(dict.fromkeys(t.day))
    dsel = st.selectbox("Day", days, index=0, key="i_day")
    d = t[t.day == dsel].copy()
    sm = sectors_cached()
    show = pd.DataFrame({"Time": d.t_in, "Stock": d.sym, "Sector": d.sym.map(sm).fillna(""), "Side": d.side,
                         "Entry": txt(d.entry, "{:,.2f}"), "Strength": txt(d.ratio, "{:.1f}x"), "Exit": txt(d.exit, "{:,.2f}", "open"),
                         "Result (bp)": txt(d.bp, "{:+.1f}", "open")})
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
                       + (f", exit 15:25 at {r.exit:,.2f} = {r.bp:+.1f} bp" if pd.notna(r.bp) else ", still open"))
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
        show = pd.DataFrame({"Stock": x.sym, "Sector": x.sym.map(sm).fillna(""), "Entry": txt(x.entry, "{:,.2f}"),
                             "Exit": txt(x.exit, "{:,.2f}"), "Result (bp)": txt(x.bp, "{:+.1f}")})
        col.markdown(f"**{ttl}**")
        col.dataframe(show.style.map(_color_bp, subset=["Result (bp)"]), hide_index=True, use_container_width=True)
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
