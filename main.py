# ============================================
# Camarilla Trade Alert Bot – Using H1–H5 / L1–L5 Labels
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
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 1234567890))

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
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000)).start()

# === Kraken OHLC Fetch ===
def fetch_ohlc_from_kraken(pair="ETHUSD", interval=5, candles=50):
    url = "https://api.kraken.com/0/public/OHLC"
    response = requests.get(url, params={"pair": pair, "interval": interval})
    if response.status_code != 200:
        return None
    data = response.json()["result"]
    pair_key = next(key for key in data if key != "last")
    raw = data[pair_key][-candles:]
    df = pd.DataFrame(raw, columns=[
        'time', 'open', 'high', 'low', 'close',
        'vwap', 'volume', 'count'
    ])
    df = df.astype({
        'open': float, 'high': float, 'low': float,
        'close': float, 'volume': float
    })
    return df

# === Camarilla H/L Level Calculation ===
def calculate_hl_levels(df):
    high = df['high'].iloc[-2]
    low = df['low'].iloc[-2]
    close = df['close'].iloc[-2]
    range_ = high - low
    return {
        'H5': close + (range_ * 1.1 / 2),
        'H4': close + (range_ * 1.1 / 4),
        'H3': close + (range_ * 1.1 / 6),
        'H2': close + (range_ * 1.1 / 12),
        'Pivot': (high + low + close) / 3,
        'L2': close - (range_ * 1.1 / 12),
        'L3': close - (range_ * 1.1 / 6),
        'L4': close - (range_ * 1.1 / 4),
        'L5': close - (range_ * 1.1 / 2),
    }

# === Trade Signal Detection ===
def detect_trade_signal(df):
    levels = calculate_hl_levels(df)
    close = df['close'].iloc[-1]
    open_ = df['open'].iloc[-1]
    volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].rolling(20).mean().iloc[-1]
    atr = df['high'].rolling(14).max() - df['low'].rolling(14).min()
    rsi = df['close'].rolling(14).apply(lambda x: 100 - (100 / (1 + (x.pct_change().mean() / x.pct_change().std()))), raw=False).iloc[-1]

    signal = None
    entry = close
    tp1 = tp2 = stop = level_price = None
    level_name = None

    for i, level in enumerate(['H2', 'H3', 'H4', 'H5']):
        if close > levels[level] and rsi > 55 and volume > 1.2 * avg_volume:
            signal = f"🟢 Breakout Long: Broke above {level} ({round(levels[level], 2)})"
            tp1 = levels.get(['H3', 'H4', 'H5', 'H5'][i], entry + atr.iloc[-1])
            tp2 = entry + 2 * atr.iloc[-1]
            stop = levels.get(['L2', 'L3', 'L4', 'L5'][i], entry - atr.iloc[-1])
            level_name = level
            level_price = levels[level]
            break

    for i, level in enumerate(['L2', 'L3', 'L4', 'L5']):
        if close < levels[level] and rsi < 45 and volume > 1.2 * avg_volume:
            signal = f"🔴 Breakout Short: Broke below {level} ({round(levels[level], 2)})"
            tp1 = levels.get(['L3', 'L4', 'L5', 'L5'][i], entry - atr.iloc[-1])
            tp2 = entry - 2 * atr.iloc[-1]
            stop = levels.get(['H2', 'H3', 'H4', 'H5'][i], entry + atr.iloc[-1])
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

# === Warning Alerts (Proximity to Levels) ===
def generate_warning_alerts(df):
    levels = calculate_hl_levels(df)
    close = df['close'].iloc[-1]
    volume = df['volume'].iloc[-1]
    avg_volume = df['volume'].rolling(20).mean().iloc[-1]
    trend = df['close'].diff().tail(5)
    rsi = df['close'].rolling(14).apply(lambda x: 100 - (100 / (1 + (x.pct_change().mean() / x.pct_change().std()))), raw=False).iloc[-1]

    alerts = []
    for name, level in levels.items():
        if name not in ['H4', 'H5', 'L4', 'L5', 'Pivot']:
            continue
        direction = "⬆️" if close < level else "⬇️"
        if abs(close - level) > 0.002 * close:
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

# === Scheduled Scan Every 1 Minute ===
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
        embed.set_footer(text="Templar Knight Crypto – H/L Camarilla Strategy")
        await channel.send(embed=embed)

    for warn in warnings:
        await channel.send(warn)

# === Bot Startup ===
@bot.event
async def on_ready():
    print(f"✅ Bot connected as {bot.user.name}")
    scheduled_scan.start()

# === Run the Bot ===
bot.run(TOKEN)
