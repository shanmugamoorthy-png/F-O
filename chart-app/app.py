"""Index and F&O multi-timeframe candle chart with price action commentary (Dhan API)."""
import hmac
import os

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import analysis as an
import dhan
import morning
import orb
import scanner

st.set_page_config(page_title="F&O Price Action", layout="wide")

# label: (Dhan source interval, resample minutes, calendar days to fetch)
TF = {"1m": (1, None, 10), "3m": (1, 3, 20), "5m": (5, None, 45), "10m": (5, 10, 60),
      "15m": (5, 15, 90), "30m": (5, 30, 90), "75m": (5, 75, 90), "1h": (5, 60, 90), "Daily": (None, None, 730)}


def secret(name):
    return os.getenv(name, "").strip()


password = secret("APP_PASSWORD")
if not password:
    st.error("Set APP_PASSWORD in Render. Without it anyone with the link could use your Dhan data.")
    st.stop()
if not hmac.compare_digest(st.sidebar.text_input("App password", type="password").encode(), password.encode()):
    st.info("Enter the app password in the sidebar.")
    st.stop()

client_id = secret("DHAN_CLIENT_ID")
token = st.sidebar.text_input("Today's Dhan token (optional override)", type="password").strip() or secret("DHAN_ACCESS_TOKEN")
if not (client_id and token):
    st.error("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")
    st.stop()


@st.cache_data(ttl=6 * 3600, show_spinner="Loading Dhan instrument list…")
def instruments():
    return dhan.load_instruments()


@st.cache_data(ttl=30, show_spinner=False)
def candles(label, interval, days, _token):
    inst = instruments()[label]
    if interval is None:
        return dhan.daily(inst, days, client_id, _token)
    return dhan.intraday(inst, interval, days, client_id, _token)


def load(label, tf):
    interval, minutes, days = TF[tf]
    df = candles(label, interval, days, token)
    return an.resample(df, minutes) if minutes else df


def scanner_page():
    st.subheader("Top 10 F&O stocks by score")
    scan_tf = st.selectbox("Scan timeframe", ["5m", "15m", "30m", "1h"], index=1)
    level = st.slider("Minimum score (either direction)", 10, 70, 30, 5)
    stocks = {k.split("| ", 1)[1]: v for k, v in instruments().items() if k.startswith("STOCK")}
    st.caption(f"{len(stocks)} stocks. Takes a few minutes. Don't change any setting while it runs.")
    if st.button("Scan now", type="primary"):
        interval, minutes, _ = TF[scan_tf]
        days = 20 if scan_tf in ("5m", "15m") else 45

        def fetch(symbol):
            df = dhan.intraday(stocks[symbol], interval, days, client_id, token)
            return an.resample(df, minutes) if minutes else df

        bar = st.progress(0.0, text="Scanning…")
        try:
            table, failed = scanner.scan(list(stocks), fetch, level,
                                         lambda i, n: bar.progress(i / n, text=f"Scanning {i} of {n}"))
            st.session_state.scan = (scan_tf, level, table, failed)
        except Exception as e:
            st.error(str(e))
        bar.empty()
    if "scan" not in st.session_state:
        return
    done_tf, done_level, table, failed = st.session_state.scan
    if table.empty:
        st.warning("No stocks returned data.")
    else:
        st.caption(f"{done_tf} scan, minimum score {done_level}. Latest candle {table['As of'].max():%d %b %H:%M} IST. "
                   f"{len(table)} stocks scored.")
        for title, bias in (("Top 10 bullish", "BULLISH"), ("Top 10 bearish", "BEARISH")):
            st.markdown(f"**{title}**")
            rows = scanner.top(table, bias)
            if rows.empty:
                st.write("None meet the minimum score.")
            else:
                st.dataframe(rows, hide_index=True)
        st.caption("Ties in score are ordered by relative volume. A high score is not a win probability: "
                   "open the stock on the Chart page and check its backtest.")
    if failed:
        with st.expander(f"Skipped: {len(failed)}"):
            st.write("\n".join(f"- {f}" for f in failed))


HISTORY_DAYS = 180   # calendar days, about 120 sessions


def backtest_page():
    st.subheader("Backtest: top 10 at a set time, held to the close")
    entry = st.selectbox("Pick time (15m candle closed)", ["09:30", "09:45", "10:15", "11:15", "12:15"], index=2)
    stocks = {k.split("| ", 1)[1]: v for k, v in instruments().items() if k.startswith("STOCK")}
    st.caption(f"Loads about 6 months for {len(stocks)} stocks. Takes 10 to 15 minutes. Don't change settings or close the page while it runs.")
    if st.button("Run backtest", type="primary"):
        def fetch(symbol):
            return an.resample(dhan.intraday(stocks[symbol], 5, HISTORY_DAYS, client_id, token), 15)

        bar = st.progress(0.0, text="Loading…")
        try:
            signals, failed = morning.collect(list(stocks), fetch, entry,
                                              lambda i, n: bar.progress(i / n, text=f"Loading {i} of {n}"))
            index = next(v for k, v in instruments().items() if k == "INDEX | NIFTY")
            nifty = morning.nifty_state(an.resample(dhan.intraday(index, 5, HISTORY_DAYS, client_id, token), 15))
            st.session_state.morning = (entry, signals, failed, nifty)
        except Exception as e:
            st.error(str(e))
        bar.empty()
    if "morning" not in st.session_state:
        return
    done_entry, signals, failed, nifty = st.session_state.morning
    if signals.empty:
        st.warning("No data returned.")
        return
    # These filters reuse the loaded data, so changing them is instant
    sessions = st.slider("Sessions to test", 10, 120, 120, 10)
    level = st.slider("Minimum score", 10, 85, 30, 5)
    cost = st.number_input("Round-trip cost and slippage (%)", 0.0, 1.0, 0.10, 0.05)
    stop = st.selectbox("Stop-loss (% move in the stock, 0 = none)", list(morning.STOPS))
    target = st.selectbox("Target (%, 0 = none)", [0.0, 1.0, 1.5, 2.0, 3.0])
    min_rel = st.selectbox("Minimum relative volume (0 = no filter)", list(morning.MIN_REL))
    trades, daily, summary = morning.pick(signals, sessions, level, cost=cost, stop=stop, target=target,
                                          min_rel=min_rel)
    if trades.empty:
        st.warning("No stocks met these filters.")
        return
    st.caption(f"Picked at {done_entry}, entry at that candle's close, exit at stop, target or the last continuous candle. "
               f"{trades.Date.min():%d %b} to {trades.Date.max():%d %b %Y}.")
    st.markdown("**Summary**")
    st.dataframe(summary, hide_index=True)
    st.markdown("**Breakout entry: wait for a 15m close beyond PDH / PDL**")
    result, entries = morning.breakout_test(signals, nifty, sessions, level, cost, min_rel)
    if result.empty:
        st.write("No breakouts met these filters.")
    else:
        st.dataframe(result, hide_index=True)
        st.download_button("Download breakout results (CSV)", result.to_csv(index=False), "breakout_results.csv",
                           "text/csv")
    st.caption("Watches the top 10 from the pick time; enters at the first 15m close beyond the level, up to 14:00. "
               "Stop 0 means exit at the close. NIFTY filter On: NIFTY is above (bullish) or below (bearish) both "
               "its VWAP and day open on that candle. 24 settings are tried, so one may look good by luck.")
    st.markdown("**1:2 risk-reward (target = 2 × stop)**")
    rr = morning.risk_reward(signals, sessions, level, cost, min_rel=min_rel)
    st.dataframe(rr, hide_index=True)
    st.download_button("Download 1:2 results (CSV)", rr.to_csv(index=False), "risk_reward_1to2.csv", "text/csv")
    st.caption("Uses the sessions, score, cost and volume filter above; the stop and target boxes don't apply here. "
               "If one 15m candle touches both levels, the stop is counted.")
    if st.checkbox("Compare stop-loss and volume settings (older vs newer dates)"):
        table = morning.compare(signals, sessions, level, cost, target)
        st.dataframe(table, hide_index=True)
        st.download_button("Download comparison (CSV)", table.to_csv(index=False), "comparison.csv", "text/csv")
        st.caption("40 combinations are tried, so some will look good by luck. Choose on the older dates, then check "
                   "the newer ones. A setting that works on only one half should be rejected.")
    st.markdown("**Day by day (average of the picks)**")
    st.dataframe(daily.sort_values("Date", ascending=False), hide_index=True)
    st.markdown("**Closing position of each pick**")
    chosen = st.selectbox("Date", sorted(trades.Date.unique(), reverse=True), format_func=lambda x: f"{x:%a %d %b %Y}")
    for side in ("BULLISH", "BEARISH"):
        st.markdown(f"*{side.title()}*")
        st.dataframe(trades[(trades.Date == chosen) & (trades.Side == side)].drop(columns=["Date", "Side"]),
                     hide_index=True)
    st.download_button("Download all trades (CSV)", trades.to_csv(index=False), "morning_backtest.csv", "text/csv")
    st.caption("Limits: uses today's F&O list, checks stops on 15m candles, assumes a fill at the candle close, and measures "
               "stock prices, not option premiums. Trades on the same day move together. Not investment advice.")
    if failed:
        with st.expander(f"Skipped: {len(failed)}"):
            st.write("\n".join(f"- {f}" for f in failed))


def orb_page():
    st.subheader("Opening range breakout backtest")
    stocks = {k.split("| ", 1)[1]: v for k, v in instruments().items() if k.startswith("STOCK")}
    st.caption(f"{len(stocks)} stocks, about 6 months. Takes 10 to 15 minutes. Rules are fixed: the first 15 or 30 "
               f"minute range, entry at the first 15m close beyond it (up to 14:00), one trade per stock per day, "
               f"ranges under {orb.MIN_RANGE}% or over {orb.MAX_RANGE}% skipped. Stop = the opposite side of the range.")
    if st.button("Run ORB test", type="primary"):
        def fetch(symbol):
            return an.resample(dhan.intraday(stocks[symbol], 5, HISTORY_DAYS, client_id, token), 15)

        bar = st.progress(0.0, text="Loading…")
        try:
            index = instruments()["INDEX | NIFTY"]
            nifty = morning.nifty_state(an.resample(dhan.intraday(index, 5, HISTORY_DAYS, client_id, token), 15))
            trades, failed = orb.collect(list(stocks), fetch, nifty,
                                         lambda i, n: bar.progress(i / n, text=f"Loading {i} of {n}"))
            st.session_state.orb = (trades, failed)
        except Exception as e:
            st.error(str(e))
        bar.empty()
    if "orb" not in st.session_state:
        return
    trades, failed = st.session_state.orb
    if trades.empty:
        st.warning("No trades.")
        return
    cost = st.number_input("Round-trip cost and slippage (%)", 0.0, 1.0, 0.10, 0.05)
    table = orb.summarize(trades, cost)
    st.dataframe(table, hide_index=True)
    st.download_button("Download ORB results (CSV)", table.to_csv(index=False), "orb_results.csv", "text/csv")
    st.caption("'Passes all checks' needs profit in both halves of the dates, profit at 0.25% cost, a daily t-stat "
               "of 2 or more and 100+ trades in each half. 48 settings are tried, so a pass can still be luck. "
               "If one 15m candle touches both stop and target, the stop counts.")
    if failed:
        with st.expander(f"Skipped: {len(failed)}"):
            st.write("\n".join(f"- {f}" for f in failed))


page = st.sidebar.radio("Page", ["Chart", "Top 10 scanner", "10:15 backtest", "Opening range test"])
if page == "Opening range test":
    orb_page()
    st.stop()
if page == "10:15 backtest":
    backtest_page()
    st.stop()
if page == "Top 10 scanner":
    scanner_page()
    st.stop()

all_inst = instruments()
group = st.sidebar.radio("Type", ["INDEX", "STOCK", "FUTURE"], horizontal=True)
label = st.sidebar.selectbox("Symbol", [k for k in all_inst if k.startswith(group)])
tf = st.sidebar.selectbox("Chart timeframe", list(TF), index=2)
bars = st.sidebar.slider("Candles shown", 50, 500, 150, 10)
horizon = st.sidebar.slider("Predict next N candles", 1, 6, 3)
threshold = st.sidebar.slider("Signal threshold", 10, 70, 30, 5)
refresh = st.sidebar.selectbox("Auto refresh (seconds)", [0, 30, 60, 180], index=2)


show_legend = st.sidebar.checkbox("Show chart legend", value=False)


def chart(d, rep, intraday):
    v = d.tail(bars)
    fmt = "%d %b %H:%M" if intraday else "%d %b %y"
    x = list(v.index.strftime(fmt))
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.68, 0.16, 0.16], vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(x=x, open=v.open, high=v.high, low=v.low, close=v.close, name="Price"), 1, 1)
    lines = [("ema9", "EMA 9", "#f5a623"), ("ema20", "EMA 20", "#4a90e2")]
    if intraday:
        lines += [("vwap", "VWAP", "#bd10e0")]
    for col, name, colour in lines:
        fig.add_trace(go.Scatter(x=x, y=v[col], name=name, line=dict(color=colour, width=1.2)), 1, 1)
    if intraday:
        for col, name, colour in (("pdh", "PDH", "#d0021b"), ("pdl", "PDL", "#2e9e44"), ("pivot", "Pivot", "#888888")):
            fig.add_trace(go.Scatter(x=x, y=v[col], name=name, line=dict(color=colour, width=1, dash="dot", shape="hv")), 1, 1)
        marked = v[v.pattern.str.contains("engulfing|Hammer|Shooting", regex=True)]
        fig.add_trace(go.Scatter(x=list(marked.index.strftime(fmt)), y=marked.high + marked.atr * 0.3, mode="markers",
                                 marker=dict(symbol="diamond", size=7, color="#9b9b9b"), name="Patterns",
                                 text=marked.pattern, hoverinfo="text+x"), 1, 1)
        if rep:
            o, h, l, c = zip(*rep["path"])
            fx = [f"+{i}" for i in range(1, len(c) + 1)]
            fig.add_trace(go.Candlestick(x=fx, open=o, high=h, low=l, close=c, name="Projection (illustrative)",
                                         opacity=0.4, increasing_line_color="#17a2b8", decreasing_line_color="#e67e22"), 1, 1)
            if rep["closed"]:
                for col, name, colour in (("pdh", "Next PDH", "#d0021b"), ("pdl", "Next PDL", "#2e9e44"),
                                          ("pivot", "Next pivot", "#888888")):
                    fig.add_trace(go.Scatter(x=[x[-1]] + fx, y=[rep["levels"][col]] * (len(fx) + 1), name=name,
                                             mode="lines", line=dict(color=colour, width=2, dash="dash")), 1, 1)
            for y, colour in ((rep["resistance"], "#d0021b"), (rep["support"], "#2e9e44")):
                fig.add_hline(y=y, line=dict(color=colour, width=1), annotation_text=f"{y:,.2f}", row=1, col=1)
    if v.volume.sum() > 0:
        colours = ["#26a69a" if c >= o else "#ef5350" for o, c in zip(v.open, v.close)]
        fig.add_trace(go.Bar(x=x, y=v.volume, marker_color=colours, name="Volume"), 2, 1)
    else:
        fig.add_annotation(text="No volume for spot index (select FUTURE for volume)", xref="x2 domain",
                           yref="y2 domain", x=0.5, y=0.5, showarrow=False)
    fig.add_trace(go.Scatter(x=x, y=v.rsi, name="RSI 14", line=dict(width=1.2)), 3, 1)
    for y in (45, 55):
        fig.add_hline(y=y, line=dict(width=0.7, dash="dash", color="#999999"), row=3, col=1)
    fig.update_xaxes(type="category", nticks=8, rangeslider_visible=False)
    fig.update_layout(height=720, margin=dict(l=5, r=5, t=40, b=5), showlegend=show_legend,
                      legend=dict(orientation="h", yanchor="top", y=-0.07, x=0),
                      hovermode="x unified")
    return fig


@st.fragment(run_every=refresh or None)
def view():
    try:
        raw = load(label, tf)
        if raw.empty:
            st.warning("Dhan returned no candles for this selection.")
            return
        intraday = tf != "Daily"
        # Commentary always uses intraday candles; the Daily chart is paired with 15m analysis
        d = an.add_features(raw)
        analysis_tf = tf if intraday else "15m"
        a = d if intraday else an.add_features(load(label, "15m"))
        end = dhan.SESSION_END[all_inst[label]["segment"]]
        rep = an.report(a, horizon, threshold, end=end)
    except Exception as e:
        st.error(str(e))
        return

    st.subheader(f"{label} · {tf}")
    m = st.columns(3)
    m[0].metric("Last candle close", f"{a.close.iloc[-1]:,.2f}", f"{a.close.iloc[-1] - a.pdc.iloc[-1]:+,.2f}")
    m[1].metric(f"Bias ({analysis_tf})", rep["bias"])
    m[2].metric("Score", f"{rep['score']:+.0f} / 100")
    st.caption(f"Latest candle starts {a.index[-1]:%d %b %Y %H:%M} IST. During market hours it is still forming.")

    st.plotly_chart(chart(d, rep if intraday else None, intraday), key="chart")
    with st.container(border=True):
        st.markdown(rep["text"])
    st.markdown("**Backtest on loaded data**")
    for name, r in rep["backtest"].items():
        if r["signals"]:
            st.write(f"{name}: right {r['hit']:.0%} of {r['signals']} signals "
                     f"(any-candle baseline {r['base']:.0%}), average {r['avg']:+.2f} points")
        else:
            st.write(f"{name}: no signals")
    st.caption("Signals overlap, costs are ignored. A hit rate near the baseline means no edge.")
    st.dataframe(rep["factors"], hide_index=True)

    if st.checkbox("Show multi-timeframe bias"):
        cols = st.columns(4)
        for col, other in zip(cols, ["5m", "15m", "30m", "1h"]):
            try:
                s = float(an.current_view(an.add_features(load(label, other)), end=end)[0].score.iloc[-1])
                col.metric(other, an.bias_of(s, threshold), f"{s:+.0f}")
            except Exception as e:
                col.error(str(e)[:100])


view()
st.caption("Rule-based estimate for study only. It is not a forecast guarantee or investment advice.")
