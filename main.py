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
from ta.momentum import rsi, stochrsi, williams_r
from ta.trend import ema_indicator, sma_indicator, adx, cci
from ta.volatility import average_true_range, bollinger_hband, bollinger_lband, keltner_channel_hband, keltner_channel_lband
from ta.volume import on_balance_volume

# === Timezones ===
CENTRAL_TZ = pytz.timezone("US/Central")
UTC_TZ = datetime.timezone.utc

# === Environment ===
load_dotenv()
TOKEN = os.getenv("TOKEN")

# === Bot State ===
bot_mode = "aggressive"  # Default mode can be toggled with !mode command
KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD",
    "ETH": "XETHZUSD",
    "SOL": "SOLUSD",
    "AVAX": "AVAXUSD",
    "ADA": "ADAUSD",
    "HBAR": "HBARUSD"
}

active_alerts = {}  # Currently active trades
cooldowns = {}       # Cooldowns to avoid repeated alerts
leaderboard_stats = {sym: {"wins": 0, "losses": 0} for sym in KRAKEN_PAIRS}  # Daily stats

# === Flask Uptime ===
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

# === Discord Setup ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)
CHANNEL_ID = 1398690647417819198
STATUS_CHANNEL_ID = 1398691425347961016

# === Time Utilities ===
def now_times():
    utc_dt = datetime.datetime.now(UTC_TZ)
    central_dt = utc_dt.astimezone(CENTRAL_TZ)
    return utc_dt, central_dt

def fmt_central(dt):
    return dt.strftime("%Y-%m-%d %I:%M %p %Z")

def fmt_utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# === Logic Placeholder ===
# ========================
def calculate_indicators(df):
    # === Core Indicators ===
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

    # === Chaikin Money Flow (CMF) ===
    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (df["high"] - df["low"] + 1e-9) * df["volume"]
    df["cmf"] = mfv.rolling(window=20).sum() / df["volume"].rolling(window=20).sum()

    # === Supertrend (Basic Implementation) ===
    atr = df["atr"]
    hl2 = (df["high"] + df["low"]) / 2
    factor = 3  # Supertrend multiplier
    df["upperband"] = hl2 + (factor * atr)
    df["lowerband"] = hl2 - (factor * atr)
    df["supertrend"] = True  # default True for bullish

    in_uptrend = [True]
    for i in range(1, len(df)):
        if df["close"].iloc[i] > df["upperband"].iloc[i - 1]:
            in_uptrend.append(True)
        elif df["close"].iloc[i] < df["lowerband"].iloc[i - 1]:
            in_uptrend.append(False)
        else:
            in_uptrend.append(in_uptrend[-1])

    df["supertrend"] = in_uptrend

    # === Alligator (Simple Teeth, Lips, Jaw)
    df["jaw"] = ema_indicator(df["close"], window=13).shift(8)
    df["teeth"] = ema_indicator(df["close"], window=8).shift(5)
    df["lips"] = ema_indicator(df["close"], window=5).shift(3)
    df["alligator"] = (df["lips"] > df["teeth"]) & (df["teeth"] > df["jaw"])

    # === Ichimoku Cloud (Bullish if price above span A & B)
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

    # === Kumo Twist Alert (Span A crosses Span B)
    twist = (senkou_span_a - senkou_span_b).diff().abs() < 1e-3
    df["twist"] = twist.fillna(False)

    return df



def detect_trade(df, mode="aggressive"):
    latest = df.iloc[-1]
    atr_avg = df["atr"].rolling(window=50).mean().iloc[-1]
    matches = []
    confidence = 0

    # === Knight Assignment ===
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

    # === No Match Fallback ===
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
                "knight": assign_knight("🟡 Weak Signal", [])
            }]
        return []

    # === Generate Trade Objects ===
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

        knight = assign_knight(match_text, ["rsi", "ema50", "supertrend", "alligator", "ichimoku_bull", "twist"])

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

def log_trade_to_csv(trade_data):
    filename = f"trade_log_{datetime.datetime.now(UTC_TZ).strftime('%Y-%m-%d')}.csv"
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
    trade_data["weak_signal"],
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

    knight = trade.get("knight", "🧙 Unknown")  # ✅ Proper indentation

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

# === Commands Placeholder ===
# ===========================
@commands.command()
async def ethreport(ctx):
    df = fetch_ohlc("ETH")
    if df is None:
        await ctx.send("❌ Could not fetch ETH data.")
        return

    df = calculate_indicators(df)
    latest = df.iloc[-1]

    header = fmt_central(datetime.datetime.now(pytz.timezone("US/Central")))
    footer = fmt_utc(datetime.datetime.now(UTC_TZ))
    trend_text = "📈 Bullish (EMA50)" if latest["close"] > latest["ema50"] else "📉 Bearish (EMA50)"
    supertrend_text = "🟢 Bullish" if latest.get("supertrend") else "🔴 Bearish"
    alligator_text = "🟢 Bullish" if latest.get("alligator") else "🔴 Bearish"
    ichimoku_text = "🟢 Bullish" if latest.get("ichimoku_bull") else "🔴 Bearish"
    twist_text = "✅ Twist" if latest.get("twist") else "No twist"
    bias = "🟢 Bullish Bias" if latest["rsi"] > 60 and latest["cmf"] > 0 else "🔴 Bearish Bias" if latest["rsi"] < 40 and latest["cmf"] < 0 else "⚠️ Neutral Bias"

    embed = discord.Embed(title=f"📊 ETH 30-Min Status – {header}", color=discord.Color.blue())
    embed.add_field(name="💵 Price & Trend", value=(f"💰 Price: **${latest['close']:.2f}**\n"
                                                    f"📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`\n"
                                                    f"{trend_text}"), inline=False)
    embed.add_field(name="📈 Indicator Summary", value=(f"🧠 Supertrend: {supertrend_text}\n"
                                                         f"🐊 Alligator: {alligator_text}\n"
                                                         f"☁️ Ichimoku: {ichimoku_text}\n"
                                                         f"🌪️ Twist Alert: {twist_text}"), inline=False)
    embed.add_field(name="📊 Market Bias", value=bias, inline=False)
    embed.set_footer(text=f"Updated {footer}")
    await ctx.send(embed=embed)

@commands.command()
async def testeth(ctx):
    df = fetch_ohlc("ETH")
    if df is None:
        await ctx.send("❌ Failed to fetch ETH data.")
        return
    df = calculate_indicators(df)
    trade = detect_trade(df)
    if trade:
        embed = format_embed("ETH", trade)
        await ctx.send("✅ ETH trade detected:")
        await ctx.send(embed=embed)
    else:
        await ctx.send("⚠️ No valid ETH trade setup found at the moment.")

@commands.command()
async def mode(ctx, selected_mode: str):
    from main import bot_mode
    if selected_mode.lower() in ["strict", "aggressive"]:
        bot_mode = selected_mode.lower()
        await ctx.send(f"✅ Bot mode set to: **{bot_mode.upper()}**")
    else:
        await ctx.send("⚠️ Invalid mode. Use `!mode strict` or `!mode aggressive`.")

@commands.command()
async def strategies(ctx):
    embed = discord.Embed(title="📘 Available Trade Strategies", color=discord.Color.teal())
    embed.add_field(name="🔁 Mean Reversion", value="RSI < 30 and Williams %R < -80\nConfidence: 4️⃣", inline=False)
    embed.add_field(name="🚀 Breakout Anticipation", value="Price > Donchian High and CMF > 0\nConfidence: 5️⃣", inline=False)
    embed.add_field(name="📊 Volatility Squeeze", value="Bollinger inside Keltner + BB Width > 5%\nConfidence: 3️⃣", inline=False)
    embed.add_field(name="🌀 Swing Trade", value="CCI > 100 and CMF > 0\nConfidence: 4️⃣", inline=False)
    embed.add_field(name="📈 Pullback Long", value="RSI > 50 and Price > EMA50\nConfidence: 3️⃣", inline=False)
    await ctx.send(embed=embed)

@commands.command()
async def logsummary(ctx):
    date_str = datetime.datetime.now(UTC_TZ).strftime("%Y-%m-%d")
    filename = f"trade_log_{date_str}.csv"

    if not os.path.exists(filename):
        await ctx.send("📭 No trades logged yet today.")
        return

    df = pd.read_csv(filename)
    total = len(df)
    weak_signals = df[df["WeakSignal"] == "Yes"]
    weak_count = len(weak_signals)
    strong_count = total - weak_count
    percent_weak = round((weak_count / total) * 100) if total > 0 else 0

    embed = discord.Embed(title="📊 Trade Log Summary", color=discord.Color.purple())
    embed.add_field(name="📅 Date", value=date_str, inline=False)
    embed.add_field(name="📦 Total Trades", value=total, inline=True)
    embed.add_field(name="🟡 Weak Signals", value=f"{weak_count} ({percent_weak}%)", inline=True)
    embed.add_field(name="🧠 Strong Signals", value=strong_count, inline=True)
    embed.set_footer(text=f"Updated {fmt_utc(datetime.datetime.now(UTC_TZ))}")
    await ctx.send(embed=embed)

@commands.command()
async def leaderboard(ctx):
    from main import leaderboard_stats
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

# === Tasks Placeholder ===
# ========================
# === ETH 30-Min Status Report ===
@tasks.loop(minutes=1)
async def eth_status_report():
    now = datetime.datetime.now(CENTRAL_TZ)

    if now.minute in (0, 30):
        print(f"[DEBUG] Triggering ETH 30-min report at {now.strftime('%Y-%m-%d %H:%M:%S')} CT")

        channel = bot.get_channel(STATUS_CHANNEL_ID)
        if channel is None:
            print(f"[ERROR] Status channel {STATUS_CHANNEL_ID} not found. Skipping report.")
            return

        df = fetch_ohlc("ETH")
        if df is None:
            await channel.send("❌ Could not fetch ETH data.")
            print("[ERROR] Failed to fetch ETH OHLC data.")
            return

        df = calculate_indicators(df)
        latest = df.iloc[-2]  # Use closed candle
        header = fmt_central(now)
        footer = fmt_utc(datetime.datetime.now(UTC_TZ))

        trend_text = "📈 Bullish (EMA50)" if latest["close"] > latest["ema50"] else "📉 Bearish (EMA50)"
        supertrend_text = "🟢 Bullish" if latest.get("supertrend") else "🔴 Bearish"
        alligator_text = "🟢 Bullish" if latest.get("alligator") else "🔴 Bearish"
        ichimoku_text = "🟢 Bullish" if latest.get("ichimoku_bull") else "🔴 Bearish"
        twist_text = "✅ Twist" if latest.get("twist") else "No twist"
        bias = calculate_market_bias(latest)

        embed = discord.Embed(
            title=f"📊 ETH 30-Min Status – {header}",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="💵 Price & Trend",
            value=(
                f"💰 Price: **${latest['close']:.2f}**\n"
                f"📉 RSI: `{latest['rsi']:.1f}` | 📏 ATR: `{latest['atr']:.2f}`\n"
                f"{trend_text}"
            ),
            inline=False
        )
        embed.add_field(
            name="📈 Indicator Summary",
            value=(
                f"🧠 Supertrend: {supertrend_text}\n"
                f"🐊 Alligator: {alligator_text}\n"
                f"☁️ Ichimoku: {ichimoku_text}\n"
                f"🌪️ Twist Alert: {twist_text}"
            ),
            inline=False
        )
        embed.add_field(name="📊 Market Bias", value=bias, inline=False)
        embed.set_footer(text=f"Updated {footer}")

        try:
            await channel.send(embed=embed)
            print(f"[DEBUG] ETH 30-min report sent at {footer}")
        except Exception as e:
            print(f"[ERROR] Failed to send ETH report: {e}")

# === Real-Time Scanner ===
@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    now_utc, now_central = now_times()

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        df = calculate_indicators(df)
        latest = df.iloc[-2]  # Use the most recent *closed* candle
        price = latest["close"]
        candle_high = latest["high"]
        candle_low = latest["low"]

        # === Check for Open Trade ===
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

                await channel.send(embed=embed)
                del active_alerts[symbol]
            continue

        # === Cooldown Check ===
        last_time = cooldowns.get(symbol)
        if last_time and (now_utc - last_time).total_seconds() < 1800:
            continue

        # === Detect New Trade ===
        trades = detect_trade(df, mode=bot_mode)

        if not trades or not isinstance(trades, list):
            continue  # Skip invalid or empty trades

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
            await channel.send(embed=embed)

            # Store active alert
            active_alerts[symbol] = (
                trade["entry"], trade["tp1"], trade["tp2"],
                trade["stop"], now_utc, trade["type"]
            )

            print(f"[DEBUG] 🚨 New Trade Set: {symbol} | Entry: {trade['entry']:.2f}, TP2: {trade['tp2']:.2f}, Stop: {trade['stop']:.2f}")

            cooldowns[symbol] = now_utc

# === Slash-style Command Support ===
@bot.command()
async def ethreport(ctx):
    """Manually triggers the ETH status report."""
    now = datetime.datetime.now(CENTRAL_TZ)
    channel = ctx.channel

    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ Could not fetch ETH data.")
        return

    df = calculate_indicators(df)
    latest = df.iloc[-2]  # Use closed candle
    header = fmt_central(now)
    footer = fmt_utc(datetime.datetime.now(UTC_TZ))

    embed = format_eth_report(df, latest, header, footer)
    await channel.send(embed=embed)

@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong!")

@bot.command()
async def mode(ctx):
    global bot_mode
    bot_mode = "strict" if bot_mode == "aggressive" else "aggressive"
    await ctx.send(f"🧠 Bot mode set to: **{bot_mode.capitalize()}**")

# === Bot Ready ===
@bot.event
async def on_ready():
    print(f"\u2705 Bot is online as {bot.user}")
    scan_coins.start()
    eth_status_report.start()
    
# === Run Bot ===
if TOKEN:
    bot.run(TOKEN)
else:
   print("❌ TOKEN not found.")