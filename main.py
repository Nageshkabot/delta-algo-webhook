import os
import time
import hmac
import hashlib
import json
import requests
import pandas as pd
from fastapi import FastAPI
from typing import Dict, Any

app = FastAPI()

# ==================== CONFIGURATION ====================
PRODUCT_SYMBOL = "BTCUSD"         # Chart ke hisab se exact symbol
TIMEFRAME = "1m"                  # 1-minute chart

LEVERAGE = 200                    # Delta Exchange Leverage (200x)
LOT_SIZE = 1                      # Lot Quantity

DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")

# DELTA EXCHANGE INDIA API BASE URL
DELTA_BASE_URL = "https://api.india.delta.exchange"

# ==================== DELTA EXCHANGE API SIGNER ====================
def generate_delta_signature(method: str, path: str, payload: str, timestamp: str) -> str:
    signature_data = method + timestamp + path + payload
    return hmac.new(
        DELTA_API_SECRET.encode('utf-8'),
        signature_data.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def delta_request(method: str, path: str, payload: dict = None) -> dict:
    if not DELTA_API_KEY or not DELTA_API_SECRET:
        return {"error": "Delta API Credentials Missing"}

    timestamp = str(int(time.time()))
    body_str = json.dumps(payload) if payload else ""
    signature = generate_delta_signature(method, path, body_str, timestamp)

    headers = {
        "api-key": DELTA_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "Content-Type": "application/json"
    }

    url = f"{DELTA_BASE_URL}{path}"
    try:
        if method == "POST":
            res = requests.post(url, headers=headers, data=body_str, timeout=10)
        elif method == "GET":
            res = requests.get(url, headers=headers, timeout=10)
        return res.json()
    except Exception as e:
        return {"error": str(e)}

def execute_delta_order(product_symbol: str, size: int, side: str) -> dict:
    path = "/v2/orders"
    payload = {
        "product_symbol": product_symbol,
        "size": size,
        "side": side.lower(),
        "order_type": "market_order"
    }
    return delta_request("POST", path, payload)

def get_active_delta_position(product_symbol: str) -> dict:
    path = f"/v2/positions?product_symbol={product_symbol}"
    res = delta_request("GET", path)
    if "result" in res and res["result"]:
        size = int(res["result"].get("size", 0))
        if size > 0:
            return {"position": "LONG", "quantity": abs(size)}
        elif size < 0:
            return {"position": "SHORT", "quantity": abs(size)}
    return {"position": None, "quantity": 0}

# ==================== DIRECT DELTA INDIA CANDLESTICK FETCHING ====================
def fetch_delta_ohlcv(symbol: str, resolution: str = "1m", limit: int = 250) -> pd.DataFrame:
    """Directly fetches live candles from Delta India API"""
    end_time = int(time.time())
    start_time = end_time - (limit * 60)
    
    url = f"{DELTA_BASE_URL}/v2/history/candles?symbol={symbol}&resolution={resolution}&start={start_time}&end={end_time}"
    res = requests.get(url, timeout=10)
    data = res.json()
    
    if "result" not in data or not data["result"]:
        raise Exception(f"Failed to fetch market data from Delta: {data}")
    
    df = pd.DataFrame(data["result"])
    df = df.sort_values(by="time").reset_index(drop=True)
    
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    return df

# ==================== INDICATOR & SIGNAL ENGINE ====================
def fetch_and_analyze() -> Dict[str, Any]:
    df = fetch_delta_ohlcv(PRODUCT_SYMBOL, resolution=TIMEFRAME, limit=250)

    # Moving Averages
    df['ema14'] = df['close'].ewm(span=14, adjust=False).mean()
    df['ema14_smooth'] = df['close'].ewm(alpha=1/14, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # Breakout Levels (20-period High/Low)
    df['res_20'] = df['high'].shift(1).rolling(20).max()
    df['sup_20'] = df['low'].shift(1).rolling(20).min()

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    bullish_ma = (curr['ema14'] > curr['ema14_smooth']) and \
                 (curr['ema14_smooth'] > curr['ema50']) and \
                 (curr['ema50'] > curr['ema200'])

    bearish_ma = (curr['ema14'] < curr['ema14_smooth']) and \
                 (curr['ema14_smooth'] < curr['ema50']) and \
                 (curr['ema50'] < curr['ema200'])

    bullish_breakout = curr['close'] > prev['res_20']
    bearish_breakout = curr['close'] < prev['sup_20']

    signal = "HOLD"
    if bullish_ma and bullish_breakout:
        signal = "BUY"
    elif bearish_ma and bearish_breakout:
        signal = "SELL"

    return {
        "price": float(curr['close']),
        "ema14": float(curr['ema14']),
        "ema14_smooth": float(curr['ema14_smooth']),
        "ema50": float(curr['ema50']),
        "ema200": float(curr['ema200']),
        "signal": signal
    }

# ==================== ENDPOINTS ====================
@app.get("/")
def home():
    return {
        "status": "online", 
        "engine": "Delta-India-Native-Engine",
        "symbol": PRODUCT_SYMBOL,
        "configured_lots": LOT_SIZE,
        "configured_leverage": LEVERAGE
    }

@app.get("/tick")
def process_market_tick():
    try:
        analysis = fetch_and_analyze()
        price = analysis["price"]
        signal = analysis["signal"]
        
        active_state = get_active_delta_position(PRODUCT_SYMBOL)
        current_pos = active_state["position"]
        pos_qty = active_state["quantity"]

        executed_trade = None

        # Exits
        if current_pos == "LONG" and (analysis["ema14"] < analysis["ema14_smooth"] or signal == "SELL"):
            trade_res = execute_delta_order(PRODUCT_SYMBOL, pos_qty, "sell")
            executed_trade = {"action": "EXIT_LONG", "price": price, "response": trade_res}

        elif current_pos == "SHORT" and (analysis["ema14"] > analysis["ema14_smooth"] or signal == "BUY"):
            trade_res = execute_delta_order(PRODUCT_SYMBOL, pos_qty, "buy")
            executed_trade = {"action": "EXIT_SHORT", "price": price, "response": trade_res}

        # Entries
        elif current_pos is None:
            if signal == "BUY":
                trade_res = execute_delta_order(PRODUCT_SYMBOL, LOT_SIZE, "buy")
                executed_trade = {"action": "ENTER_LONG", "price": price, "lots": LOT_SIZE, "response": trade_res}

            elif signal == "SELL":
                trade_res = execute_delta_order(PRODUCT_SYMBOL, LOT_SIZE, "sell")
                executed_trade = {"action": "ENTER_SHORT", "price": price, "lots": LOT_SIZE, "response": trade_res}

        return {
            "status": "success",
            "market_price": price,
            "analysis": analysis,
            "trade_event": executed_trade,
            "synced_state": active_state
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
