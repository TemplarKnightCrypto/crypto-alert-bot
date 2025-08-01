# === Templar Control Tower: ETH Camarilla Alert Bot ===

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
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

# === Timezones ===
UTC = pytz.utc
CENTRAL = pytz.timezone("US/Central")

# === Flask Web Server for Render ===
app = Flask(__name__)

@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Initialize Discord Bot ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Pine Script-style Camarilla Calculation ===
def calculate_camarilla(high, low, close):
    D4 = 0.55
    D3 = 0.275
    P = (high + low + close) / 3
    H5 = (high / low) * close
    H4 = ((high - low) * D4) + close
    H3 = ((high - low) * D3) + close
    L3 = close - ((high - low) * D3)
    L4 = close - ((high - low) * D4)
    L5 = close - (H5 - close)
    return {
        "H5": H5, "H4": H4, "H3": H3,
        "L3": L3, "L4": L4, "L5": L5,
        "Pivot": P
    }

# === Fetch OHLC from Kraken ===
def fetch_ohlc():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XETHZUSD", "interval": 1}
    raw = requests.get(url, params=params).json()["result"]["XETHZUSD"]
    df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df = df.astype({"time": int, "open": float, "high": float, "low": float, "close": float, "volume": float})
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("datetime", inplace=True)
    return df

# === Fetch Daily Candle ===
def fetch_daily_ohlc():
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XETHZUSD", "interval": 1440}
    raw = requests.get(url, params=params).json()["result"]["XETHZUSD"]
    day = raw[-2]  # yesterday’s candle
    return float(day[2]), float(day[3]), float(day[4])  # high, low, close

# === Build Support/Resistance Map ===
def build_s_r_map(levels, price):
    rows = [
        f"L5  {levels['L5']:.2f}",
        f"L4  {levels['L4']:.2f}",
        f"L3  {levels['L3']:.2f}",
        f"P   {levels['Pivot']:.2f}",
        f"H3  {levels['H3']:.2f}",
        f"{'➡️  Price':<6} {price:.2f}",
        f"H4  {levels['H4']:.2f}",
        f"H5  {levels['H5']:.2f}",
    ]
    return "```\n" + "\n".join(rows) + "\n```"

# === Build Alert Embed ===
def build_warning_embed(level_name, level_price, price, bias, score, probability, outcome, levels):
    direction_emoji = "⬆️" if price < level_price else "⬇️"
    utc_time = datetime.datetime.now(UTC)
    central_time = utc_time.astimezone(CENTRAL)
    footer = f"🕒 UTC: {utc_time.strftime('%Y-%m-%d %H:%M:%S')} | CT: {central_time.strftime('%I:%M %p')}"

    embed = discord.Embed(
        title=f"⚠️ ETH Approaching {level_name} (${level_price:.2f})",
        description=(
            f"🎯 **Bias:** `{bias}`\n"
            f"📊 **Confidence:** {probability}\n"
            f"🧮 **Score:** `{score}/6`\n"
            f"{outcome}"
        ),
        color=discord.Color.orange(),
        timestamp=utc_time,
    )

    embed.add_field(name=f"{direction_emoji} Current Price", value=f"`$ {price:.2f}`", inline=True)
    embed.add_field(name="📍 Support/Resistance Map", value=build_s_r_map(levels, price), inline=False)
    embed.set_footer(text=f"Camarilla Alert • {footer}")
    return embed

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

def determine_outcome(agreement):
    if agreement >= 3: return "🔴 Likely Break"
    elif agreement <= 1: return "🟢 Likely Reversal"
    else: return "⚪ Unclear / 50/50"

# === Cooldown Tracker ===
last_alert_time = {}

# === Main Camarilla Alert Scanner ===
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

    daily_high, daily_low, daily_close = fetch_daily_ohlc()
    levels = calculate_camarilla(daily_high, daily_low, daily_close)
    closest_level = min(levels.items(), key=lambda x: abs(x[1] - price))
    level_name, level_price = closest_level

    now = datetime.datetime.now(UTC)
    if level_name in last_alert_time and (now - last_alert_time[level_name]).total_seconds() < 600:
        return  # 10-minute cooldown

    direction_match = (price_trend and "H" in level_name) or (not price_trend and "L" in level_name)
    rsi_match = (rsi_trend == "up" and "H" in level_name) or (rsi_trend == "down" and "L" in level_name)
    candle_match = price > df["open"].iloc[-1] if "H" in level_name else price < df["open"].iloc[-1]
    volume_match = volume > avg_volume
    agreement = sum([direction_match, rsi_match, candle_match, volume_match])

    # Override Filters
    proximity = abs(price - level_price) / level_price
    if 45 <= rsi <= 55 or volume < avg_volume * 0.9 or proximity > 0.005:
        return

    outcome = determine_outcome(agreement)
    score, prob_label = score_probability(price, level_price, rsi, rsi_trend, direction_match, volume, avg_volume)
    bias = "Break" if agreement >= 3 else "Reversal"

    embed = build_warning_embed(level_name, level_price, price, bias, score, prob_label, outcome, levels)
    channel = bot.get_channel(CHANNEL_ID)
    await channel.send(embed=embed)
    last_alert_time[level_name] = now

# === Command: Show Daily Levels ===
@bot.command(name="levels")
async def show_camarilla_levels(ctx):
    high, low, close = fetch_daily_ohlc()
    levels = calculate_camarilla(high, low, close)
    embed = discord.Embed(
        title="📊 ETH Camarilla Levels (Daily)",
        description=f"Based on yesterday’s candle\nHigh: **${high:.2f}**, Low: **${low:.2f}**, Close: **${close:.2f}**",
        color=discord.Color.blue(),
        timestamp=datetime.datetime.now(UTC)
    )
    embed.add_field(name="📈 Resistance", value=f"**H5:** ${levels['H5']:.2f}\n**H4:** ${levels['H4']:.2f}\n**H3:** ${levels['H3']:.2f}", inline=True)
    embed.add_field(name="📉 Support", value=f"**L3:** ${levels['L3']:.2f}\n**L4:** ${levels['L4']:.2f}\n**L5:** ${levels['L5']:.2f}", inline=True)
    embed.add_field(name="🎯 Pivot", value=f"**Pivot:** ${levels['Pivot']:.2f}", inline=False)
    embed.set_footer(text="Camarilla Levels • UTC " + embed.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
    await ctx.send(embed=embed)

# === On Ready ===
@bot.event
async def on_ready():
    print(f"🟢 Logged in as {bot.user}")
    scan_camarilla_levels.start()

# === Start Flask + Bot ===
if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)
