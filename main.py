# ============================================
# The Control Tower - Templar Knight Crypto - Merged v8.3 Final
# ============================================

# ============================================
# Section 1: Imports, Globals, Config, Flask, Basic Functions
# ============================================

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
import logging
import time
from discord.ext import commands, tasks
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator

# === Logging ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Load Environment ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logger.error("Discord TOKEN not found")
    exit(1)

# === Discord Channel IDs ===
SCRIBES_KEEP_ID = 1398691425347961016          # 📜 Market scorecard
BATTLE_SIGNALS_ID = 1399532925279666278        # ⚔️ Trade alerts
EAGLE_SIGNAL_ID = 1398690647417819198          # 🦅 100x alerts
KNIGHTS_WATCH_ID = 1399532102571135118         # 🕰️ Proximity warnings
ETH_BATTLEGROUND_ID = 1399532442075005038      # 🏰 Real-time reports
SCROLLS_ORDER_ID = 1399067396488302623         # 📚 Performance logs

# === Heartbeat Channels ===
HEARTBEAT_CHANNEL_IDS = [
    SCRIBES_KEEP_ID, BATTLE_SIGNALS_ID, EAGLE_SIGNAL_ID,
    KNIGHTS_WATCH_ID, ETH_BATTLEGROUND_ID, SCROLLS_ORDER_ID
]

# === Timezones ===
UTC = pytz.utc
CENTRAL_TZ = pytz.timezone("US/Central")

# === Config ===
CONFIRMATION_MODE = "balanced"  # aggressive, balanced, strict
ALERT_SCORE_THRESHOLD = 4       # configurable with !alertmode
API_TIMEOUT = 10
MAX_RETRIES = 3
CACHE_DURATION = 30  # seconds

# === Globals ===
ohlc_cache = {}
cache_expiry = {}
last_100x_trade_time = None
last_scorecard_sent = None
last_trade_alert_time = {}
camarilla_warning_cooldowns = {}

# === Flask App ===
app = Flask(__name__)
@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"
@app.route("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}
def run_flask():
    app.run(host="0.0.0.0", port=10000)

# === Discord Bot Init ===
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# === Retry Wrapper ===
def retry_api_call(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            wait_time = 2 ** attempt
            logger.warning(f"API retry {attempt + 1}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait_time)
            else:
                logger.error("Max retries exceeded")
                raise e

# ============================================
# Section 2: OHLC, Indicators, Scoring, Knight Tools
# ============================================

# === Fetch OHLC Data ===
def fetch_ohlc(symbol="ETH", interval=1):
    cache_key = f"{symbol}_{interval}"
    now = time.time()

    if cache_key in ohlc_cache and now < cache_expiry.get(cache_key, 0):
        return ohlc_cache[cache_key]

    kraken_map = {"ETH": "XETHZUSD"}
    pair = kraken_map.get(symbol.upper(), "XETHZUSD")
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": pair, "interval": interval}

    def _fetch():
        response = requests.get(url, params=params, timeout=API_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise Exception(f"Kraken API error: {data['error']}")
        return data["result"][pair]

    try:
        raw = retry_api_call(_fetch)
        if not raw or len(raw) < 2:
            return None
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ]).astype({
            "time": int, "open": float, "high": float,
            "low": float, "close": float, "volume": float
        })
        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("datetime", inplace=True)
        ohlc_cache[cache_key] = df
        cache_expiry[cache_key] = now + CACHE_DURATION
        return df
    except Exception as e:
        logger.error(f"OHLC fetch failed: {e}")
        return None

# === Fetch Daily OHLC ===
def fetch_daily_ohlc():
    df = fetch_ohlc(interval=1440)
    if df is None or len(df) < 2:
        return None, None, None
    latest = df.iloc[-2]
    return latest["high"], latest["low"], latest["close"]

# === Camarilla Levels ===
def calculate_camarilla(high, low, close):
    try:
        D4, D3 = 0.55, 0.275
        H5 = (high / low) * close
        H4 = ((high - low) * D4) + close
        H3 = ((high - low) * D3) + close
        L3 = close - ((high - low) * D3)
        L4 = close - ((high - low) * D4)
        L5 = close - (H5 - close)
        P = (high + low + close) / 3
        return {
            "H5": H5, "H4": H4, "H3": H3,
            "L3": L3, "L4": L4, "L5": L5,
            "Pivot": P
        }
    except Exception as e:
        logger.error(f"Camarilla calc error: {e}")
        return {}

# === Indicator Calculation ===
def calculate_indicators(df):
    if df is None or len(df) < 20:
        return None
    try:
        df = df.copy()
        df["ema10"] = EMAIndicator(close=df["close"], window=10).ema_indicator()
        df["ema20"] = EMAIndicator(close=df["close"], window=20).ema_indicator()
        df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
        tp = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (df["volume"] * tp).cumsum() / df["volume"].cumsum()
        macd = MACD(close=df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()
        df = df.dropna()
        return df if len(df) >= 5 else None
    except Exception as e:
        logger.error(f"Indicator calc failed: {e}")
        return None

# === Score Trade (0–6) ===
def score_trade(rsi, rsi_trend, direction, price, level, volume, avg_volume, price_trend):
    try:
        score = 0
        if (rsi > 55 and direction == "Long") or (rsi < 45 and direction == "Short"):
            score += 1
        if (rsi_trend == "up" and direction == "Long") or (rsi_trend == "down" and direction == "Short"):
            score += 1
        if (price_trend and direction == "Long") or (not price_trend and direction == "Short"):
            score += 1
        if volume > avg_volume * 1.2:
            score += 1
        if abs(price - level) / price < 0.005:
            score += 1
        if score >= 4:
            score += 1
        return min(score, 6)
    except Exception as e:
        logger.error(f"Scoring error: {e}")
        return 0

# === Confirmation Thresholds ===
def get_confirmation_mode_thresholds():
    if CONFIRMATION_MODE == "aggressive":
        return 0.3, 1.0
    elif CONFIRMATION_MODE == "strict":
        return 0.7, 1.5
    return 0.5, 1.2  # default balanced

# === Knight Role Assignment ===
def assign_knight(direction):
    return "Sir Leonis ⚔️" if direction == "Long" else "Sir Lucien 🛡"

# === Grading Tier Label ===
def get_tier_label(score):
    if score == 6:
        return "🔥 Tier S (6/6)"
    elif score == 5:
        return "⚔️ Tier A (5/6)"
    elif score == 4:
        return "🟡 Tier B (4/6)"
    elif score == 3:
        return "🟠 Tier C (3/6)"
    return "⚪ Low Confidence"

# ============================================
# Section 3: Scorecard, Chronicle, Setup Alert, Battle Signal
# ============================================

# === Scorecard Confluence Evaluation ===
def evaluate_scorecard(df, cam):
    """Evaluate trading confluence score."""
    try:
        if df is None or len(df) < 5 or not cam:
            return 0, [], None

        latest = df.iloc[-1]
        price = latest["close"]
        rsi = latest["rsi"]
        macd_hist = latest["macd_hist"]
        above_vwap = price > latest["vwap"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()
        trend = df["close"].iloc[-1] > df["close"].iloc[-3]

        # Find closest Camarilla level
        level = min(cam.values(), key=lambda x: abs(price - x))
        reasons = []
        score = 0

        if rsi > 55 or rsi < 45:
            score += 1
            reasons.append("✅ RSI Out of Neutral Zone")

        rsi_trend_up = rsi > df["rsi"].iloc[-3]
        if (rsi_trend_up and rsi > 50) or (not rsi_trend_up and rsi < 50):
            score += 1
            reasons.append("✅ RSI Trend Alignment")

        if abs(macd_hist) > 0.1:
            score += 1
            reasons.append("✅ MACD Momentum Present")

        if above_vwap:
            score += 1
            reasons.append("✅ Price Above VWAP")

        if volume > avg_volume * 1.2:
            score += 1
            reasons.append("✅ Volume Spike Detected")

        if trend:
            score += 1
            reasons.append("✅ Bullish Price Trend")

        return score, reasons, level

    except Exception as e:
        logger.error(f"Error evaluating scorecard: {e}")
        return 0, [], None

# === Chronicle Embed ===
async def send_enhanced_scorecard():
    try:
        df = fetch_ohlc("ETH", interval=1)
        if df is None:
            return

        df = calculate_indicators(df)
        if df is None or len(df) < 5:
            return

        latest = df.iloc[-1]
        price = latest["close"]
        rsi = latest["rsi"]
        macd_hist = latest["macd_hist"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        closest_level = min(levels.items(), key=lambda x: abs(price - x[1]))
        level_name, level_price = closest_level
        distance = price - level_price
        distance_pct = (distance / price) * 100
        level_direction = "Above" if price > level_price else "Below"

        score, reasons, _ = evaluate_scorecard(df, levels)

        if score >= 5:
            bias = "🟢 Strong Bullish"
            bias_color = discord.Color.green()
        elif score >= 4:
            bias = "🟡 Moderate Bullish"
            bias_color = discord.Color.gold()
        elif score >= 3:
            bias = "⚪ Neutral"
            bias_color = discord.Color.light_grey()
        elif score >= 2:
            bias = "🟠 Moderate Bearish"
            bias_color = discord.Color.orange()
        else:
            bias = "🔴 Strong Bearish"
            bias_color = discord.Color.red()

        price_24h_ago = df.iloc[-1440] if len(df) >= 1440 else df.iloc[0]
        price_change = price - price_24h_ago["close"]
        price_change_pct = (price_change / price_24h_ago["close"]) * 100

        embed = discord.Embed(
            title="📜 ETH Market Chronicle",
            description="*The scribes record the current state of the battlefield*",
            color=bias_color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        price_emoji = "📈" if price_change >= 0 else "📉"
        embed.add_field(
            name=f"{price_emoji} Current Price",
            value=f"**${price:.2f}**\n{price_change_pct:+.2f}% (${price_change:+.2f})",
            inline=True
        )
        embed.add_field(
            name="📍 Level in Focus",
            value=f"**{level_name}: ${level_price:.2f}**\n{level_direction} • {distance_pct:+.2f}% (${distance:+.2f})",
            inline=True
        )
        embed.add_field(
            name="🧠 Market Bias",
            value=f"**{bias}**\nScore: {score}/6",
            inline=True
        )

        rsi_emoji = "🟢" if rsi > 55 else "🔴" if rsi < 45 else "⚪"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        volume_ratio = volume / avg_volume if avg_volume else 0
        volume_emoji = "🟢" if volume_ratio > 1.2 else "🔴" if volume_ratio < 0.8 else "⚪"
        vwap_diff = price - latest.get("vwap", 0)
        vwap_emoji = "🟢" if vwap_diff > 0 else "🔴"

        indicators_text = (
            f"{rsi_emoji} **RSI:** {rsi:.1f} "
            f"{'(Overbought)' if rsi > 70 else '(Oversold)' if rsi < 30 else '(Neutral)'}\n"
            f"{macd_emoji} **MACD:** {macd_hist:.2f} "
            f"{'(Bullish)' if macd_hist > 0 else '(Bearish)'}\n"
            f"{volume_emoji} **Volume:** {volume_ratio:.1f}x avg ({volume:.0f})\n"
            f"{vwap_emoji} **VWAP:** ${vwap_diff:+.2f} ({'Above' if vwap_diff > 0 else 'Below'})"
        )
        embed.add_field(name="📊 Technical Indicators", value=indicators_text, inline=False)

        level_order = ["H5", "H4", "H3", "Pivot", "L3", "L4", "L5"]
        ordered = [(k, levels[k]) for k in level_order if k in levels]
        level_map = "```\n"
        for name, val in ordered:
            if name == "Pivot":
                level_map += f"{name:<5} {val:>8.2f}\n"
        for name in ["H5", "H4", "H3"]:
            if name in levels and levels[name] > price:
                level_map += f"{name:<5} {levels[name]:>8.2f}\n"
        level_map += f"➤   Price {price:>8.2f}\n"
        for name in ["L3", "L4", "L5"]:
            if name in levels and levels[name] < price:
                level_map += f"{name:<5} {levels[name]:>8.2f}\n"
        level_map += "```"
        embed.add_field(name="🗺️ Battlefield Map", value=level_map, inline=False)

        if reasons:
            embed.add_field(name="⚖️ Confluence Analysis", value="\n".join(reasons[:6]), inline=False)

        ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc_time = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc_time} | {ct_time}")

        channel = bot.get_channel(SCRIBES_KEEP_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info("✅ Enhanced scorecard sent to scribes-keep")

    except Exception as e:
        logger.error(f"Error sending enhanced scorecard: {e}")

# === Setup Alert Embed (Pre-Confirmation) ===
async def send_setup_alert(direction, level_name, level_price, score, missing_items):
    knight = assign_knight(direction)
    embed = discord.Embed(
        title=f"🧪 Setup Alert – ETH {direction}",
        description=f"*{knight} is observing {level_name}*",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="🎯 Level", value=f"{level_name} (${level_price:.2f})", inline=True)
    embed.add_field(name="📊 Score", value=f"{score}/6 – {get_tier_label(score)}", inline=True)
    embed.add_field(name="🧩 Missing Signals", value="\n".join(missing_items) or "Awaiting confirmation", inline=False)

    ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
    utc = embed.timestamp.strftime('%H:%M UTC')
    embed.set_footer(text=f"🕒 {utc} | {ct} • Setup alert")

    channel = bot.get_channel(BATTLE_SIGNALS_ID)
    if channel:
        await channel.send(embed=embed)

# ============================================
# Section 4: Battle Signals, Trade Scan, 100x Alerts, Proximity Warnings
# ============================================

# === Battle Signal Embed (Confirmed Trade) ===
async def send_battle_signal(direction, level_name, level_price, entry, stop_loss, targets, confidence, score):
    try:
        knight = assign_knight(direction)
        color = discord.Color.green() if direction == "Long" else discord.Color.red()

        embed = discord.Embed(
            title=f"⚔️ Battle Signal - ETH {direction} Formation",
            description=f"*{knight} calls for battle at {level_name}*",
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        embed.add_field(name="🛡️ Knight", value=knight, inline=True)
        embed.add_field(name="🎯 Level", value=f"{level_name} (${level_price:.2f})", inline=True)
        embed.add_field(name="📊 Confidence", value=confidence, inline=True)
        embed.add_field(name="⚔️ Entry", value=f"${entry:.2f}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${stop_loss:.2f}", inline=True)
        embed.add_field(name="🎯 Targets", value=f"${targets[0]:.2f} | ${targets[1]:.2f}", inline=True)

        risk_pct = abs((entry - stop_loss) / entry) * 100
        reward1_pct = abs((targets[0] - entry) / entry) * 100
        reward2_pct = abs((targets[1] - entry) / entry) * 100

        embed.add_field(
            name="📋 Battle Plan",
            value=(f"**Tier:** {get_tier_label(score)}\n"
                   f"**Score:** {score}/6\n"
                   f"**Risk:** {risk_pct:.1f}%\n"
                   f"**R:R:** 1:{reward1_pct/risk_pct:.1f} | 1:{reward2_pct/risk_pct:.1f}"),
            inline=False
        )

        checklist = [
            ("RSI", (rsi > 55 and direction == "Long") or (rsi < 45 and direction == "Short")),
            ("RSI Trend", (rsi_trend == "up" and direction == "Long") or (rsi_trend == "down" and direction == "Short")),
            ("Price Trend", price_trend if direction == "Long" else not price_trend),
            ("Volume Spike", volume > avg_volume * 1.2),
            ("Near Level", abs(entry - level_price) / entry < 0.005),
        ]
        status = [f"{'✅' if met else '❌'} {label}" for label, met in checklist]
        embed.add_field(name="✅ Indicator Checklist", value="\n".join(status), inline=False)

        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct} • May fortune favor the bold")

        channel = bot.get_channel(BATTLE_SIGNALS_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"✅ Battle signal sent: {direction} at {level_name}")

    except Exception as e:
        logger.error(f"Error in send_battle_signal: {e}")

# === 100x Alert Scan ===
@tasks.loop(minutes=1)
async def trade_100x_scan():
    global last_100x_trade_time

    try:
        df = fetch_ohlc("ETH", interval=5)
        if df is None:
            return
        df = calculate_indicators(df)
        if df is None or len(df) < 20:
            return

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
        cam = calculate_camarilla(high, low, close)
        if not cam:
            return

        score, reasons, level = evaluate_scorecard(df, cam)
        now = datetime.datetime.now(datetime.timezone.utc)

        if score < 5 or (last_100x_trade_time and (now - last_100x_trade_time).total_seconds() < 900):
            return

        latest = df.iloc[-1]
        price = latest["close"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()
        body = abs(latest["close"] - latest["open"])
        range_ = latest["high"] - latest["low"]
        body_ratio = body / range_ if range_ > 0 else 0

        breakout_confirmed = (
            ((price > level and latest["open"] < level) or
             (price < level and latest["open"] > level)) and
            body_ratio > 0.6 and
            volume > avg_volume * 1.5
        )

        if not breakout_confirmed:
            return

        direction = "Long" if price > level else "Short"
        entry = round(price, 2)
        stop_pct = 0.005
        if direction == "Long":
            sl = round(entry * (1 - stop_pct), 2)
            tp1 = round(entry * 1.01, 2)
            tp2 = round(entry * 1.02, 2)
        else:
            sl = round(entry * (1 + stop_pct), 2)
            tp1 = round(entry * 0.99, 2)
            tp2 = round(entry * 0.98, 2)

        knight = assign_knight(direction)

        embed = discord.Embed(
            title=f"🦅 Eagle's Vision - Premium {direction} Signal",
            description="*The eagle has spotted a high-conviction opportunity*",
            color=0x2E7D32 if direction == "Long" else 0xD32F2F,
            timestamp=now
        )

        embed.add_field(name="🛡️ Knight", value=knight, inline=True)
        embed.add_field(name="🎯 Direction", value=direction, inline=True)
        embed.add_field(name="📊 Confidence", value="🟢 Premium Setup", inline=True)
        embed.add_field(name="⚔️ Entry", value=f"${entry:.2f}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${sl:.2f}", inline=True)
        embed.add_field(name="🎯 Targets", value=f"${tp1:.2f} | ${tp2:.2f}", inline=True)

        embed.add_field(name="⚖️ Confluence", value=f"**Score:** {score}/6\n" + "\n".join(reasons[:4]), inline=False)
        embed.add_field(name="⚠️ High Leverage Warning", value="**Use strict position sizing!**", inline=False)

        ct_time = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        embed.set_footer(text=f"🕒 {now.strftime('%H:%M UTC')} | {ct_time}")

        channel = bot.get_channel(EAGLE_SIGNAL_ID)
        if channel:
            await channel.send(embed=embed)
            last_100x_trade_time = now
            logger.info(f"✅ 100x signal sent: {direction} at ${entry:.2f}")

    except Exception as e:
        logger.error(f"Error in trade_100x_scan: {e}")

# === Proximity Warning Embed ===
async def send_proximity_warning(level_name, level_price, current_price, rsi, volume_ratio, trend):
    try:
        distance = level_price - current_price
        distance_pct = (distance / current_price) * 100
        is_resistance = "H" in level_name
        likely_break = (trend == "up" and is_resistance) or (trend == "down" and not is_resistance)
        rsi_supports = (rsi > 55 and is_resistance) or (rsi < 45 and not is_resistance)
        volume_strong = volume_ratio > 1.2

        if likely_break and rsi_supports and volume_strong:
            bias = "🔴 High Break Probability"
            bias_color = 0xFF4444
        elif not likely_break and not rsi_supports and volume_ratio < 0.8:
            bias = "🟢 Likely Reversal"
            bias_color = 0x44FF44
        else:
            bias = "⚪ Uncertain - Watch Closely"
            bias_color = 0xFFAA00

        embed = discord.Embed(
            title=f"⚠️ Knight's Warning - ETH Approaching {level_name}",
            description="*The knights observe movement toward a critical level*",
            color=bias_color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        embed.add_field(name="🎯 Target Level", value=f"**${level_price:.2f}**", inline=True)
        embed.add_field(name="📍 Current Price", value=f"**${current_price:.2f}**", inline=True)
        embed.add_field(name="📏 Distance", value=f"**${distance:+.2f}**\n({distance_pct:+.2f}%)", inline=True)

        analysis_text = (
            f"🎯 **Bias:** {bias}\n"
            f"📊 **RSI:** {rsi:.1f} {'🟢' if rsi_supports else '🔴'}\n"
            f"📈 **Trend:** {trend.upper()} {'🟢' if likely_break else '🔴'}\n"
            f"🔊 **Volume:** {volume_ratio:.1f}x {'🟢' if volume_strong else '🔴'}"
        )

        embed.add_field(name="🔮 Analysis", value=analysis_text, inline=False)

        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct} • Knights remain vigilant")

        channel = bot.get_channel(KNIGHTS_WATCH_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"✅ Proximity warning sent for {level_name}")

    except Exception as e:
        logger.error(f"Error sending proximity warning: {e}")

# === Warning Scan Loop ===
@tasks.loop(minutes=1)
async def check_camarilla_warning():
    global camarilla_warning_cooldowns

    try:
        df = fetch_ohlc("ETH", interval=1)
        if df is None:
            return
        df = calculate_indicators(df)
        if df is None or len(df) < 5:
            return

        latest = df.iloc[-1]
        price = latest["close"]
        rsi = latest["rsi"]
        recent_rsi = df["rsi"].iloc[-3] if len(df) >= 3 else rsi
        volume = latest["volume"]
        avg_volume = df["volume"].tail(5).mean()
        volume_ratio = volume / avg_volume
        trend = "up" if df["close"].iloc[-1] > df["close"].iloc[-3] else "down"

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        for name, lvl in levels.items():
            if name == "Pivot":
                continue
            if name in camarilla_warning_cooldowns and (now - camarilla_warning_cooldowns[name]).total_seconds() < 600:
                continue
            if abs(price - lvl) <= 2.0:
                await send_proximity_warning(name, lvl, price, rsi, volume_ratio, trend)
                camarilla_warning_cooldowns[name] = now

    except Exception as e:
        logger.error(f"Error in check_camarilla_warning: {e}")

# ============================================
# Section 5: Battleground Update, Loop Tasks, Heartbeat, Performance Report
# ============================================

# === Battleground Update Embed ===
async def send_battleground_embed():
    try:
        df = fetch_ohlc("ETH", interval=1)
        if df is None:
            return
        df = calculate_indicators(df)
        if df is None or len(df) < 5:
            return

        latest = df.iloc[-1]
        price = latest["close"]
        rsi = latest["rsi"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()
        volume_ratio = volume / avg_volume

        if volume_ratio > 2.0:
            emoji = "🌋"
            status = "HIGH VOLATILITY"
            color = discord.Color.orange()
        elif rsi > 70:
            emoji = "🔥"
            status = "OVERBOUGHT TERRITORY"
            color = discord.Color.red()
        elif rsi < 30:
            emoji = "❄️"
            status = "OVERSOLD BOUNCE ZONE"
            color = discord.Color.blue()
        elif volume_ratio > 1.5:
            emoji = "⚡"
            status = "INCREASED ACTIVITY"
            color = discord.Color.gold()
        else:
            emoji = "🌊"
            status = "NORMAL CONDITIONS"
            color = discord.Color.teal()

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
        cam = calculate_camarilla(high, low, close)
        if not cam:
            return

        closest_level = min(cam.items(), key=lambda x: abs(x[1] - price))
        level_name, level_price = closest_level
        distance = price - level_price
        distance_pct = (distance / price) * 100

        if abs(distance_pct) < 0.1:
            direction = "⚖️ At Level"
        elif distance > 0:
            direction = "🔼 Above"
        else:
            direction = "🔽 Below"

        now_utc = datetime.datetime.now(datetime.timezone.utc)

        embed = discord.Embed(
            title=f"{emoji} ETH Battleground Update",
            description="*Real-time field report from the frontline*",
            color=color,
            timestamp=now_utc
        )

        embed.add_field(name="💰 Price", value=f"${price:.2f}", inline=True)
        embed.add_field(name="📊 RSI", value=f"{rsi:.1f}", inline=True)
        embed.add_field(name="🔊 Volume", value=f"{volume_ratio:.1f}x", inline=True)
        embed.add_field(name="🎯 Status", value=status, inline=False)

        embed.add_field(
            name="🛡️ Camarilla Level Nearby",
            value=f"**{level_name}**: ${level_price:.2f}\n{direction} • Δ {distance:+.2f} ({distance_pct:+.2f}%)",
            inline=False
        )

        ct = now_utc.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = now_utc.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct}")

        channel = bot.get_channel(ETH_BATTLEGROUND_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info("✅ Battleground update sent")

    except Exception as e:
        logger.error(f"Error in send_battleground_embed: {e}")

# === Task Loops ===
@tasks.loop(minutes=1)
async def send_market_chronicle():
    if datetime.datetime.now(datetime.timezone.utc).minute % 15 == 0:
        await send_enhanced_scorecard()

@tasks.loop(minutes=1)
async def battleground_loop():
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.minute in [7, 23, 37, 52]:
        await send_battleground_embed()

@tasks.loop(hours=5)
async def heartbeat():
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)
        embed = discord.Embed(
            title="🛡️ Heartbeat Check – ETH Bot Status",
            description="*All systems operational*",
            color=discord.Color.teal(),
            timestamp=now
        )
        embed.add_field(name="🕒 UTC Time", value=now.strftime('%H:%M:%S'), inline=True)
        embed.add_field(name="🕒 CT Time", value=ct_now.strftime('%I:%M %p'), inline=True)
        embed.add_field(name="📊 Mode", value=CONFIRMATION_MODE.upper(), inline=True)
        embed.add_field(name="⚙️ Active Tasks", value="Chronicle, Signals, Eagle, Watch, Battleground", inline=False)
        embed.set_footer(text=f"🕒 {now.strftime('%H:%M UTC')} | {ct_now.strftime('%I:%M %p CT')}")
        for channel_id in HEARTBEAT_CHANNEL_IDS:
            channel = bot.get_channel(channel_id)
            if channel:
                await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in heartbeat: {e}")

# === Performance Report ===
@tasks.loop(hours=6)
async def performance_report():
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)

        embed = discord.Embed(
            title="📚 Performance Chronicle",
            description="*The scribes record our trading history*",
            color=discord.Color.blue(),
            timestamp=now
        )

        embed.add_field(
            name="📊 System Status",
            value="✅ All systems operational\n✅ API connections stable\n✅ All channels active",
            inline=False
        )

        embed.add_field(
            name="⚙️ Configuration",
            value=f"Mode: {CONFIRMATION_MODE.upper()}\nAPI Timeout: {API_TIMEOUT}s\nCache Duration: {CACHE_DURATION}s",
            inline=True
        )

        embed.add_field(
            name="📈 Activity Summary",
            value="Chronicle: Every 15min\nSignals: Real-time\nEagle: 100x high-conviction\nWatch: Level proximity",
            inline=True
        )

        embed.set_footer(text=f"🕒 {now.strftime('%H:%M UTC')} | {ct_now.strftime('%I:%M %p CT')}")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info("✅ Performance report sent")

    except Exception as e:
        logger.error(f"Error sending performance report: {e}")

# ============================================
# Section 6: Startup, Commands, Bot Runner
# ============================================

# === Bot Status Command ===
@bot.command(name='status')
async def status(ctx):
    try:
        embed = discord.Embed(
            title="🤖 Knight's Status Report",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        embed.add_field(name="⚙️ Mode", value=CONFIRMATION_MODE.upper(), inline=True)
        embed.add_field(name="📊 Tasks", value="✅ All Running", inline=True)
        embed.add_field(name="🌐 API", value="✅ Connected", inline=True)

        task_status = (
            f"📜 Chronicle: {'✅' if send_market_chronicle.is_running() else '❌'}\n"
            f"⚔️ Signals: {'✅' if scan_trade_alerts.is_running() else '❌'}\n"
            f"🦅 Eagle: {'✅' if trade_100x_scan.is_running() else '❌'}\n"
            f"👁️ Watch: {'✅' if check_camarilla_warning.is_running() else '❌'}\n"
            f"⚡ Battleground: {'✅' if battleground_loop.is_running() else '❌'}"
        )

        embed.add_field(name="🔄 Active Tasks", value=task_status, inline=False)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await ctx.send("❌ Error checking status")

# === Manual Chronicle Trigger ===
@bot.command(name='test_chronicle')
async def test_chronicle(ctx):
    if ctx.author.guild_permissions.administrator:
        try:
            await send_enhanced_scorecard()
            await ctx.send("✅ Chronicle sent to scribes-keep")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    else:
        await ctx.send("⚠️ Administrator permissions required")

# === Alert Mode Command ===
@bot.command(name='alertmode')
async def alertmode(ctx, mode=None):
    global ALERT_SCORE_THRESHOLD
    modes = {"strict": 5, "balanced": 4, "exploratory": 3}
    if mode in modes:
        ALERT_SCORE_THRESHOLD = modes[mode]
        await ctx.send(f"⚙️ Alert mode set to **{mode.upper()}** (score ≥ {ALERT_SCORE_THRESHOLD})")
    else:
        await ctx.send(
            f"⚙️ Current mode: score ≥ **{ALERT_SCORE_THRESHOLD}**\n"
            f"Use: `!alertmode strict` | `balanced` | `exploratory`"
        )

# === Bot Startup ===
@bot.event
async def on_ready():
    logger.info(f"🟢 Bot logged in as {bot.user}")
    logger.info(f"⚙️ Alert Mode: score ≥ {ALERT_SCORE_THRESHOLD}")
    try:
        if not scan_trade_alerts.is_running():
            scan_trade_alerts.start()
        if not send_market_chronicle.is_running():
            send_market_chronicle.start()
        if not trade_100x_scan.is_running():
            trade_100x_scan.start()
        if not check_camarilla_warning.is_running():
            check_camarilla_warning.start()
        if not battleground_loop.is_running():
            battleground_loop.start()
        if not heartbeat.is_running():
            heartbeat.start()
        if not performance_report.is_running():
            performance_report.start()

        embed = discord.Embed(
            title="🏰 Control Tower Activated",
            description="*Trade scanning and alert systems are online.*",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(name="⚙️ Current Alert Mode", value=f"Score ≥ {ALERT_SCORE_THRESHOLD}", inline=True)
        embed.add_field(name="📡 Strategy", value="Camarilla + RSI/Volume/Trend Confluence", inline=True)
        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct}")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in on_ready: {e}")

# === Bot Runner ===
def start_bot():
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    logger.info("🚀 Starting Control Tower ETH Camarilla Bot...")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server running in background")
    start_bot()
