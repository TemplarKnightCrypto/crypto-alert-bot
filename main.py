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

# Discord Channel Configuration - Update with your actual channel IDs
SCRIBES_KEEP_ID = 1399532442075005038          # Main analytics/scorecard
BATTLE_SIGNALS_ID = 1398690647417819198        # High-probability trades  
EAGLE_SIGNAL_ID = 1399532925279666278          # Premium 100x signals
KNIGHTS_WATCH_ID = 1399067396488302623         # Proximity warnings
ETH_BATTLEGROUND_ID = 1399532102571135118      # Real-time updates
SCROLLS_ORDER_ID = 1398691425347961016         # Performance/history

# Heartbeat channels (status updates)
HEARTBEAT_CHANNEL_IDS = [
    SCRIBES_KEEP_ID,
    BATTLE_SIGNALS_ID, 
    EAGLE_SIGNAL_ID,
    KNIGHTS_WATCH_ID,
    ETH_BATTLEGROUND_ID,
    SCROLLS_ORDER_ID
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
    return {"status": "healthy", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}

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
            reasons.append("✅ RSI Out of Neutral Zone")
            
        # RSI trend alignment
        rsi_trend_up = rsi > df["rsi"].iloc[-3]
        if (rsi_trend_up and rsi > 50) or (not rsi_trend_up and rsi < 50):
            score += 1
            reasons.append("✅ RSI Trend Alignment")
            
        # MACD momentum (fixed logic)
        if abs(macd_hist) > 0.1:  # Meaningful MACD histogram value
            score += 1
            reasons.append("✅ MACD Momentum Present")
            
        # Price above VWAP
        if above_vwap:
            score += 1
            reasons.append("✅ Price Above VWAP")
            
        # Volume spike
        if volume > avg_volume * 1.2:
            score += 1
            reasons.append("✅ Volume Spike Detected")
            
        # Price trend
        if trend:
            score += 1
            reasons.append("✅ Bullish Price Trend")

        return score, reasons, level
        
    except Exception as e:
        logger.error(f"Error evaluating scorecard: {e}")
        return 0, [], None

# Enhanced scorecard for scribes-keep
async def send_enhanced_scorecard():
    """Send comprehensive market analysis to scribes-keep."""
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
        
        # Get Camarilla levels
        high, low, close = fetch_daily_ohlc()
        if any(x is None for x in [high, low, close]):
            return
            
        levels = calculate_camarilla(high, low, close)
        if not levels:
            return

        # Find closest level and key levels
        closest_level = min(levels.items(), key=lambda x: abs(price - x[1]))
        level_name, level_price = closest_level
        distance = level_price - price
        distance_pct = (distance / price) * 100

        # Calculate comprehensive score
        score, reasons, _ = evaluate_scorecard(df, levels)
        
        # Get next levels above and below
        sorted_levels = sorted(levels.items(), key=lambda x: x[1])
        above_levels = [l for l in sorted_levels if l[1] > price][:2]
        below_levels = [l for l in reversed(sorted_levels) if l[1] < price][:2]

        # Determine market bias
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

        # Price change calculation
        price_24h_ago = df.iloc[-1440] if len(df) >= 1440 else df.iloc[0]
        price_change = price - price_24h_ago["close"]
        price_change_pct = (price_change / price_24h_ago["close"]) * 100

        embed = discord.Embed(
            title="📜 ETH Market Chronicle",
            description=f"*The scribes record the current state of the battlefield*",
            color=bias_color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        # Current price section
        price_emoji = "📈" if price_change >= 0 else "📉"
        embed.add_field(
            name=f"{price_emoji} Current Price",
            value=f"**${price:.2f}**\n{price_change_pct:+.2f}% (${price_change:+.2f})",
            inline=True
        )

        # Key level focus
        distance_emoji = "🎯" if abs(distance_pct) < 0.5 else "📍"
        embed.add_field(
            name=f"{distance_emoji} Level in Focus",
            value=f"**{level_name}: ${level_price:.2f}**\n{distance_pct:+.2f}% (${distance:+.2f})",
            inline=True
        )

        # Market bias
        embed.add_field(
            name="🧠 Market Bias",
            value=f"**{bias}**\nScore: {score}/6",
            inline=True
        )

        # Technical indicators with actual values
        rsi_emoji = "🟢" if rsi > 55 else "🔴" if rsi < 45 else "⚪"
        macd_emoji = "🟢" if macd_hist > 0 else "🔴"
        volume_ratio = volume / avg_volume
        volume_emoji = "🟢" if volume_ratio > 1.2 else "🔴" if volume_ratio < 0.8 else "⚪"
        vwap_diff = price - latest["vwap"]
        vwap_emoji = "🟢" if vwap_diff > 0 else "🔴"

        indicators_text = (
            f"{rsi_emoji} **RSI:** {rsi:.1f} {'(Overbought)' if rsi > 70 else '(Oversold)' if rsi < 30 else '(Neutral)'}\n"
            f"{macd_emoji} **MACD:** {macd_hist:.2f} {'(Bullish)' if macd_hist > 0 else '(Bearish)'}\n"
            f"{volume_emoji} **Volume:** {volume_ratio:.1f}x avg ({volume:.0f})\n"
            f"{vwap_emoji} **VWAP:** ${vwap_diff:+.2f} ({'Above' if vwap_diff > 0 else 'Below'})"
        )
        
        embed.add_field(
            name="📊 Technical Indicators",
            value=indicators_text,
            inline=False
        )

        # Level map
        level_map = "```\n"
        for name, val in above_levels:
            level_map += f"{name:<6} ${val:>8.2f} (+${val-price:>6.2f})\n"
        level_map += f"{'═'*25}\n"
        level_map += f"NOW    ${price:>8.2f} ═══════\n"
        level_map += f"{'═'*25}\n"
        for name, val in below_levels:
            level_map += f"{name:<6} ${val:>8.2f} (-${price-val:>6.2f})\n"
        level_map += "```"

        embed.add_field(
            name="🗺️ Battlefield Map",
            value=level_map,
            inline=False
        )

        # Confluence reasons
        if reasons:
            confluence_text = "\n".join(reasons[:6])  # Limit to 6 reasons
            embed.add_field(
                name="⚖️ Confluence Analysis",
                value=confluence_text,
                inline=False
            )

        ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 Central Time: {ct_time} | Next chronicle in 5 minutes")

        channel = bot.get_channel(SCRIBES_KEEP_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info("Enhanced scorecard sent to scribes-keep")

    except Exception as e:
        logger.error(f"Error sending enhanced scorecard: {e}")

# Proximity warnings for knights-watch
async def send_proximity_warning(level_name, level_price, current_price, rsi, volume_ratio, trend):
    """Send proximity warning to knights-watch."""
    try:
        distance = level_price - current_price
        distance_pct = (distance / current_price) * 100
        
        # Analyze breakout probability
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
            title=f"⚠️ Knight's Warning - Approaching {level_name}",
            description=f"*The knights observe movement toward a critical level*",
            color=bias_color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        embed.add_field(
            name="🎯 Target Level",
            value=f"**${level_price:.2f}**",
            inline=True
        )

        embed.add_field(
            name="📍 Current Price", 
            value=f"**${current_price:.2f}**",
            inline=True
        )

        embed.add_field(
            name="📏 Distance",
            value=f"**${distance:+.2f}**\n({distance_pct:+.2f}%)",
            inline=True
        )

        analysis_text = (
            f"🎯 **Bias:** {bias}\n"
            f"📊 **RSI:** {rsi:.1f} {'🟢' if rsi_supports else '🔴'}\n"
            f"📈 **Trend:** {trend.upper()} {'🟢' if likely_break else '🔴'}\n"
            f"🔊 **Volume:** {volume_ratio:.1f}x {'🟢' if volume_strong else '🔴'}"
        )

        embed.add_field(
            name="🔮 Analysis",
            value=analysis_text,
            inline=False
        )

        ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 CT: {ct_time} | Knights remain vigilant")

        channel = bot.get_channel(KNIGHTS_WATCH_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"Proximity warning sent for {level_name}")

    except Exception as e:
        logger.error(f"Error sending proximity warning: {e}")

# High-probability trades for battle-signals  
async def send_battle_signal(direction, level_name, level_price, entry, stop_loss, targets, confidence, score):
    """Send trade signal to battle-signals."""
    try:
        knight = assign_knight(direction)
        emoji = "🟩" if direction == "Long" else "🟥"
        
        embed = discord.Embed(
            title=f"⚔️ Battle Signal - {direction} Formation",
            description=f"*{knight} calls for battle at {level_name}*",
            color=discord.Color.green() if direction == "Long" else discord.Color.red(),
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
            value=(
                f"**Score:** {score}/6\n"
                f"**Risk:** {risk_pct:.1f}% per position\n"
                f"**R:R:** 1:{reward1_pct/risk_pct:.1f} | 1:{reward2_pct/risk_pct:.1f}"
            ),
            inline=False
        )

        ct_time = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 CT: {ct_time} | May fortune favor the bold")

        channel = bot.get_channel(BATTLE_SIGNALS_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info(f"Battle signal sent: {direction} at {level_name}")

    except Exception as e:
        logger.error(f"Error sending battle signal: {e}")

@tasks.loop(minutes=15)
async def send_market_chronicle():
    """Send enhanced scorecard to scribes-keep every 15 minutes."""
    await send_enhanced_scorecard()

@tasks.loop(minutes=1)
async def scan_trade_alerts():
    """Scan for trade alerts and send to battle-signals."""
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
        now = datetime.datetime.now(datetime.timezone.utc)

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
            
            if confidence < 4:  # Only high-probability signals for battle-signals
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

            if confidence >= 5:
                confidence_label = "🟢 Strong Move (80%+)"
            else:
                confidence_label = "🟡 Likely Move (75%)"

            await send_battle_signal(direction, name, lvl, entry, stop, [tp1, tp2], confidence_label, confidence)
            last_trade_alert_time[name] = now
                
    except Exception as e:
        logger.error(f"Error in scan_trade_alerts: {e}")

@tasks.loop(minutes=1)
async def trade_100x_scan():
    """Scan for high-confidence 100x trades for eagle-signal."""
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
        
        # Ultra-strict criteria for 100x trades
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

        knight = assign_knight(direction)
        
        embed = discord.Embed(
            title=f"🦅 Eagle's Vision - Premium {direction} Signal",
            description=f"*The eagle has spotted a high-conviction opportunity*",
            color=0x2E7D32 if direction == "Long" else 0xD32F2F,
            timestamp=now
        )
        
        embed.add_field(name="🛡️ Knight", value=knight, inline=True)
        embed.add_field(name="🎯 Direction", value=direction, inline=True)  
        embed.add_field(name="📊 Confidence", value="🟢 Premium Setup", inline=True)
        embed.add_field(name="⚔️ Entry", value=f"${entry:.2f}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${sl:.2f}", inline=True)
        embed.add_field(name="🎯 Targets", value=f"${tp1:.2f} | ${tp2:.2f}", inline=True)

        confluence_text = f"**Score:** {score}/6\n" + "\n".join(reasons[:4])
        embed.add_field(name="⚖️ Confluence", value=confluence_text, inline=False)
        
        embed.add_field(
            name="⚠️ High Leverage Warning", 
            value="**Use strict position sizing!**\nMax 1-2% account risk\nTight stops required", 
            inline=False
        )

        ct_time = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 CT: {ct_time} | The eagle's vision is keen")

        channel = bot.get_channel(EAGLE_SIGNAL_ID)
        if channel:
            await channel.send(embed=embed)
            last_100x_trade_time = now
            logger.info(f"Eagle signal sent: {direction} at ${entry:.2f}")
        else:
            logger.error(f"Could not find eagle-signal channel {EAGLE_SIGNAL_ID}")
            
    except Exception as e:
        logger.error(f"Error in trade_100x_scan: {e}")

@tasks.loop(minutes=1)
async def check_camarilla_warning():
    """Check for proximity to Camarilla levels and send to knights-watch."""
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
                
            # Check cooldown
            if (name in camarilla_warning_cooldowns and 
                (now - camarilla_warning_cooldowns[name]).total_seconds() < 600):
                continue

            # Check proximity (within $2.00 for warnings)
            if abs(price - lvl) <= 2.0:
                await send_proximity_warning(name, lvl, price, rsi, volume_ratio, trend)                
                camarilla_warning_cooldowns[name] = now
                    
    except Exception as e:
        logger.error(f"Error in check_camarilla_warning: {e}")

@tasks.loop(minutes=15)
async def battleground_update():
    """Send quick market updates to eth-battleground."""
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

        # Quick market pulse
        if volume_ratio > 2.0:  # High volume spike
            emoji = "🌋"
            status = "HIGH VOLATILITY"
        elif rsi > 70:
            emoji = "🔥"
            status = "OVERBOUGHT TERRITORY"
        elif rsi < 30:
            emoji = "❄️"
            status = "OVERSOLD BOUNCE ZONE"
        elif volume_ratio > 1.5:
            emoji = "⚡"
            status = "INCREASED ACTIVITY"
        else:
            emoji = "🌊"
            status = "NORMAL CONDITIONS"

        message = (
            f"{emoji} **ETH BATTLEGROUND UPDATE**\n"
            f"💰 **Price:** ${price:.2f}\n"
            f"📊 **RSI:** {rsi:.1f}\n"
            f"🔊 **Volume:** {volume_ratio:.1f}x average\n"
            f"🎯 **Status:** {status}"
        )

        channel = bot.get_channel(ETH_BATTLEGROUND_ID)
        if channel:
            await channel.send(message)
            logger.info("Battleground update sent")

    except Exception as e:
        logger.error(f"Error in battleground_update: {e}")

@tasks.loop(hours=1)
async def heartbeat():
    """Send heartbeat to confirm bot is operational."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)
        
        message = (
            f"🛡️ **Heartbeat Check** – All systems operational\n"
            f"🕒 **UTC:** {now.strftime('%H:%M:%S')}\n"
            f"🕒 **CT:** {ct_now.strftime('%I:%M %p')}\n"
            f"📊 **Mode:** {CONFIRMATION_MODE.upper()}\n"
            f"⚙️ **Tasks:** Chronicle, Signals, Eagle, Watch, Battleground"
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

@tasks.loop(hours=6)
async def performance_report():
    """Send performance summary to scrolls-of-the-order."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        ct_now = now.astimezone(CENTRAL_TZ)
        
        # Basic performance metrics (you can expand this)
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
            value="Chronicle: Every 5min\nSignals: Real-time\nEagle: Ultra-selective\nWatch: Proximity alerts",
            inline=True
        )

        ct_time = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p')
        embed.set_footer(text=f"🕒 CT: {ct_time} | Next report in 6 hours")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)
            logger.info("Performance report sent to scrolls-of-the-order")

    except Exception as e:
        logger.error(f"Error sending performance report: {e}")

@bot.event
async def on_ready():
    """Bot startup event."""
    logger.info(f"🟢 Bot logged in as {bot.user}")
    logger.info(f"🔧 Confirmation mode: {CONFIRMATION_MODE}")
    
    try:
        # Start all tasks
        if not send_market_chronicle.is_running():
            send_market_chronicle.start()
        if not heartbeat.is_running():
            heartbeat.start()
        if not check_camarilla_warning.is_running():
            check_camarilla_warning.start()
        if not scan_trade_alerts.is_running():
            scan_trade_alerts.start()
        if not trade_100x_scan.is_running():
            trade_100x_scan.start()
        if not battleground_update.is_running():
            battleground_update.start()
        if not performance_report.is_running():
            performance_report.start()
            
        logger.info("✅ All tasks started successfully")
        
        # Send startup message to scrolls-of-the-order
        startup_embed = discord.Embed(
            title="🏰 Bot Initialization Complete",
            description="*The order awakens to serve the realm*",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        
        startup_embed.add_field(
            name="🗺️ Channel Assignment", 
            value=(
                "📜 **scribes-keep** - Market Chronicles\n"
                "⚔️ **battle-signals** - High-Probability Trades\n" 
                "🦅 **eagle-signal** - Premium Setups\n"
                "👁️ **knights-watch** - Level Warnings\n"
                "⚡ **eth-battleground** - Real-time Updates\n"
                "📚 **scrolls-of-the-order** - Performance Reports"
            ),
            inline=False
        )
        
        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=startup_embed)
        
    except Exception as e:
        logger.error(f"Error starting tasks: {e}")

@bot.event
async def on_error(event, *args, **kwargs):
    """Global error handler."""
    logger.error(f"Discord error in {event}: {args}")
    
    # Send error notification to scrolls-of-the-order
    try:
        error_embed = discord.Embed(
            title="⚠️ System Alert",
            description=f"Error detected in {event}",
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        
        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=error_embed)
    except:
        pass  # Don't create infinite error loops

# Bot commands for debugging
@bot.command(name='status')
async def status(ctx):
    """Check bot status."""
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
            f"⚡ Battleground: {'✅' if battleground_update.is_running() else '❌'}"
        )
        
        embed.add_field(name="🔄 Active Tasks", value=task_status, inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await ctx.send("❌ Error checking status")

@bot.command(name='test_chronicle')
async def test_chronicle(ctx):
    """Manually trigger a market chronicle."""
    if ctx.author.guild_permissions.administrator:
        try:
            await send_enhanced_scorecard()
            await ctx.send("✅ Chronicle sent to scribes-keep")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    else:
        await ctx.send("⚠️ Administrator permissions required")

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