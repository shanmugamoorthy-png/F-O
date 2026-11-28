# F&O Price Action Chart (Dhan API)

Index, F&O stock and futures candles (1m to Daily), EMA, VWAP, CPR/pivot, patterns, RSI,
commentary from the previous 2 sessions, next-candle estimate and a backtest.

The **Top 10 scanner** page ranks F&O stocks by score (bullish and bearish).

## Deploy on Render
1. Render dashboard > New > Blueprint > pick this repository and branch.
2. Enter `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, and an `APP_PASSWORD` of your choice.
3. Open the service URL and type the app password in the sidebar.

Dhan access tokens last 24 hours: paste the new one in the sidebar, or update it in Render.
Candle data needs an active Dhan Data API subscription.

## Run locally
    pip install -r requirements.txt
    export DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... APP_PASSWORD=...
    streamlit run app.py

## How the score works
Seven weighted factors give a score from -100 to +100. It describes the current price
action (trend, VWAP, previous-day levels, RSI, momentum). The projected candles are an
illustration capped at the nearest support or resistance. It is not a forecast.

## Backtest findings (Sep 2026 to Nov 2026, F&O stocks, stock prices)
No tested rule made money after costs. Treat the app as a way to read price action, not as a signal.

| Test | Result |
|---|---|
| Top 10 bullish / bearish at 10:15, held to close (50 sessions) | Bullish -0.14%, bearish +0.04% per trade |
| Same, with 1:2 stop and target (0.5% to 2% stops) | No setting profitable in both halves |
| Breakout beyond previous day high/low, NIFTY filter (120 sessions) | About 0.00% per trade; the 50-session +0.18% did not hold |
| Opening range breakout, 15 and 30 minute (124 sessions) | Bullish -0.09% to -0.15%, bearish -0.01% to -0.04%; all negative at 0.25% cost |

Notes
- Costs assumed: 0.10% round trip, also checked at 0.25%. Real costs and slippage can be higher.
- Results are for stock prices, not option premiums, and use today's F&O list.
- Short samples looked promising twice and then disappeared with more data. Judge any new idea on
  older and newer dates separately, at a realistic cost, and try each setting only once.
- Not investment advice.
