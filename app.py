import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Pure Python Technical Calculations ---

def calculate_sma(data_list, period):
    """Calculates Simple Moving Average for a given period."""
    if len(data_list) < period:
        # Return average of available data if less than period
        return sum(data_list) / len(data_list)
    return sum(data_list[-period:]) / period

def get_pivot_low(candles, window=10):
    """Returns the lowest price among the last 'window' candles."""
    recent = candles[-window:] if len(candles) >= window else candles
    return min(c['low'] for c in recent)

# --- Algorithmic Trading Engine ---

class TradingEngine:
    def __init__(self, resistance_level=50.0):
        self.resistance_level = resistance_level
        self.pos_active = False
        self.entry_price = 0.0
        self.current_stop_loss = 0.0

    def process_candles(self, candles):
        """
        Expects a list of dictionaries: [{'open': 10, 'high': 12, 'low': 9, 'close': 11}, ...]
        """
        if len(candles) < 2:
            return {"status": "error", "message": "Minimum 2 candles required"}

        closes = [c['close'] for c in candles]

        # Calculate Indicators
        sma_200 = calculate_sma(closes, 200)
        sma_50 = calculate_sma(closes, 50)
        sma_14 = calculate_sma(closes, 14)
        
        # Double Smoothed 14 MA (MA of 14 MA series)
        sma_14_series = []
        for i in range(1, len(closes) + 1):
            sub_closes = closes[:i]
            sma_14_series.append(calculate_sma(sub_closes, 14))
        
        sma_14_smooth = calculate_sma(sma_14_series, 14)

        curr_close = closes[-1]
        prev_close = closes[-2]
        curr_low = candles[-1]['low']

        # 1. Moving Average Alignment Check (14_smooth > 14 > 50 > 200)
        ma_alignment = (sma_14_smooth > sma_14) and (sma_14 > sma_50) and (sma_50 > sma_200)

        # 2. Resistance Breakout Check
        breakout = (curr_close > self.resistance_level) and (prev_close <= self.resistance_level)

        signal = "HOLD"
        message = "No trigger conditions met."

        # --- ENTRY TRIGGER ---
        if not self.pos_active and breakout and ma_alignment:
            self.pos_active = True
            self.entry_price = float(curr_close)
            self.current_stop_loss = float(get_pivot_low(candles, window=10))
            signal = "BUY_ENTRY"
            message = f"BUY Triggered at {self.entry_price}. SL set at {self.current_stop_loss}"

        # --- POSITION MANAGEMENT & TRAILING STOP LOSS ---
        elif self.pos_active:
            if curr_low <= self.current_stop_loss:
                signal = "EXIT_SL_HIT"
                message = f"Trailing SL Hit! Exit triggered at level: {self.current_stop_loss}"
                self.pos_active = False
                self.entry_price = 0.0
                self.current_stop_loss = 0.0
            else:
                new_support = float(get_pivot_low(candles, window=10))
                if new_support > self.current_stop_loss:
                    self.current_stop_loss = new_support
                    message = f"Position Active. Trailing SL updated to {self.current_stop_loss}"
                else:
                    message = f"Position Active. SL held at {self.current_stop_loss}"

        return {
            "signal": signal,
            "message": message,
            "position_active": self.pos_active,
            "entry_price": self.entry_price,
            "current_stop_loss": self.current_stop_loss,
            "indicators": {
                "sma_200": round(sma_200, 2),
                "sma_50": round(sma_50, 2),
                "sma_14": round(sma_14, 2),
                "sma_14_smooth": round(sma_14_smooth, 2)
            }
        }

# Engine Instance
engine = TradingEngine(resistance_level=50.0)

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "running", "engine": "zero-dependency-python"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data or "candles" not in data:
            return jsonify({"status": "error", "message": "JSON must include 'candles' array"}), 400
        
        result = engine.process_candles(data["candles"])
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
