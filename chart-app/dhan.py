"""Minimal DhanHQ v2 data client (read-only: instruments and candles)."""
import csv
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.dhan.co/v2"
MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
INDEX_FALLBACK = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27", "MIDCPNIFTY": "442", "SENSEX": "51"}


# End of continuous trading (IST). F&O stocks move to the closing auction at 15:15; derivatives run to 15:40.
SESSION_END = {"IDX_I": (15, 30), "NSE_EQ": (15, 15), "NSE_FNO": (15, 40), "BSE_FNO": (15, 40)}


def trim_session(df, segment):
    """Keep candles inside market hours. Only derivatives have candles after 15:29."""
    last = "15:39" if segment.endswith("_FNO") else "15:29"
    return df.between_time("09:15", last)


def load_instruments():
    """Return {label: {id, segment, instrument}} for indices, F&O stocks and nearest-expiry futures."""
    r = requests.get(MASTER, stream=True, timeout=180)
    r.raise_for_status()
    rows = csv.DictReader(x.decode("utf-8", "ignore") for x in r.iter_lines())
    return parse_master(rows)


def parse_master(rows, today=None):
    today = today or datetime.now(IST).date().isoformat()
    equity, futures, index_ids = {}, {}, dict(INDEX_FALLBACK)
    for x in rows:
        exch, kind = x.get("SEM_EXM_EXCH_ID"), x.get("SEM_INSTRUMENT_NAME")
        sym, sid = x.get("SEM_TRADING_SYMBOL", ""), x.get("SEM_SMST_SECURITY_ID")
        if kind == "INDEX" and sym in index_ids and exch == ("BSE" if sym == "SENSEX" else "NSE"):
            index_ids[sym] = sid
        elif exch == "NSE" and kind == "EQUITY" and x.get("SEM_SERIES") == "EQ":
            equity[sym] = sid
        elif kind in ("FUTSTK", "FUTIDX") and exch in ("NSE", "BSE") and "NSETEST" not in sym:
            name = sym.rsplit("-", 2)[0]          # keeps hyphenated names such as BAJAJ-AUTO
            expiry = x.get("SEM_EXPIRY_DATE", "")[:10]
            wanted = name in INDEX_FALLBACK if kind == "FUTIDX" else exch == "NSE"
            if wanted and expiry >= today and (name not in futures or expiry < futures[name][0]):
                futures[name] = (expiry, sid, kind, "BSE_FNO" if exch == "BSE" else "NSE_FNO")
    out = {}
    for name, sid in index_ids.items():
        out[f"INDEX | {name}"] = dict(id=sid, segment="IDX_I", instrument="INDEX")
    for name, (expiry, sid, kind, seg) in sorted(futures.items()):
        out[f"FUTURE | {name} {expiry}"] = dict(id=sid, segment=seg, instrument=kind)
        if kind == "FUTSTK" and name in equity:
            out[f"STOCK | {name}"] = dict(id=equity[name], segment="NSE_EQ", instrument="EQUITY")
    return out


def _post(path, body, client_id, token):
    headers = {"access-token": token, "client-id": client_id,
               "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(4):
        r = requests.post(BASE + path, json=body, headers=headers, timeout=30)
        if r.status_code != 429:
            break
        time.sleep(1 + attempt)
    if not r.ok:
        raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def to_frame(data):
    cols = ["open", "high", "low", "close", "volume"]
    if not data or not data.get("timestamp"):
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame({c: data[c] for c in cols})
    df.index = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert(IST)
    return df[~df.index.duplicated(keep="last")].sort_index()


def intraday(inst, interval, days, client_id, token):
    """interval: 1, 5, 15, 25 or 60 (Dhan native). Dhan serves at most 90 days per request, so longer
    histories are fetched in 80-day windows, newest first, and joined."""
    now = datetime.now(IST)
    frames, end = [], now
    while (now - end).days < days:
        start = max(end - timedelta(days=80), now - timedelta(days=days))
        body = dict(securityId=inst["id"], exchangeSegment=inst["segment"], instrument=inst["instrument"],
                    interval=str(interval), oi=False,
                    fromDate=start.strftime("%Y-%m-%d 09:15:00"), toDate=end.strftime("%Y-%m-%d %H:%M:%S"))
        frames.append(to_frame(_post("/charts/intraday", body, client_id, token)))
        end = start
    df = pd.concat(frames) if frames else pd.DataFrame()
    if df.empty:
        return df
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return trim_session(df, inst["segment"])


def daily(inst, days, client_id, token):
    now = datetime.now(IST)
    body = dict(securityId=inst["id"], exchangeSegment=inst["segment"], instrument=inst["instrument"],
                expiryCode=0, oi=False,
                fromDate=(now - timedelta(days=days)).strftime("%Y-%m-%d"),
                toDate=(now + timedelta(days=1)).strftime("%Y-%m-%d"))   # toDate is exclusive
    return to_frame(_post("/charts/historical", body, client_id, token))
