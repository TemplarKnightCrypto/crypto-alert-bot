import os
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from ta.trend import ema_indicator, sma_indicator
from ta.momentum import rsi
from ta.volatility import average_true_range
from ta.volume import on_balance_volume
import datetime
import pytz

CENTRAL_TZ = pytz.timezone("US/Central")
load_dotenv()
TOKEN = os.getenv("TOKEN")

active_alerts = {}     # {symbol: (entry, tp1, tp2, stop, open_time)}
cooldowns = {}         # {symbol: last_alert_time}

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD",
    "HBAR": "HBARUSD", "AVAX": "AVAXUSD"
}

leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

CHANNEL_ID = 1395604673737789460
STATUS_CHANNEL_ID = 1397320600359272469

def fetch_ohlc(symbol, interval=30):
    try:
        pair = KRAKEN_PAIRS.get(symbol)
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        raw = response.json()
        if 'error' in raw and raw['error']:
            print(f"[ERROR] Kraken API error for {symbol}: {raw['error']}")
            return None
        result = raw.get('result', {})
        pair_key = next((k for k in result if k != 'last'), None)
        df = pd.DataFrame(result[pair_key], columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        print(f"[ERROR] fetch_ohlc failed: {e}")
        return None

def calculate_indicators(df):
    df["ema50"] = ema_indicator(df["close"], window=50)
    df["rsi"] = rsi(df["close"], window=14)
    df["atr"] = average_true_range(df["high"], df["low"], df["close"], window=14)
    df["obv"] = on_balance_volume(df["close"], df["volume"])

    atr = df["atr"]
    hl2 = (df["high"] + df["low"]) / 2
    factor = 3.0
    upperband = hl2 + (factor * atr)
    lowerband = hl2 - (factor * atr)
    direction = [True] * len(df)
    for i in range(1, len(df)):
        if df["close"].iloc[i] > upperband.iloc[i - 1]:
            direction[i] = True
        elif df["close"].iloc[i] < lowerband.iloc[i - 1]:
            direction[i] = False
        else:
            direction[i] = direction[i - 1]
    df["supertrend"] = direction

    df["jaw"] = sma_indicator(df["close"], window=13).shift(8)
    df["teeth"] = sma_indicator(df["close"], window=8).shift(5)
    df["lips"] = sma_indicator(df["close"], window=5).shift(3)
    df["alligator"] = (df["lips"] > df["teeth"]) & (df["teeth"] > df["jaw"])

    nine_high = df["high"].rolling(window=9).max()
    nine_low = df["low"].rolling(window=9).min()
    df["tenkan"] = (nine_high + nine_low) / 2
    period26_high = df["high"].rolling(window=26).max()
    period26_low = df["low"].rolling(window=26).min()
    df["kijun"] = (period26_high + period26_low) / 2
    df["senkou_a"] = ((df["tenkan"] + df["kijun"]) / 2).shift(26)
    period52_high = df["high"].rolling(window=52).max()
    period52_low = df["low"].rolling(window=52).min()
    df["senkou_b"] = ((period52_high + period52_low) / 2).shift(26)

    df["ichimoku_bull"] = (df["close"] > df["senkou_a"]) & (df["close"] > df["senkou_b"])
    df["twist"] = (df["senkou_a"].shift(1) < df["senkou_b"].shift(1)) & (df["senkou_a"] > df["senkou_b"])
    return df

def detect_trade(df):
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    previous = df.iloc[-2]

    confidence = 0
    if latest["close"] > latest["ema50"]:
        confidence += 1
    if latest["rsi"] > 50:
        confidence += 1
    if latest["obv"] > previous["obv"]:
        confidence += 1
    if latest["supertrend"]:
        confidence += 1
    if latest["alligator"]:
        confidence += 1
    if latest["ichimoku_bull"]:
        confidence += 1

    if confidence >= 4:
        return {
            "type": "Breakout Long",
            "entry": latest["close"],
            "stop": latest["close"] - latest["atr"],
            "tp1": latest["close"] + latest["atr"] * 1.5,
            "tp2": latest["close"] + latest["atr"] * 2.5,
            "confidence": confidence
        }
    elif confidence <= 2:
        return {
            "type": "Breakdown Short",
            "entry": latest["close"],
            "stop": latest["close"] + latest["atr"],
            "tp1": latest["close"] - latest["atr"] * 1.5,
            "tp2": latest["close"] - latest["atr"] * 2.5,
            "confidence": 6 - confidence
        }
    return None

def format_embed(symbol, trade):
    direction_emoji = "🟢" if "Long" in trade["type"] else "🔴"
    embed = discord.Embed(
        title=f"{direction_emoji} {symbol} {trade['type']} Alert",
        color=discord.Color.green() if "Long" in trade["type"] else discord.Color.red()
    )
    embed.add_field(name="📈 Entry", value=f"${trade['entry']:.2f}", inline=True)
    embed.add_field(name="🛑 Stop", value=f"${trade['stop']:.2f}", inline=True)
    embed.add_field(name="🎯 TP1", value=f"${trade['tp1']:.2f}", inline=True)
    embed.add_field(name="🎯 TP2", value=f"${trade['tp2']:.2f}", inline=True)
    emoji = {6: "🔥", 5: "✅", 4: "🟢", 3: "⚪", 2: "🔻", 1: "🟥", 0: "❌"}.get(trade["confidence"], "❓")
    embed.add_field(name="🧠 Confidence", value=f"{emoji} {trade['confidence']}/6", inline=True)
    embed.set_footer(text=f"Generated {datetime.datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d %I:%M %p CST')}")
    return embed

@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    now = datetime.datetime.now(datetime.timezone.utc)

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        df = calculate_indicators(df)
        latest = df.iloc[-1]
        price = latest["close"]

        if symbol in active_alerts:
            entry, tp1, tp2, stop, open_time = active_alerts[symbol]
            direction = "Long" if entry < stop else "Short"
            hit_tp = price >= tp1 or price >= tp2 if direction == "Long" else price <= tp1 or price <= tp2
            hit_sl = price <= stop if direction == "Long" else price >= stop
            if hit_tp:
                leaderboard_stats[symbol]["wins"] += 1
                result = "🎯 Take Profit Hit!"
                color = discord.Color.green()
            elif hit_sl:
                leaderboard_stats[symbol]["losses"] += 1
                result = "💥 Stop Loss Hit!"
                color = discord.Color.red()
            else:
                continue

            embed = discord.Embed(title=f"{symbol} {direction} Exit Alert", description=result, color=color)
            embed.add_field(name="📈 Entry", value=f"${entry:.2f}", inline=True)
            embed.add_field(name="🎯 TP1", value=f"${tp1:.2f}", inline=True)
            embed.add_field(name="🛑 Stop", value=f"${stop:.2f}", inline=True)
            embed.add_field(name="📉 Price", value=f"${price:.2f}", inline=True)
            embed.set_footer(text=f"Closed at {datetime.datetime.now(CENTRAL_TZ).strftime('%I:%M %p CST')}")
            await channel.send(embed=embed)
            del active_alerts[symbol]
            continue

        # ⏱ Cooldown check
        cooldown_minutes = 30
        last_time = cooldowns.get(symbol)
        if last_time and (now - last_time).total_seconds() < cooldown_minutes * 60:
            continue

        trade = detect_trade(df)
        if trade:
            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"], trade["stop"], now
            )
            cooldowns[symbol] = now
            await channel.send(embed=format_embed(symbol, trade))

@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    reset_leaderboard_daily.start()

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")


