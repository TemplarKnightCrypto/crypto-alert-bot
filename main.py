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

# ==== TIMEZONES ====
CENTRAL_TZ = pytz.timezone("US/Central")  # auto DST
UTC_TZ = datetime.timezone.utc

# ==== ENV ====
load_dotenv()
TOKEN = os.getenv("TOKEN")

# ==== STATE ====
active_alerts = {}     # {symbol: (entry, tp1, tp2, stop, open_time_utc)}
cooldowns = {}         # {symbol: last_alert_time_utc}

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD",
    "HBAR": "HBARUSD", "AVAX": "AVAXUSD"
}

leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

# ==== FLASK UPTIME PING ====
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

# ==== DISCORD BOT ====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Channel IDs
CHANNEL_ID = 1395604673737789460            # Trade alerts
STATUS_CHANNEL_ID = 1397320600359272469     # ETH 30-min report

# -----------------------------------------------------------------------------
# Time Utilities
# -----------------------------------------------------------------------------
def now_times():
    """Return (utc_dt, central_dt) timezone-aware datetimes."""
    utc_dt = datetime.datetime.now(UTC_TZ)
    central_dt = utc_dt.astimezone(CENTRAL_TZ)
    return utc_dt, central_dt

def fmt_central(dt):
    return dt.strftime("%Y-%m-%d %I:%M %p %Z")  # e.g., 2025-07-23 03:00 PM CDT

def fmt_utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")   # 24h format

# -----------------------------------------------------------------------------
# Data Fetch
# -----------------------------------------------------------------------------
def fetch_ohlc(symbol, interval=30):
    try:
        pair = KRAKEN_PAIRS.get(symbol)
        if not pair:
            print(f"[ERROR] Unknown symbol {symbol}")
            return None
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        raw = response.json()
        if 'error' in raw and raw['error']:
            print(f"[ERROR] Kraken API error for {symbol}: {raw['error']}")
            return None
        result = raw.get('result', {})
        pair_key = next((k for k in result if k != 'last'), None)
        if pair_key is None:
            print(f"[ERROR] No OHLC key for {symbol}")
            return None
        df = pd.DataFrame(result[pair_key], columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)  # keep UTC-aware
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        print(f"[ERROR] fetch_ohlc failed for {symbol}: {e}")
        return None

# -----------------------------------------------------------------------------
# Indicators
# -----------------------------------------------------------------------------
def calculate_indicators(df):
    df["ema50"] = ema_indicator(df["close"], window=50)
    df["rsi"] = rsi(df["close"], window=14)
    df["atr"] = average_true_range(df["high"], df["low"], df["close"], window=14)
    df["obv"] = on_balance_volume(df["close"], df["volume"])

    # Supertrend (simplified)
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

    # Alligator (shifted SMAs)
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

    df["ichimoku_bull"] = (df["close"] > df["senkou_a"]) & (df["close"] > df["senkou_b"])
    df["twist"] = (df["senkou_a"].shift(1) < df["senkou_b"].shift(1)) & (df["senkou_a"] > df["senkou_b"])
    return df

# -----------------------------------------------------------------------------
# Trade Detection (returns dict or None)
# -----------------------------------------------------------------------------
def detect_trade(df):
    df = calculate_indicators(df)
    if len(df) < 2:
        return None
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

    if confidence >= 4:  # Long setup
        return {
            "type": "Breakout Long",
            "entry": latest["close"],
            "stop": latest["close"] - latest["atr"],
            "tp1": latest["close"] + latest["atr"] * 1.5,
            "tp2": latest["close"] + latest["atr"] * 2.5,
            "confidence": confidence
        }
    elif confidence <= 2:  # Short setup
        return {
            "type": "Breakdown Short",
            "entry": latest["close"],
            "stop": latest["close"] + latest["atr"],
            "tp1": latest["close"] - latest["atr"] * 1.5,
            "tp2": latest["close"] - latest["atr"] * 2.5,
            "confidence": 6 - confidence
        }
    return None

# -----------------------------------------------------------------------------
# Embed Builders
# -----------------------------------------------------------------------------
def build_header_timestamp():
    _, central_dt = now_times()
    return fmt_central(central_dt)

def build_footer_timestamp():
    utc_dt, _ = now_times()
    return fmt_utc(utc_dt)

def format_embed(symbol, trade):
    """Trade entry embed."""
    header_ts = build_header_timestamp()
    footer_ts = build_footer_timestamp()

    direction_emoji = "🟢" if "Long" in trade["type"] else "🔴"
    title = f"{direction_emoji} {symbol} {trade['type']} Alert – {header_ts}"
    embed = discord.Embed(
        title=title,
        color=discord.Color.green() if "Long" in trade["type"] else discord.Color.red()
    )
    embed.add_field(name="📈 Entry", value=f"${trade['entry']:.2f}", inline=True)
    embed.add_field(name="🛑 Stop", value=f"${trade['stop']:.2f}", inline=True)
    embed.add_field(name="🎯 TP1", value=f"${trade['tp1']:.2f}", inline=True)
    embed.add_field(name="🎯 TP2", value=f"${trade['tp2']:.2f}", inline=True)
    emoji = {6: "🔥", 5: "✅", 4: "🟢", 3: "⚪", 2: "🔻", 1: "🟥", 0: "❌"}.get(trade["confidence"], "❓")
    embed.add_field(name="🧠 Confidence", value=f"{emoji} {trade['confidence']}/6", inline=True)
    embed.set_footer(text=f"Generated {footer_ts}")
    return embed

def format_exit_embed(symbol, direction, entry, tp1, tp2, stop, price, result):
    """Trade exit embed (TP/SL)."""
    header_ts = build_header_timestamp()
    footer_ts = build_footer_timestamp()
    color = discord.Color.green() if "Take Profit" in result else discord.Color.red()
    title = f"{symbol} {direction} Exit – {header_ts}"
    embed = discord.Embed(title=title, description=result, color=color)
    embed.add_field(name="📈 Entry", value=f"${entry:.2f}", inline=True)
    embed.add_field(name="🎯 TP1", value=f"${tp1:.2f}", inline=True)
    embed.add_field(name="🛑 Stop", value=f"${stop:.2f}", inline=True)
    embed.add_field(name="📉 Price", value=f"${price:.2f}", inline=True)
    embed.set_footer(text=f"Closed {footer_ts}")
    return embed

def format_eth_status_embed(latest):
    header_ts = build_header_timestamp()
    footer_ts = build_footer_timestamp()
    rsi_state = "🟢 Overbought" if latest["rsi"] > 70 else "🔴 Oversold" if latest["rsi"] < 30 else "⚪ Neutral"
    trend = "📈 Bullish" if latest["close"] > latest["ema50"] else "📉 Bearish"
    color = discord.Color.blue()
    title = f"📊 ETH 30-Minute Status Report – {header_ts}"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="💵 Price", value=f"${latest['close']:.2f}", inline=True)
    embed.add_field(name="📉 RSI", value=f"{latest['rsi']:.2f} ({rsi_state})", inline=True)
    embed.add_field(name="📏 ATR", value=f"{latest['atr']:.2f}", inline=True)
    embed.add_field(name="📈 Trend", value=trend, inline=True)
    embed.add_field(name="🧠 Supertrend", value="🟢 Bullish" if latest["supertrend"] else "🔴 Bearish", inline=True)
    embed.add_field(name="🐊 Alligator", value="🟢 Bullish" if latest["alligator"] else "🔴 Bearish", inline=True)
    embed.add_field(name="☁️ Ichimoku", value="🟢 Bullish" if latest["ichimoku_bull"] else "🔴 Bearish", inline=True)
    embed.add_field(name="🌪️ Twist Alert", value="✅ Twist" if latest["twist"] else "No twist", inline=True)
    embed.set_footer(text=f"Updated {footer_ts}")
    return embed

# -----------------------------------------------------------------------------
# ETH Status Report (every 30m)
# -----------------------------------------------------------------------------
async def send_eth_status_report(channel):
    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return
    df = calculate_indicators(df)
    latest = df.iloc[-1]
    embed = format_eth_status_embed(latest)
    await channel.send(embed=embed)

@tasks.loop(minutes=30)
async def eth_status_report():
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    await send_eth_status_report(channel)

# -----------------------------------------------------------------------------
# Trade Scan Loop (all tokens, every 1m)
# -----------------------------------------------------------------------------
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

        # --- Manage open trades ---
        if symbol in active_alerts:
            entry, tp1, tp2, stop, open_time_utc = active_alerts[symbol]
            direction = "Long" if entry < stop else "Short"
            hit_tp = (price >= tp1 or price >= tp2) if direction == "Long" else (price <= tp1 or price <= tp2)
            hit_sl = (price <= stop) if direction == "Long" else (price >= stop)

            if hit_tp or hit_sl:
                result = "🎯 Take Profit Hit!" if hit_tp else "💥 Stop Loss Hit!"
                if hit_tp:
                    leaderboard_stats[symbol]["wins"] += 1
                else:
                    leaderboard_stats[symbol]["losses"] += 1
                embed = format_exit_embed(symbol, direction, entry, tp1, tp2, stop, price, result)
                await channel.send(embed=embed)
                del active_alerts[symbol]
            continue  # Don't look for a new trade while one is active

        # --- Cooldown ---
        cooldown_minutes = 30
        last_time = cooldowns.get(symbol)
        if last_time and (now_utc - last_time).total_seconds() < cooldown_minutes * 60:
            continue

        # --- New trade detection ---
        trade = detect_trade(df)
        if trade:
            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"], trade["stop"], now_utc
            )
            cooldowns[symbol] = now_utc
            await channel.send(embed=format_embed(symbol, trade))

# -----------------------------------------------------------------------------
# Daily Leaderboard Reset (00:00 UTC)
# -----------------------------------------------------------------------------
@tasks.loop(minutes=1)
async def reset_leaderboard_daily():
    now_utc, _ = now_times()
    if now_utc.hour == 0 and now_utc.minute == 0:
        channel = bot.get_channel(CHANNEL_ID)

        # Find top performer
        top_symbol = None
        top_wins = -1
        top_winrate = 0
        top_losses = 0
        for symbol, stats in leaderboard_stats.items():
            wins = stats["wins"]
            losses = stats["losses"]
            total = wins + losses
            winrate = round((wins / total) * 100) if total > 0 else 0
            if wins > top_wins or (wins == top_wins and winrate > top_winrate):
                top_symbol = symbol
                top_wins = wins
                top_losses = losses
                top_winrate = winrate

        if top_symbol and top_wins > 0:
            await channel.send(
                f"🏅 Top performer of the day: **{top_symbol}** with {top_wins} wins and {top_losses} loss(es) (📊 {top_winrate}% win rate)"
            )
        else:
            await channel.send("📊 No trades were completed today.")

        # Reset
        for symbol in leaderboard_stats:
            leaderboard_stats[symbol] = {"wins": 0, "losses": 0}
        await channel.send("🔄 Daily leaderboard has been reset (UTC 00:00).")

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
@bot.command()
async def scan(ctx):
    """Manual scan for all symbols."""
    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            await ctx.send(f"❌ {symbol} fetch failed.")
            continue
        trade = detect_trade(df)
        if trade:
            await ctx.send(embed=format_embed(symbol, trade))
        else:
            await ctx.send(f"🔍 No setup for {symbol}.")

@bot.command()
async def confidence(ctx, symbol: str):
    """Show current trade signal + confidence for one symbol."""
    symbol = symbol.upper()
    if symbol not in KRAKEN_PAIRS:
        await ctx.send("❌ Invalid symbol.")
        return
    df = fetch_ohlc(symbol)
    if df is None:
        await ctx.send(f"❌ Failed to fetch {symbol}.")
        return
    trade = detect_trade(df)
    if trade:
        await ctx.send(embed=format_embed(symbol, trade))
    else:
        await ctx.send(f"ℹ️ No signal for {symbol}.")

@bot.command()
async def ethreport(ctx):
    """Send ETH 30-min status report on demand."""
    await send_eth_status_report(ctx.channel)

@bot.command()
async def testfetch(ctx, symbol: str = "ETH"):
    """Quick data fetch test."""
    symbol = symbol.upper()
    df = fetch_ohlc(symbol)
    if df is not None:
        await ctx.send(f"✅ Successfully fetched {symbol} data.")
    else:
        await ctx.send(f"❌ Failed to fetch {symbol} data.")

@bot.command()
async def leaderboard(ctx):
    """Show current wins/losses since last reset."""
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
    # UTC timestamp in footer
    embed.set_footer(text=f"Updated {fmt_utc(datetime.datetime.now(UTC_TZ))}")
    await ctx.send(embed=embed)

# -----------------------------------------------------------------------------
# Bot Startup
# -----------------------------------------------------------------------------
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


