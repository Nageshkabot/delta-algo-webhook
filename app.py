import os
import time
import hmac
import hashlib
import requests
import json
import threading
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------
# SYSTEM CONFIGURATION & GLOBAL STATES
# ---------------------------------------------------------
alerts_buffer = []
buffer_lock = threading.Lock()
timer = None

BUFFER_TIME = 3  # Wait time (seconds) to accumulate volume alerts

# Strategy Parameters (1-Minute Timeframe Configuration)
DEFINED_RESISTANCE_LEVEL = 50.0  # Buy Breakout boundary
DEFINED_SUPPORT_LEVEL = 48.0     # Sell Breakdown boundary (Apne level ke hisab se set karein)

ACTIVE_POSITIONS = {}  # In-memory tracking: {'symbol': {'side': 'buy/sell', 'entry_price': 0, 'trailing_sl': 0}}

# Delta Exchange India Configuration
DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")
DELTA_BASE_URL = "https://api.india.delta.exchange"


# ---------------------------------------------------------
# DELTA EXCHANGE API SIGNATURE & REQUEST ENGINE
# ---------------------------------------------------------
def generate_signature(secret, method, path, payload="", query_string=""):
    timestamp = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    message = bytes(signature_data, 'utf-8')
    secret_bytes = bytes(secret, 'utf-8')
    hash_object = hmac.new(secret_bytes, message, hashlib.sha256)
    return hash_object.hexdigest(), timestamp


def send_delta_request(method, endpoint, payload=None):
    if not DELTA_API_KEY or not DELTA_API_SECRET:
        print("❌ ERROR: Delta API Keys missing in Environment Variables!")
        return None

    payload_str = json.dumps(payload) if payload else ""
    signature, timestamp = generate_signature(DELTA_API_SECRET, method, endpoint, payload_str)

    headers = {
        "api-key": DELTA_API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }

    url = DELTA_BASE_URL + endpoint
    try:
        if method == "POST":
            res = requests.post(url, data=payload_str, headers=headers)
        elif method == "GET":
            res = requests.get(url, headers=headers)
        return res.json()
    except Exception as e:
        print(f"❌ Delta API Request Failed: {str(e)}")
        return None


# ---------------------------------------------------------
# ORDER EXECUTION ENGINE
# ---------------------------------------------------------
def place_order(symbol, side, size=1):
    """Buy ya Sell Market Order Place karta hai"""
    endpoint = "/v2/orders"
    payload = {
        "product_symbol": symbol,
        "size": size,
        "side": side.lower(),
        "order_type": "market_order"
    }
    print(f"🚀 Placing Market Order: {side.upper()} {symbol} (Size: {size})")
    response = send_delta_request("POST", endpoint, payload)
    print(f"📥 Delta Order Response: {response}")
    return response


def close_position(symbol):
    """Stop Loss ya Exit Condition par Position Close karta hai"""
    if symbol in ACTIVE_POSITIONS:
        pos = ACTIVE_POSITIONS[symbol]
        exit_side = "sell" if pos['side'] == "buy" else "buy"
        print(f"🛑 EXIT SIGNAL HIT! Closing Position for {symbol} via {exit_side.upper()} Order.")
        place_order(symbol, side=exit_side, size=1)
        del ACTIVE_POSITIONS[symbol]


# ---------------------------------------------------------
# DATA FETCHING & TECHNICAL ANALYSIS (PANDAS ENGINE)
# ---------------------------------------------------------
def fetch_and_prepare_df(symbol):
    """Delta API se 1m candles fetch karke indicators compute karta hai"""
    candles = send_delta_request("GET", f"/v2/history/candles?resolution=1m&symbol={symbol}")
    if not candles or 'result' not in candles or len(candles['result']) < 201:
        return None

    df = pd.DataFrame(candles['result'])
    
    for col in ['open', 'high', 'low', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    if 'start_time' in df.columns and df['start_time'].iloc[0] > df['start_time'].iloc[-1]:
        df = df.iloc[::-1].reset_index(drop=True)

    # Moving Average calculations (1m timeframe)
    df['sma_200'] = df['close'].rolling(window=200).mean()
    df['sma_50'] = df['close'].rolling(window=50).mean()
    df['sma_14'] = df['close'].rolling(window=14).mean()
    df['sma_14_faster'] = df['close'].rolling(window=7).mean()

    return df


def get_recent_pivot_low(df, lookback=20):
    """Buy Trailing SL: Last N candles ka lowest point"""
    recent_data = df.tail(lookback)
    if len(recent_data) > 3:
        return float(recent_data['low'].min())
    return None


def get_recent_pivot_high(df, lookback=20):
    """Sell Trailing SL: Last N candles ka highest point"""
    recent_data = df.tail(lookback)
    if len(recent_data) > 3:
        return float(recent_data['high'].max())
    return None


# ---------------------------------------------------------
# STRATEGY & MONITORING ENGINE (1-MINUTE RUNNER)
# ---------------------------------------------------------
def monitor_and_execute():
    """Har active trade (BUY/SELL) ke trailing SL aur exit rules check karta hai"""
    symbols = list(ACTIVE_POSITIONS.keys())
    for symbol in symbols:
        df = fetch_and_prepare_df(symbol)
        if df is None or len(df) < 201:
            continue

        pos = ACTIVE_POSITIONS[symbol]
        current_row = df.iloc[-1]
        current_price = float(current_row['close'])
        current_low = float(current_row['low'])
        current_high = float(current_row['high'])

        # --- 1. BUY POSITION EXIT & TRAILING LOGIC ---
        if pos['side'] == "buy":
            # SL Hit Check (Price breaks Pivot Low SL)
            if pos['trailing_sl'] > 0 and current_low <= pos['trailing_sl']:
                print(f"⚠️ [BUY SL HIT] Price ({current_low}) broke Trailing SL ({pos['trailing_sl']})")
                close_position(symbol)
                continue

            # Exit Condition (Close below 50 SMA)
            if current_price < current_row['sma_50']:
                print(f"⚠️ [BUY EXIT] Price ({current_price}) dropped below 50 SMA ({current_row['sma_50']})")
                close_position(symbol)
                continue

            # Dynamic Trailing SL Update (Only move UP)
            new_pivot_sl = get_recent_pivot_low(df, lookback=20)
            if new_pivot_sl and new_pivot_sl > pos['trailing_sl']:
                pos['trailing_sl'] = new_pivot_sl
                print(f"📈 [BUY TRAIL] Updated SL to Higher Pivot Low: {pos['trailing_sl']}")

        # --- 2. SELL POSITION EXIT & TRAILING LOGIC ---
        elif pos['side'] == "sell":
            # SL Hit Check (Price breaks Pivot High SL)
            if pos['trailing_sl'] > 0 and current_high >= pos['trailing_sl']:
                print(f"⚠️ [SELL SL HIT] Price ({current_high}) broke Trailing SL ({pos['trailing_sl']})")
                close_position(symbol)
                continue

            # Exit Condition (Close above 50 SMA)
            if current_price > current_row['sma_50']:
                print(f"⚠️ [SELL EXIT] Price ({current_price}) crossed above 50 SMA ({current_row['sma_50']})")
                close_position(symbol)
                continue

            # Dynamic Trailing SL Update (Only move DOWN)
            new_pivot_sl = get_recent_pivot_high(df, lookback=20)
            if new_pivot_sl and (pos['trailing_sl'] == 0 or new_pivot_sl < pos['trailing_sl']):
                pos['trailing_sl'] = new_pivot_sl
                print(f"📉 [SELL TRAIL] Updated SL to Lower Pivot High: {pos['trailing_sl']}")


def background_sl_checker():
    """Har 10 second mein continuous position monitoring karta hai"""
    while True:
        try:
            if ACTIVE_POSITIONS:
                monitor_and_execute()
        except Exception as e:
            print(f"Error in Background Engine: {str(e)}")
        time.sleep(10)


threading.Thread(target=background_sl_checker, daemon=True).start()


# ---------------------------------------------------------
# ALGO BATCH PROCESSOR (BUY & SELL ENTRY TRIGGER)
# ---------------------------------------------------------
def process_alerts_and_trade():
    global alerts_buffer, timer
    with buffer_lock:
        if not alerts_buffer:
            return

        print(f"\n⚡ Processing {len(alerts_buffer)} alerts from webhook scan...")

        sorted_alerts = sorted(
            alerts_buffer,
            key=lambda x: float(x.get("volume", 0)),
            reverse=True
        )

        top_alert = sorted_alerts[0]
        symbol = top_alert.get("symbol", "BTCUSD")
        side = top_alert.get("action", "buy").lower()  # 'buy' ya 'sell'

        df = fetch_and_prepare_df(symbol)
        if df is not None and len(df) >= 201:
            current_row = df.iloc[-1]
            prev_row = df.iloc[-2]

            # --- BUY CONDITIONS ---
            buy_breakout = (current_row['close'] > DEFINED_RESISTANCE_LEVEL) and (prev_row['close'] <= DEFINED_RESISTANCE_LEVEL)
            buy_ma_order = (
                (current_row['sma_14'] > current_row['sma_50']) and
                (current_row['sma_50'] > current_row['sma_200']) and
                (current_row['sma_14'] > current_row['sma_14_faster'])
            )

            # --- SELL CONDITIONS (OPPOSITE) ---
            sell_breakdown = (current_row['close'] < DEFINED_SUPPORT_LEVEL) and (prev_row['close'] >= DEFINED_SUPPORT_LEVEL)
            sell_ma_order = (
                (current_row['sma_14'] < current_row['sma_50']) and
                (current_row['sma_50'] < current_row['sma_200']) and
                (current_row['sma_14'] < current_row['sma_14_faster'])
            )

            if symbol not in ACTIVE_POSITIONS:
                # 1. Execute BUY
                if side == "buy" and buy_breakout and buy_ma_order:
                    place_order(symbol, side="buy", size=1)
                    entry_price = float(current_row['close'])
                    initial_sl = get_recent_pivot_low(df, lookback=20) or (entry_price * 0.98)

                    ACTIVE_POSITIONS[symbol] = {
                        'side': 'buy',
                        'entry_price': entry_price,
                        'trailing_sl': initial_sl
                    }
                    print(f"✅ BUY Trade Executed! Symbol: {symbol} | Entry: {entry_price} | SL: {initial_sl}")

                # 2. Execute SELL (SHORT)
                elif side == "sell" and sell_breakdown and sell_ma_order:
                    place_order(symbol, side="sell", size=1)
                    entry_price = float(current_row['close'])
                    initial_sl = get_recent_pivot_high(df, lookback=20) or (entry_price * 1.02)

                    ACTIVE_POSITIONS[symbol] = {
                        'side': 'sell',
                        'entry_price': entry_price,
                        'trailing_sl': initial_sl
                    }
                    print(f"✅ SELL Trade Executed! Symbol: {symbol} | Entry: {entry_price} | SL: {initial_sl}")

                else:
                    print(f"⚠️ Strategy Filters Failed for {symbol} (Action Requested: {side.upper()}). Trade Skipped.")

        alerts_buffer = []
        timer = None


# ---------------------------------------------------------
# FLASK WEBHOOK ROUTES
# ---------------------------------------------------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "active", "active_trades": len(ACTIVE_POSITIONS)}), 200


@app.route('/webhook', methods=['POST'])
def webhook_receiver():
    global timer
    data = request.json or {}
    print(f"📥 Webhook Triggered: {data}")

    with buffer_lock:
        alerts_buffer.append(data)
        if timer is None:
            timer = threading.Timer(BUFFER_TIME, process_alerts_and_trade)
            timer.start()

    return jsonify({"status": "buffered"}), 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
