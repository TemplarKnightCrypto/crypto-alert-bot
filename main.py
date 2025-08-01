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

# === Load Environment Variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")

# === Channel IDs ===
SCORECARD_CHANNEL_ID = 1399532442075005038  # 🏰・eth-battleground
TRADE_100X_CHANNEL_ID = 1399532925279666278  # ⚔️・battle-signals

# === Globals ===
last_100x_trade_time = None
last_scorecard_sent = None

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

# === Fetch OHLC Data ===
def fetch_ohlc(symbol="ETH", interval=1):
    kraken_map = {"ETH": "XETHZUSD"}
    pair = kraken_map.get(symbol.upper(), "XETHZUSD")
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": pair, "interval": interval}
    response = requests.get(url, params=params)
    raw = response.json()["result"][pair]
    df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df = df.astype({"time": int, "open": float, "high": float, "low": float, "close": float, "volume": float})
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("datetime", inplace=True)
    return df

# === Fetch Daily OHLC for Camarilla ===
def fetch_daily_ohlc():
    df = fetch_ohlc(interval=1440)
    latest = df.iloc[-2]
    return latest["high"], latest["low"], latest["close"]

# === Calculate Camarilla Levels ===
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

def calculate_camarilla_levels(high, low, close):
    return calculate_camarilla(high, low, close)

# === Apply Indicators ===
def calculate_indicators(df):
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
    df["vwap"] = (df["volume"] * (df["high"] + df["low"] + df["close"]) / 3).cumsum() / df["volume"].cumsum()
    df["macd"] = df["close"].ewm(span=12).mean() - df["close"].ewm(span=26).mean()
    df["macd_signal"] = df["macd"].ewm(span=9).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df.dropna()

# === Knight Assignment ===
def assign_knight(direction):
    return "Sir Leonis Ironhart" if direction == "Long" else "Sir Lucien Frostveil"

# === Trade Confidence Scoring ===
def score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend):
    direction_match = (price_trend and direction == "Long") or (not price_trend and direction == "Short")
    rsi_match = (rsi_trend == "up" and direction == "Long") or (rsi_trend == "down" and direction == "Short")
    rsi_score = (rsi > 55 if direction == "Long" else rsi < 45)
    proximity = abs(price - level_price) / level_price < 0.003
    volume_confirm = volume > avg_volume * 1.2
    return sum([direction_match, rsi_match, rsi_score, proximity, volume_confirm])

# === Scorecard Logic ===
def evaluate_scorecard(df, cam_levels):
    latest = df.iloc[-1]
    prev_10 = df.iloc[-11:-1]

    score = 0
    reasons = []

    price = latest['close']
    rsi_val = latest['rsi']
    macd_hist = latest['macd_hist']
    vwap = latest['vwap']
    volume = latest['volume']
    avg_volume = prev_10['volume'].mean()

    all_levels = [cam_levels[lvl] for lvl in ['L3', 'L4', 'L5', 'H3', 'H4', 'H5']]
    nearest = min(all_levels, key=lambda x: abs(price - x))
    near_level = abs(price - nearest) / price <= 0.0025
    if near_level:
        score += 1
        reasons.append("Price near Camarilla ✅")

    if (price <= nearest and rsi_val < 40) or (price >= nearest and rsi_val > 60):
        score += 1
        reasons.append("RSI aligned ✅")
    else:
        reasons.append("RSI neutral ❌")

    candle_body = abs(latest['close'] - latest['open'])
    candle_range = latest['high'] - latest['low']
    body_to_range = candle_body / candle_range if candle_range > 0 else 0
    if body_to_range > 0.5:
        score += 1
        reasons.append("Strong candle ✅")
    else:
        reasons.append("Weak candle ❌")

    if volume > avg_volume * 1.2:
        score += 1
        reasons.append("Volume spike ✅")
    else:
        reasons.append("Volume normal ❌")

    if (price > vwap and nearest > price) or (price < vwap and nearest < price):
        score += 1
        reasons.append("VWAP aligned ✅")
    else:
        reasons.append("VWAP misaligned ❌")

    if (price > nearest and macd_hist > 0) or (price < nearest and macd_hist < 0):
        score += 1
        reasons.append("MACD histogram ✅")
    else:
        reasons.append("MACD mismatch ❌")

    return score, reasons, nearest

# === 100x Trade Detection ===
def detect_100x_trade(df, cam):
    global last_100x_trade_time
    latest = df.iloc[-1]
    prev_10 = df.iloc[-11:-1]

    score, reasons, level = evaluate_scorecard(df, cam)
    now = datetime.datetime.utcnow()

    if score < 5:
        return None

    price = latest['close']
    volume = latest['volume']
    avg_volume = prev_10['volume'].mean()
    body = abs(latest['close'] - latest['open'])
    range_ = latest['high'] - latest['low']
    body_ratio = body / range_ if range_ > 0 else 0

    breakout_confirmed = (
        (price > level and latest['open'] < level) or
        (price < level and latest['open'] > level)
    ) and body_ratio > 0.5 and volume > avg_volume * 1.2

    if not breakout_confirmed or (last_100x_trade_time and (now - last_100x_trade_time).total_seconds() < 900):
        return None

    last_100x_trade_time = now

    direction = "Long" if price > level else "Short"
    entry = price
    sl = entry * (0.99 if direction == "Long" else 1.01)
    tp1 = entry * (1.015 if direction == "Long" else 0.985)
    tp2 = entry * (1.03 if direction == "Long" else 0.97)

    return {
        "direction": direction, "entry": entry, "tp1": tp1, "tp2": tp2, "sl": sl,
        "score": score, "reasons": reasons, "level": level, "timestamp": now
    }

# === Camarilla Trade Alert ===
@tasks.loop(minutes=1)
async def scan_trade_alerts():
    df = fetch_ohlc("ETH", interval=1)
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

    high, low, close = fetch_daily_ohlc()
    levels = calculate_camarilla_levels(high, low, close)

    df5 = fetch_ohlc("ETH", interval=5)
    confirm = df5.iloc[-1]
    body = abs(confirm["close"] - confirm["open"])
    wick = confirm["high"] - confirm["low"]
    strong_body = body / wick > 0.5 if wick else False
    volume_valid = confirm["volume"] > df5["volume"].iloc[-5:].mean() * 1.2

    for name, lvl in levels.items():
        is_upper = "H" in name
        broken = (confirm["close"] > lvl and confirm["open"] < lvl) if is_upper else (confirm["close"] < lvl and confirm["open"] > lvl)
        if not broken or not strong_body or not volume_valid:
            continue

        direction = "Long" if is_upper else "Short"
        confidence = score_trade(rsi, rsi_trend, direction, price, lvl, volume, avg_volume, price_trend)
        if confidence < 3:
            continue

        entry = round(price, 2)
        risk = entry * 0.01
        stop = round(entry - risk, 2) if direction == "Long" else round(entry + risk, 2)
        tp1 = round(entry + risk * 1.5, 2) if direction == "Long" else round(entry - risk * 1.5, 2)
        tp2 = round(entry + risk * 3.0, 2) if direction == "Long" else round(entry - risk * 3.0, 2)

        knight = assign_knight(direction)
        emoji = "🟩" if direction == "Long" else "🟥"
        label = "🟢 80%+ – Strong Move" if confidence >= 5 else "🟠 75% – Likely Move" if confidence == 4 else "🟡 60% – Possible Move"

        embed = discord.Embed(
            title=f"{emoji} ETH {direction} at {name} (${lvl:.2f})",
            color=discord.Color.green() if direction == "Long" else discord.Color.red(),
            timestamp=datetime.datetime.now(UTC)
        )
        embed.add_field(name="🛡 Knight", value=knight, inline=True)
        embed.add_field(name="🎯 Direction", value=direction, inline=True)
        embed.add_field(name="📊 Confidence", value=label, inline=True)
        embed.add_field(name="📟 Score", value=f"{confidence}/6", inline=True)
        embed.add_field(name="🎯 Entry", value=f"${entry}", inline=True)
        embed.add_field(name="🎯 TP1 | TP2", value=f"${tp1} | ${tp2}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${stop}", inline=True)

        sorted_levels = dict(sorted(levels.items(), key=lambda x: x[1], reverse=True))
        map_str = "".join(f"{k:<3} {v:.2f} {'➡️' if abs(price - v) < 0.5 else ''}\n" for k, v in sorted_levels.items())
        embed.add_field(name="📍 Support/Resistance Map", value=f"```{map_str}```", inline=False)
        embed.set_footer(text=f"🕒 UTC: {embed.timestamp.strftime('%Y-%m-%d %H:%M:%S')} | CT: {embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')}")

        channel = bot.get_channel(SCORECARD_CHANNEL_ID)
        await channel.send(embed=embed)

# === Scorecard Task ===
@tasks.loop(minutes=5)
async def scorecard_check():
    global last_scorecard_sent
    df = fetch_ohlc("ETH", interval=5)
    if df is None or len(df) < 20:
        return
    df = calculate_indicators(df)
    high, low, close = fetch_daily_ohlc()
    cam = calculate_camarilla_levels(high, low, close)
    score, reasons, level = evaluate_scorecard(df, cam)

    now = datetime.datetime.utcnow()
    if last_scorecard_sent and (now - last_scorecard_sent).seconds < 300:
        return
    last_scorecard_sent = now

    embed = discord.Embed(
        title=f"📊 ETH Camarilla Scorecard",
        description=f"🧠 Confluence Score: **{score} / 6**\n🎯 Level in focus: `${level:,.2f}`\n\n" + "\n".join(reasons),
        color=0x7289da
    )
    embed.set_footer(text=f"UTC Time: {now.strftime('%H:%M')}")
    embed.add_field(name="⚠️ Note", value="This is a scorecard only. Not a trade signal.", inline=False)

    channel = bot.get_channel(SCORECARD_CHANNEL_ID)
    await channel.send(embed=embed)

# === 100x Trade Alert ===
@tasks.loop(minutes=1)
async def trade_100x_scan():
    df = fetch_ohlc("ETH", interval=5)
    if df is None or len(df) < 20:
        return
    df = calculate_indicators(df)
    high, low, close = fetch_daily_ohlc()
    cam = calculate_camarilla_levels(high, low, close)
    result = detect_100x_trade(df, cam)
    if not result:
        return

    embed = discord.Embed(
        title=f"⚔️ 100x Trade Alert – ETH {result['direction']}",
        description=(
            f"📍 **Entry:** `${result['entry']:.2f}`\n"
            f"🎯 **TP1:** `${result['tp1']:.2f}`\n"
            f"🎯 **TP2:** `${result['tp2']:.2f}`\n"
            f"🛡️ **SL:** `${result['sl']:.2f}`\n\n"
            f"📊 **Confluence Score:** {result['score']} / 6\n" + "\n".join(result['reasons'])
        ),
        color=0xD32F2F if result['direction'] == "Short" else 0x2E7D32
    )
    embed.set_footer(text=f"5-min | UTC: {result['timestamp'].strftime('%H:%M')} | High-Leverage Trade")

    channel = bot.get_channel(TRADE_100X_CHANNEL_ID)
    await channel.send(embed=embed)

# === On Ready ===
@bot.event
async def on_ready():
    print(f"🟢 Logged in as {bot.user}")
    scan_trade_alerts.start()
    scorecard_check.start()
    trade_100x_scan.start()

# === Start Flask + Discord Bot ===
if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)

