import os
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from ta.momentum import rsi, stochrsi, williams_r
from ta.trend import ema_indicator, sma_indicator, adx, cci
from ta.volatility import average_true_range, bollinger_hband, bollinger_lband, keltner_channel_hband, keltner_channel_lband
from ta.volume import on_balance_volume
import datetime
import pytz
import csv

# ==== TIMEZONES ====
CENTRAL_TZ = pytz.timezone("US/Central")
UTC_TZ = datetime.timezone.utc

# ==== ENV ====
load_dotenv()
TOKEN = os.getenv("TOKEN")

# ==== STATE ====
KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD", "HBAR": "HBARUSD",
    "AVAX": "AVAXUSD", "LINK": "LINKUSD", "TON": "TONUSD", "PEPE": "PEPEUSD",
    "OP": "OPUSD", "INJ": "INJUSD"
}

active_alerts = {}
cooldowns = {}
leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

# ==== FLASK UPTIME PING ====
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

# ==== DISCORD SETUP ====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

CHANNEL_ID = 1395604673737789460
STATUS_CHANNEL_ID = 1397320600359272469

# -------------------------------------------------------------------
def now_times():
    utc_dt = datetime.datetime.now(UTC_TZ)
    central_dt = utc_dt.astimezone(CENTRAL_TZ)
    return utc_dt, central_dt

def fmt_central(dt):
    return dt.strftime("%Y-%m-%d %I:%M %p %Z")

def fmt_utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# -------------------------------------------------------------------
def calculate_indicators(df):
    df["ema50"] = ema_indicator(df["close"], window=50)
    df["ema200"] = ema_indicator(df["close"], window=200)
    df["rsi"] = rsi(df["close"], window=14)
    df["atr"] = average_true_range(df["high"], df["low"], df["close"], window=14)
    df["obv"] = on_balance_volume(df["close"], df["volume"])
    df["adx"] = adx(df["high"], df["low"], df["close"], window=14)
    df["cci"] = cci(df["high"], df["low"], df["close"], window=20)
    df["williams_r"] = williams_r(df["high"], df["low"], df["close"], lbp=14)
    df["bb_upper"] = bollinger_hband(df["close"], window=20)
    df["bb_lower"] = bollinger_lband(df["close"], window=20)
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_lower"]
    df["donchian_high"] = df["high"].rolling(window=20).max()
    df["donchian_low"] = df["low"].rolling(window=20).min()
    df["kc_upper"] = keltner_channel_hband(df["high"], df["low"], df["close"], window=20)
    df["kc_lower"] = keltner_channel_lband(df["high"], df["low"], df["close"], window=20)
    df["squeeze"] = (df["bb_lower"] > df["kc_lower"]) & (df["bb_upper"] < df["kc_upper"])

    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-9) * df["volume"]
    df["cmf"] = mfv.rolling(window=20).sum() / df["volume"].rolling(window=20).sum()

    return df

# -------------------------------------------------------------------
def detect_trade(df):
    latest = df.iloc[-1]
    if latest["adx"] < 20 or latest["close"] < latest["ema200"]:
        return None

    strategy = None
    emoji = ""
    confidence = 3

    if latest["rsi"] < 30 and latest["williams_r"] < -80:
        strategy = "Mean Reversion"
        emoji = "🔁"
        confidence = 4
    elif latest["close"] > latest["donchian_high"] and latest["cmf"] > 0:
        strategy = "Breakout Anticipation"
        emoji = "🚀"
        confidence = 5
    elif latest["squeeze"] and latest["bb_width"] > 0.05:
        strategy = "Volatility Squeeze"
        emoji = "📊"
        confidence = 3
    elif latest["cci"] > 100 and latest["cmf"] > 0:
        strategy = "Swing Trade"
        emoji = "🌀"
        confidence = 4
    elif latest["rsi"] > 50 and latest["close"] > latest["ema50"]:
        strategy = "Pullback Long"
        emoji = "📈"
        confidence = 3

    if strategy:
        return {
            "type": f"{emoji} {strategy}",
            "entry": latest["close"],
            "stop": latest["close"] - latest["atr"] * 1.5,
            "tp1": latest["close"] + latest["atr"] * 1.5,
            "tp2": latest["close"] + latest["atr"] * 2.5,
            "confidence": confidence
        }
    return None

# -------------------------------------------------------------------
def log_trade_to_csv(trade_data):
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"trade_log_{date_str}.csv"
    file_exists = os.path.isfile(filename)
    with open(filename, mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Time", "Symbol", "Type", "Entry", "Stop", "TP1", "TP2", "Confidence"])
        writer.writerow([
            trade_data["time"],
            trade_data["symbol"],
            trade_data["type"],
            f"{trade_data['entry']:.2f}",
            f"{trade_data['stop']:.2f}",
            f"{trade_data['tp1']:.2f}",
            f"{trade_data['tp2']:.2f}",
            trade_data["confidence"]
        ])

# -------------------------------------------------------------------
def format_embed(symbol, trade):
    header = fmt_central(now_times()[1])
    footer = fmt_utc(now_times()[0])
    emoji = "🟢" if "Long" in trade["type"] else "🔴"
    confidence_emoji = {6: "🔥", 5: "✅", 4: "🟢", 3: "⚪", 2: "🔻", 1: "🟥", 0: "❌"}.get(trade["confidence"], "❓")

    embed = discord.Embed(
        title=f"{emoji} {symbol} {trade['type']} – {header}",
        color=discord.Color.green() if "Long" in trade["type"] else discord.Color.red()
    )
    embed.add_field(
        name="📊 Trade Setup",
        value=f"📈 Entry: **${trade['entry']:.2f}**\n🛑 Stop:  `${trade['stop']:.2f}`",
        inline=False
    )
    embed.add_field(
        name="🎯 Targets",
        value=f"TP1: ${trade['tp1']:.2f}\nTP2: ${trade['tp2']:.2f}",
        inline=False
    )
    embed.add_field(
        name="🧠 Confidence",
        value=f"{confidence_emoji} {trade['confidence']}/6",
        inline=False
    )
    embed.set_footer(text=f"Generated {footer}")
    return embed

# -------------------------------------------------------------------
@tasks.loop(minutes=30)
async def eth_status_report():
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return
    df = calculate_indicators(df)
    latest = df.iloc[-1]

    header = fmt_central(now_times()[1])
    footer = fmt_utc(now_times()[0])
    trend_text = "📈 Bullish (EMA50)" if latest["close"] > latest["ema50"] else "📉 Bearish (EMA50)"
    supertrend_text = "🟢 Bullish" if latest["supertrend"] else "🔴 Bearish"
    alligator_text = "🟢 Bullish" if latest["alligator"] else "🔴 Bearish"
    ichimoku_text = "🟢 Bullish" if latest["ichimoku_bull"] else "🔴 Bearish"
    twist_text = "✅ Twist" if latest["twist"] else "No twist"
    bias = calculate_market_bias(latest)
    bias_emoji = bias.split()[0]

    embed = discord.Embed(
        title=f"📊 ETH 30-Min Status – {header} {bias_emoji}",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="💵 Price & Trend",
        value=(
            f"""💰 Price: **${latest['close']:.2f}**
📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`
{trend_text}"""
        ),
        inline=False
    )
    embed.add_field(
        name="📈 Indicator Summary",
        value=(
            f"""🧠 Supertrend: {supertrend_text}
🐊 Alligator: {alligator_text}
☁️ Ichimoku: {ichimoku_text}
🌪️ Twist Alert: {twist_text}"""
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Market Bias",
        value=bias,
        inline=False
    )
    embed.set_footer(text=f"Updated {footer}")
    await channel.send(embed=embed)

@bot.command()
async def ethreport(ctx):
    channel = ctx.channel
    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    header = fmt_central(now_times()[1])
    footer = fmt_utc(now_times()[0])
    trend_text = "📈 Bullish (EMA50)" if latest["close"] > latest["ema50"] else "📉 Bearish (EMA50)"
    supertrend_text = "🟢 Bullish" if latest["supertrend"] else "🔴 Bearish"
    alligator_text = "🟢 Bullish" if latest["alligator"] else "🔴 Bearish"
    ichimoku_text = "🟢 Bullish" if latest["ichimoku_bull"] else "🔴 Bearish"
    twist_text = "✅ Twist" if latest["twist"] else "No twist"
    bias = calculate_market_bias(latest)
    bias_emoji = bias.split()[0]

    embed = discord.Embed(
        title=f"📊 ETH 30-Min Status – {header} {bias_emoji}",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="💵 Price & Trend",
        value=(
            f"""💰 Price: **${latest['close']:.2f}**
📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`
{trend_text}"""
        ),
        inline=False
    )
    embed.add_field(
        name="📈 Indicator Summary",
        value=(
            f"""🧠 Supertrend: {supertrend_text}
🐊 Alligator: {alligator_text}
☁️ Ichimoku: {ichimoku_text}
🌪️ Twist Alert: {twist_text}"""
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Market Bias",
        value=bias,
        inline=False
    )
    embed.set_footer(text=f"Updated {footer}")
    await channel.send(embed=embed)

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
            name=symbol,
            value=f"✅ Wins: {wins} | 💥 Losses: {losses} | 📊 Win Rate: {win_rate}%",
            inline=False
        )
    embed.set_footer(text=f"Updated {fmt_utc(datetime.datetime.now(UTC_TZ))}")
    await ctx.send(embed=embed)

# -------------------------------------------------------------------
@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    now_utc, _ = now_times()

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue
        df = calculate_indicators(df)
        latest = df.iloc[-1]
        price = latest["close"]

        if symbol in active_alerts:
            entry, tp1, tp2, stop, open_time_utc = active_alerts[symbol]
            direction = "Long" if entry < stop else "Short"
            hit_tp2 = (price >= tp2) if direction == "Long" else (price <= tp2)
            hit_sl = (price <= stop) if direction == "Long" else (price >= stop)

            if hit_tp2 or hit_sl:
                result = "🎯 Take Profit 2 Hit!" if hit_tp2 else "💥 Stop Loss Hit!"
                if hit_tp2:
                    leaderboard_stats[symbol]["wins"] += 1
                else:
                    leaderboard_stats[symbol]["losses"] += 1
                embed = format_exit_embed(symbol, direction, entry, tp1, tp2, stop, price, result)
                if symbol == "ETH":
                    await channel.send(embed=embed)
                del active_alerts[symbol]
            continue

        last_time = cooldowns.get(symbol)
        if last_time and (now_utc - last_time).total_seconds() < 30 * 60:
            continue

        trade = detect_trade(df)
        if trade:
            log_trade_to_csv({
                "time": now_utc.isoformat(),
                "symbol": symbol,
                "type": trade["type"],
                "entry": trade["entry"],
                "stop": trade["stop"],
                "tp1": trade["tp1"],
                "tp2": trade["tp2"],
                "confidence": trade["confidence"]
            })
            if symbol == "ETH":
                await channel.send(embed=format_embed(symbol, trade))
            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"], trade["stop"], now_utc
            )
            cooldowns[symbol] = now_utc

# -------------------------------------------------------------------
# Bot Startup
# -------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    reset_leaderboard_daily.start()
    send_leaderboard_report.start()

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")


