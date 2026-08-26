import os
import time
import hmac
import hashlib
import requests
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# Buffer for holding multiple alerts triggered in a scan cycle
alerts_buffer = []
buffer_lock = threading.Lock()
timer = None

BUFFER_TIME = 3  # Wait 3 seconds to collect all alert triggers before sorting

# Delta Exchange API Credentials (Environment Variables se aayenge)
DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")
DELTA_BASE_URL = "https://api.india.delta.exchange"  # Delta Exchange India API Base URL

def generate_signature(secret, method, path, payload="", query_string=""):
    """Delta Exchange HMAC SHA256 Signature Generator"""
    timestamp = str(int(time.time()))
    signature_data = method + timestamp + path + query_string + payload
    message = bytes(signature_data, 'utf-8')
    secret_bytes = bytes(secret, 'utf-8')
    hash_object = hmac.new(secret_bytes, message, hashlib.sha256)
    return hash_object.hexdigest(), timestamp

def place_delta_order(symbol, side="buy", size=1):
    """Delta Exchange India API par Order Execution"""
    if not DELTA_API_KEY or not DELTA_API_SECRET:
        print("❌ ERROR: Delta API Keys missing in Environment Variables!")
        return

    endpoint = "/v2/orders"
    payload_dict = {
        "product_symbol": symbol, # e.g. BTCUSD, ETHUSD
        "size": size,
        "side": side.lower(),
        "order_type": "market_order"
    }
    
    import json
    payload_str = json.dumps(payload_dict)
    signature, timestamp = generate_signature(DELTA_API_SECRET, "POST", endpoint, payload_str)

    headers = {
        "api-key": DELTA_API_KEY,
        "signature": signature,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(DELTA_BASE_URL + endpoint, data=payload_str, headers=headers)
        print(f"🚀 Delta Response [{response.status_code}]: {response.text}")
    except Exception as e:
        print(f"❌ Order Execution Failed: {str(e)}")

def process_and_execute():
    """Buffered alerts ko Volume ke basis par Sort karke Top 1 me Trade lena"""
    global alerts_buffer, timer
    with buffer_lock:
        if not alerts_buffer:
            return

        print(f"\n[SCANNER BATCH] Received {len(alerts_buffer)} alerts.")

        # Volume ke aadhar par Descending Sort (High to Low Volume)
        sorted_alerts = sorted(
            alerts_buffer,
            key=lambda x: float(x.get("volume", 0)),
            reverse=True
        )

        top_alert = sorted_alerts[0]
        symbol = top_alert.get("symbol", "BTCUSD")
        side = top_alert.get("side", "buy")
        
        print(f"🔥 TOP SELECTED COIN: {symbol} | Volume: {top_alert.get('volume')}")

        # Execute Trade on Delta Exchange
        place_delta_order(symbol, side=side, size=1)

        # Reset Buffer for next cycle
        alerts_buffer = []
        timer = None

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "running"}), 200

@app.route('/webhook', methods=['POST'])
def webhook_receiver():
    global timer
    data = request.json or {}
    print(f"📥 Alert Received: {data}")

    with buffer_lock:
        alerts_buffer.append(data)
        if timer is None:
            timer = threading.Timer(BUFFER_TIME, process_and_execute)
            timer.start()

    return jsonify({"status": "buffered"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
