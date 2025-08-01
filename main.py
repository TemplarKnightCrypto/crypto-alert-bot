# === Templar Control Tower: ETH Camarilla Alert Bot ===

import os
import requests
import pandas as pd
import numpy as np
import datetime
import pytz
import discord
import asyncio
from discord.ext import commands, tasks
from flask import Flask
from dotenv import load_dotenv
from ta.momentum import rsi

# === Load .env ===
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 0))

# === Init Bot ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)
CENTRAL_TZ = pytz.timezone("US/Central")

# === Camarilla Calculation ===
def calculate_camarilla_levels(df):
    high = df['high'].iloc[-2]
    low = df['low'].iloc[-2]
    close = df['close'].iloc[-2]
    diff = high - low

    levels = {
        'H5': close + 1.1 * diff * 1.168,
        'H4': close + 1.1 * diff * 0.55,
        'H3': close + 1.1 * diff * 0.275,
        'H2': close + 1.1 * diff * 0.183,
        'H1': close + 1.1 * diff * 0.0916,
        'L1': close - 1.1 * diff * 0.0916,
        'L2': close - 1.1 * diff * 0.183,
        'L3': close - 1.1 * diff * 0.275,
        'L4': close - 1.1 * diff * 0.55,
        'L5': close - 1.1 * diff * 1.168,
        'Pivot': (high + low + close) / 3
    }
    return levels

# === Kraken Data Fetch ===
def fetch_ohlc(symbol="ETHUSD", interval=1):
    url = f'https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}'
    data = requests.get(url).json()
    key = list(data['result'].keys())[0]
    df = pd.DataFrame(data['result'][key], columns=[
        'time', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'count'
    ])
    df = df.astype({'close': 'float', 'high': 'float', 'low': 'float', 'volume': 'float'})
    return df

# === Probability Scoring ===
def get_probability_label(score):
    if score >= 6:
        return "🔴 85% – Imminent Move"
    elif score == 5:
        return "🟠 75% – Likely Move"
    elif score == 4:
        return "🟡 65% – Possible Move"
    elif score in [2, 3]:
        return "⚪ 50% – Neutral Watch"
    else:
        return "⚪ 30% – Low Probability"

def calculate_score(df, level_name, level_val, close):
    score = 0
    diff = abs(close - level_val)
    pct = diff / close

    # Proximity
    if pct < 0.0015: score += 3
    elif pct < 0.003: score += 2
    elif pct < 0.005: score += 1

    # Volume Spike
    vol = df['volume'].iloc[-1]
    avg_vol = df['volume'].iloc[-6:-1].mean()
    if vol > 1.2 * avg_vol:
        score += 1

    # RSI
    rsi_val = rsi(df['close']).iloc[-1]
    if level_name.startswith("H") and rsi_val > 55:
        score += 1
    if level_name.startswith("L") and rsi_val < 45:
        score += 1

    # Trend Direction
    trend = df['close'].iloc[-1] - df['close'].iloc[-4]
    if (level_val > close and trend > 0) or (level_val < close and trend < 0):
        score += 1

    return score

# === Bias Classifier ===
def classify_bias(df, level_name, level_val, close):
    agree = 0

    # Price moving toward level
    trend = df['close'].iloc[-1] - df['close'].iloc[-4]
    toward = (level_val > close and trend > 0) or (level_val < close and trend < 0)
    if toward: agree += 1

    # RSI trend
    rsi_series = rsi(df['close'])
    if level_name.startswith("H") and rsi_series.iloc[-1] > rsi_series.iloc[-4]: agree += 1
    if level_name.startswith("L") and rsi_series.iloc[-1] < rsi_series.iloc[-4]: agree += 1

    # Candle pattern
    candles = df['close'].iloc[-3:]
    if level_val > close and all(candles.diff().dropna() > 0): agree += 1
    if level_val < close and all(candles.diff().dropna() < 0): agree += 1

    # Volume trend
    vol = df['volume'].iloc[-1]
    avg_vol = df['volume'].iloc[-6:-1].mean()
    if vol > avg_vol: agree += 1

    # Classification
    if agree >= 3: return "🔴 Likely Break"
    elif agree <= 1: return "🟢 Likely Reversal"
    else: return "⚪ Unclear / 50/50"

# === Warning Cooldown ===
last_warnings = {}

# === Scheduler Task ===
@tasks.loop(minutes=1)
async def scan_price():
    df = fetch_ohlc()
    levels = calculate_camarilla_levels(df)
    close = df['close'].iloc[-1]
    channel = bot.get_channel(CHANNEL_ID)
    now = datetime.datetime.now(datetime.timezone.utc)

    for name in ['H5', 'H4', 'H3', 'L3', 'L4', 'L5', 'Pivot']:
        level_val = levels[name]
        score = calculate_score(df, name, level_val, close)
        if score < 2:
            continue  # skip weak warnings

        probability = get_probability_label(score)
        classification = classify_bias(df, name, level_val, close)
        bias = "Break" if "Break" in classification else "Reversal"
        emoji = "🔼" if level_val > close else "🔽"

        key = f"{name}-{bias}"
        if key in last_warnings and (now - last_warnings[key]).total_seconds() < 180:
            continue  # cooldown 3 min

        msg = (
            f"⚠️ Approaching **{name}** (${level_val:.2f})\n"
            f"{emoji} Bias: {bias} | {probability} | Score: {score}\n"
            f"🎯 Outcome: {classification}"
        )
        await channel.send(msg)
        last_warnings[key] = now

# === Flask App for Render Keepalive ===
@bot.event
async def on_ready():
    print(f"[{datetime.datetime.now()}] Logged in as {bot.user}")
    scan_price.start()

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Entry Point ===
if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)

