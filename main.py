# === ETH Camarilla Trade Alert Bot ===

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

# === Load environment variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
BATTLE_SIGNALS_CHANNEL_ID = int(os.getenv("BATTLE_SIGNALS_CHANNEL_ID"))

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

# === Fetch Kraken OHLC ===
def fetch_ohlc(interval=1):
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XETHZUSD", "interval": interval}
    response = requests.get(url, params=params)
    raw = response.json()["result"]["XETHZUSD"]
    df = pd.DataFrame(raw, columns=[
        "time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    df = df.astype({
        "time": int, "open": float, "high": float, "low": float,
        "close": float, "volume": float
    })
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("datetime", inplace=True)
    return df

# === Fetch Daily OHLC for Camarilla ===
def fetch_daily_ohlc():
    df = fetch_ohlc(interval=1440)
    latest = df.iloc[-2]  # Use previous full day
    return latest["high"], latest["low"], latest["close"]

# === Camarilla Levels ===
def calculate_camarilla(high, low, close):
    D4 = 0.55
    D3 = 0.275
    H5 = (high / low) * close
    H4 = ((high - low) * D4) + close
    H3 = ((high - low) * D3) + close
    L3 = close - ((high - low) * D3)
    L4 = close - ((high - low) * D4)
    L5 = close - (H5 - close)
    P = (high + low + close) / 3
    return {"H5": H5, "H4": H4, "H3": H3, "L3": L3, "L4": L4, "L5": L5, "Pivot": P}

# === Determine Knight ===
def assign_knight(direction):
    return "Sir Leonis Ironhart" if direction == "Long" else "Sir Lucien Frostveil"

# === Confidence Score ===
def score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend):
    direction_match = (price_trend and direction == "Long") or (not price_trend and direction == "Short")
    rsi_match = (rsi_trend == "up" and direction == "Long") or (rsi_trend == "down" and direction == "Short")
    rsi_score = (rsi > 55 if direction == "Long" else rsi < 45)
    proximity = abs(price - level_price) / level_price < 0.003
    volume_confirm = volume > avg_volume * 1.2
    return sum([direction_match, rsi_match, rsi_score, proximity, volume_confirm])

# === Trade Alert Scanner with 5m Confirmation ===
@tasks.loop(minutes=1)
async def scan_trade_alerts():
    df = fetch_ohlc(interval=1)
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
    df = df.dropna()
    latest = df.iloc[-1]
    recent = df[-5:]

    price = latest["close"]
    rsi = latest["rsi"]
    volume = latest["volume"]
    avg_volume = recent["volume"].mean()
    price_trend = df["close"].iloc[-1] > df["close"].iloc[-3]
    rsi_trend = "up" if df["rsi"].iloc[-1] > df["rsi"].iloc[-3] else "down"

    # === Daily Camarilla Levels ===
    daily_high, daily_low, daily_close = fetch_daily_ohlc()
    levels = calculate_camarilla(daily_high, daily_low, daily_close)

    # === Fetch 5m Candle for Confirmation ===
    df5 = fetch_ohlc(interval=5)
    confirm = df5.iloc[-1]
    confirm_open = confirm["open"]
    confirm_close = confirm["close"]
    confirm_high = confirm["high"]
    confirm_low = confirm["low"]
    confirm_volume = confirm["volume"]
    confirm_body = abs(confirm_close - confirm_open)
    confirm_wick = confirm_high - confirm_low
    body_ratio = confirm_body / confirm_wick if confirm_wick else 0
    strong_body = body_ratio > 0.5
    volume_valid = confirm_volume > df5["volume"].iloc[-5:].mean() * 1.2

    for level_name, level_price in levels.items():
        is_upper = "H" in level_name
        broken = False
        direction = None

        if is_upper:
            if confirm_close > level_price and confirm_open < level_price:
                broken = True
                direction = "Long"
        else:
            if confirm_close < level_price and confirm_open > level_price:
                broken = True
                direction = "Short"

        if not broken or not strong_body or not volume_valid:
            continue

        confidence = score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend)
        if confidence < 3:
            continue

        entry = round(price, 2)
        risk = entry * 0.01
        if direction == "Long":
            stop = round(entry - risk, 2)
            tp1 = round(entry + risk * 1.5, 2)
            tp2 = round(entry + risk * 3.0, 2)
        else:
            stop = round(entry + risk, 2)
            tp1 = round(entry - risk * 1.5, 2)
            tp2 = round(entry - risk * 3.0, 2)

        knight = assign_knight(direction)
        emoji = "🟩" if direction == "Long" else "🟥"
        conf_label = "🟢 80%+ – Strong Move" if confidence >= 5 else "🟠 75% – Likely Move" if confidence == 4 else "🟡 60% – Possible Move"

        embed = discord.Embed(
            title=f"{emoji} ETH {direction} at {level_name} (${level_price:.2f})",
            color=discord.Color.green() if direction == "Long" else discord.Color.red(),
            timestamp=datetime.datetime.now(UTC)
        )
        embed.add_field(name="🛡 Knight", value=knight, inline=True)
        embed.add_field(name="🎯 Direction", value=direction, inline=True)
        embed.add_field(name="📊 Confidence", value=f"{conf_label}", inline=True)
        embed.add_field(name="📟 Score", value=f"{confidence}/6", inline=True)
        embed.add_field(name="🎯 Entry", value=f"${entry}", inline=True)
        embed.add_field(name="🎯 TP1 | TP2", value=f"${tp1} | ${tp2}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${stop}", inline=True)

        levels_sorted = dict(sorted(levels.items(), key=lambda x: x[1], reverse=True))
        map_str = ""
        for name, lvl in levels_sorted.items():
            marker = "➡️" if abs(price - lvl) < 0.5 else ""
            map_str += f"{name:<3} {lvl:.2f} {marker}\n"
        embed.add_field(name="📍 Support/Resistance Map", value=f"```{map_str}```", inline=False)
        embed.set_footer(text=f"🕒 UTC: {embed.timestamp.strftime('%Y-%m-%d %H:%M:%S')} | CT: {embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')}")

        channel = bot.get_channel(BATTLE_SIGNALS_CHANNEL_ID)
        await channel.send(embed=embed)

# === On Ready ===
@bot.event
async def on_ready():
    print(f"🟢 Logged in as {bot.user}")
    scan_trade_alerts.start()

# === Start Bot and Flask ===
if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)
