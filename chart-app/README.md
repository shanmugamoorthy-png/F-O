# F&O Price Action Chart (Dhan API)

Index, F&O stock and futures candles (1m to Daily), EMA, VWAP, CPR/pivot, patterns, RSI,
commentary from the previous 2 sessions, next-candle estimate and a backtest.

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

## How the estimate works
Seven weighted factors give a score from -100 to +100. The projected candles are an
illustration capped at the nearest support or resistance. Check the backtest panel before
relying on any signal. Study use only, not investment advice.
