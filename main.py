
# ============================================
# Camarilla Trade Alert Bot – Render Deployment
# Scans ETHUSD from Kraken every 1 min
# Detects breakouts, reversals, warning alerts
# Sends results to Discord
# ============================================

import os
import threading
import datetime
import pytz
import discord
import pandas as pd
import numpy as np
import requests
from flask import Flask
from dotenv import load_dotenv
from discord.ext import commands, tasks

# === Load environment variables ===
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 1399532925279666278))  # Replace with real channel ID

# === Timezones ===
CENTRAL_TZ = pytz.timezone("US/Central")
UTC_TZ = pytz.utc

# === Discord bot ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Flask App for Render Uptime ===
app = Flask(__name__)

@app.route('/')
def index():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask).start()

# === Kraken OHLC Fetch ===
def fetch_ohlc_from_kraken(pair="ETHUSD", interval=5, candles=50):
    url = f"https://api.kraken.com/0/public/OHLC"
    response = requests.get(url, params={"pair": pair, "interval": interval})
    if response.status_code != 200:
        print("Kraken API error")
        return None
    data = response.json()["result"]
    pair_key = list(data.keys())[0]
    raw = data[pair_key][-candles:]
    df = pd.DataFrame(raw, columns=[
        'time', 'open', 'high', 'low', 'close',
        'vwap', 'volume', 'count'
    ])
    df = df.astype({
        'open': float,
        'high': float,
        'low': float,
        'close': float,
        'volume': float
    })
    return df

def calculate_camarilla_levels(df):
    high = df['high'].iloc[-2]
    low = df['low'].iloc[-2]
    close = df['close'].iloc[-2]
    range_ = high - low
    return {
        'R4': close + (range_ * 1.1 / 2),
        'R3': close + (range_ * 1.1 / 4),
        'R2': close + (range_ * 1.1 / 6),
        'R1': close + (range_ * 1.1 / 12),
        'S1': close - (range_ * 1.1 / 12),
        'S2': close - (range_ * 1.1 / 6),
        'S3': close - (range_ * 1.1 / 4),
        'S4': close - (range_ * 1.1 / 2),
        'Pivot': (high + low + close) / 3
    }

def detect_trade_signal(df):
    levels = calculate_camarilla_levels(df)
    close = df['close'].iloc[-1]
    open_ = df['open'].iloc[-1]
    high = df['high'].iloc[-2]
    low = df['low'].iloc[-2]
    volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].rolling(20).mean().iloc[-1]
    atr = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    rsi = df['close'].rolling(14).apply(lambda x: 100 - (100 / (1 + (x.pct_change().mean() / x.pct_change().std()))), raw=False).iloc[-1]

    signal = None
    entry = close
    tp1 = tp2 = stop = level_price = None
    level_name = None

    for i, level in enumerate(['R1', 'R2', 'R3', 'R4']):
        if close > levels[level] and rsi > 55 and volume > 1.2 * avg_volume:
            signal = f"🟢 Breakout Long: Broke above {level} ({round(levels[level], 2)})"
            tp1 = levels.get(['R2', 'R3', 'R4', 'R4'][i], entry + atr.iloc[-1])
            tp2 = entry + 2 * atr.iloc[-1]
            stop = levels.get(['S1', 'S2', 'S3', 'S4'][i], entry - atr.iloc[-1])
            level_name = level
            level_price = levels[level]
            break

    for i, level in enumerate(['S1', 'S2', 'S3', 'S4']):
        if close < levels[level] and rsi < 45 and volume > 1.2 * avg_volume:
            signal = f"🔴 Breakout Short: Broke below {level} ({round(levels[level], 2)})"
            tp1 = levels.get(['S2', 'S3', 'S4', 'S4'][i], entry - atr.iloc[-1])
            tp2 = entry - 2 * atr.iloc[-1]
            stop = levels.get(['R1', 'R2', 'R3', 'R4'][i], entry + atr.iloc[-1])
            level_name = level
            level_price = levels[level]
            break

    if signal:
        return {
            "signal": signal,
            "entry": round(entry, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "stop": round(stop, 2),
            "level": level_name,
            "level_price": round(level_price, 2)
        }
    return None

def generate_warning_alerts(df):
    levels = calculate_camarilla_levels(df)
    close = df['close'].iloc[-1]
    volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].rolling(20).mean().iloc[-1]
    trend = df['close'].diff().tail(5)
    rsi = df['close'].rolling(14).apply(lambda x: 100 - (100 / (1 + (x.pct_change().mean() / x.pct_change().std()))), raw=False).iloc[-1]

    alerts = []
    for name, level in levels.items():
        if name not in ['R3', 'R4', 'S3', 'S4', 'Pivot']:
            continue
        direction = "⬆️" if close < level else "⬇️"
        distance = abs(close - level)
        if distance > 0.002 * close:
            continue

        score = 2
        if volume > 1.1 * avg_volume:
            score += 1
        if (direction == "⬆️" and rsi > 55) or (direction == "⬇️" and rsi < 45):
            score += 1
        if (direction == "⬆️" and trend.mean() > 0) or (direction == "⬇️" and trend.mean() < 0):
            score += 1

        bias = "Unclear"
        if direction == "⬆️" and rsi > 60:
            bias = "Break"
        elif direction == "⬆️" and rsi < 50:
            bias = "Reversal"
        elif direction == "⬇️" and rsi < 40:
            bias = "Break"
        elif direction == "⬇️" and rsi > 50:
            bias = "Reversal"

        alerts.append(f"⚠️ Approaching {name} ({round(level, 2)}): {direction} | Bias: {bias} | Score: {score}")
    return alerts

@tasks.loop(minutes=1)
async def scheduled_scan():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    df = fetch_ohlc_from_kraken("ETHUSD", interval=5)

    if df is None:
        await channel.send("⚠️ Failed to fetch Kraken data.")
        return

    trade = detect_trade_signal(df)
    warnings = generate_warning_alerts(df)

    if trade:
        embed = discord.Embed(title=trade['signal'], color=0x00ff00 if "Long" in trade['signal'] else 0xff0000)
        embed.add_field(name="Entry", value=f"${trade['entry']}", inline=True)
        embed.add_field(name="TP1", value=f"${trade['tp1']}", inline=True)
        embed.add_field(name="TP2", value=f"${trade['tp2']}", inline=True)
        embed.add_field(name="Stop", value=f"${trade['stop']}", inline=True)
        embed.add_field(name="Level", value=f"{trade['level']} ({trade['level_price']})", inline=False)
        embed.set_footer(text="Templar Knight Crypto – Camarilla Strategy")
        await channel.send(embed=embed)

    for warn in warnings:
        await channel.send(warn)

@bot.event
async def on_ready():
    print(f"✅ Bot connected as {bot.user.name}")
    scheduled_scan.start()

bot.run(TOKEN)
