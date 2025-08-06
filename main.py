# ============================================
# The Control Tower - Templar Knight Crypto - v8.7
# ============================================

# ============================================
# Section 1: Imports, Globals, Config, Flask, Bot Init
# ============================================

import os
import discord
import asyncio
import requests
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
from datetime import datetime, timezone, timedelta
import uuid

active_trades = {}  # format: {symbol: {entry, tp1, tp2, sl, side, knight, rating, thread_id, id}}

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
SETUP_ALERTS_ID = 1402053509490151424 	       # 🗺️・camarilla-alerts

# === Timezones ===
UTC = pytz.utc
CENTRAL_TZ = pytz.timezone("US/Central")

# === Configurable Settings ===
API_TIMEOUT = 10
MAX_RETRIES = 3
CACHE_DURATION = 30  # seconds

# === Global Variables ===
ohlc_cache = {}
cache_expiry = {}
last_100x_trade_time = None
last_scorecard_sent = None
last_trade_alert_time = {}
camarilla_warning_cooldowns = {}
CAMARILLA_COOLDOWN = {}
CAMARILLA_COOLDOWN_MINUTES = 5  # Cooldown per level/direction
setup_alert_cooldowns = {}
SETUP_ALERT_COOLDOWN_MINUTES = 15  # customize as needed

# === Flask App ===
app = Flask(__name__)
@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"
@app.route("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
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
# Section 2: OHLC, Indicators, Scoring Logic, Knight Role Assignment
# ============================================

# === Fetch OHLC Data with Caching ===
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

# === Fetch Daily OHLC for Camarilla Levels ===
def fetch_daily_ohlc():
    df = fetch_ohlc(interval=1440)
    if df is None or len(df) < 2:
        return None, None, None
    latest = df.iloc[-2]
    return latest["high"], latest["low"], latest["close"]

# === Camarilla Pivot Levels (v8.4 version) ===
def calculate_camarilla(high, low, close):
    try:
        range_ = high - low
        return {
            "L5": close - (range_ * 1.1 / 2),
            "L4": close - (range_ * 1.1 / 4),
            "L3": close - (range_ * 1.1 / 6),
            "P": close,
            "H3": close + (range_ * 1.1 / 6),
            "H4": close + (range_ * 1.1 / 4),
            "H5": close + (range_ * 1.1 / 2)
        }
    except Exception as e:
        logger.error(f"Camarilla calculation error: {e}")
        return {}

# === Indicator Calculation (Hybrid) ===
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
        df["volume_avg"] = df["volume"].rolling(10).mean()
        df = df.dropna()
        return df if len(df) >= 5 else None
    except Exception as e:
        logger.error(f"Indicator calculation error: {e}")
        return None

# === Scoring System (from v8.4) ===
def score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend):
    """Generate a 0–6 score based on conditions met."""
    score = 0
    if (direction == "Long" and rsi > 50) or (direction == "Short" and rsi < 50):
        score += 1
    if rsi_trend == "up" and direction == "Long":
        score += 1
    if rsi_trend == "down" and direction == "Short":
        score += 1
    if volume > avg_volume:
        score += 1
    if price_trend and direction == "Long":
        score += 1
    if not price_trend and direction == "Short":
        score += 1
    return score

# === Tier Label (from v8.4) ===
def get_tier_label(score):
    if score >= 5:
        return "S"
    elif score == 4:
        return "A"
    elif score == 3:
        return "B"
    else:
        return "C"

# === Knight Assignment by Strategy Type (from v8.4) ===
def assign_knight(strategy_type):
    if strategy_type == "Breakout":
        return "Sir Leonis Ironhart ⚔️"
    elif strategy_type == "Reversal":
        return "Sir Lucien Frostveil 🛡️"
    else:
        return "Orion Vellum 🌘"

# === Confirmation Body/Volume Thresholds (by mode) ===
def get_confirmation_mode_thresholds():
    if CONFIRMATION_MODE == "aggressive":
        return 1.0, 0.4
    elif CONFIRMATION_MODE == "strict":
        return 1.5, 0.6
    return 1.2, 0.5  # balanced default

# ============================================
# Section 3: Scorecard, Market Chronicle, Setup Alerts, Trade Embeds
# ============================================

# === Evaluate Scorecard Confluence ===
def evaluate_scorecard(df, cam):
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
        if ((rsi > df["rsi"].iloc[-3]) and rsi > 50) or ((rsi < df["rsi"].iloc[-3]) and rsi < 50):
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

# === Enhanced Scorecard (Chronicle) Embed ===
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
            timestamp=datetime.now(timezone.utc)
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

async def send_battlefield_map():
    try:
        df = fetch_ohlc("ETH", interval=1)
        if df is None or len(df) < 5:
            return

        df = calculate_indicators(df)
        if df is None:
            return

        latest = df.iloc[-1]
        price = latest["close"]

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return

        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        # === Build Battlefield Map with dynamic price placement ===
        level_map = "```\n"

        # Add price into levels with a custom key for sorting
        levels_with_price = {**levels, "➤ Price": price}
        sorted_levels = sorted(levels_with_price.items(), key=lambda x: -x[1])  # descending order

        for name, val in sorted_levels:
            label = f"{name:<8}"
            level_map += f"{label}{val:>8.2f}\n"

        level_map += "```"

        # === Create Embed ===
        embed = discord.Embed(
            title="🗺️ ETH Battlefield Map",
            description="*A tactical overlay of the Camarilla battlefield*",
            color=discord.Color.dark_blue(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="🔍 Levels Overview", value=level_map, inline=False)

        ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc_time = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc_time} | {ct_time}")

        channel = bot.get_channel(SCRIBES_KEEP_ID)  # update this if you want to route to a new map channel
        if channel:
            await channel.send(embed=embed)
            logger.info("📡 Battlefield map sent to scribes-keep")

    except Exception as e:
        logger.error(f"Error sending battlefield map: {e}")


# === Setup Alert Embed (Pre-Confirmation) ===
async def send_setup_alert(direction, level_name, level_price, score, missing_items):
    try:
        embed = discord.Embed(
            title=f"⚠️ Setup Alert - ETH {direction}",
            description=f"**Setup detected at {level_name}**\n*Awaiting full confirmation*",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)  
        )  

        embed.add_field(name="🧭 Level", value=f"{level_name} (${level_price:.2f})", inline=True)
        embed.add_field(name="📊 Score", value=f"{score}/6", inline=True)
        embed.add_field(name="❌ Missing Confirmation", value="\n".join(missing_items), inline=False)

        now = embed.timestamp
        ct = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = now.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct} • Awaiting confirmation...")

        channel = bot.get_channel(SETUP_ALERTS_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"⚠️ Setup alert sent for {level_name}")
        else:
            logger.warning("⚠️ Setup alert channel not found")

    except Exception as e:
        logger.error(f"Error in send_setup_alert: {e}")


# ============================================
# Section 4: Trade Signal Scanner, 100x Scanner, Proximity Warning Logic
# ============================================

# === Send Confirmed Battle Signal ===
async def send_battle_signal(direction, level_name, level_price, entry, stop_loss, targets, confidence, score, trade_type="Breakout"):
    try:
        knight = assign_knight(trade_type)
        color = discord.Color.green() if direction == "Long" else discord.Color.red()
        trade_id = str(uuid.uuid4())[:8]  # Short unique ID

        embed = discord.Embed(
            title=f"⚔️ Battle Signal - ETH {direction} {trade_type}",
            description=f"*{knight} calls for battle at {level_name}*",
            color=color,
            timestamp=datetime.now(timezone.utc)
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

        embed.add_field(name="🆔 Trade ID", value=trade_id, inline=False)

        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct} • May fortune favor the bold")

        channel = bot.get_channel(BATTLE_SIGNALS_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"✅ Battle signal sent: {direction} at {level_name}")

        # === Store Trade for Exit Monitoring ===
        active_trades["ETH"] = {
            "id": trade_id,
            "entry": entry,
            "tp1": targets[0],
            "tp2": targets[1],
            "sl": stop_loss,
            "side": direction,
            "thread_id": None,
            "knight": knight,
            "rating": confidence
        }

    except Exception as e:
        logger.error(f"Error in send_battle_signal: {e}")

async def send_exit_alert(reason, price, thread_id, direction, alert_id):
    """Send TP/SL hit alert."""
    now = datetime.now(timezone.utc)
    ct = now.astimezone(CENTRAL_TZ)

    embed = discord.Embed(
        title=f"📍 ETH Trade Exit Alert – {reason}",
        color=discord.Color.green() if "TP" in reason else discord.Color.red(),
        timestamp=now
    )
    embed.add_field(name="Type", value=direction, inline=True)
    embed.add_field(name="Exit Price", value=f"${price:.2f}", inline=True)
    embed.add_field(name="Outcome", value=reason, inline=True)
    embed.add_field(name="Trade ID", value=alert_id, inline=False)
    embed.set_footer(text=f"🕒 UTC: {now.strftime('%H:%M:%S')} | CT: {ct.strftime('%I:%M %p')}")

    channel = bot.get_channel(BATTLE_SIGNALS_ID)

    if thread_id:
        thread = channel.get_thread(thread_id)
        if thread:
            await thread.send(embed=embed)
            return

    await channel.send(embed=embed)

# === Camarilla Trade Signal Scanner ===
@tasks.loop(minutes=1)
async def scan_camarilla_trades():
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

        latest = df.iloc[-1]
        price = latest["close"]
        open_ = latest["open"]
        high_ = latest["high"]
        low_ = latest["low"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()
        rsi = latest["rsi"]
        rsi_trend = "up" if rsi > df["rsi"].iloc[-3] else "down"
        price_trend = price > df["close"].iloc[-3]

        # === Closest Camarilla Level
        closest = min(cam.items(), key=lambda x: abs(price - x[1]))
        level_name, level_price = closest
        level_dist_pct = abs(price - level_price) / price * 100
        direction = "Long" if price > level_price else "Short"

        # === Candle Confirmation
        body = abs(price - open_)
        range_ = high_ - low_
        body_ratio = body / range_ if range_ > 0 else 0
        volume_ok = volume > avg_volume * 1.2

        # === Confirmed Breakout
        breakout_confirmed = (
            ((price > level_price and open_ < level_price) or
             (price < level_price and open_ > level_price)) and
            body_ratio > 0.5 and
            volume_ok
        )

        # === Confirmed Reversal
        reversal_confirmed = (
            level_dist_pct <= 0.2 and
            ((high_ > level_price > price and direction == "Short") or
             (low_ < level_price < price and direction == "Long")) and
            body_ratio > 0.5 and
            volume_ok and
            ((rsi < 40 and direction == "Long") or (rsi > 60 and direction == "Short"))
        )

        # === Score Trade
        score = score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend)
        confidence = get_tier_label(score)

        # === Entry and Targets
        entry = round(price, 2)
        stop_pct = 0.01
        if direction == "Long":
            sl = round(entry * (1 - stop_pct), 2)
            tp1 = round(entry * 1.015, 2)
            tp2 = round(entry * 1.03, 2)
        else:
            sl = round(entry * (1 + stop_pct), 2)
            tp1 = round(entry * 0.985, 2)
            tp2 = round(entry * 0.97, 2)

        # === Cooldown Key
        key = f"{level_name}_{direction}"
        now = datetime.now(timezone.utc)

        if breakout_confirmed or reversal_confirmed:
            last_alert = CAMARILLA_COOLDOWN.get(key)
            if last_alert and (now - last_alert < timedelta(minutes=CAMARILLA_COOLDOWN_MINUTES)):
                logger.info(f"[Cooldown] Skipping alert for {key}")
                return

            CAMARILLA_COOLDOWN[key] = now

            trade_type = "Breakout" if breakout_confirmed else "Reversal"
            knight = assign_knight(trade_type)

            await send_battle_signal(
                direction=direction,
                level_name=level_name,
                level_price=level_price,
                entry=entry,
                stop_loss=sl,
                targets=[tp1, tp2],
                confidence=confidence,
                score=score,
                trade_type=trade_type
            )

            # === Generate Unique Trade ID and Track
            import uuid
            alert_id = str(uuid.uuid4())[:8]
            active_trades["ETH"] = {
                "id": alert_id,
                "entry": entry,
                "tp1": tp1,
                "tp2": tp2,
                "sl": sl,
                "side": direction,
                "thread_id": None,  # optional: update if using threads
                "knight": knight,
                "rating": confidence
            }

        else:
            # === Send Setup Alert with Cooldown
            missing = []
            if body_ratio <= 0.5:
                missing.append("🧱 Weak Candle Body")
            if not volume_ok:
                missing.append("🔇 Volume Below Threshold")
            if (price > level_price and open_ > level_price) or (price < level_price and open_ < level_price):
                missing.append("📉 No Breakout Structure")

            # === Setup Cooldown Key
            setup_key = f"{level_name}_{direction}_setup"
            last_setup = setup_alert_cooldowns.get(setup_key)
            if last_setup and (now - last_setup).total_seconds() < SETUP_ALERT_COOLDOWN_MINUTES * 60:
                logger.info(f"[Cooldown] Skipping setup alert for {setup_key}")
                return

            setup_alert_cooldowns[setup_key] = now
            await send_setup_alert(
                direction=direction,
                level_name=level_name,
                level_price=level_price,
                score=score,
                missing_items=missing
            )

    except Exception as e:
        logger.error(f"Error in scan_camarilla_trades: {e}")

# === 100x Trade Alert Scanner ===
@tasks.loop(minutes=1)
async def trade_100x_scan():
    global last_100x_trade_time
    try:
        now = datetime.now(timezone.utc)
        if last_100x_trade_time and (now - last_100x_trade_time).total_seconds() < 900:
            return

        df = fetch_ohlc("ETH", interval=1)
        if df is None:
            return
        df = calculate_indicators(df)
        if df is None or len(df) < 5:
            return

        score = score_trade(
            df["rsi"].iloc[-1],
            "up" if df["rsi"].iloc[-1] > df["rsi"].iloc[-3] else "down",
            "Long",
            df["close"].iloc[-1],
            df["close"].iloc[-2],
            df["volume"].iloc[-1],
            df["volume"].tail(10).mean(),
            True
        )

        if score >= 5:
            await send_100x_alert(df["close"].iloc[-1], score)
            last_100x_trade_time = now

    except Exception as e:
        logger.error(f"Error in trade_100x_scan: {e}")

# === Send 100x Alert ===
async def send_100x_alert(price, score):
    try:
        embed = discord.Embed(
            title="🦅 100x ETH Trade Opportunity",
            color=discord.Color.dark_gold()
        )
        embed.add_field(name="Current Price", value=f"${price:.2f}", inline=True)
        embed.add_field(name="Confidence Score", value=f"{score}/6", inline=True)

        now = datetime.now(timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)
        embed.set_footer(text=f"🕒 UTC: {now.strftime('%H:%M')} | CT: {ct_now.strftime('%I:%M %p')}")

        channel = bot.get_channel(EAGLE_SIGNAL_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"✅ 100x alert sent at ${price:.2f}")

    except Exception as e:
        logger.error(f"Error sending 100x alert: {e}")

# === Camarilla Proximity Warning Scanner ===
@tasks.loop(minutes=2)
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
        volume = latest["volume"]
        avg_volume = df["volume"].tail(5).mean()
        volume_ratio = volume / avg_volume if avg_volume else 1
        trend = "up" if df["close"].iloc[-1] > df["close"].iloc[-3] else "down"

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        now = datetime.now(timezone.utc)
        for name, lvl in levels.items():
            if name == "P":
                continue
            if name in camarilla_warning_cooldowns and (now - camarilla_warning_cooldowns[name]).total_seconds() < 600:
                continue
            if abs(price - lvl) <= 2.0:
                await send_proximity_warning(name, lvl, price, rsi, volume_ratio, trend)
                camarilla_warning_cooldowns[name] = now

    except Exception as e:
        logger.error(f"Error in check_camarilla_warning: {e}")

@tasks.loop(seconds=30)
async def monitor_trade_exits():
    try:
        if "ETH" not in active_trades:
            return

        df = fetch_ohlc("ETH", interval=1)
        if df is None or len(df) < 1:
            return

        latest = df.iloc[-1]
        price = latest["close"]
        trade = active_trades["ETH"]

        tp2 = trade["tp2"]
        sl = trade["sl"]
        direction = trade["side"]
        alert_id = trade["id"]
        thread_id = trade.get("thread_id")

        if direction == "Long":
            if price >= tp2:
                await send_exit_alert("TP HIT", price, thread_id, direction, alert_id)
                del active_trades["ETH"]
            elif price <= sl:
                await send_exit_alert("SL HIT", price, thread_id, direction, alert_id)
                del active_trades["ETH"]
        elif direction == "Short":
            if price <= tp2:
                await send_exit_alert("TP HIT", price, thread_id, direction, alert_id)
                del active_trades["ETH"]
            elif price >= sl:
                await send_exit_alert("SL HIT", price, thread_id, direction, alert_id)
                del active_trades["ETH"]

    except Exception as e:
        logger.error(f"Error in monitor_trade_exits: {e}")

@tasks.loop(minutes=15)
async def battlefield_map_loop():
    await send_battlefield_map()

# ============================================
# Section 5: Battleground Update + Performance Chronicle
# ============================================

# === ETH Battleground Update ===
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
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1

        if volume_ratio > 2.0:
            emoji = "🌋"
            status = "HIGH VOLATILITY"
            color = discord.Color.orange()
        elif rsi > 70:
            emoji = "🔥"
            status = "OVERBOUGHT"
            color = discord.Color.red()
        elif rsi < 30:
            emoji = "❄️"
            status = "OVERSOLD"
            color = discord.Color.blue()
        else:
            emoji = "⚪"
            status = "STABLE"
            color = discord.Color.greyple()

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

        now_utc = datetime.now(timezone.utc)

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

# === Performance Chronicle (basic log info for now) ===
@tasks.loop(hours=6)
async def performance_report():
    try:
        now = datetime.now(timezone.utc)
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

# === Command: !status ===
@bot.command(name='status')
async def status(ctx):
    try:
        embed = discord.Embed(
            title="🤖 Knight's Status Report",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="📊 Tasks", value="✅ All Running", inline=True)

        task_status = (
            f"📜 Chronicle: {'✅' if send_enhanced_scorecard.is_running() else '❌'}\n"
            f"⚔️ Signals: {'✅' if scan_camarilla_trades.is_running() else '❌'}\n"
            f"🦅 Eagle: {'✅' if trade_100x_scan.is_running() else '❌'}\n"
            f"👁️ Watch: {'✅' if check_camarilla_warning.is_running() else '❌'}\n"
            f"🏰 Battleground: {'✅' if battleground_loop.is_running() else '❌'}"
        )

        embed.add_field(name="🔄 Active Tasks", value=task_status, inline=False)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in !status command: {e}")
        await ctx.send("❌ Error checking status")

# === Command: !test_chronicle (admin only) ===
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

# === Scheduled Loops ===
@tasks.loop(minutes=1)
async def battleground_loop():
    now = datetime.now(timezone.utc)
    if now.minute in [7, 23, 37, 52]:
        await send_battleground_embed()

@tasks.loop(minutes=1)
async def chronicle_loop():
    if datetime.now(timezone.utc).minute % 15 == 0:
        await send_enhanced_scorecard()

# === Startup Event ===
@bot.event
async def on_ready():
    logger.info(f"🟢 Bot logged in as {bot.user}")

    try:
        if not scan_camarilla_trades.is_running():
            scan_camarilla_trades.start()
        if not chronicle_loop.is_running():
            chronicle_loop.start()
        if not trade_100x_scan.is_running():
            trade_100x_scan.start()
        if not check_camarilla_warning.is_running():
            check_camarilla_warning.start()
        if not battleground_loop.is_running():
            battleground_loop.start()
        if not performance_report.is_running():
            performance_report.start()
        if not monitor_trade_exits.is_running():
            monitor_trade_exits.start()
        if not battlefield_map_loop.is_running():  # FIXED INDENT HERE
            battlefield_map_loop.start()
      
        embed = discord.Embed(
            title="🏰 Control Tower Activated",
            description="*Trade scanning and alert systems are online.*",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="📡 Strategy", value="Camarilla + RSI/Volume/Trend Confluence", inline=True)
        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct}")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)

    except Exception as e:
        logger.error(f"Error during startup: {e}")

# === Flask Thread Runner ===
def start_flask_thread():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

# === Bot Runner ===
if __name__ == "__main__":
    logger.info("🚀 Starting Control Tower ETH Camarilla Bot...")
    start_flask_thread()
    bot.run(TOKEN)