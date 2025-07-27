import os
import threading
import requests
import pandas as pd
import numpy as np
import datetime
import pytz
import csv
import discord
from flask import Flask
from dotenv import load_dotenv
from discord.ext import commands, tasks
from discord import File
from ta.momentum import rsi, stochrsi, williams_r
from ta.trend import ema_indicator, sma_indicator, adx, cci
from ta.volatility import average_true_range, bollinger_hband, bollinger_lband, keltner_channel_hband, keltner_channel_lband
from ta.volume import on_balance_volume

# === Timezones ===
CENTRAL_TZ = pytz.timezone("US/Central")
UTC_TZ = datetime.timezone.utc
LEONIS_LUCIEN_CHANNEL_ID = 1398691425347961016

load_dotenv()
TOKEN = os.getenv("TOKEN")

bot_mode = "aggressive"
KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD",
    "ETH": "XETHZUSD",
    "SOL": "SOLUSD",
    "AVAX": "AVAXUSD",
    "ADA": "ADAUSD",
    "HBAR": "HBARUSD"
}

active_alerts = {}
cooldowns = {}
leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
CHANNEL_ID = 1398690647417819198
STATUS_CHANNEL_ID = 1398691425347961016

def now_times():
    utc_dt = datetime.datetime.now(UTC_TZ)
    central_dt = utc_dt.astimezone(CENTRAL_TZ)
    return utc_dt, central_dt

def fmt_central(dt):
    return dt.strftime("%Y-%m-%d %I:%M %p %Z")

def fmt_utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# === Indicator Calculation Function ===
def calculate_indicators(df):
    # === Core Indicators ===
    df["ema9"] = ema_indicator(df["close"], window=9)
    df["ema21"] = ema_indicator(df["close"], window=21)
    df["ema50"] = ema_indicator(df["close"], window=50)
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

    # === CMF ===
    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-9) * df["volume"]
    df["cmf"] = mfv.rolling(window=20).sum() / df["volume"].rolling(window=20).sum()

    # === MACD Histogram ===
    df["macd"] = df["close"].ewm(span=12).mean() - df["close"].ewm(span=26).mean()
    df["signal"] = df["macd"].ewm(span=9).mean()
    df["macd_hist"] = df["macd"] - df["signal"]

    # === Supertrend ===
    atr = df["atr"]
    hl2 = (df["high"] + df["low"]) / 2
    factor = 3
    df["upperband"] = hl2 + (factor * atr)
    df["lowerband"] = hl2 - (factor * atr)

    in_uptrend = [True]
    for i in range(1, len(df)):
        if df["close"].iloc[i] > df["upperband"].iloc[i - 1]:
            in_uptrend.append(True)
        elif df["close"].iloc[i] < df["lowerband"].iloc[i - 1]:
            in_uptrend.append(False)
        else:
            in_uptrend.append(in_uptrend[-1])
    df["supertrend"] = in_uptrend

    # === Alligator ===
    df["jaw"] = ema_indicator(df["close"], window=13).shift(8)
    df["teeth"] = ema_indicator(df["close"], window=8).shift(5)
    df["lips"] = ema_indicator(df["close"], window=5).shift(3)
    df["alligator"] = (df["lips"] > df["teeth"]) & (df["teeth"] > df["jaw"])

    # === Ichimoku Cloud ===
    period9_high = df["high"].rolling(window=9).max()
    period9_low = df["low"].rolling(window=9).min()
    tenkan_sen = (period9_high + period9_low) / 2

    period26_high = df["high"].rolling(window=26).max()
    period26_low = df["low"].rolling(window=26).min()
    kijun_sen = (period26_high + period26_low) / 2

    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
    period52_high = df["high"].rolling(window=52).max()
    period52_low = df["low"].rolling(window=52).min()
    senkou_span_b = ((period52_high + period52_low) / 2).shift(26)

    df["ichimoku_bull"] = (df["close"] > senkou_span_a) & (df["close"] > senkou_span_b)
    df["ichimoku_bear"] = (df["close"] < senkou_span_a) & (df["close"] < senkou_span_b)
    df["twist"] = (senkou_span_a - senkou_span_b).diff().abs() < 1e-3
    df["kumo_breakout"] = (df["close"] > senkou_span_a) & (df["close"] > senkou_span_b)
    df["kumo_breakdown"] = (df["close"] < senkou_span_a) & (df["close"] < senkou_span_b)
    df["inside_kumo"] = ((df["close"] > senkou_span_b) & (df["close"] < senkou_span_a)) | \
                         ((df["close"] > senkou_span_a) & (df["close"] < senkou_span_b))

    return df

# === Main Detection Logic ===
def detect_trade(df, mode="aggressive"):
    latest = df.iloc[-1]
    matches = []
    confidence = 0

    def assign_knight(trade_type, indicators):
        if "Breakout" in trade_type or "Breakdown" in trade_type:
            return "⚔️ Sir Leonis Ironhart"
        elif "Mean Reversion" in trade_type:
            return "🌙 Orion Vellum"
        elif "Swing Trade" in trade_type:
            return "⚔️ Sir Leonis Ironhart"
        elif "Pullback" in trade_type:
            return "🛡️ Sir Lucien Frostveil"
        elif "Volatility Squeeze" in trade_type:
            return "🌙 Orion Vellum"
        elif "Weak Signal" in trade_type:
            return "🌙 Orion Vellum"
        if "supertrend" in indicators or "ema50" in indicators:
            return "🛡️ Sir Lucien Frostveil"
        if "alligator" in indicators:
            return "⚔️ Sir Leonis Ironhart"
        if "ichimoku_bull" in indicators or "twist" in indicators:
            return "🌙 Orion Vellum"
        return "🧙 Unknown"

    # === Long Trade Setups ===
    if latest["rsi"] < 30 and latest["williams_r"] < -80:
        matches.append(("📈 🔁 Mean Reversion Long", 4))
    if latest["close"] > latest["donchian_high"] and latest["cmf"] > 0:
        matches.append(("📈 🚀 Breakout Anticipation", 5))
    if latest["squeeze"] and latest["bb_width"] > 0.05:
        matches.append(("📈 📊 Volatility Squeeze Long", 3))
    if latest["cci"] > 100 and latest["cmf"] > 0:
        matches.append(("📈 🌀 Swing Trade Long", 4))
    if latest["rsi"] > 50 and latest["close"] > latest["ema50"]:
        matches.append(("📈 📈 Pullback Long", 3))

    # === Short Trade Setups ===
    if latest["rsi"] > 70 and latest["williams_r"] > -20:
        matches.append(("📉 🔁 Mean Reversion Short", 4))
    if latest["close"] < latest["donchian_low"] and latest["cmf"] < 0:
        matches.append(("📉 🔻 Breakdown Anticipation", 5))
    if latest["squeeze"] and latest["bb_width"] > 0.05 and latest["cmf"] < 0:
        matches.append(("📉 📊 Volatility Squeeze Short", 3))
    if latest["cci"] < -100 and latest["cmf"] < 0:
        matches.append(("📉 🌀 Swing Trade Short", 4))
    if latest["rsi"] < 50 and latest["close"] < latest["ema50"]:
        matches.append(("📉 📉 Pullback Short", 3))

    if not matches:
        if mode == "aggressive":
            return [{
                "type": "🟡 Weak Signal",
                "entry": latest["close"],
                "stop": latest["close"] - latest["atr"] * 1.5,
                "tp1": latest["close"] + latest["atr"] * 1.5,
                "tp2": latest["close"] + latest["atr"] * 2.5,
                "confidence": 1,
                "strategies_matched": [],
                "knight": assign_knight("Weak Signal", [])
            }]
        return []

    trades = []
    for match_text, weight in matches:
        direction = "short" if "📉" in match_text else "long"
        if direction == "long":
            stop = latest["close"] - latest["atr"] * 1.5
            tp1 = latest["close"] + latest["atr"] * 1.5
            tp2 = latest["close"] + latest["atr"] * 2.5
        else:
            stop = latest["close"] + latest["atr"] * 1.5
            tp1 = latest["close"] - latest["atr"] * 1.5
            tp2 = latest["close"] - latest["atr"] * 2.5

        knight = assign_knight(match_text, [
            "rsi", "ema50", "supertrend", "alligator", "ichimoku_bull", "twist"
        ])
        trades.append({
            "type": match_text,
            "entry": latest["close"],
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "confidence": weight,
            "strategies_matched": [match_text],
            "knight": knight
        })

    return trades

# === Orion’s Daily Strategy Detection (OUTSIDE detect_trade) ===
def detect_breakout_orion(df):
    candle = df.iloc[-2]

    long_breakout = (
        candle["close"] > 2920 and
        candle["ema9"] > candle["ema21"] > candle["ema50"] and
        candle["close"] > candle["ema9"] and
        candle.get("kumo_breakout", False)
    )

    short_breakout = (
        candle["close"] < 2920 and
        candle["ema9"] < candle["ema21"] < candle["ema50"] and
        candle["close"] < candle["ema9"] and
        candle.get("kumo_breakdown", False)
    )

    if long_breakout:
        return "Breakout Long"
    elif short_breakout:
        return "Breakout Short"
    else:
        return None

def detect_pullback_orion(df):
    candle = df.iloc[-2]

    long_pullback = (
        2880 <= candle["close"] <= 2900 and
        candle["rsi"] < 40 and
        candle["close"] > candle["ema21"] and
        candle.get("inside_kumo", True)
    )

    short_pullback = (
        2940 <= candle["close"] <= 2960 and
        candle["rsi"] > 60 and
        candle["close"] < candle["ema21"] and
        candle.get("inside_kumo", True)
    )

    if long_pullback:
        return "Pullback Long"
    elif short_pullback:
        return "Pullback Short"
    else:
        return None

def log_trade_to_csv(trade_data):
    date_str = datetime.datetime.now(UTC_TZ).strftime('%Y-%m-%d')
    filename = f"logs/{date_str}_trades.csv"
    os.makedirs("logs", exist_ok=True)
    file_exists = os.path.isfile(filename)

    with open(filename, mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(["Time", "Symbol", "Type", "Entry", "Stop", "TP1", "TP2", "Confidence", "Matched", "WeakSignal", "Knight"])

        writer.writerow([
            trade_data["time"], trade_data["symbol"], trade_data["type"],
            f"{trade_data['entry']:.2f}", f"{trade_data['stop']:.2f}",
            f"{trade_data['tp1']:.2f}", f"{trade_data['tp2']:.2f}",
            trade_data["confidence"],
            ", ".join(trade_data.get("strategies_matched", [])),
            "Yes" if trade_data["type"] == "🟡 Weak Signal" else "No",
            trade_data.get("knight", "🧙 Unknown")
        ])

def fetch_ohlc(symbol, interval="30"):
    url = f"https://api.kraken.com/0/public/OHLC?pair={KRAKEN_PAIRS[symbol]}&interval={interval}"
    try:
        response = requests.get(url)
        data = response.json()
        key = list(data["result"].keys())[0]
        df = pd.DataFrame(data["result"][key], columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df = df.astype({
            "time": "int64", "open": "float", "high": "float",
            "low": "float", "close": "float", "volume": "float"
        })
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        print(f"OHLC fetch error: {e}")
        return None
def calculate_market_bias(latest):
    if latest["rsi"] > 60 and latest["cmf"] > 0:
        return "🟢 Bullish Bias"
    elif latest["rsi"] < 40 and latest["cmf"] < 0:
        return "🔴 Bearish Bias"
    else:
        return "⚠️ Neutral Bias"

def format_embed(symbol, trade, central_time, utc_time):
    confidence_emoji = {
        6: "🔥", 5: "✅", 4: "🟢", 3: "⚪", 2: "🔻", 1: "🟥", 0: "❌"
    }.get(trade["confidence"], "❓")

    matched = "\n".join(trade.get("strategies_matched", [trade["type"]]))
    if trade["type"] == "🟡 Weak Signal":
        matched += "\n⚠️ *Only weak signal triggered*"

    direction = "Short" if "Short" in trade["type"] else "Long"
    emoji = "📉" if direction == "Short" else "📈"
    color = discord.Color.red() if direction == "Short" else discord.Color.green()

    knight = trade.get("knight", "🧙 Unknown")

    embed = discord.Embed(
        title=f"{emoji} {symbol} {trade['type']} – {central_time}",
        color=color
    )
    embed.add_field(
        name="📊 Trade Setup",
        value=f"{emoji} Entry: **${trade['entry']:.2f}**\n🛑 Stop: `${trade['stop']:.2f}`",
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
    embed.add_field(
        name="🧪 Strategy Match",
        value=matched,
        inline=False
    )
    embed.add_field(
        name="🧙 Signal Issued By",
        value=knight,
        inline=False
    )
    embed.set_footer(text=f"Generated {utc_time}")
    return embed

def format_exit_embed(symbol, direction, entry, tp1, tp2, stop, exit_price, result,
                      close_time_ct, close_time_utc, open_time_ct, elapsed_str):
    embed = discord.Embed(
        title=f"{symbol} Trade Exit – {result}",
        color=discord.Color.green() if "Take Profit" in result else discord.Color.red()
    )
    embed.add_field(name="📈 Entry", value=f"${entry:.2f}", inline=True)
    embed.add_field(name="🎯 TP2", value=f"${tp2:.2f}", inline=True)
    embed.add_field(name="🛑 Stop", value=f"${stop:.2f}", inline=True)
    embed.add_field(name="💰 Exit Price", value=f"${exit_price:.2f}", inline=True)
    embed.add_field(name="⏰ Alert Sent", value=f"{open_time_ct} CT", inline=True)
    embed.add_field(name="⏳ Elapsed", value=f"{elapsed_str}", inline=True)
    embed.set_footer(text=f"Closed {close_time_utc} UTC")
    return embed
def format_orion_embed(strategy_type, entry, stop, tp1, tp2):
    now_ct = datetime.datetime.now(CENTRAL_TZ).strftime('%b %d • %I:%M %p CT')
    now_utc = datetime.datetime.now(UTC_TZ).strftime('%H:%M UTC')

    # Orion’s poetic tone based on signal type
    quotes = {
        "Breakout Long": "🌘 Orion whispers: *From the depths we rise, unseen yet unstoppable.*",
        "Pullback Long": "🌘 Orion murmurs: *The moon retreats, only to gather strength anew.*",
        "Breakout Short": "🌘 Orion intones: *Foundations fracture. The silence begins to scream.*",
        "Pullback Short": "🌘 Orion breathes: *The winds return. Shadows reclaim what was borrowed.*"
    }
    quote = quotes.get(strategy_type, "🌘 Orion watches silently...")

    embed = discord.Embed(
        title=f"🌘 Orion's Daily ETH Signal – {strategy_type}",
        color=0x6f42c1  # Mystic purple
    )
    embed.add_field(name="🎯 Entry", value=f"`{entry}`", inline=True)
    embed.add_field(name="🛡️ Stop Loss", value=f"`{stop}`", inline=True)
    embed.add_field(name="🎯 Targets", value=f"`TP1: {tp1}` → `TP2: {tp2}`", inline=False)
    embed.set_footer(text=f"{quote}  •  UTC Time: {now_utc}")
    return embed

@bot.command()
async def ethreport(ctx):
    now = datetime.datetime.now(CENTRAL_TZ)
    df = fetch_ohlc("ETH")
    if df is None:
        await ctx.send("❌ Could not fetch ETH data.")
        return

    df = calculate_indicators(df)
    latest = df.iloc[-2]  # Closed candle
    header = fmt_central(now)
    footer = fmt_utc(datetime.datetime.now(UTC_TZ))

    bias = calculate_market_bias(latest)
    embed = discord.Embed(title=f"📊 ETH 30-Min Status – {header}", color=discord.Color.blue())
    embed.add_field(name="💵 Price & Trend", value=(f"💰 Price: **${latest['close']:.2f}**\n"
                                                    f"📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`"), inline=False)
    embed.add_field(name="📊 Market Bias", value=bias, inline=False)
    embed.set_footer(text=f"Updated {footer}")
    await ctx.send(embed=embed)


@bot.command()
async def testtrade(ctx, symbol: str.upper = "ETH"):
    df = fetch_ohlc(symbol)
    if df is None:
        await ctx.send(f"❌ Could not fetch data for {symbol}.")
        return

    df = calculate_indicators(df)
    trades = detect_trade(df, mode=bot_mode)

    if not trades:
        await ctx.send(f"🔍 No trade detected for {symbol}.")
    else:
        for trade in trades:
            now_utc, now_ct = now_times()
            embed = format_embed(symbol, trade, fmt_central(now_ct), fmt_utc(now_utc))
            await ctx.send(embed=embed)


@bot.command()
async def mode(ctx):
    global bot_mode
    bot_mode = "strict" if bot_mode == "aggressive" else "aggressive"
    await ctx.send(f"🧠 Bot mode set to: **{bot_mode.capitalize()}**")


@bot.command()
async def forcerescan(ctx):
    await ctx.send("🔁 Forcing rescan of all coins...")
    await scan_coins()


@bot.command()
async def cooldownreset(ctx, symbol: str.upper):
    if symbol in cooldowns:
        del cooldowns[symbol]
        await ctx.send(f"♻️ Cooldown reset for {symbol}")
    else:
        await ctx.send(f"⚠️ No cooldown set for {symbol}")


@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")


@bot.command()
async def logsummary(ctx):
    date_str = datetime.datetime.now(UTC_TZ).strftime("%Y-%m-%d")
    filename = f"logs/{date_str}_trades.csv"
    if not os.path.exists(filename):
        await ctx.send("📭 No trades logged yet today.")
        return

    df = pd.read_csv(filename)
    total = len(df)
    weak_count = df[df["WeakSignal"] == "Yes"].shape[0]
    strong_count = total - weak_count
    percent_weak = round((weak_count / total) * 100) if total > 0 else 0

    embed = discord.Embed(title="📊 Trade Log Summary", color=discord.Color.purple())
    embed.add_field(name="📅 Date", value=date_str, inline=False)
    embed.add_field(name="📦 Total Trades", value=total, inline=True)
    embed.add_field(name="🟡 Weak Signals", value=f"{weak_count} ({percent_weak}%)", inline=True)
    embed.add_field(name="🧠 Strong Signals", value=strong_count, inline=True)
    embed.set_footer(text=f"Updated {fmt_utc(datetime.datetime.now(UTC_TZ))}")
    await ctx.send(embed=embed)


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

@bot.command()
async def commands(ctx):
    embed = discord.Embed(title="📜 Available Commands", color=discord.Color.teal())
    
    embed.add_field(
        name="📊 Market & Trade Reports",
        value=(
            "`!ethreport` – Get ETH 30-minute status report\n"
            "`!testtrade [SYMBOL]` – Run trade scan for a symbol (default: ETH)\n"
            "`!logsummary` – Show today’s trade log summary\n"
            "`!leaderboard` – View current trade alert performance"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Bot Settings & Tools",
        value=(
            "`!mode` – Toggle bot mode between `aggressive` and `strict`\n"
            "`!forcerescan` – Force full scan of all tracked coins\n"
            "`!cooldownreset SYMBOL` – Reset cooldown for a specific token"
        ),
        inline=False
    )

    embed.add_field(
        name="🛠️ Utility",
        value=(
            "`!ping` – Test if the bot is online\n"
            "`!commands` – Show this command list"
        ),
        inline=False
    )

    embed.set_footer(text="Templar Knight Crypto • Forge the Future")
    await ctx.send(embed=embed)

@tasks.loop(minutes=1)
async def eth_status_report():
    now = datetime.datetime.now(CENTRAL_TZ)
    if now.minute not in (0, 30):
        return

    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Status channel {STATUS_CHANNEL_ID} not found.")
        return

    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return

    df = calculate_indicators(df)
    latest = df.iloc[-2]
    header = fmt_central(now)
    footer = fmt_utc(datetime.datetime.now(UTC_TZ))

    def icon(state): return "🟢" if state == "bullish" else "🔴" if state == "bearish" else "⚪"
    def assess_rsi(): return "bullish" if latest["rsi"] > 55 else "bearish" if latest["rsi"] < 45 else "neutral"
    def assess_macd(): return "bullish" if latest["macd_hist"] > 0 else "bearish" if latest["macd_hist"] < 0 else "neutral"
    def assess_supertrend(): return "bullish" if latest["supertrend"] else "bearish"
    def assess_alligator():
        if latest["lips"] > latest["teeth"] > latest["jaw"]: return "bullish"
        elif latest["lips"] < latest["teeth"] < latest["jaw"]: return "bearish"
        return "neutral"
    def assess_ichimoku():
        return "bullish" if latest["ichimoku_bull"] else "bearish" if latest["ichimoku_bear"] else "neutral"
    def assess_twist():
        if latest["twist"] and latest["ichimoku_bull"]: return "bullish"
        if latest["twist"] and latest["ichimoku_bear"]: return "bearish"
        return "neutral"

    groups = {
        "🛡️ Defense": [
            (icon(assess_supertrend()), "Supertrend", assess_supertrend()),
            (icon(assess_alligator()), "Alligator", assess_alligator())
        ],
        "⚔️ Momentum": [
            (icon(assess_rsi()), f"RSI {latest['rsi']:.1f}", assess_rsi()),
            (icon(assess_macd()), f"MACD Hist {latest['macd_hist']:.3f}", assess_macd())
        ],
        "🌘 Reversal": [
            (icon(assess_ichimoku()), "Ichimoku Cloud", assess_ichimoku()),
            (icon(assess_twist()), "Kumo Twist", assess_twist())
        ]
    }

    bullish = sum(1 for g in groups.values() for _, _, s in g if s == "bullish")
    bearish = sum(1 for g in groups.values() for _, _, s in g if s == "bearish")
    total = sum(len(g) for g in groups.values())
    bias = "🟢 Bullish" if bullish > bearish else "🔴 Bearish" if bearish > bullish else "⚪ Neutral"

    embed = discord.Embed(
        title=f"📊 ETH 30-Min Status – {header}",
        color=discord.Color.green() if bullish > bearish else discord.Color.red() if bearish > bullish else discord.Color.light_grey()
    )

    for group, indicators in groups.items():
        value = "\n".join(f"{emoji} {label}" for emoji, label, _ in indicators)
        embed.add_field(name=group, value=value, inline=False)

    embed.add_field(
        name="🧠 Market Bias",
        value=f"{bias}\n({bullish} Bullish / {bearish} Bearish of {total})",
        inline=False
    )

    quote = {
        "🟢": "⚔️ *Momentum stirs. Ready the charge.*\n– Sir Leonis Ironhart",
        "🔴": "🌘 *Shadows lengthen. Caution… or be claimed.*\n– Orion Vellum",
        "⚪": "🛡️ *Patience steadies the hand when the winds are unclear.*\n– Sir Lucien Frostveil"
    }.get(bias[:2], "🧙")

    embed.add_field(name="📜 Knight's Insight", value=quote, inline=False)
    embed.set_footer(text=f"Updated {footer}")

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Failed to send ETH report: {e}")

@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    now_utc, now_central = now_times()

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        df = calculate_indicators(df)
        latest = df.iloc[-2]  # Use most recent closed candle
        price = latest["close"]
        candle_high = latest["high"]
        candle_low = latest["low"]

        # === Check for Active Trade ===
        if symbol in active_alerts:
            entry, tp1, tp2, stop, open_time_utc, trade_type = active_alerts[symbol]
            direction = "Short" if "Short" in trade_type else "Long"

            print(f"\n[DEBUG] ----- {symbol} Active Trade Check -----")
            print(f"[DEBUG] Direction: {direction}")
            print(f"[DEBUG] Entry: {entry:.2f}, Stop: {stop:.2f}, TP1: {tp1:.2f}, TP2: {tp2:.2f}")
            print(f"[DEBUG] Candle Close: {price:.2f}, High: {candle_high:.2f}, Low: {candle_low:.2f}")
            print(f"[DEBUG] TP2 Condition: {candle_high >= tp2 if direction == 'Long' else candle_low <= tp2}")
            print(f"[DEBUG] SL Condition: {candle_low <= stop if direction == 'Long' else candle_high >= stop}")
            print(f"[DEBUG] Timestamp (UTC): {now_utc.isoformat()}")

            hit_tp2 = (candle_high >= tp2) if direction == "Long" else (candle_low <= tp2)
            hit_sl = (candle_low <= stop) if direction == "Long" else (candle_high >= stop)

            if hit_tp2 or hit_sl:
                result = "🎯 Take Profit 2 Hit!" if hit_tp2 else "💥 Stop Loss Hit!"
                leaderboard_stats[symbol]["wins" if hit_tp2 else "losses"] += 1

                central_time = fmt_central(now_utc.astimezone(CENTRAL_TZ))
                utc_time = fmt_utc(now_utc)

                elapsed = now_utc - open_time_utc
                elapsed_minutes = int(elapsed.total_seconds() // 60)
                hours, minutes = divmod(elapsed_minutes, 60)
                elapsed_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

                central_open_time = fmt_central(open_time_utc.astimezone(CENTRAL_TZ))

                embed = format_exit_embed(
                    symbol, direction, entry, tp1, tp2, stop, price, result,
                    central_time, utc_time, central_open_time, elapsed_str
                )

                if symbol == "ETH":
                    await channel.send(embed=embed)

                del active_alerts[symbol]
            continue

        # === Cooldown Check (30 min) ===
        last_time = cooldowns.get(symbol)
        if last_time and (now_utc - last_time).total_seconds() < 1800:
            continue

        # === Detect New Trades ===
        trades = detect_trade(df, mode=bot_mode)
        if not trades or not isinstance(trades, list):
            continue

        for trade in trades:
            log_trade_to_csv({
                "time": now_utc.isoformat(),
                "symbol": symbol,
                "type": trade["type"],
                "entry": trade["entry"],
                "stop": trade["stop"],
                "tp1": trade["tp1"],
                "tp2": trade["tp2"],
                "confidence": trade["confidence"],
                "strategies_matched": trade.get("strategies_matched", []),
                "weak_signal": trade["type"] == "🟡 Weak Signal",
                "knight": trade.get("knight", "🧙 Unknown")
            })

            central_time = fmt_central(now_utc.astimezone(CENTRAL_TZ))
            utc_time = fmt_utc(now_utc)
            embed = format_embed(symbol, trade, central_time, utc_time)

            if symbol == "ETH":
                await channel.send(embed=embed)

            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"],
                trade["stop"], now_utc, trade["type"]
            )

            print(f"[DEBUG] 🚨 New Trade Set: {symbol} | Entry: {trade['entry']:.2f}, TP2: {trade['tp2']:.2f}, Stop: {trade['stop']:.2f}")
            cooldowns[symbol] = now_utc

@tasks.loop(minutes=1)
async def hourly_knight_report():
    now = datetime.datetime.now(CENTRAL_TZ)
    if now.minute != 45:
        return

    channel = bot.get_channel(STATUS_CHANNEL_ID)
    if channel is None:
        print(f"[ERROR] Report channel not found.")
        return

    now_utc, now_central = now_times()
    header = fmt_central(now_central)
    footer = fmt_utc(now_utc)

    embed = discord.Embed(
        title=f"📈 Knight Watch: Crypto Market Overview – {header}",
        color=discord.Color.gold()
    )

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        df = calculate_indicators(df)
        latest = df.iloc[-2]
        price = latest["close"]
        ema = latest["ema50"]
        rsi = latest["rsi"]
        donchian_high = latest["donchian_high"]
        donchian_low = latest["donchian_low"]

        if symbol in active_alerts:
            _, tp1, tp2, stop, open_time_utc, trade_type = active_alerts[symbol]
            direction = "Short" if "Short" in trade_type else "Long"
            status = f"🟢 Active {direction} trade in progress. Targeting **${tp2:.2f}**."
        else:
            # ⚔️ Breakout/Breakdown
            if price > donchian_high * 0.98:
                status = f"⚔️ Leonis eyes breakout above **${donchian_high:.2f}**."
            elif price < donchian_low * 1.02:
                status = f"⚔️ Leonis watches for breakdown below **${donchian_low:.2f}**."
            # 🛡️ Pullback
            elif price > ema and rsi < 40:
                status = f"🛡️ Lucien monitors dip near **${ema:.2f}** for long re-entry."
            elif price < ema and rsi > 60:
                status = f"🛡️ Lucien tracking potential short from **${ema:.2f}**."
            else:
                status = f"🔎 No active setup. Awaiting clean structure."

        embed.add_field(name=f"{symbol} – ${price:.2f}", value=status, inline=False)

    embed.set_footer(text=f"Report updated {footer}")
    try:
        await channel.send(embed=embed)
        print(f"[DEBUG] Knight report sent at {footer}")
    except Exception as e:
        print(f"[ERROR] Failed to send Knight report: {e}")

@tasks.loop(minutes=1)
async def orion_daily_report():
    now = datetime.datetime.now(CENTRAL_TZ)
    if now.hour != 11 or now.minute != 0:
        return

    df = fetch_ohlc("ETH")
    if df is None:
        print("[Orion] ❌ Failed to fetch ETH data.")
        return

    df = calculate_indicators(df)
    breakout_signal = detect_breakout_orion(df)
    pullback_signal = detect_pullback_orion(df)
    signal = breakout_signal or pullback_signal

    if signal:
        candle = df.iloc[-2]
        entry = candle["close"]
        atr = candle["atr"]

        if "Long" in signal:
            stop = entry - atr * 1.5
            tp1 = entry + atr * 1.5
            tp2 = entry + atr * 2.5
        else:
            stop = entry + atr * 1.5
            tp1 = entry - atr * 1.5
            tp2 = entry - atr * 2.5

        # Quote based on signal type
        if signal == "Breakout Long":
            quote = "📜 Orion whispers: *Momentum surges. The skies stir.*"
        elif signal == "Breakout Short":
            quote = "🌘 Orion murmurs: *The moon retreats. Shadows grow where greed once stood.*"
        elif signal == "Pullback Long":
            quote = "🕊️ Orion intones: *A soft step back, then the rise begins.*"
        elif signal == "Pullback Short":
            quote = "☁️ Orion speaks low: *A breath before descent. Even titans exhale.*"
        else:
            quote = "🌓 Orion observes: *All patterns return… in time.*"

        embed = discord.Embed(
            title=f"🌙 Orion Vellum – ETH {signal}",
            description=(
                f"**Entry**: `{entry:.2f}`\n"
                f"**Stop Loss**: `{stop:.2f}`\n"
                f"**TP1**: `{tp1:.2f}`\n"
                f"**TP2**: `{tp2:.2f}`\n\n"
                f"{quote}"
            ),
            color=discord.Color.purple()
        )
        embed.set_footer(text="Templar Knight Crypto • UTC " + fmt_utc(datetime.datetime.utcnow()))

        channel = bot.get_channel(ETH_REPORT_CHANNEL_ID)
        await channel.send(embed=embed)
        print(f"[Orion] ✅ Sent {signal} alert.")
    else:
        print("[Orion] No valid ETH signal detected at 11 AM.")


@tasks.loop(minutes=1)
async def orion_daily_report():
    now_utc = datetime.datetime.now(UTC_TZ)
    if now_utc.hour == 23 and now_utc.minute == 59:
        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if not channel:
            print("[ERROR] Orion daily report channel not found.")
            return

        report_date = now_utc.date()
        alerts_today = {sym: 0 for sym in KRAKEN_PAIRS}
        results_today = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}

        try:
            for sym in KRAKEN_PAIRS:
                filepath = f"logs/{report_date}_trades.csv"
                if os.path.exists(filepath):
                    with open(filepath, "r") as file:
                        reader = csv.DictReader(file)
                        for row in reader:
                            if row["symbol"] != sym:
                                continue
                            alerts_today[sym] += 1
                            if "Take Profit" in row["type"]:
                                results_today[sym]["wins"] += 1
                            elif "Stop Loss" in row["type"]:
                                results_today[sym]["losses"] += 1
        except Exception as e:
            await channel.send("❌ Failed to load trade logs.")
            print(f"[ERROR] Failed to load logs: {e}")
            return

        def orion_expectation(symbol, wins, losses):
            if wins > losses:
                return f"🌕 Orion sees light on the path for **{symbol}**."
            elif losses > wins:
                return f"🌑 Orion warns: shadows deepen around **{symbol}**."
            else:
                return f"🌗 Orion waits… the signs for **{symbol}** remain unclear."

        embed = discord.Embed(
            title=f"🌘 Orion’s Whisper – {report_date.strftime('%Y-%m-%d')}",
            color=discord.Color.dark_purple()
        )

        for symbol in KRAKEN_PAIRS:
            total = alerts_today[symbol]
            wins = results_today[symbol]["wins"]
            losses = results_today[symbol]["losses"]

            field_text = (
                f"📣 Alerts: `{total}`\n"
                f"✅ Wins: `{wins}` | ❌ Losses: `{losses}`\n"
                f"{orion_expectation(symbol, wins, losses)}"
            )
            embed.add_field(name=symbol, value=field_text, inline=False)

        embed.set_footer(text=f"Sent at {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
        await channel.send(embed=embed)
        print(f"[DEBUG] Orion daily report sent at {now_utc.isoformat()}")

@tasks.loop(minutes=1)
async def daily_trade_log_upload():
    now = datetime.datetime.now(UTC_TZ)
    if now.hour == 23 and now.minute == 58:
        channel = bot.get_channel(1399067396488302623)  # 📜・scrolls-of-the-order
        if channel is None:
            print("[ERROR] Log channel not found.")
            return

        date_str = now.strftime("%Y-%m-%d")
        filename = f"{date_str}_trades.csv"
        filepath = os.path.join("logs", filename)

        if not os.path.exists(filepath):
            await channel.send(f"⚠️ No trades recorded for {date_str}.")
            print(f"[INFO] No log file for {filename}")
            return

        df = pd.read_csv(filepath)
        total_trades = len(df)
        wins = df[df["type"].str.contains("Take Profit", na=False)].shape[0]
        losses = df[df["type"].str.contains("Stop Loss", na=False)].shape[0]
        symbols = df["symbol"].value_counts().to_dict()
        top_symbol = max(symbols, key=symbols.get) if symbols else "N/A"

        summary = (
            f"📜 **Daily Trade Summary – {date_str}**\n"
            f"📈 Trades: `{total_trades}`  ✅ Wins: `{wins}`  ❌ Losses: `{losses}`\n"
            f"🥇 Most Traded: `{top_symbol}`"
        )

        try:
            await channel.send(content=summary, file=File(filepath))
            print(f"[DEBUG] Sent daily log file: {filename}")
        except Exception as e:
            print(f"[ERROR] Failed to send daily trade log: {e}")
@tasks.loop(minutes=1)
async def weekly_scroll_summary():
    now = datetime.datetime.now(UTC_TZ)
    if now.weekday() == 6 and now.hour == 23 and now.minute == 59:  # Sunday 23:59 UTC
        channel = bot.get_channel(1399067396488302623)  # 📜・scrolls-of-the-order
        if channel is None:
            print("[ERROR] Weekly scroll channel not found.")
            return

        end_date = now.date()
        start_date = end_date - datetime.timedelta(days=6)

        weekly_logs = []
        for i in range(7):
            day = start_date + datetime.timedelta(days=i)
            filename = f"logs/{day.strftime('%Y-%m-%d')}_trades.csv"
            if os.path.exists(filename):
                df = pd.read_csv(filename)
                weekly_logs.append(df)

        if not weekly_logs:
            await channel.send("📭 No trades recorded this past week.")
            return

        full_df = pd.concat(weekly_logs)
        summary_lines = []
        tokens = full_df["symbol"].unique()

        token_stats = {}
        for token in tokens:
            token_df = full_df[full_df["symbol"] == token]
            wins = token_df[token_df["type"].str.contains("Take Profit", na=False)].shape[0]
            losses = token_df[token_df["type"].str.contains("Stop Loss", na=False)].shape[0]
            token_stats[token] = {"wins": wins, "losses": losses}
            summary_lines.append(f"• `{token}`: ✅ {wins}W / ❌ {losses}L")

        top_token = max(token_stats, key=lambda t: token_stats[t]["wins"], default="N/A")
        total_trades = len(full_df)

        embed = discord.Embed(
            title="📜 End of Week Scroll",
            description=f"Dates: `{start_date}` → `{end_date}`",
            color=discord.Color.gold()
        )
        embed.add_field(
            name="📈 Weekly Totals",
            value=f"Total Trades: `{total_trades}`\nTop Performer: 🥇 `{top_token}`",
            inline=False
        )
        embed.add_field(
            name="📊 Win/Loss by Token",
            value="\n".join(summary_lines),
            inline=False
        )
        embed.set_footer(text="May your edge stay sharp. – The Founder")

        try:
            await channel.send(embed=embed)
            print("[DEBUG] Weekly scroll posted.")
        except Exception as e:
            print(f"[ERROR] Failed to send weekly scroll: {e}")

@bot.event
async def on_ready():
    print(f"🟢 Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    hourly_knight_report.start()
    orion_daily_report.start()
    daily_trade_log_upload.start()
    weekly_scroll_summary.start()

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")