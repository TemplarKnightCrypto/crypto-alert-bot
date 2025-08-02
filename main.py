# === ETH Camarilla Trade Alert Bot with Warning System ===

import os
import discord
import asyncio
import requests
import datetime
import pytz
import pandas as pd
import numpy as np
from flask import Flask
import threading
from discord.ext import commands, tasks
from dotenv import load_dotenv
from ta.momentum import RSIIndicator

# === Load Environment Variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")

# === Channel IDs ===
SCORECARD_CHANNEL_ID = 1399532442075005038  # 🏠･eth-battleground
EAGLE_SIGNAL_CHANNEL_ID = 1398690647417819198  # 🦅･eagle-signal
TRADE_100X_CHANNEL_ID = 1399532925279666278  # ⚔️･battle-signals

HEARTBEAT_CHANNEL_IDS = [
    1399067396488302623,  # 📜･scrolls-of-the-order
    1399532102571135118,  # 🥰･knights’-watch
    1398691425347961016,  # ✂️･scribe’s-keep
    SCORECARD_CHANNEL_ID,
    EAGLE_SIGNAL_CHANNEL_ID,
    TRADE_100X_CHANNEL_ID
]

# === Globals ===
last_100x_trade_time = None
last_scorecard_sent = None
camarilla_warning_cooldowns = {}
last_trade_alert_time = {}

# === Timezones ===
UTC = pytz.utc
CENTRAL_TZ = pytz.timezone("US/Central")

# === Flask Web Server for Render ===
app = Flask(__name__)
@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"
def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Discord Bot ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Placeholder Functions ===
def score_trade(rsi, rsi_trend, direction, price, lvl, volume, avg_volume, price_trend):
    score = 0
    if direction == "Long" and rsi > 50: score += 1
    if direction == "Short" and rsi < 50: score += 1
    if rsi_trend == "up" and direction == "Long": score += 1
    if rsi_trend == "down" and direction == "Short": score += 1
    if volume > avg_volume: score += 1
    if price_trend: score += 1
    return score

def assign_knight(direction):
    return "Sir Leonis" if direction == "Long" else "Sir Lucien"

def evaluate_scorecard(df, cam):
    score = 0
    reasons = []
    latest = df.iloc[-1]
    price = latest["close"]
    rsi = latest["rsi"]
    if rsi > 60: score += 1; reasons.append("✅ RSI > 60")
    elif rsi < 40: score += 1; reasons.append("✅ RSI < 40")
    if latest["macd_hist"] > 0: score += 1; reasons.append("✅ MACD Bullish")
    if price > latest["vwap"]: score += 1; reasons.append("✅ Above VWAP")
    level = cam["H3"] if price > cam["Pivot"] else cam["L3"]
    return score, reasons, level

@tasks.loop(minutes=1)
async def heartbeat():
    now = datetime.datetime.now(datetime.timezone.utc)
    ct_time = now.astimezone(CENTRAL_TZ).strftime("%I:%M %p")
    utc_time = now.strftime("%H:%M UTC")
    message = f"🛡️ The Watchtower stands vigilant. Last check-in at **{utc_time} / {ct_time} CT**."
    for channel_id in HEARTBEAT_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(message)

@tasks.loop(minutes=5)
async def scorecard_check():
    global last_scorecard_sent
    now = datetime.datetime.utcnow()
    if last_scorecard_sent and (now - last_scorecard_sent).seconds < 300:
        return
    last_scorecard_sent = now
    # Placeholder scorecard content
    embed = discord.Embed(title="ETH Camarilla Scorecard", color=0x3498db)
    embed.set_footer(text=f"🕒 UTC: {now.strftime('%Y-%m-%d %H:%M:%S')} | CT: {now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')}")
    channel = bot.get_channel(SCORECARD_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

@tasks.loop(minutes=1)
async def check_camarilla_warning():
    global camarilla_warning_cooldowns
    df = fetch_ohlc("ETH", interval=1)
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    price = latest["close"]
    rsi = latest["rsi"]
    high, low, close = fetch_daily_ohlc()
    levels = calculate_camarilla(high, low, close)
    now = datetime.datetime.utcnow()

    for name, lvl in levels.items():
        if name == "Pivot": continue
        if abs(price - lvl) > 0.5: continue
        if name in camarilla_warning_cooldowns:
            last_alert = camarilla_warning_cooldowns[name]
            if (now - last_alert).total_seconds() < 600:
                continue

        is_upper = "H" in name
        rsi_trend = "up" if df["rsi"].iloc[-1] > df["rsi"].iloc[-3] else "down"
        price_trend = price > df["close"].iloc[-3] if is_upper else price < df["close"].iloc[-3]
        volume_trend = latest["volume"] > df["volume"].iloc[-5:].mean()

        if price_trend and rsi_trend == ("up" if is_upper else "down") and volume_trend:
            verdict = "🔴 Likely Break"
        elif not price_trend and rsi_trend != ("up" if is_upper else "down"):
            verdict = "🟢 Likely Reversal"
        else:
            verdict = "⚪ Unclear / 50/50"

        embed = discord.Embed(title=f"🔹 ETH Warning near {name}", color=0xffa500)
        embed.add_field(name="📈 Current Price", value=f"${price:.2f}")
        embed.add_field(name="🔹 Camarilla Level", value=f"{name} = ${lvl:.2f}")
        embed.add_field(name="🕵️ Outlook", value=verdict)
        embed.set_footer(text=f"🕒 UTC: {now.strftime('%Y-%m-%d %H:%M:%S')} | CT: {now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')}")

        channel = bot.get_channel(EAGLE_SIGNAL_CHANNEL_ID)
        await channel.send(embed=embed)
        camarilla_warning_cooldowns[name] = now

# (Other functions like fetch_ohlc, fetch_daily_ohlc, calculate_camarilla, calculate_indicators, scan_trade_alerts, trade_100x_scan, and on_ready remain defined as they are in your full script.)

@bot.event
async def on_ready():
    print(f"🟢 Logged in as {bot.user}")
    heartbeat.start()
    check_camarilla_warning.start()
    scorecard_check.start()
    scan_trade_alerts.start()
    trade_100x_scan.start()

if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)


