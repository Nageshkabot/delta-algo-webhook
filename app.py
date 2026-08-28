import os
import time
import math
import hmac
import hashlib
import json
import requests
import ccxt
import pandas as pd
import pandas_ta as ta
from fastapi import FastAPI
from typing import Dict, Any

app = FastAPI()

# ==================== CONFIGURATION ====================
SYMBOL_BINANCE = "BTC/USDT"
SYMBOL_DELTA = "BTCUSD"           # Delta Exchange Perpetual Symbol
TIMEFRAME = "1m"
LEVERAGE = 10                     # Delta Leverage
POSITION_SIZE_USD = 100.0         # Allocated USD per trade

# Delta Exchange API Credentials (Set in Render Environment Variables)
DELTA_API_KEY = os.environ.get("DELTA_API_KEY", "")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "")
DELTA_BASE_URL = "https://api.delta.exchange"

# In-Memory State Tracking
state = {
    "position": None,         # "LONG", "SHORT", or None
    "entry_price": 0.0,
    "quantity": 0.0,
    "last_signal": None
}

# CCXT Binance Client (Public Data Engine)
binance = ccxt.binance({'enableRateLimit': True})


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
    """Executes Market Order on Delta Exchange"""
    path = "/v2/orders"
    payload = {
        "product_symbol": product_symbol,
        "size": size,
        "side": side.lower(),       # "buy" or "sell"
        "order_type": "market_order"
    }
    return delta_request("POST", path, payload)


# ==================== INDICATOR & SIGNAL ENGINE ====================
def fetch_and_analyze() -> Dict[str, Any]:
    # Fetch 250 1-minute candles from Binance
    ohlcv = binance.fetch_ohlcv(SYMBOL_BINANCE, timeframe=TIMEFRAME, limit=250)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    # Exponential Moving Averages
    df['ema14'] = ta.ema(df['close'], length=14)
    df['ema14_smooth'] = ta.rma(df['close'], length=14)  # Smoothed 14 EMA (RMA/SMMA)
    df['ema50'] = ta.ema(df['close'], length=50)
    df['ema200'] = ta.ema(df['close'], length=200)

    # Breakout Levels (20-period High/Low)
    df['res_20'] = df['high'].shift(1).rolling(20).max()
    df['sup_20'] = df['low'].shift(1).rolling(20).min()

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    # Alignment Sequence Checks
    bullish_ma = (curr['ema14'] > curr['ema14_smooth']) and \
                 (curr['ema14_smooth'] > curr['ema50']) and \
                 (curr['ema50'] > curr['ema200'])

    bearish_ma = (curr['ema14'] < curr['ema14_smooth']) and \
                 (curr['ema14_smooth'] < curr['ema50']) and \
                 (curr['ema50'] < curr['ema200'])

    # Breakout Checks
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


# ==================== CRON / WEBHOOK TICKER ENDPOINT ====================
@app.get("/")
def home():
    return {
        "status": "online",
        "engine": "Binance-Data-Delta-Execution-Trading-Bot",
        "state": state
    }

@app.get("/tick")
def process_market_tick():
    """Call this route every minute via Cron/UptimeRobot to evaluate and trade"""
    try:
        analysis = fetch_and_analyze()
        price = analysis["price"]
        signal = analysis["signal"]
        
        executed_trade = None

        # Exit Conditions (Trailing SL via EMA 14 Smooth Cross)
        if state["position"] == "LONG" and (analysis["ema14"] < analysis["ema14_smooth"] or signal == "SELL"):
            trade_res = execute_delta_order(SYMBOL_DELTA, int(state["quantity"]), "sell")
            state["position"] = None
            executed_trade = {"action": "EXIT_LONG", "price": price, "response": trade_res}

        elif state["position"] == "SHORT" and (analysis["ema14"] > analysis["ema14_smooth"] or signal == "BUY"):
            trade_res = execute_delta_order(SYMBOL_DELTA, int(state["quantity"]), "buy")
            state["position"] = None
            executed_trade = {"action": "EXIT_SHORT", "price": price, "response": trade_res}

        # Entry Conditions
        if state["position"] is None:
            contract_size = max(1, math.floor((POSITION_SIZE_USD * LEVERAGE) / price))

            if signal == "BUY":
                trade_res = execute_delta_order(SYMBOL_DELTA, contract_size, "buy")
                state["position"] = "LONG"
                state["entry_price"] = price
                state["quantity"] = contract_size
                executed_trade = {"action": "ENTER_LONG", "price": price, "size": contract_size, "response": trade_res}

            elif signal == "SELL":
                trade_res = execute_delta_order(SYMBOL_DELTA, contract_size, "sell")
                state["position"] = "SHORT"
                state["entry_price"] = price
                state["quantity"] = contract_size
                executed_trade = {"action": "ENTER_SHORT", "price": price, "size": contract_size, "response": trade_res}

        return {
            "status": "success",
            "market_price": price,
            "analysis": analysis,
            "trade_event": executed_trade,
            "current_state": state
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
