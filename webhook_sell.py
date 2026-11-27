import hashlib
import hmac
import json
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from flask import Flask, jsonify, request
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest


# This service is deliberately PAPER ONLY. Do not change paper=True to False here.
API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["TRADINGVIEW_WEBHOOK_SECRET"]
ALLOWED_SYMBOLS = {
    item.strip().upper()
    for item in os.environ.get(
        "ALLOWED_SYMBOLS",
        "IBIT,ALAB,AMD,ARM,AVGO,ASML,MRVL,MU,NVDA,PLTR,TSLA,TSM",
    ).split(",")
    if item.strip()
}
DB_PATH = os.environ.get("WEBHOOK_DB_PATH", "webhook_events.sqlite3")
SELL_FRACTION = Decimal("0.10")

if len(WEBHOOK_SECRET) < 24:
    raise RuntimeError("TRADINGVIEW_WEBHOOK_SECRET must be at least 24 characters.")

trading = TradingClient(API_KEY, SECRET_KEY, paper=True)
app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS webhook_events (
            event_key TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            status TEXT NOT NULL,
            order_id TEXT,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return conn


def normalize_symbol(raw_symbol):
    # TradingView may send an exchange-prefixed ticker, e.g. NASDAQ:ALAB.
    symbol = str(raw_symbol or "").strip().upper().rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Z0-9.]{1,15}", symbol):
        raise ValueError("Invalid symbol")
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Symbol is not enabled in ALLOWED_SYMBOLS")
    return symbol


def parse_event(payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON object required")
    supplied_secret = str(payload.get("secret", ""))
    if not hmac.compare_digest(supplied_secret, WEBHOOK_SECRET):
        raise PermissionError("Invalid webhook secret")
    if str(payload.get("action", "")).lower() != "sell":
        raise ValueError("Only action=sell is accepted")
    if str(payload.get("level", "")) != "5":
        raise ValueError("Only Level 5 alerts are accepted")

    symbol = normalize_symbol(payload.get("symbol"))
    bar_time = str(payload.get("time", "")).strip()
    if not bar_time or len(bar_time) > 80:
        raise ValueError("A valid TradingView bar time is required")
    try:
        price = Decimal(str(payload.get("price", "")))
    except (InvalidOperation, ValueError):
        raise ValueError("A numeric price is required")
    if not price.is_finite() or price <= 0:
        raise ValueError("Price must be a positive finite number")

    # US listed stocks at/above $1 are rounded down to cents. This is a
    # conservative limit price based on the TradingView bar close supplied.
    limit_price = price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if limit_price <= 0:
        raise ValueError("Price is too small for a valid limit price")
    return symbol, bar_time, limit_price


def existing_open_sell(symbol):
    orders = trading.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
            limit=500,
            direction="desc",
        )
    )
    for order in orders:
        side = getattr(getattr(order, "side", None), "value", str(getattr(order, "side", ""))).lower()
        if side == "sell":
            return order
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "mode": "paper"}), 200


@app.post("/tradingview")
def tradingview_webhook():
    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type must be application/json"}), 400
    payload = request.get_json(silent=True)
    try:
        symbol, bar_time, limit_price = parse_event(payload)
    except PermissionError:
        # Do not echo secrets or payload contents to logs/responses.
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    event_key = hashlib.sha256(f"sell|{symbol}|{bar_time}".encode()).hexdigest()
    conn = get_db()
    try:
        # Keep the transaction lock through the position/order checks and submit,
        # preventing simultaneous requests in this small single-service setup.
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT status, order_id FROM webhook_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if prior:
            conn.execute("COMMIT")
            return jsonify({"ok": True, "duplicate": True, "status": prior[0], "order_id": prior[1]}), 200

        conn.execute(
            "INSERT INTO webhook_events (event_key, symbol, bar_time, status) VALUES (?, ?, ?, ?)",
            (event_key, symbol, bar_time, "processing"),
        )

        open_sell = existing_open_sell(symbol)
        if open_sell:
            detail = "An open sell order already exists for this symbol; no additional order submitted."
            conn.execute(
                "UPDATE webhook_events SET status = ?, detail = ? WHERE event_key = ?",
                ("skipped_open_sell", detail, event_key),
            )
            conn.execute("COMMIT")
            return jsonify({"ok": True, "status": "skipped_open_sell"}), 200

        position = trading.get_open_position(symbol)
        position_qty = Decimal(str(position.qty))
        if position_qty <= 0:
            conn.execute(
                "UPDATE webhook_events SET status = ?, detail = ? WHERE event_key = ?",
                ("skipped_no_long_position", "No long position available to sell.", event_key),
            )
            conn.execute("COMMIT")
            return jsonify({"ok": True, "status": "skipped_no_long_position"}), 200

        # Alpaca limit sell quantities are sent as whole shares here. Always
        # round down; never round up past 10% of the held position.
        quantity = int((position_qty * SELL_FRACTION).to_integral_value(rounding=ROUND_DOWN))
        if quantity < 1:
            conn.execute(
                "UPDATE webhook_events SET status = ?, detail = ? WHERE event_key = ?",
                ("skipped_quantity_below_one", "10% of the position is less than one whole share.", event_key),
            )
            conn.execute("COMMIT")
            return jsonify({"ok": True, "status": "skipped_quantity_below_one"}), 200

        client_order_id = "TV-" + event_key[:32]
        order = trading.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=float(limit_price),
                client_order_id=client_order_id,
            )
        )
        conn.execute(
            "UPDATE webhook_events SET status = ?, order_id = ?, detail = ? WHERE event_key = ?",
            ("submitted", str(order.id), f"qty={quantity}; limit={limit_price}", event_key),
        )
        conn.execute("COMMIT")
        return jsonify({
            "ok": True,
            "status": "submitted",
            "symbol": symbol,
            "qty": quantity,
            "limit_price": str(limit_price),
            "order_id": str(order.id),
        }), 200
    except Exception:
        # A request failure can be ambiguous if Alpaca accepted an order but the
        # connection timed out. Retain the event key and require manual review;
        # do not automatically resubmit this bar's alert.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        app.logger.exception("Webhook processing failed for symbol=%s", symbol)
        return jsonify({"ok": False, "error": "Processing failed; inspect service logs and Alpaca paper orders."}), 500
    finally:
        conn.close()


if __name__ == "__main__":
    # Local development only; use gunicorn or the host's production server when deployed.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
