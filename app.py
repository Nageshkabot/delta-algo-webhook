import os
from flask import Flask, request, jsonify
import pandas as pd
import numpy as np

app = Flask(__name__)

# Core Trading Strategy Engine
class TradingEngine:
    def __init__(self, resistance_level=50.0):
        self.resistance_level = resistance_level
        self.pos_active = False
        self.entry_price = 0.0
        self.current_stop_loss = 0.0
        
    def calculate_indicators(self, df):
        """
        Calculates 200 SMA, 50 SMA, 14 SMA, and 14 Smooth SMA
        """
        df['sma_200'] = df['close'].rolling(window=200, min_periods=1).mean()
        df['sma_50'] = df['close'].rolling(window=50, min_periods=1).mean()
        df['sma_14'] = df['close'].rolling(window=14, min_periods=1).mean()
        # Double-smoothed 14 MA (MA of 14 MA)
        df['sma_14_smooth'] = df['sma_14'].rolling(window=14, min_periods=1).mean()
        return df

    def get_pivot_low(self, df, window=10):
        """Finds recent dynamic support/pivot low for trailing SL"""
        if len(df) >= window:
            return df['low'].tail(window).min()
        return df['low'].min()

    def process_candle_data(self, ohlc_data):
        """
        Input: List of dicts or DataFrame with columns: open, high, low, close
        Returns: Dict status with signal and current trade state
        """
        df = pd.DataFrame(ohlc_data)
        if len(df) < 5:
            return {"status": "error", "message": "Insufficient candle data"}

        df = self.calculate_indicators(df)
        
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        signal = "HOLD"
        message = "No trigger conditions met."

        # 1. Moving Average Alignment Condition
        # Hierarchy top to bottom: 14_smooth > 14 > 50 > 200
        ma_alignment = (
            (curr['sma_14_smooth'] > curr['sma_14']) and
            (curr['sma_14'] > curr['sma_50']) and
            (curr['sma_50'] > curr['sma_200'])
        )

        # 2. Resistance Breakout Condition
        breakout = (curr['close'] > self.resistance_level) and (prev['close'] <= self.resistance_level)

        # --- ENTRY TRIGGER ---
        if not self.pos_active and breakout and ma_alignment:
            self.pos_active = True
            self.entry_price = float(curr['close'])
            # Initial Stop Loss set to recent pivot low
            self.current_stop_loss = float(self.get_pivot_low(df, window=10))
            signal = "BUY_ENTRY"
            message = f"BUY Triggered at {self.entry_price}. SL set at {self.current_stop_loss}"

        # --- POSITION MANAGEMENT & TRAILING STOP LOSS ---
        elif self.pos_active:
            # Check Trailing Stop Loss Hit
            if curr['low'] <= self.current_stop_loss:
                signal = "EXIT_SL_HIT"
                message = f"Trailing SL Hit! Exit price level: {self.current_stop_loss}"
                self.pos_active = False
                self.entry_price = 0.0
                self.current_stop_loss = 0.0
            else:
                # Update Trailing Stop Loss (Trail Upwards Only)
                new_support = float(self.get_pivot_low(df, window=10))
                if new_support > self.current_stop_loss:
                    self.current_stop_loss = new_support
                    message = f"Position Active. Trailing SL updated up to {self.current_stop_loss}"
                else:
                    message = f"Position Active. SL held at {self.current_stop_loss}"

        return {
            "signal": signal,
            "message": message,
            "position_active": self.pos_active,
            "entry_price": self.entry_price,
            "current_stop_loss": self.current_stop_loss,
            "latest_close": float(curr['close']),
            "ma_alignment_valid": bool(ma_alignment)
        }

# Global Engine Instance
engine = TradingEngine(resistance_level=50.0)

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "running", "service": "delta-algo-webhook"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        # Payload must contain 'candles': [{'open':.., 'high':.., 'low':.., 'close':..}, ...]
        if not payload or "candles" not in payload:
            return jsonify({"status": "error", "message": "Missing 'candles' key in JSON"}), 400
        
        result = engine.process_candle_data(payload["candles"])
        return jsonify(result), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
