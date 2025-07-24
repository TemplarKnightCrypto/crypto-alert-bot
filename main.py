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
from ta.momentum import rsi, stochrsi
from ta.volatility import average_true_range
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
def calculate_indicators(df):
    df["ema50"] = ema_indicator(df["close"], window=50)
    df["rsi"] = rsi(df["close"], window=14)
    df["atr"] = average_true_range(df["high"], df["low"], df["close"], window=14)
    df["obv"] = on_balance_volume(df["close"], df["volume"])

    ema_12 = df["close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema_12 - ema_26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["stoch_rsi"] = stochrsi(df["close"])

    hl2 = (df["high"] + df["low"]) / 2
    factor = 3.0
    upperband = hl2 + factor * df["atr"]
    lowerband = hl2 - factor * df["atr"]
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

    nine_high = df["high"].rolling(9).max()
    nine_low = df["low"].rolling(9).min()
    df["tenkan"] = (nine_high + nine_low) / 2
    period26_high = df["high"].rolling(26).max()
    period26_low = df["low"].rolling(26).min()
    df["kijun"] = (period26_high + period26_low) / 2
    df["senkou_a"] = ((df["tenkan"] + df["kijun"]) / 2).shift(26)
    period52_high = df["high"].rolling(52).max()
    period52_low = df["low"].rolling(52).min()
    df["senkou_b"] = ((period52_high + period52_low) / 2).shift(26)
    df["ichimoku_bull"] = (df["close"] > df["senkou_a"]) & (df["close"] > df["senkou_b"])
    df["twist"] = (df["senkou_a"].shift(1) < df["senkou_b"].shift(1)) & (df["senkou_a"] > df["senkou_b"])

    return df

# -------------------------------------------------------------------
def calculate_market_bias(latest):
    bullish_signals = 0
    if latest["close"] > latest["ema50"]:
        bullish_signals += 1
    if latest["rsi"] > 50:
        bullish_signals += 1
    if latest["supertrend"]:
        bullish_signals += 1
    if latest["alligator"]:
        bullish_signals += 1
    if latest["ichimoku_bull"]:
        bullish_signals += 1
    if bullish_signals >= 4:
        return "🟢 Bullish"
    elif bullish_signals <= 2:
        return "🔴 Bearish"
    else:
        return "⚠️ Mixed"

# -------------------------------------------------------------------
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
    value="📈 Entry: **${:.2f}**\n🛑 Stop:  `${:.2f}`".format(trade['entry'], trade['stop']),
    inline=False
    )
    eembed.add_field(
    name="🎯 Targets",
    value="TP1: ${:.2f}\nTP2: ${:.2f}".format(trade['tp1'], trade['tp2']),
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
def format_exit_embed(symbol, direction, entry, tp1, tp2, stop, price, result):
    header = fmt_central(now_times()[1])
    footer = fmt_utc(now_times()[0])
    emoji = "🟢" if direction == "Long" else "🔴"
    color = discord.Color.green() if "Take Profit" in result else discord.Color.red()

    embed = discord.Embed(
        title=f"{emoji} {symbol} {direction} Exit – {header}",
        description=result,
        color=color
    )
    embed.add_field(
        name="📊 Trade Summary",
        value=f"📈 Entry: **${entry:.2f}**
🎯 TP2:   **${tp2:.2f}**
🛑 Stop:  `${stop:.2f}`",
        inline=False
    )
    embed.add_field(
        name="📉 Exit Price",
        value=f"**${price:.2f}**",
        inline=False
    )
    embed.set_footer(text=f"Closed {footer}")
    return embed

# -------------------------------------------------------------------
def fetch_ohlc(symbol, interval=30):
    try:
        pair = KRAKEN_PAIRS.get(symbol)
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        raw = response.json()
        if 'error' in raw and raw['error']:
            return None
        result = raw.get('result', {})
        pair_key = next((k for k in result if k != 'last'), None)
        if not pair_key:
            return None
        df = pd.DataFrame(result[pair_key], columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        return df
    except:
        return None

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
            f"💰 Price: **${latest['close']:.2f}**
"
            f"📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`
"
            f"{trend_text}"
        ),
        inline=False
    )
    embed.add_field(
        name="📈 Indicator Summary",
        value=(
            f"🧠 Supertrend: {supertrend_text}
"
            f"🐊 Alligator: {alligator_text}
"
            f"☁️ Ichimoku: {ichimoku_text}
"
            f"🌪️ Twist Alert: {twist_text}"
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

# -------------------------------------------------------------------
@tasks.loop(minutes=1)
async def send_leaderboard_report():
    now_utc, _ = now_times()
    if now_utc.hour == 23 and now_utc.minute == 59:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        embed = discord.Embed(title="📊 Daily Leaderboard Summary", color=discord.Color.gold())
        sorted_stats = sorted(leaderboard_stats.items(), key=lambda x: x[1]["wins"], reverse=True)
        for symbol, stats in sorted_stats:
            wins = stats["wins"]
            losses = stats["losses"]
            total = wins + losses if (wins + losses) > 0 else 1
            winrate = round((wins / total) * 100)
            embed.add_field(
                name=symbol,
                value=f"✅ Wins: {wins} | 💥 Losses: {losses} | 📊 Win Rate: {winrate}%",
                inline=False
            )
        embed.set_footer(text=f"Report sent {fmt_utc(now_utc)}")
        await channel.send(embed=embed)

        file_name = f"trade_log_{now_utc.strftime('%Y-%m-%d')}.csv"
        if os.path.exists(file_name):
            try:
                await channel.send("📄 Daily Trade Log File:", file=discord.File(file_name))
            except Exception as e:
                await channel.send(f"❌ Could not send trade log: {e}")

# -------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    send_leaderboard_report.start()

# -------------------------------------------------------------------
@bot.command()
async def scan(ctx):
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
            f"💰 Price: **${latest['close']:.2f}**
"
            f"📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`
"
            f"{trend_text}"
        ),
        inline=False
    )
    embed.add_field(
        name="📈 Indicator Summary",
        value=(
            f"🧠 Supertrend: {supertrend_text}
"
            f"🐊 Alligator: {alligator_text}
"
            f"☁️ Ichimoku: {ichimoku_text}
"
            f"🌪️ Twist Alert: {twist_text}"
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


