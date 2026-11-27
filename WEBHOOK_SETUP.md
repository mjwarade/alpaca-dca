# TradingView → Alpaca Paper Trading webhook

This is an independent, paper-only webhook service. It does not import or modify `trade.py`. It accepts a Level 5 sell alert, checks the matching symbol's paper position, and submits a DAY limit sell for 10% of the current long position (rounded down to whole shares) at the alert's TradingView `{{close}}` price. If 10% is less than one whole share, it skips the order. It also skips when an open sell order already exists for that symbol.

> **Paper-only guard:** `webhook_sell.py` constructs the Alpaca client with `paper=True`. It does not submit to the live endpoint. Do not change this for testing.

## 1. Create a paper API key pair

In Alpaca, switch to **Paper Trading**, then open the API keys/Manage Keys area and generate a paper key. You need both the **API Key ID** and **Secret Key**. The account number is not an API credential. The secret is typically shown only when generated; if you no longer have it, generate a new paper key pair and use the new pair.

## 2. Add environment variables (never commit credentials)

In the hosting service's dashboard, open this service's **Environment Variables / Secrets** settings and add:

- `ALPACA_API_KEY` = paper API Key ID
- `ALPACA_SECRET_KEY` = paper Secret Key
- `TRADINGVIEW_WEBHOOK_SECRET` = a long random value (at least 24 characters), known to you and included in the TradingView alert message
- `ALLOWED_SYMBOLS` = comma-separated symbols allowed to trade, for example `ALAB,AMD,NVDA` (or your full watchlist)

Do not put real values in `.env.webhook.example`, `webhook_sell.py`, TradingView alert URLs, GitHub commits, or chat. The example file only documents variable names. For local testing, copy it to a local environment file only if your tooling loads it, and ensure the real file is ignored by Git; otherwise export variables in your shell. The service itself reads environment variables and does not load a dotenv file.

### Local environment example (macOS/Linux shell)

```sh
export ALPACA_API_KEY='your-paper-key-id'
export ALPACA_SECRET_KEY='your-paper-secret-key'
export TRADINGVIEW_WEBHOOK_SECRET='your-long-random-secret'
export ALLOWED_SYMBOLS='ALAB,AMD,NVDA'
pip install -r requirements-webhook.txt
gunicorn --bind 0.0.0.0:8080 webhook_sell:app
```

A local server is not reachable by TradingView unless you expose it through a secure HTTPS tunnel; use paper credentials only. Check `GET /health` to see `{"ok":true,"mode":"paper"}`.

## 3. Deploy the service

Deploy this repository/branch to a Python web host that provides a public HTTPS URL and persistent environment variables. Configure:

- Build/install command: `pip install -r requirements-webhook.txt`
- Start command: `gunicorn --bind 0.0.0.0:$PORT webhook_sell:app`
- Environment variables as listed above
- Persistent storage for `webhook_events.sqlite3`, if the host offers it

The SQLite database deduplicates alerts by symbol and bar time. If the host's filesystem is ephemeral, duplicate protection may be lost after a restart/redeploy; use persistent disk or a managed database for reliable operation. Free instances may sleep or have cold starts. TradingView expects a prompt webhook response, so a sleeping host can make delivery unreliable; use an always-on service for dependable alerts. Keep the service independent from the scheduled GitHub Actions workflow running `trade.py`.

## 4. Set the TradingView alert

Create an alert for the indicator's **Crossing Up → Level 5** condition. Choose the intended 8-hour chart interval and **Once Per Bar Close**. Set the Webhook URL to:

```text
https://YOUR-HOST/tradingview
```

Set the alert message to valid JSON (replace the placeholder with your actual webhook secret; do not include Alpaca credentials):

```json
{
  "secret": "YOUR_LONG_RANDOM_WEBHOOK_SECRET",
  "action": "sell",
  "symbol": "{{ticker}}",
  "level": "5",
  "price": "{{close}}",
  "time": "{{time}}"
}
```

The service accepts only symbols in `ALLOWED_SYMBOLS`. It normalizes exchange-prefixed tickers such as `NASDAQ:ALAB`. The alert's close is used as the limit price, rounded down to cents. Ensure the chart is the ticker you intend to sell; `{{close}}` is the chart's close, not a separately fetched Alpaca quote.

## 5. Test safely

1. Start with paper API credentials and only the intended test symbol in `ALLOWED_SYMBOLS` (for example `ALAB`).
2. Confirm the paper account has a long position of at least 10 whole shares; otherwise the 10% calculation rounds below one share and the service skips it.
3. Trigger a test alert and inspect the service response/logs and Alpaca **paper** orders/positions.
4. Confirm the limit price and whole-share quantity. Limit orders may remain open or expire unfilled; this service does not cancel or replace them.
5. Keep `trade.py` and its existing workflow unchanged. This webhook process is a separate file and process.

## Operational notes

- Alert secrets authenticate requests but are carried in the alert body; keep them private and rotate them if exposed. Never use Alpaca API credentials as the webhook secret.
- The service rejects invalid secrets, non-Level-5 alerts, symbols outside the allowlist, missing/invalid prices, duplicate symbol/bar alerts, and attempts when a sell order is already open.
- Errors are not automatically retried as new orders because a network timeout can occur after Alpaca accepted an order. Check paper orders and service logs before retrying.
- This is a small example service, not a high-availability execution system. Keep paper mode enabled while validating it.
