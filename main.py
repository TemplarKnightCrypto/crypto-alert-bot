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
from ta.trend import MACD

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    logger.error("Discord TOKEN not found in environment variables")
    exit(1)

# Channel IDs
SCORECARD_CHANNEL_ID = 1399532442075005038
EAGLE_SIGNAL_CHANNEL_ID = 1398690647417819198
TRADE_100X_CHANNEL_ID = 1399532925279666278

HEARTBEAT_CHANNEL_IDS = [
    1399067396488302623,
    1399532102571135118,
    1398691425347961016,
    SCORECARD_CHANNEL_ID,
    EAGLE_SIGNAL_CHANNEL_ID,
    TRADE_100X_CHANNEL_ID
]

# Globals
last_100x_trade_time = None
last_scorecard_sent = None
camarilla_warning_cooldowns = {}
last_trade_alert_time = {}
ohlc_cache = {}
cache_expiry = {}

# Timezones
UTC = pytz.utc
CENTRAL_TZ = pytz.timezone("US/Central")

# Configuration
CONFIRMATION_MODE = "balanced"  # Options: aggressive, balanced, strict
API_TIMEOUT = 10
MAX_RETRIES = 3
CACHE_DURATION = 30  # seconds

app = Flask(__name__)

@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"

@app.route("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.datetime.utcnow().isoformat()}

def run_flask():
    app.run(host="0.0.0.0", port=10000)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def retry_api_call(func, *args, **kwargs):
    """Retry API calls with exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            wait_time = 2 ** attempt
            logger.warning(f"API call failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait_time)
            else:
                logger.error(f"API call failed after {MAX_RETRIES} attempts")
                raise e

def fetch_ohlc(symbol="ETH", interval=1):
    """Fetch OHLC data from Kraken API with caching and error handling."""
    cache_key = f"{symbol}_{interval}"
    current_time = time.time()
    
    # Check cache
    if (cache_key in ohlc_cache and 
        cache_key in cache_expiry and 
        current_time < cache_expiry[cache_key]):
        logger.debug(f"Using cached data for {cache_key}")
        return ohlc_cache[cache_key]
    
    try:
        kraken_map = {"ETH": "XETHZUSD"}
        pair = kraken_map.get(symbol.upper(), "XETHZUSD")
        url = "https://api.kraken.com/0/public/OHLC"
        params = {"pair": pair, "interval": interval}
        
        def _fetch():
            response = requests.get(url, params=params, timeout=API_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            
            if "error" in data and data["error"]:
                raise Exception(f"Kraken API error: {data['error']}")
            
            if "result" not in data or pair not in data["result"]:
                raise Exception(f"Invalid API response structure")
                
            return data["result"][pair]
        
        raw = retry_api_call(_fetch)
        
        if not raw or len(raw) < 2:
            logger.warning(f"Insufficient data returned for {symbol}")
            return None
            
        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
        df = df.astype({
            "time": int, 
            "open": float, 
            "high": float, 
            "low": float, 
            "close": float, 
            "volume": float
        })
        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("datetime", inplace=True)
        
        # Cache the result
        ohlc_cache[cache_key] = df
        cache_expiry[cache_key] = current_time + CACHE_DURATION
        
        logger.debug(f"Successfully fetched {len(df)} candles for {symbol}")
        return df
        
    except Exception as e:
        logger.error(f"Error fetching OHLC data for {symbol}: {e}")
        return None

def fetch_daily_ohlc():
    """Fetch daily OHLC data."""
    try:
        df = fetch_ohlc(interval=1440)
        if df is None or len(df) < 2:
            logger.warning("Insufficient daily OHLC data")
            return None, None, None
            
        latest = df.iloc[-2]  # Use completed candle
        return latest["high"], latest["low"], latest["close"]
    except Exception as e:
        logger.error(f"Error fetching daily OHLC: {e}")
        return None, None, None

def calculate_camarilla(high, low, close):
    """Calculate Camarilla pivot levels."""
    if any(x is None for x in [high, low, close]):
        return {}
        
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
        logger.error(f"Error calculating Camarilla levels: {e}")
        return {}

def calculate_indicators(df):
    """Calculate technical indicators with error handling."""
    if df is None or len(df) < 20:
        logger.warning("Insufficient data for indicator calculation")
        return None
        
    try:
        df = df.copy()
        
        # RSI
        df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
        
        # VWAP
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (df["volume"] * typical_price).cumsum() / df["volume"].cumsum()
        
        # MACD
        macd_indicator = MACD(close=df["close"])
        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_hist"] = macd_indicator.macd_diff()
        
        # Drop NaN values
        df = df.dropna()
        
        if len(df) < 5:
            logger.warning("Insufficient data after indicator calculation")
            return None
            
        return df
        
    except Exception as e:
        logger.error(f"Error calculating indicators: {e}")
        return None

def get_confirmation_mode_thresholds():
    """Get thresholds based on confirmation mode."""
    if CONFIRMATION_MODE == "aggressive":
        return 0.3, 1.0
    elif CONFIRMATION_MODE == "strict":
        return 0.7, 1.5
    return 0.5, 1.2  # balanced

def score_trade(rsi, rsi_trend, direction, price, level, volume, avg_volume, price_trend):
    """Score trade quality from 0-6."""
    try:
        score = 0
        
        # RSI conditions
        if (rsi > 55 and direction == "Long") or (rsi < 45 and direction == "Short"):
            score += 1
            
        # RSI trend alignment
        if (rsi_trend == "up" and direction == "Long") or (rsi_trend == "down" and direction == "Short"):
            score += 1
            
        # Price trend alignment
        if (price_trend and direction == "Long") or (not price_trend and direction == "Short"):
            score += 1
            
        # Volume confirmation
        if volume > avg_volume * 1.2:
            score += 1
            
        # Distance from level (closer = better)
        distance_factor = abs(price - level) / price
        if distance_factor < 0.005:  # Within 0.5%
            score += 1
            
        # Additional confluence point for strong setups
        if score >= 4:
            score += 1
            
        return min(score, 6)
        
    except Exception as e:
        logger.error(f"Error scoring trade: {e}")
        return 0

def assign_knight(direction):
    """Assign knight based on direction."""
    return "Sir Leonis ⚔️" if direction == "Long" else "Sir Lucien 🛡"

@tasks.loop(minutes=1)
async def scan_trade_alerts():
    """Scan for trade alerts based on Camarilla levels."""
    global last_trade_alert_time
    
    try:
        # Fetch data
        df = fetch_ohlc("ETH", interval=1)
        df5 = fetch_ohlc("ETH", interval=5)
        
        if df is None or df5 is None:
            logger.warning("Failed to fetch OHLC data for trade alerts")
            return
            
        df = calculate_indicators(df)
        if df is None or len(df) < 5:
            logger.warning("Insufficient data for trade analysis")
            return

        latest = df.iloc[-1]
        recent = df.tail(5)
        confirm = df5.iloc[-1]

        price = latest["close"]
        rsi = latest["rsi"]
        volume = latest["volume"]
        avg_volume = recent["volume"].mean()
        price_trend = df["close"].iloc[-1] > df["close"].iloc[-3]
        rsi_trend = "up" if df["rsi"].iloc[-1] > df["rsi"].iloc[-3] else "down"

        # Get Camarilla levels
        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
            
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        # Check confirmation criteria
        body = abs(confirm["close"] - confirm["open"])
        wick = confirm["high"] - confirm["low"]
        body_thresh, volume_thresh = get_confirmation_mode_thresholds()
        
        strong_body = (body / wick) > body_thresh if wick > 0 else False
        volume_valid = confirm["volume"] > df5["volume"].tail(5).mean() * volume_thresh
        now = datetime.datetime.utcnow()

        for name, lvl in levels.items():
            if name == "Pivot":
                continue
                
            is_upper = "H" in name
            
            # Check for level break
            broken = (
                (confirm["close"] > lvl and confirm["open"] < lvl) if is_upper 
                else (confirm["close"] < lvl and confirm["open"] > lvl)
            )
            
            if not (broken and strong_body and volume_valid):
                continue
                
            # Check cooldown
            if (name in last_trade_alert_time and 
                (now - last_trade_alert_time[name]).total_seconds() < 1800):
                continue

            direction = "Long" if is_upper else "Short"
            confidence = score_trade(rsi, rsi_trend, direction, price, lvl, volume, avg_volume, price_trend)
            
            if confidence < 3:
                continue

            # Calculate trade parameters
            entry = round(price, 2)
            risk_pct = 0.01  # 1% risk
            risk = entry * risk_pct
            
            if direction == "Long":
                stop = round(entry - risk, 2)
                tp1 = round(entry + risk * 1.5, 2)
                tp2 = round(entry + risk * 3.0, 2)
            else:
                stop = round(entry + risk, 2)
                tp1 = round(entry - risk * 1.5, 2)
                tp2 = round(entry - risk * 3.0, 2)

            knight = assign_knight(direction)
            emoji = "🟩" if direction == "Long" else "🟥"
            
            if confidence >= 5:
                label = "🟢 80%+ – Strong Move"
            elif confidence == 4:
                label = "🟠 75% – Likely Move"
            else:
                label = "🟡 60% – Possible Move"

            embed = discord.Embed(
                title=f"{emoji} ETH {direction} at {name} (${lvl:.2f})",
                color=discord.Color.green() if direction == "Long" else discord.Color.red(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            
            embed.add_field(name="🛡 Knight", value=knight, inline=True)
            embed.add_field(name="🎯 Direction", value=direction, inline=True)
            embed.add_field(name="📊 Confidence", value=label, inline=True)
            embed.add_field(name="📟 Score", value=f"{confidence}/6", inline=True)
            embed.add_field(name="🎯 Entry", value=f"${entry}", inline=True)
            embed.add_field(name="🎯 TP1 | TP2", value=f"${tp1} | ${tp2}", inline=True)
            embed.add_field(name="🛑 Stop Loss", value=f"${stop}", inline=True)

            # Support/Resistance map
            sorted_levels = sorted(levels.items(), key=lambda x: x[1], reverse=True)
            map_str = ""
            for k, v in sorted_levels:
                proximity = "➡️" if abs(price - v) < 0.5 else ""
                map_str += f"{k:<6} ${v:.2f} {proximity}\n"
                
            embed.add_field(name="📍 Support/Resistance Map", value=f"```{map_str}```", inline=False)
            
            ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
            embed.set_footer(text=f"Mode: {CONFIRMATION_MODE.upper()} | CT: {ct_time}")

            channel = bot.get_channel(SCORECARD_CHANNEL_ID)
            if channel:
                await channel.send(embed=embed)
                last_trade_alert_time[name] = now
                logger.info(f"Sent trade alert for {name} at ${price:.2f}")
            else:
                logger.error(f"Could not find channel {SCORECARD_CHANNEL_ID}")
                
    except Exception as e:
        logger.error(f"Error in scan_trade_alerts: {e}")

def evaluate_scorecard(df, cam):
    """Evaluate trading scorecard."""
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

        # Find closest level
        level = min(cam.values(), key=lambda x: abs(price - x))
        reasons = []
        score = 0

        # RSI out of neutral zone
        if rsi > 55 or rsi < 45:
            score += 1
            reasons.append("✅ RSI Out of Neutral")
            
        # RSI trend alignment
        rsi_trend_up = rsi > df["rsi"].iloc[-3]
        if (rsi_trend_up and rsi > 50) or (not rsi_trend_up and rsi < 50):
            score += 1
            reasons.append("✅ RSI Trend Aligns")
            
        # MACD momentum (fixed logic)
        if abs(macd_hist) > 0.1:  # Meaningful MACD histogram value
            score += 1
            reasons.append("✅ MACD Momentum")
            
        # Price above VWAP
        if above_vwap:
            score += 1
            reasons.append("✅ Price Above VWAP")
            
        # Volume spike
        if volume > avg_volume * 1.2:
            score += 1
            reasons.append("✅ Volume Spike")
            
        # Price trend
        if trend:
            score += 1
            reasons.append("✅ Price Trend Direction")

        return score, reasons, level
        
    except Exception as e:
        logger.error(f"Error evaluating scorecard: {e}")
        return 0, [], None

@tasks.loop(minutes=1)
async def trade_100x_scan():
    """Scan for high-confidence 100x trades."""
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

        now = datetime.datetime.utcnow()
        
        # More conservative threshold for 100x trades
        if (score < 5 or 
            (last_100x_trade_time and (now - last_100x_trade_time).total_seconds() < 900)):  # 15 min cooldown
            return

        latest = df.iloc[-1]
        price = latest["close"]
        volume = latest["volume"]
        avg_volume = df["volume"].tail(10).mean()
        
        # Stricter confirmation for 100x
        body = abs(latest["close"] - latest["open"])
        range_ = latest["high"] - latest["low"]
        body_ratio = body / range_ if range_ > 0 else 0
        
        breakout_confirmed = (
            ((price > level and latest["open"] < level) or 
             (price < level and latest["open"] > level)) and
            body_ratio > 0.6 and  # Stronger body requirement
            volume > avg_volume * 1.5  # Higher volume requirement
        )

        if not breakout_confirmed:
            return

        direction = "Long" if price > level else "Short"
        entry = round(price, 2)
        
        # Tighter stops for high leverage
        stop_pct = 0.005  # 0.5% stop
        if direction == "Long":
            sl = round(entry * (1 - stop_pct), 2)
            tp1 = round(entry * 1.01, 2)  # 1% target
            tp2 = round(entry * 1.02, 2)  # 2% target
        else:
            sl = round(entry * (1 + stop_pct), 2)
            tp1 = round(entry * 0.99, 2)
            tp2 = round(entry * 0.98, 2)

        embed = discord.Embed(
            title=f"⚔️ 100x Trade Alert – ETH {direction}",
            description=(
                f"📍 **Entry:** `${entry:.2f}`\n"
                f"🎯 **TP1:** `${tp1:.2f}`\n"
                f"🎯 **TP2:** `${tp2:.2f}`\n"
                f"🛡️ **SL:** `${sl:.2f}`\n\n"
                f"📊 **Confluence Score:** {score}/6\n" + 
                "\n".join(reasons) +
                f"\n\n⚠️ **HIGH LEVERAGE WARNING:** Use proper position sizing!"
            ),
            color=0x2E7D32 if direction == "Long" else 0xD32F2F
        )
        
        ct_time = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 CT: {ct_time} | High-Leverage Trade")

        channel = bot.get_channel(TRADE_100X_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)
            last_100x_trade_time = now
            logger.info(f"Sent 100x trade alert: {direction} at ${entry:.2f}")
        else:
            logger.error(f"Could not find 100x channel {TRADE_100X_CHANNEL_ID}")
            
    except Exception as e:
        logger.error(f"Error in trade_100x_scan: {e}")

@tasks.loop(minutes=1)
async def check_camarilla_warning():
    """Check for proximity to Camarilla levels."""
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
        trend = "up" if df["close"].iloc[-1] > df["close"].iloc[-3] else "down"

        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
            
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return
            
        now = datetime.datetime.utcnow()

        for name, lvl in levels.items():
            if name == "Pivot":
                continue
                
            # Check cooldown
            if (name in camarilla_warning_cooldowns and 
                (now - camarilla_warning_cooldowns[name]).total_seconds() < 600):
                continue

            # Check proximity (within $1.00)
            if abs(price - lvl) <= 1.0:
                likely_break = (trend == "up" and "H" in name) or (trend == "down" and "L" in name)
                rsi_aligns = (rsi > recent_rsi and "H" in name) or (rsi < recent_rsi and "L" in name)
                vol_trend = volume > avg_volume

                if likely_break and rsi_aligns and vol_trend:
                    bias = "🔴 Likely Break"
                elif not likely_break and not rsi_aligns and volume < avg_volume:
                    bias = "🟢 Likely Reversal"
                else:
                    bias = "⚪ Unclear / 50/50"

                embed = discord.Embed(
                    title=f"⚠️ Camarilla Warning – Approaching {name} (${lvl:.2f})",
                    description=(
                        f"📈 **Current Price:** `${price:.2f}` (${price-lvl:+.2f})\n"
                        f"📊 **RSI:** `{rsi:.1f}` | **Trend:** {trend.upper()}\n"
                        f"🔍 **Volume:** `{volume:.0f}` vs avg `{avg_volume:.0f}`\n"
                        f"🧠 **Bias:** {bias}"
                    ),
                    color=0xFBC02D,
                    timestamp=now
                )
                
                ct_time = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
                embed.set_footer(text=f"🕒 CT: {ct_time}")
                
                channel = bot.get_channel(SCORECARD_CHANNEL_ID)
                if channel:
                    await channel.send(embed=embed)
                    camarilla_warning_cooldowns[name] = now
                    logger.info(f"Sent proximity warning for {name} at ${price:.2f}")
                    
    except Exception as e:
        logger.error(f"Error in check_camarilla_warning: {e}")

@tasks.loop(hours=1)
async def heartbeat():
    """Send heartbeat to confirm bot is operational."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)
        
        message = (
            f"🛡️ **Heartbeat Check** – Bot is operational.\n"
            f"🕒 **UTC:** {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🕒 **CT:** {ct_now.strftime('%I:%M %p')}\n"
            f"📊 **Mode:** {CONFIRMATION_MODE.upper()}"
        )
        
        for channel_id in HEARTBEAT_CHANNEL_IDS:
            try:
                channel = bot.get_channel(channel_id)
                if channel:
                    await channel.send(message)
                else:
                    logger.warning(f"Heartbeat channel not found: {channel_id}")
            except Exception as e:
                logger.error(f"Error sending heartbeat to {channel_id}: {e}")
                
        logger.info("Heartbeat sent successfully")
        
    except Exception as e:
        logger.error(f"Error in heartbeat: {e}")

@bot.event
async def on_ready():
    """Bot startup event."""
    logger.info(f"🟢 Bot logged in as {bot.user}")
    logger.info(f"🔧 Confirmation mode: {CONFIRMATION_MODE}")
    
    try:
        # Start all tasks
        if not heartbeat.is_running():
            heartbeat.start()
        if not check_camarilla_warning.is_running():
            check_camarilla_warning.start()
        if not scan_trade_alerts.is_running():
            scan_trade_alerts.start()
        if not trade_100x_scan.is_running():
            trade_100x_scan.start()
            
        logger.info("✅ All tasks started successfully")
        
    except Exception as e:
        logger.error(f"Error starting tasks: {e}")

@bot.event
async def on_error(event, *args, **kwargs):
    """Global error handler."""
    logger.error(f"Discord error in {event}: {args}")

# Bot commands for debugging
@bot.command(name='status')
async def status(ctx):
    """Check bot status."""
    try:
        embed = discord.Embed(
            title="🤖 Bot Status",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        
        embed.add_field(name="Mode", value=CONFIRMATION_MODE.upper(), inline=True)
        embed.add_field(name="Tasks Running", value="✅", inline=True)
        embed.add_field(name="API Status", value="✅", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await ctx.send("❌ Error checking status")

# Bot runner
def start_bot():
    """Start the Discord bot."""
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    try:
        logger.info("🚀 Starting ETH Camarilla Trading Bot...")
        
        # Start Flask in background thread
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("🌐 Flask server started")
        
        # Start Discord bot
        start_bot()
        
    except KeyboardInterrupt:
        logger.info("👋 Bot shutdown requested")
    except Exception as e:
        logger.error(f"💥 Critical error: {e}")
    finally:
        logger.info("🔴 Bot terminated")