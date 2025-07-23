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

# Active trades: {symbol: (entry, tp1, tp2, stop, open_time)}
active_alerts = {}

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD",
    "HBAR": "HBARUSD", "AVAX": "AVAXUSD"
}

# Leaderboard stats: {symbol: {"wins": 0, "losses": 0}}
leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

CHANNEL_ID = 1395604673737789460  # Trade alerts
STATUS_CHANNEL_ID = 1397320600359272469  # ETH 30-min report

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

    # Supertrend
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

    # Alligator
    df["jaw"] = sma_indicator(df["close"], window=13).shift(8)
    df["teeth"] = sma_indicator(df["close"], window=8).shift(5)
    df["lips"] = sma_indicator(df["close"], window=5).shift(3)
    df["alligator"] = (df["lips"] > df["teeth"]) & (df["teeth"] > df["jaw"])

    # Ichimoku
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
    df["ichimoku_bull"] = df["close"] > df["senkou_a"] and df["close"] > df["senkou_b"]
    df["twist"] = (df["senkou_a"].shift(1) < df["senkou_b"].shift(1)) & (df["senkou_a"] > df["senkou_b"])
    return df

def detect_trade(df):
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    previous = df.iloc[-2]

    is_bull = (
        latest["supertrend"]
        and latest["alligator"]
        and latest["ichimoku_bull"]
    )
    is_bear = not is_bull

    # Confidence Scoring
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

    score = trade['confidence']
    emoji = {6: "🔥", 5: "✅", 4: "🟢", 3: "⚪", 2: "🔻", 1: "🟥", 0: "❌"}.get(score, "❓")
    embed.add_field(name="🧠 Confidence", value=f"{emoji} {score}/6", inline=True)

    embed.set_footer(text=f"Generated {datetime.datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d %I:%M %p CST')}")
    return embed

async def send_eth_status_report(channel):
    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    rsi_state = "🟢 Overbought" if latest["rsi"] > 70 else "🔴 Oversold" if latest["rsi"] < 30 else "⚪ Neutral"
    trend = "📈 Bullish" if latest["close"] > latest["ema50"] else "📉 Bearish"
    embed = discord.Embed(title="📊 ETH 30-Minute Status Report", color=discord.Color.blue())
    embed.add_field(name="💵 Price", value=f"${latest['close']:.2f}", inline=True)
    embed.add_field(name="📉 RSI", value=f"{latest['rsi']:.2f} ({rsi_state})", inline=True)
    embed.add_field(name="📏 ATR", value=f"{latest['atr']:.2f}", inline=True)
    embed.add_field(name="📈 Trend", value=trend, inline=True)
    embed.add_field(name="🧠 Supertrend", value="🟢 Bullish" if latest["supertrend"] else "🔴 Bearish", inline=True)
    embed.add_field(name="🐊 Alligator", value="🟢 Bullish" if latest["alligator"] else "🔴 Bearish", inline=True)
    embed.add_field(name="☁️ Ichimoku", value="🟢 Bullish" if latest["ichimoku_bull"] else "🔴 Bearish", inline=True)
    embed.add_field(name="🌪️ Twist Alert", value="✅ Twist" if latest["twist"] else "No twist", inline=True)
    embed.set_footer(text=f"Updated {datetime.datetime.now(CENTRAL_TZ).strftime('%Y-%m-%d %I:%M %p CST')}")
    await channel.send(embed=embed)

@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    now = datetime.datetime.utcnow()

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        df = calculate_indicators(df)
        latest = df.iloc[-1]
        price = latest["close"]

        # Check active alert
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
                # still active
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

        # No active alert: check for new trade
        trade = detect_trade(df)
        if trade:
            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"], trade["stop"], now
            )
            await channel.send(embed=format_embed(symbol, trade))


@tasks.loop(minutes=30)
async def eth_status_report():
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    await send_eth_status_report(channel)

@tasks.loop(minutes=1)
async def reset_leaderboard_daily():
    now = datetime.datetime.utcnow()
    if now.hour == 0 and now.minute == 0:
        channel = bot.get_channel(CHANNEL_ID)

        # Find top performer
        top_symbol = None
        top_wins = -1
        top_winrate = 0
        for symbol, stats in leaderboard_stats.items():
            wins = stats["wins"]
            losses = stats["losses"]
            total = wins + losses
            winrate = round((wins / total) * 100) if total > 0 else 0

            if wins > top_wins or (wins == top_wins and winrate > top_winrate):
                top_symbol = symbol
                top_wins = wins
                top_winrate = winrate
                top_losses = losses

        if top_symbol and top_wins > 0:
            await channel.send(
                f"🏅 Top performer of the day: **{top_symbol}** with {top_wins} wins and {top_losses} loss(es) (📊 {top_winrate}% win rate)"
            )
        else:
            await channel.send("📊 No trades were completed today.")

        for symbol in leaderboard_stats:
            leaderboard_stats[symbol] = {"wins": 0, "losses": 0}
        await channel.send("🔄 Daily leaderboard has been reset (UTC 00:00).")

@bot.command()
async def confidence(ctx, symbol: str):
    symbol = symbol.upper()
    if symbol not in KRAKEN_PAIRS:
        await ctx.send("❌ Invalid symbol.")
        return
    df = fetch_ohlc(symbol)
    if df is None:
        await ctx.send(f"❌ Failed to fetch {symbol}")
        return
    trade = detect_trade(df)
    if trade:
        await ctx.send(embed=format_embed(symbol, trade))
    else:
        await ctx.send(f"ℹ️ No signal for {symbol}")

@bot.command()
async def ethreport(ctx):
    await send_eth_status_report(ctx.channel)

@bot.command()
async def testfetch(ctx, symbol: str = "ETH"):
    df = fetch_ohlc(symbol.upper())
    if df is not None:
        await ctx.send(f"✅ Successfully fetched {symbol.upper()} data.")
    else:
        await ctx.send(f"❌ Failed to fetch {symbol.upper()} data.")

@bot.command()
async def leaderboard(ctx):
    embed = discord.Embed(title="🏆 Trade Alert Leaderboard", color=discord.Color.gold())
    sorted_stats = sorted(leaderboard_stats.items(), key=lambda x: x[1]["wins"], reverse=True)

    for symbol, stats in sorted_stats:
        wins = stats["wins"]
        losses = stats["losses"]
        total = wins + losses if (wins + losses) > 0 else 1
        win_rate = round((wins / total) * 100)
        embed.add_field(
            name=f"{symbol}",
            value=f"✅ Wins: {wins} | 💥 Losses: {losses} | 📊 Win Rate: {win_rate}%",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    reset_leaderboard_daily.start()  # ✅ Start the reset task

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")

