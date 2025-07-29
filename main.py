# ========================================
# Templar Knight Crypto Bot – bot.py (Single-File Deployment)
# ========================================

import os
import csv
import threading
import datetime
import pytz
import requests
import pandas as pd
import numpy as np
import discord
from flask import Flask
from discord.ext import commands, tasks
from discord import File
from dotenv import load_dotenv
from ta.trend import ema_indicator, sma_indicator, macd, macd_signal
from ta.momentum import rsi, stochrsi
from ta.volume import on_balance_volume
from ta.volatility import (
    average_true_range, bollinger_hband, bollinger_lband,
    keltner_channel_hband, keltner_channel_lband
)

# === Load Environment Variables ===
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# === Timezones ===
CENTRAL_TZ = pytz.timezone("US/Central")
UTC_TZ = datetime.timezone.utc

# === Discord Bot Initialization ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# === Flask App for Render Uptime ===
app = Flask(__name__)
@app.route('/')
def home():
    return "Templar Knight Crypto Bot is running!"

# === Channel Assignments (Update as needed) ===
BATTLE_SIGNALS_CHANNEL_ID = 1399532925279666278
EAGLE_SIGNAL_CHANNEL_ID = 1398690647417819198
SCRIBE_KEEP_CHANNEL_ID = 1398691425347961016

# === Global Variables ===
KRKN_API_URL = "https://api.kraken.com/0/public/OHLC"
cooldowns = {}
active_trades = {}
ALL_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "AVAXUSD", "ADAUSD", "HBARUSD"]
ETH_SYMBOL = "ETHUSD"

# === File Paths ===
TRADE_LOG_FILE = "trade_log.csv"
ORION_LOG_FILE = "orion_log.csv"
LEADERBOARD_FILE = "leaderboard.csv"

# ========================================
# Indicator Calculations
# ========================================

def calculate_supertrend(df, period=10, multiplier=3):
    atr = average_true_range(df['high'], df['low'], df['close'], window=period)
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    supertrend = [True] * len(df)

    for i in range(1, len(df)):
        if df['close'][i] > upperband[i - 1]:
            supertrend[i] = True
        elif df['close'][i] < lowerband[i - 1]:
            supertrend[i] = False
        else:
            supertrend[i] = supertrend[i - 1]
            if supertrend[i] and lowerband[i] < lowerband[i - 1]:
                lowerband[i] = lowerband[i - 1]
            if not supertrend[i] and upperband[i] > upperband[i - 1]:
                upperband[i] = upperband[i - 1]

    return pd.Series(supertrend, index=df.index)

def calculate_alligator(df):
    jaw = sma_indicator(df['close'], window=13).shift(8)
    teeth = sma_indicator(df['close'], window=8).shift(5)
    lips = sma_indicator(df['close'], window=5).shift(3)
    return jaw, teeth, lips

def calculate_ichimoku(df):
    nine_high = df['high'].rolling(window=9).max()
    nine_low = df['low'].rolling(window=9).min()
    period26_high = df['high'].rolling(window=26).max()
    period26_low = df['low'].rolling(window=26).min()
    period52_high = df['high'].rolling(window=52).max()
    period52_low = df['low'].rolling(window=52).min()

    tenkan_sen = (nine_high + nine_low) / 2
    kijun_sen = (period26_high + period26_low) / 2
    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
    senkou_span_b = ((period52_high + period52_low) / 2).shift(26)
    chikou_span = df['close'].shift(-26)

    return tenkan_sen, kijun_sen, senkou_span_a, senkou_span_b, chikou_span

# ========================================
# Trade Detection Logic
# ========================================

def detect_trade(df, symbol):
    last = df.iloc[-1]
    previous = df.iloc[-2]
    close = df['close']

    ema50 = ema_indicator(close, window=50)
    ema200 = ema_indicator(close, window=200)
    rsi_val = rsi(close, window=14)
    macd_line = macd(close)
    macd_sig = macd_signal(close)
    obv = on_balance_volume(close, df['volume'])
    supertrend = calculate_supertrend(df)
    jaw, teeth, lips = calculate_alligator(df)
    tenkan, kijun, span_a, span_b, chikou = calculate_ichimoku(df)
    squeeze = detect_squeeze(df)
    cmf = calculate_cmf(df)

    signal = None
    confidence = 0
    knight = None
    pattern = None

    if (
        macd_line.iloc[-1] > macd_sig.iloc[-1] and
        span_a.iloc[-1] > span_b.iloc[-1] and
        df['volume'].iloc[-1] > df['volume'].rolling(20).mean().iloc[-1]
    ):
        signal = "Breakout Long"
        confidence += 3
        knight = "Sir Leonis Ironhart"

    elif (
        rsi_val.iloc[-1] < 40 and
        close.iloc[-1] > ema50.iloc[-1]
    ):
        signal = "Pullback Long"
        confidence += 2
        knight = "Sir Lucien Frostveil"

    elif (
        macd_line.iloc[-1] < macd_sig.iloc[-1] and
        span_a.iloc[-1] < span_b.iloc[-1] and
        obv.iloc[-1] < obv.iloc[-5]
    ):
        signal = "Breakout Short"
        confidence += 3
        knight = "Sir Leonis Ironhart"

    elif (
        rsi_val.iloc[-1] > 60 and
        close.iloc[-1] < ema50.iloc[-1]
    ):
        signal = "Pullback Short"
        confidence += 2
        knight = "Sir Lucien Frostveil"

    elif (
        macd_line.iloc[-1] > macd_sig.iloc[-1] and
        macd_line.iloc[-2] < macd_sig.iloc[-2]
    ):
        signal = "Trend Reversal"
        confidence += 2
        knight = "Orion Vellum"

    elif (
        obv.iloc[-1] < obv.iloc[-10] and
        close.iloc[-1] < span_b.iloc[-1]
    ):
        signal = "Macro Bear Bias"
        confidence += 2
        knight = "Orion Vellum"

    if supertrend.iloc[-1]:
        confidence += 1
    if squeeze.iloc[-1]:
        confidence += 1
    if cmf.iloc[-1] > 0:
        confidence += 1

    if signal:
        return {
            "symbol": symbol,
            "signal": signal,
            "confidence": confidence,
            "knight": knight
        }

    return None

# ========================================
# Discord Embed Formatting
# ========================================

def get_knight_quote(knight, signal):
    if knight == "Sir Leonis Ironhart":
        return "⚔️ Leonis roars: “Chart. Conquer. Repeat.”"
    elif knight == "Sir Lucien Frostveil":
        return "🛡️ Lucien warns: “Steady hands forge steady futures.”"
    elif knight == "Orion Vellum":
        if "Long" in signal:
            return "🌘 Orion whispers: “Greed stirs, but caution watches still.”"
        elif "Short" in signal:
            return "🌘 Orion murmurs: “The moon retreats. Shadows grow where greed once stood.”"
        elif "Reversal" in signal:
            return "🌘 Orion writes: “Cycles churn in silence. A turning of fate may loom.”"
        elif "Macro" in signal:
            return "🌘 Orion scrawls: “A colder tide has turned. Expect little, fear none.”"
    return "⚔️ Templar Signal Fired"

def create_trade_embed(data, price, timestamp, timeframe="30m", pattern=None):
    ct_time = timestamp.astimezone(CENTRAL_TZ).strftime("%b %d • %I:%M %p CT")
    utc_time = timestamp.strftime("%H:%M UTC")

    signal = data['signal']
    knight = data['knight']
    emoji = "🟢" if "Long" in signal else "🔴" if "Short" in signal else "🌘"

    embed = discord.Embed(
        title=f"{emoji} {signal} Alert – {data['symbol']}",
        description=f"**Timeframe**: {timeframe}  |  **Price**: `${price}`",
        color=0x2ecc71 if "Long" in signal else 0xe74c3c if "Short" in signal else 0x95a5a6
    )
    embed.set_author(name=knight)
    embed.add_field(name="Confidence", value=f"{data['confidence']} / 6", inline=True)
    if pattern:
        embed.add_field(name="Candle Pattern", value=pattern, inline=True)
    embed.add_field(name="Knight Quote", value=get_knight_quote(knight, signal), inline=False)
    embed.set_footer(text=f"Report generated • {utc_time}")

    return embed

def create_orion_daily_embed(summary, timestamp):
    ct_time = timestamp.astimezone(CENTRAL_TZ).strftime("%b %d • %I:%M %p CT")
    utc_time = timestamp.strftime("%H:%M UTC")

    embed = discord.Embed(
        title="🌘 Orion Vellum’s Daily Whisper",
        description=f"Summary for {ct_time}",
        color=0x8e44ad
    )
    embed.add_field(name="Alerts Issued", value=summary['alerts'], inline=True)
    embed.add_field(name="Wins / Losses", value=summary['wins'], inline=True)
    embed.add_field(name="Expectations", value=summary['expect'], inline=False)
    embed.set_footer(text=f"Whisper ends • {utc_time}")
    return embed

# ========================================
# Utility Functions: Logging, Pattern Detection, Leaderboard
# ========================================

# === Leaderboard Updater ===
def update_leaderboard(symbol, outcome, knight):
    today = datetime.datetime.now(pytz.UTC).date()

    if os.path.exists(LEADERBOARD_FILE):
        df = pd.read_csv(LEADERBOARD_FILE)
    else:
        df = pd.DataFrame(columns=["date", "knight", "wins", "losses"])

    match = (df["date"] == str(today)) & (df["knight"] == knight)
    if not match.any():
        new_row = pd.DataFrame([{
            "date": str(today),
            "knight": knight,
            "wins": 1 if outcome == "TP2" else 0,
            "losses": 1 if outcome == "SL" else 0
        }])
        df = pd.concat([df, new_row], ignore_index=True)
    else:
        idx = df[match].index[0]
        if outcome == "TP2":
            df.at[idx, "wins"] += 1
        elif outcome == "SL":
            df.at[idx, "losses"] += 1

    df.to_csv(LEADERBOARD_FILE, index=False)

# === Log Trade Entry ===
def log_trade_entry(symbol, price, timestamp, trade_data, pattern=None):
    row = {
        "timestamp": timestamp.isoformat(),
        "symbol": symbol,
        "price": price,
        "signal": trade_data["signal"],
        "confidence": trade_data["confidence"],
        "knight": trade_data["knight"],
        "pattern": pattern or "—"
    }

    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    if trade_data["knight"] == "Orion Vellum":
        orion_row = {
            "timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "signal": trade_data["signal"],
            "confidence": trade_data["confidence"],
            "result": "pending"
        }
        file_exists = os.path.isfile(ORION_LOG_FILE)
        with open(ORION_LOG_FILE, "a", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=orion_row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(orion_row)

# === Log Trade Exit ===
def log_trade_exit(symbol, price, timestamp, result):
    df = pd.read_csv(TRADE_LOG_FILE)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    latest = df[df["symbol"] == symbol].iloc[-1]
    knight = latest["knight"]

    if knight == "Orion Vellum" and os.path.exists(ORION_LOG_FILE):
        df_orion = pd.read_csv(ORION_LOG_FILE)
        df_orion["timestamp"] = pd.to_datetime(df_orion["timestamp"])
        mask = (df_orion["symbol"] == symbol) & (df_orion["result"] == "pending")
        if mask.any():
            df_orion.loc[mask.idxmax(), "result"] = result
            df_orion.to_csv(ORION_LOG_FILE, index=False)

    update_leaderboard(symbol, result, knight)

# === Candlestick Pattern Detection ===
def detect_candle_pattern(df):
    open_ = df['open'].iloc[-1]
    close = df['close'].iloc[-1]
    high = df['high'].iloc[-1]
    low = df['low'].iloc[-1]

    body = abs(close - open_)
    candle_range = high - low
    if body < candle_range * 0.1:
        return "Doji"
    elif close > open_ and (close - open_) > (high - low) * 0.6:
        return "Bullish Engulfing"
    elif open_ > close and (open_ - close) > (high - low) * 0.6:
        return "Bearish Engulfing"
    elif (high - max(open_, close)) > body and (min(open_, close) - low) < body * 0.3:
        return "Shooting Star"
    elif (min(open_, close) - low) > body and (high - max(open_, close)) < body * 0.3:
        return "Hammer"
    return None

# === Send End-of-Day Summary to Discord ===
async def log_summary_to_channel(channel_id):
    if not os.path.exists(TRADE_LOG_FILE):
        return

    df = pd.read_csv(TRADE_LOG_FILE)
    today = datetime.datetime.now(pytz.UTC).date()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df_today = df[df['timestamp'].dt.date == today]

    if df_today.empty:
        return

    counts = df_today["signal"].value_counts()
    summary_lines = [f"📊 **{today} Signal Summary:**"]
    for signal, count in counts.items():
        summary_lines.append(f"- {signal}: {count}")

    summary = "\n".join(summary_lines)
    await bot.get_channel(channel_id).send(summary)

# ========================================
# Real-Time Trade Scanning + TP/SL Detection
# ========================================

def fetch_ohlc(symbol, interval=30, since=None):
    params = {
        "pair": symbol,
        "interval": interval,
    }
    if since:
        params["since"] = since
    response = requests.get(KRKN_API_URL, params=params)
    result = response.json()["result"]
    key = list(result.keys())[0]
    data = result[key]
    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)
    df = df.astype(float)
    return df

def scan_coins(symbols, forced=False):
    now = datetime.datetime.now(pytz.UTC)

    for symbol in symbols:
        if not forced and cooldowns.get(symbol, None):
            if (now - cooldowns[symbol]).total_seconds() < 1800:
                continue

        df = fetch_ohlc(symbol, interval=30)
        if df is None or df.empty:
            continue

        trade = detect_trade(df, symbol)
        if trade:
            pattern = detect_candle_pattern(df)
            price = df['close'].iloc[-1]
            embed = create_trade_embed(trade, price, now, pattern=pattern)

            if symbol == "ETHUSD":
                bot.loop.create_task(
                    bot.get_channel(BATTLE_SIGNALS_CHANNEL_ID).send(embed=embed)
                )

            log_trade_entry(symbol, price, now, trade, pattern)
            cooldowns[symbol] = now
            active_trades[symbol] = {
                "entry": price,
                "timestamp": now,
                "signal": trade["signal"]
            }

        # Exit check
        if symbol in active_trades:
            entry_price = active_trades[symbol]["entry"]
            close_price = df['close'].iloc[-1]
            signal = active_trades[symbol]["signal"]

            tp2 = round(entry_price * 1.04, 2) if "Long" in signal else round(entry_price * 0.96, 2)
            sl = round(entry_price * 0.97, 2) if "Long" in signal else round(entry_price * 1.03, 2)

            hit_tp = close_price >= tp2 if "Long" in signal else close_price <= tp2
            hit_sl = close_price <= sl if "Long" in signal else close_price >= sl

            if hit_tp or hit_sl:
                outcome = "TP2" if hit_tp else "SL"
                log_trade_exit(symbol, close_price, now, outcome)
                del active_trades[symbol]

# ========================================
# Orion Vellum – Daily & Weekly Reports
# ========================================

def summarize_orion_daily():
    if not os.path.exists(ORION_LOG_FILE):
        return {"alerts": "0", "wins": "0/0", "expect": "—"}

    df = pd.read_csv(ORION_LOG_FILE)
    today = datetime.datetime.now(pytz.UTC).date()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df_today = df[df['timestamp'].dt.date == today]

    alerts = len(df_today)
    wins = len(df_today[df_today['result'] == "TP2"])
    losses = len(df_today[df_today['result'] == "SL"])

    if wins > losses:
        expectation = "Cautious optimism for upside signals."
    elif losses > wins:
        expectation = "Further downside bias expected."
    else:
        expectation = "Neutral to low-confidence setups likely."

    return {
        "alerts": str(alerts),
        "wins": f"{wins}/{alerts}",
        "expect": expectation
    }

async def run_orion_daily_report():
    now = datetime.datetime.now(pytz.UTC)
    summary = summarize_orion_daily()
    embed = create_orion_daily_embed(summary, now)
    channel = bot.get_channel(BATTLE_SIGNALS_CHANNEL_ID)
    await channel.send(embed=embed)

async def run_orion_weekly_report():
    if not os.path.exists(ORION_LOG_FILE):
        return

    df = pd.read_csv(ORION_LOG_FILE)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    one_week_ago = datetime.datetime.now(pytz.UTC) - datetime.timedelta(days=7)
    df_week = df[df['timestamp'] >= one_week_ago]

    csv_path = "orion_weekly_summary.csv"
    df_week.to_csv(csv_path, index=False)

    channel = bot.get_channel(BATTLE_SIGNALS_CHANNEL_ID)
    await channel.send("📜 Orion's Weekly Summary", file=File(csv_path))

# ========================================
# Discord Bot Commands, Tasks, and Scheduled Events
# ========================================

@bot.event
async def on_ready():
    print(f"🛡️ Bot is online: {bot.user.name}")
    scheduled_scan.start()
    daily_orion_report.start()
    daily_leaderboard_reset.start()
    end_of_day_summary.start()

# === Manual ETH Report Command ===
@bot.command(name='ethreport')
async def manual_eth_report(ctx):
    await ctx.send("Generating manual ETH report...")
    scan_coins(["ETHUSD"], forced=True)

# === Manual Symbol Test Command ===
@bot.command(name='test')
async def test_symbol(ctx, symbol: str):
    await ctx.send(f"Testing trade detection for `{symbol.upper()}`...")
    scan_coins([symbol.upper()], forced=True)

# === Leaderboard Command ===
@bot.command(name='leaderboard')
async def show_leaderboard(ctx):
    if not os.path.exists(LEADERBOARD_FILE):
        await ctx.send("No leaderboard data available.")
        return

    df = pd.read_csv(LEADERBOARD_FILE)
    today = str(datetime.datetime.now(pytz.UTC).date())
    df_today = df[df["date"] == today]

    if df_today.empty:
        await ctx.send("No trades logged today.")
        return

    lines = [f"🏆 **{today} Leaderboard**"]
    for _, row in df_today.iterrows():
        lines.append(f"🛡️ {row['knight']}: {row['wins']}W / {row['losses']}L")
    await ctx.send("\n".join(lines))

# === Scheduled Coin Scan (every 60 seconds) ===
@tasks.loop(seconds=60)
async def scheduled_scan():
    threading.Thread(target=scan_coins, args=(ALL_SYMBOLS,)).start()

# === Orion Daily Report (11:59 UTC) ===
@tasks.loop(time=datetime.time(hour=11, minute=59, tzinfo=pytz.UTC))
async def daily_orion_report():
    await run_orion_daily_report()

# === End of Day Trade Summary (23:59 UTC) ===
@tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=pytz.UTC))
async def end_of_day_summary():
    await log_summary_to_channel(EAGLE_SIGNAL_CHANNEL_ID)

# === Reset Leaderboard (00:00 UTC) ===
@tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=pytz.UTC))
async def daily_leaderboard_reset():
    if not os.path.exists(LEADERBOARD_FILE):
        return

    df = pd.read_csv(LEADERBOARD_FILE)
    today = str(datetime.datetime.now(pytz.UTC).date())
    df_today = df[df["date"] == today]

    if not df_today.empty:
        lines = [f"📜 Daily Leaderboard – {today}"]
        for _, row in df_today.iterrows():
            lines.append(f"⚔️ {row['knight']}: {row['wins']}W / {row['losses']}L")
        await bot.get_channel(SCRIBE_KEEP_CHANNEL_ID).send("\n".join(lines))

    archive_name = f"leaderboard_{today}.csv"
    os.rename(LEADERBOARD_FILE, archive_name)

# ========================================
# Run Flask Webserver and Discord Bot
# ========================================

if __name__ == "__main__":
    # Start Flask server in background thread (keeps Render instance alive)
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

    threading.Thread(target=run_flask).start()

    # Run Discord bot (blocking main thread)
    bot.run(TOKEN)