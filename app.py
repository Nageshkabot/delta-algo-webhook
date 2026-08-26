import os
import time
import hmac
import hashlib
import requests
import json
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# System Buffer & Active Trade State
alerts_buffer = []
buffer_lock = threading.Lock()
timer = None

BUFFER_TIME = 3  # Wait time (seconds) to accumulate volume alerts

# Active Trade Monitoring State (In-Memory)
# Structure: {'symbol': {'side': 'buy', 'entry_price': 0, 'trailing_sl': 0, 'last_green_low': 0}}
active_positions = {}

# Delta Exchange India Configuration
DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")
DELTA_BASE_URL = "https://api.india.delta.exchange"

# ---------------------------------------------------------
# DELTA EXCHANGE API SIGNATURE GENERATOR
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
# ORDER EXECUTION FUNCTIONS
# ---------------------------------------------------------
def place_order(symbol, side, size=1):
    """Buy ya Sell Order Place karne ke liye"""
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
    """Position Exit (Stop Loss hit hone par)"""
    if symbol in active_positions:
        pos = active_positions[symbol]
        exit_side = "sell" if pos['side'] == "buy" else "buy"
        print(f"🛑 STOP LOSS HIT! Closing Position for {symbol} via {exit_side.upper()} Order.")
        place_order(symbol, side=exit_side, size=1)
        del active_positions[symbol]

# ---------------------------------------------------------
# TRAILING STOP LOSS ENGINE (Candle Low / High Trail)
# ---------------------------------------------------------
def monitor_trailing_sl(symbol):
    """Har 15-sec me price aur green/red candle low trail check karega"""
    if symbol not in active_positions:
        return

    pos = active_positions[symbol]
    
    # Delta Exchange se Latest Ticker / Candle Data fetch karein
    ticker = send_delta_request("GET", f"/v2/tickers/{symbol}")
    if not ticker or 'result' not in ticker:
        return

    current_price = float(ticker['result']['mark_price'])
    
    # 15 Minute Candle History Fetching
    candles = send_delta_request("GET", f"/v2/history/candles?resolution=15m&symbol={symbol}")
    if not candles or 'result' not in candles or len(candles['result']) < 2:
        return

    # Previous Closed Candle Data
    last_candle = candles['result'][-2] 
    c_open = float(last_candle['open'])
    c_close = float(last_candle['close'])
    c_high = float(last_candle['high'])
    c_low = float(last_candle['low'])

    # 1. BUY POSITION TRAILING SL LOGIC
    if pos['side'] == "buy":
        # Green Candle check (Close > Open)
        if c_close > c_open:
            # Shift SL to the Low of this Green Candle if it is higher than previous SL
            if c_low > pos['trailing_sl']:
                pos['trailing_sl'] = c_low
                print(f"📈 [BUY TRAIL] New Green Candle Low Found! Trailing SL updated to: {pos['trailing_sl']}")

        # SL Hit Check: Current price breaks Green Candle Low
        if current_price < pos['trailing_sl'] and pos['trailing_sl'] > 0:
            print(f"⚠️ Price ({current_price}) broke Green Candle Low SL ({pos['trailing_sl']})")
            close_position(symbol)

    # 2. SELL POSITION TRAILING SL LOGIC
    elif pos['side'] == "sell":
        # Red Candle check (Close < Open)
        if c_close < c_open:
            # Shift SL to the High of this Red Candle if it is lower than previous SL
            if pos['trailing_sl'] == 0 or c_high < pos['trailing_sl']:
                pos['trailing_sl'] = c_high
                print(f"📉 [SELL TRAIL] New Red Candle High Found! Trailing SL updated to: {pos['trailing_sl']}")

        # SL Hit Check: Current price breaks Red Candle High
        if current_price > pos['trailing_sl'] and pos['trailing_sl'] > 0:
            print(f"⚠️ Price ({current_price}) broke Red Candle High SL ({pos['trailing_sl']})")
            close_position(symbol)

# ---------------------------------------------------------
# ALGO BATCH PROCESSOR
# ---------------------------------------------------------
def process_alerts_and_trade():
    global alerts_buffer, timer
    with buffer_lock:
        if not alerts_buffer:
            return

        print(f"\n⚡ Processing {len(alerts_buffer)} alerts from webhook scan...")

        # Filter: Volume ke hisaab se Descending (High Volume First) Sort
        sorted_alerts = sorted(
            alerts_buffer,
            key=lambda x: float(x.get("volume", 0)),
            reverse=True
        )

        top_alert = sorted_alerts[0]
        symbol = top_alert.get("symbol", "BTCUSD")
        side = top_alert.get("action", "buy").lower()  # 'buy' or 'sell'
        current_price = float(top_alert.get("price", 0))

        print(f"🏆 TOP SELECTED SYMBOL: {symbol} | Action: {side.upper()} | Price: {current_price}")

        # Agar pehle se koi active trade hai, toh skip ya handle karein
        if symbol not in active_positions:
            order_res = place_order(symbol, side=side, size=1)
            
            # Active Position State Initialize Karein
            active_positions[symbol] = {
                'side': side,
                'entry_price': current_price,
                'trailing_sl': 0,  # Pehle Green/Red Candle close par update hoga
                'last_green_low': 0
            }
            print(f"✅ Trade Registered in Algo Engine for {symbol}")

        # Buffer Reset
        alerts_buffer = []
        timer = None

# Background Worker for Continuous SL Monitoring (Every 10 Seconds)
def background_sl_checker():
    while True:
        try:
            symbols = list(active_positions.keys())
            for sym in symbols:
                monitor_trailing_sl(sym)
        except Exception as e:
            print(f"Error in SL Checker: {str(e)}")
        time.sleep(10)

# Start Background Thread for SL Checking
threading.Thread(target=background_sl_checker, daemon=True).start()

# ---------------------------------------------------------
# FLASK WEBHOOK ROUTES
# ---------------------------------------------------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "active", "active_trades": len(active_positions)}), 200

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
