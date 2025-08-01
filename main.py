# ============================================
# ETH Camarilla Trade Alert Bot – Discord + Kraken
# ============================================

import os
import asyncio
import discord
import requests
import pandas as pd
from datetime import datetime
from discord.ext import tasks, commands
from dotenv import load_dotenv

# === Load Environment Variables ===
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# === Discord Bot Setup ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Camarilla Calculation ===
def calculate_hl_levels(df):
    high = df['high'].iloc[-2]
    low = df['low'].iloc[-2]
    close = df['close'].iloc[-2]

    range_ = high - low
    levels = {
        "H5": close + 1.1 * range_ * 1.1 / 2,
        "H4": close + 1.1 * range_ / 2,
        "H3": close + 1.1 * range_ / 4,
        "H2": close + 1.1 * range_ / 6,
        "H1": close + 1.1 * range_ / 12,
        "Pivot": (high + low + close) / 3,
        "L1": close - 1.1 * range_ / 12,
        "L2": close - 1.1 * range_ / 6,
        "L3": close - 1.1 * range_ / 4,
        "L4": close - 1.1 * range_ / 2,
        "L5": close - 1.1 * range_ * 1.1 / 2,
    }
    return levels

# === Kraken OHLC Fetch ===
def fetch_ohlc():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "ETHUSD", "interval": 5}
    r = requests.get(url, params=params)
    ohlc = r.json()["result"]
    pair = next(k for k in ohlc if k != "last")
    data = ohlc[pair]
    df = pd.DataFrame(data, columns=[
        'time', 'open', 'high', 'low', 'close', 'vwap', 'volume', 'count'
    ])
    df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
    return df

# === Detect Trade Signal ===
def detect_trade(df, levels):
    close = df['close'].iloc[-1]
    prev_close = df['close'].iloc[-2]
    signal = None
    level_hit = None

    for name in ['H4', 'H5', 'L4', 'L5']:
        level = float(levels[name])
        if prev_close < level < close:
            signal = f"🚀 Breakout Long above {name}"
            level_hit = name
        elif prev_close > level > close:
            signal = f"🩸 Breakdown Short below {name}"
            level_hit = name

    if not signal:
        for name in ['H4', 'H5', 'L4', 'L5']:
            level = float(levels[name])
            if abs(close - level) < 0.0015 * close:
                signal = f"↩️ Bounce from {name}"
                level_hit = name

    if signal:
        return {
            "signal": signal,
            "price": close,
            "level": level_hit
        }
    return None

# === Scheduled Scan Task ===
@tasks.loop(minutes=1.0)
async def scheduled_scan():
    channel = bot.get_channel(CHANNEL_ID)
    df = fetch_ohlc()
    levels = calculate_hl_levels(df)
    trade = detect_trade(df, levels)

    if trade:
        embed = discord.Embed(title=trade['signal'], color=0x00ff00)
        embed.add_field(name="Price", value=f"${trade['price']:.2f}", inline=True)
        embed.add_field(name="Level", value=trade['level'], inline=True)
        embed.set_footer(text=datetime.utcnow().strftime("UTC %Y-%m-%d %H:%M:%S"))
        await channel.send(embed=embed)

    # Warnings (Approaching Levels)
    warnings = []
    close = float(df['close'].iloc[-1])

    for name, level in levels.items():
        if name not in ['H4', 'H5', 'L4', 'L5', 'Pivot']:
            continue
        level_val = float(level)
        diff = abs(close - level_val)
        threshold = 0.002 * close
        if diff < threshold:
            bias = "Reversal" if (name in ['H4', 'H5'] and close < level_val) or (name in ['L4', 'L5'] and close > level_val) else "Break"
            emoji = "🔼" if level_val > close else "🔽"
            warnings.append(f"⚠️ Approaching {name} ({level_val:.2f}): {emoji} | Bias: {bias} | Score: 3")

    for warn in warnings:
        await channel.send(warn)

# === Bot Ready Event ===
@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    scheduled_scan.start()

# === Flask Keepalive Server ===
from flask import Flask
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running."

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Start Threads ===
import threading
threading.Thread(target=run_flask).start()

# === Start Bot ===
if TOKEN:
    bot.run(TOKEN)
else:
    print("Missing DISCORD_TOKEN in environment.")

