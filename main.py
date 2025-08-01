# === Templar Control Tower: ETH Camarilla Alert Bot ===

import os
import discord
import asyncio
import requests
import datetime
import pytz
import pandas as pd
import numpy as np
from discord.ext import commands, tasks
from dotenv import load_dotenv
from ta.momentum import RSIIndicator

# === Load environment variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

# === Timezones ===
UTC = pytz.utc

# === Initialize Bot ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Camarilla Calculation ===
def calculate_camarilla(high, low, close):
    range_ = high - low
    return {
        "H5": close + (range_ * 1.5000),
        "H4": close + (range_ * 1.2500),
        "H3": close + (range_ * 1.1666),
        "H2": close + (range_ * 1.0833),
        "H1": close + (range_ * 1.0000),
        "L1": close - (range_ * 1.0000),
        "L2": close - (range_ * 1.0833),
        "L3": close - (range_ * 1.1666),
        "L4": close - (range_ * 1.2500),
        "L5": close - (range_ * 1.5000),
        "Pivot": (high + low + close) / 3,
    }

# === Fetch Kraken ETH OHLC ===
def fetch_ohlc():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XETHZUSD", "interval": 1}
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

# === Scoring Logic ===
def score_probability(price, level_price, rsi, rsi_trend, price_trend, volume, avg_volume):
    proximity = abs(price - level_price) / level_price
    proximity_score = 3 if proximity < 0.001 else 2 if proximity < 0.002 else 1
    volume_spike = 1 if volume > avg_volume * 1.1 else 0
    rsi_momentum = 1 if (rsi > 55 or rsi < 45) else 0
    trend_score = 1 if price_trend else 0
    score = proximity_score + volume_spike + rsi_momentum + trend_score
    if score >= 6: return score, "🔴 80%+ – Imminent Move"
    elif score == 5: return score, "🟠 75% – Likely Move"
    elif score == 4: return score, "🟡 60–70% – Possible Move"
    else: return score, "⚪ 40–60% – Neutral Watch"

# === Bias Outcome Logic ===
def determine_outcome(agreement):
    if agreement >= 3:
        return "🔴 Likely Break"
    elif agreement <= 1:
        return "🟢 Likely Reversal"
    else:
        return "⚪ Unclear / 50/50"

# === Embed Builder ===
def build_warning_embed(level_name, level_price, price, bias, score, probability, outcome):
    embed = discord.Embed(
        title=f"⚠️ Approaching {level_name} (${level_price:.2f})",
        description=f"**Bias:** {bias} | **{probability}** | **Score:** {score}",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.now(UTC),
    )
    embed.add_field(name="🎯 Outcome", value=outcome)
    embed.set_footer(text="UTC " + embed.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    return embed

# === Main Camarilla Scanner ===
@tasks.loop(minutes=1)
async def scan_camarilla_levels():
    df = fetch_ohlc()
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
    df = df.dropna()
    latest = df.iloc[-1]
    recent = df[-5:]

    price = latest["close"]
    rsi = latest["rsi"]
    rsi_trend = "up" if df["rsi"].iloc[-1] > df["rsi"].iloc[-3] else "down"
    price_trend = df["close"].iloc[-1] > df["close"].iloc[-3]
    avg_volume = recent["volume"].mean()
    volume = latest["volume"]

    levels = calculate_camarilla(df["high"].max(), df["low"].min(), df["close"].iloc[-1])
    closest_level = min(levels.items(), key=lambda x: abs(x[1] - price))
    level_name, level_price = closest_level

    # Bias checks
    is_upper = "H" in level_name
    direction_match = (price_trend and is_upper) or (not price_trend and "L" in level_name)
    rsi_match = (rsi_trend == "up" and is_upper) or (rsi_trend == "down" and "L" in level_name)
    candle_match = price > df["open"].iloc[-1] if is_upper else price < df["open"].iloc[-1]
    volume_match = volume > avg_volume
    agreement = sum([direction_match, rsi_match, candle_match, volume_match])

    outcome = determine_outcome(agreement)
    score, prob_label = score_probability(price, level_price, rsi, rsi_trend, direction_match, volume, avg_volume)
    bias = "Break" if agreement >= 3 else "Reversal"

    # Send embed
    channel = bot.get_channel(CHANNEL_ID)
    embed = build_warning_embed(level_name, level_price, price, bias, score, prob_label, outcome)
    await channel.send(embed=embed)

# === Bot Ready ===
@bot.event
async def on_ready():
    print(f"🟢 Logged in as {bot.user}")
    scan_camarilla_levels.start()

bot.run(TOKEN)
