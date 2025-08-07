# ============================================
# The Control Tower - Templar Knight Crypto - v9.0 PRODUCTION
# Complete Trade Tracking & Zero-Cost Deployment Ready
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
import gc
import json
import csv
from io import StringIO
import aiohttp
from discord.ext import commands, tasks
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from datetime import datetime, timezone, timedelta
import uuid

# ============================================
# RENDER FREE TIER OPTIMIZATIONS
# ============================================

MAX_CACHE_SIZE = 10
CACHE_CLEANUP_INTERVAL = 300
MAX_DATAFRAME_SIZE = 1000

# ============================================
# CONFIGURATION & GLOBALS
# ============================================

# === Logging (optimized for production) ===
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
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
SCROLLS_ORDER_ID = 1399067396488302623         # 📚 Performance logs & tracking
SETUP_ALERTS_ID = 1402053509490151424          # 🗺️ Camarilla alerts

# === Timezones ===
UTC = pytz.utc
CENTRAL_TZ = pytz.timezone("US/Central")

# === Optimized Settings ===
API_TIMEOUT = 8
MAX_RETRIES = 2
CACHE_DURATION = 60

# === Global Variables ===
active_trades = {}
last_100x_trade_time = None
last_scorecard_sent = None
last_trade_alert_time = {}
camarilla_warning_cooldowns = {}
CAMARILLA_COOLDOWN = {}
CAMARILLA_COOLDOWN_MINUTES = 5
setup_alert_cooldowns = {}
SETUP_ALERT_COOLDOWN_MINUTES = 15
bot_start_time = None

# ============================================
# MEMORY-OPTIMIZED CACHE SYSTEM
# ============================================

class MemoryOptimizedCache:
    def __init__(self, max_size=MAX_CACHE_SIZE):
        self.cache = {}
        self.expiry = {}
        self.access_times = {}
        self.max_size = max_size
    
    def get(self, key):
        if key in self.cache and time.time() < self.expiry.get(key, 0):
            self.access_times[key] = time.time()
            return self.cache[key]
        return None
    
    def set(self, key, value, ttl=30):
        if len(self.cache) >= self.max_size:
            oldest_key = min(self.access_times.items(), key=lambda x: x[1])[0]
            self.remove(oldest_key)
        
        self.cache[key] = value
        self.expiry[key] = time.time() + ttl
        self.access_times[key] = time.time()
    
    def remove(self, key):
        self.cache.pop(key, None)
        self.expiry.pop(key, None)
        self.access_times.pop(key, None)
    
    def cleanup(self):
        current_time = time.time()
        expired_keys = [k for k, exp_time in self.expiry.items() if current_time >= exp_time]
        for key in expired_keys:
            self.remove(key)
        gc.collect()

ohlc_cache = MemoryOptimizedCache()

# ============================================
# FLASK APP (OPTIMIZED FOR RENDER)
# ============================================

app = Flask(__name__)

@app.route("/")
def home():
    return "ETH Camarilla Alert Bot is running!"

@app.route("/health")
def health():
    global trade_tracker
    active_count = len(active_trades)
    cache_size = len(ohlc_cache.cache)
    
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "active_trades": active_count,
        "cache_size": cache_size,
        "tracking_enabled": trade_tracker is not None,
        "memory_usage": f"{cache_size}/{MAX_CACHE_SIZE} cache slots"
    }

@app.route("/stats")
def stats_endpoint():
    global trade_tracker, bot_start_time
    uptime = str(datetime.now(timezone.utc) - bot_start_time) if bot_start_time else "unknown"
    
    return {
        "uptime": uptime,
        "active_trades": len(active_trades),
        "cache_size": len(ohlc_cache.cache),
        "tracking_online": trade_tracker is not None
    }

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ============================================
# DISCORD BOT INITIALIZATION
# ============================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================
# TRADE TRACKING SYSTEM
# ============================================

class IntegratedTradeTracker:
    def __init__(self, bot):
        self.bot = bot
        self.trade_messages = {}
        self.daily_stats = {
            'date': datetime.now(timezone.utc).date(),
            'trades': 0,
            'wins': 0,
            'total_pnl': 0.0
        }
        
        # Google Sheets integration (if webhook provided)
        self.sheets_webhook = os.environ.get('GOOGLE_SHEETS_WEBHOOK')
        
    async def log_trade_entry(self, trade_data):
        """Log new trade entry to Discord"""
        try:
            channel = self.bot.get_channel(SCROLLS_ORDER_ID)
            if not channel:
                return False
                
            embed = discord.Embed(
                title=f"📈 Trade Entry - {trade_data['id']}",
                description=f"New {trade_data['direction']} signal at {trade_data['level_name']}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(name="🎯 Entry", value=f"${trade_data['entry_price']:.2f}", inline=True)
            embed.add_field(name="🛑 Stop Loss", value=f"${trade_data['sl']:.2f}", inline=True)
            embed.add_field(name="🎪 Targets", value=f"${trade_data['tp1']:.2f} / ${trade_data['tp2']:.2f}", inline=True)
            
            embed.add_field(name="⚔️ Knight", value=trade_data['knight'], inline=True)
            embed.add_field(name="📊 Score", value=f"{trade_data['score']}/6", inline=True)
            embed.add_field(name="🏆 Tier", value=get_tier_label(trade_data['score']), inline=True)
            
            risk_pct = abs((trade_data['entry_price'] - trade_data['sl']) / trade_data['entry_price']) * 100
            reward1_pct = abs((trade_data['tp1'] - trade_data['entry_price']) / trade_data['entry_price']) * 100
            
            embed.add_field(
                name="⚖️ Risk/Reward", 
                value=f"Risk: {risk_pct:.1f}%\nR:R = 1:{reward1_pct/risk_pct:.1f}", 
                inline=False
            )
            
            footer_data = {
                'id': trade_data['id'],
                'entry_price': trade_data['entry_price'],
                'direction': trade_data['direction'],
                'entry_time': datetime.now(timezone.utc).isoformat()
            }
            embed.set_footer(text=f"ID:{trade_data['id']} | Entry logged")
            
            message = await channel.send(embed=embed)
            self.trade_messages[trade_data['id']] = message.id
            
            self._update_daily_stats('entry')
            
            # Send to Google Sheets if configured
            if self.sheets_webhook:
                await self._send_to_sheets(trade_data, 'entry')
            
            logger.warning(f"✅ Trade {trade_data['id']} logged")
            return True
            
        except Exception as e:
            logger.error(f"Error logging trade entry: {e}")
            return False
    
    async def log_trade_exit(self, trade_id, exit_price, exit_reason, pnl_pct):
        """Update trade with exit information"""
        try:
            channel = self.bot.get_channel(SCROLLS_ORDER_ID)
            if not channel:
                return False
                
            message_id = self.trade_messages.get(trade_id)
            updated_original = False
            
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    embed = message.embeds[0]
                    
                    embed.color = discord.Color.green() if pnl_pct > 0 else discord.Color.red()
                    
                    embed.add_field(name="🏁 Exit Price", value=f"${exit_price:.2f}", inline=True)
                    embed.add_field(name="📋 Exit Reason", value=exit_reason, inline=True)
                    embed.add_field(name="💰 PnL", value=f"{pnl_pct:+.2f}%", inline=True)
                    
                    result_emoji = "🟢" if pnl_pct > 0 else "🔴"
                    embed.title = f"{result_emoji} Trade Complete - {trade_id}"
                    
                    await message.edit(embed=embed)
                    updated_original = True
                    
                except discord.NotFound:
                    pass
                    
            if not updated_original:
                embed = discord.Embed(
                    title=f"🏁 Trade Exit - {trade_id}",
                    description="Trade completed",
                    color=discord.Color.green() if pnl_pct > 0 else discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                embed.add_field(name="Exit Price", value=f"${exit_price:.2f}", inline=True)
                embed.add_field(name="Reason", value=exit_reason, inline=True)
                embed.add_field(name="PnL", value=f"{pnl_pct:+.2f}%", inline=True)
                
                await channel.send(embed=embed)
            
            self._update_daily_stats('exit', pnl_pct)
            
            # Update Google Sheets
            if self.sheets_webhook:
                await self._send_to_sheets({
                    'trade_id': trade_id,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl_pct': pnl_pct
                }, 'exit')
            
            logger.warning(f"✅ Trade exit {trade_id}: {pnl_pct:+.2f}%")
            return True
            
        except Exception as e:
            logger.error(f"Error logging trade exit: {e}")
            return False
    
    async def _send_to_sheets(self, data, action):
        """Send data to Google Sheets"""
        try:
            if action == 'entry':
                payload = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'trade_id': data['id'],
                    'direction': data['direction'],
                    'level_name': data['level_name'],
                    'entry_price': data['entry_price'],
                    'target1': data['tp1'],
                    'target2': data['tp2'],
                    'stop_loss': data['sl'],
                    'score': data['score'],
                    'knight': data['knight'],
                    'status': 'OPEN'
                }
            else:  # exit
                payload = {
                    'action': 'update',
                    'trade_id': data['trade_id'],
                    'exit_price': data['exit_price'],
                    'exit_reason': data['exit_reason'],
                    'pnl_pct': data['pnl_pct'],
                    'exit_time': datetime.now(timezone.utc).isoformat(),
                    'status': 'CLOSED'
                }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.sheets_webhook, json=payload, timeout=5) as response:
                    return response.status == 200
                    
        except Exception as e:
            logger.error(f"Sheets integration error: {e}")
            return False
    
    def _update_daily_stats(self, action, pnl=None):
        """Update daily statistics"""
        today = datetime.now(timezone.utc).date()
        
        if today > self.daily_stats['date']:
            self.daily_stats = {
                'date': today,
                'trades': 0,
                'wins': 0,
                'total_pnl': 0.0
            }
        
        if action == 'entry':
            self.daily_stats['trades'] += 1
        elif action == 'exit' and pnl is not None:
            if pnl > 0:
                self.daily_stats['wins'] += 1
            self.daily_stats['total_pnl'] += pnl
    
    async def generate_performance_report(self, days=7):
        """Generate performance report from Discord messages"""
        try:
            channel = self.bot.get_channel(SCROLLS_ORDER_ID)
            if not channel:
                return None
                
            since = datetime.now(timezone.utc) - timedelta(days=days)
            trades = []
            
            async for message in channel.history(after=since, limit=1000):
                if message.embeds and ("Trade Entry" in message.embeds[0].title or "Trade Complete" in message.embeds[0].title):
                    trade_data = self._parse_trade_from_message(message)
                    if trade_data:
                        trades.append(trade_data)
            
            return self._calculate_performance_stats(trades, days)
            
        except Exception as e:
            logger.error(f"Error generating performance report: {e}")
            return None
    
    def _parse_trade_from_message(self, message):
        """Extract trade data from Discord message"""
        try:
            embed = message.embeds[0]
            trade_data = {'timestamp': message.created_at}
            
            for field in embed.fields:
                if "PnL" in field.name:
                    pnl_str = field.value.replace('%', '').replace('+', '')
                    trade_data['pnl'] = float(pnl_str)
                    trade_data['closed'] = True
                elif "Exit Reason" in field.name:
                    trade_data['exit_reason'] = field.value
                elif "Score" in field.name:
                    score_str = field.value.split('/')[0]
                    trade_data['score'] = int(score_str)
            
            if "Trade Complete" in embed.title or embed.color == discord.Color.green() or embed.color == discord.Color.red():
                trade_data['closed'] = True
            else:
                trade_data['closed'] = False
                
            return trade_data
            
        except Exception:
            return None
    
    def _calculate_performance_stats(self, trades, days):
        """Calculate comprehensive performance statistics"""
        if not trades:
            return {'error': f'No trades found in last {days} days'}
        
        total_trades = len(trades)
        closed_trades = [t for t in trades if t.get('closed', False)]
        
        if not closed_trades:
            return {
                'period_days': days,
                'total_trades': total_trades,
                'closed_trades': 0,
                'pending_trades': total_trades,
                'message': 'No closed trades in this period'
            }
        
        winning_trades = [t for t in closed_trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in closed_trades if t.get('pnl', 0) <= 0]
        
        total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
        win_rate = (len(winning_trades) / len(closed_trades)) * 100
        avg_pnl = total_pnl / len(closed_trades)
        
        pnl_values = [t.get('pnl', 0) for t in closed_trades]
        best_trade = max(pnl_values) if pnl_values else 0
        worst_trade = min(pnl_values) if pnl_values else 0
        
        scores = [t.get('score', 0) for t in trades if t.get('score')]
        avg_score = sum(scores) / len(scores) if scores else 0
        
        exit_reasons = {}
        for trade in closed_trades:
            reason = trade.get('exit_reason', 'Unknown')
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        return {
            'period_days': days,
            'total_trades': total_trades,
            'closed_trades': len(closed_trades),
            'pending_trades': total_trades - len(closed_trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'best_trade': best_trade,
            'worst_trade': worst_trade,
            'avg_score': avg_score,
            'exit_reasons': exit_reasons
        }
    
    async def export_trades_csv(self, days=30):
        """Export trades as CSV file"""
        try:
            channel = self.bot.get_channel(SCROLLS_ORDER_ID)
            if not channel:
                return None
                
            since = datetime.now(timezone.utc) - timedelta(days=days)
            trades = []
            
            async for message in channel.history(after=since, limit=1000):
                if message.embeds and "Trade" in message.embeds[0].title:
                    trade_data = self._parse_trade_from_message(message)
                    if trade_data:
                        trades.append({
                            'Timestamp': trade_data['timestamp'].isoformat(),
                            'Trade_ID': self._extract_trade_id(message),
                            'Status': 'CLOSED' if trade_data.get('closed') else 'OPEN',
                            'PnL_Percent': trade_data.get('pnl', ''),
                            'Exit_Reason': trade_data.get('exit_reason', ''),
                            'Score': trade_data.get('score', '')
                        })
            
            if not trades:
                return None
            
            output = StringIO()
            writer = csv.DictWriter(output, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
            csv_content = output.getvalue()
            
            filename = f"trade_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
            return discord.File(StringIO(csv_content), filename=filename)
            
        except Exception as e:
            logger.error(f"Error exporting trades: {e}")
            return None
    
    def _extract_trade_id(self, message):
        """Extract trade ID from message"""
        try:
            if message.embeds:
                footer_text = message.embeds[0].footer.text
                if "ID:" in footer_text:
                    return footer_text.split("ID:")[1].split(" ")[0]
        except:
            pass
        return "Unknown"

# Initialize tracker
trade_tracker = None

# ============================================
# MARKET DATA & ANALYSIS FUNCTIONS
# ============================================

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

def fetch_ohlc(symbol="ETH", interval=1):
    cache_key = f"{symbol}_{interval}"
    
    cached_data = ohlc_cache.get(cache_key)
    if cached_data is not None:
        return cached_data

    kraken_map = {"ETH": "XETHZUSD"}
    pair = kraken_map.get(symbol.upper(), "XETHZUSD")
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": pair, "interval": interval}

    try:
        response = requests.get(url, params=params, timeout=API_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        if data.get("error"):
            raise Exception(f"Kraken API error: {data['error']}")
        
        raw = data["result"][pair]
        if not raw or len(raw) < 2:
            return None
            
        raw = raw[-MAX_DATAFRAME_SIZE:] if len(raw) > MAX_DATAFRAME_SIZE else raw
        
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ]).astype({
            "time": int, "open": float, "high": float,
            "low": float, "close": float, "volume": float
        })
        
        df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("datetime", inplace=True)
        
        ohlc_cache.set(cache_key, df, ttl=CACHE_DURATION)
        return df
        
    except Exception as e:
        logger.error(f"OHLC fetch failed: {e}")
        return None

def fetch_daily_ohlc():
    df = fetch_ohlc(interval=1440)
    if df is None or len(df) < 2:
        return None, None, None
    latest = df.iloc[-2]
    return latest["high"], latest["low"], latest["close"]

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

def score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend):
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

def get_tier_label(score):
    if score >= 5:
        return "S"
    elif score == 4:
        return "A"
    elif score == 3:
        return "B"
    else:
        return "C"

def assign_knight(strategy_type):
    if strategy_type == "Breakout":
        return "Sir Leonis Ironhart ⚔️"
    elif strategy_type == "Reversal":
        return "Sir Lucien Frostveil 🛡️"
    else:
        return "Orion Vellum 🌘"

# ============================================
# DISCORD EMBED FUNCTIONS
# ============================================

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
            logger.warning("✅ Enhanced scorecard sent")

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

        level_map = "```\n"
        levels_with_price = {**levels, "➤ Price": price}
        sorted_levels = sorted(levels_with_price.items(), key=lambda x: -x[1])

        for name, val in sorted_levels:
            label = f"{name:<8}"
            level_map += f"{label}{val:>8.2f}\n"

        level_map += "```"

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

        channel = bot.get_channel(SCRIBES_KEEP_ID)
        if channel:
            await channel.send(embed=embed)
            logger.warning("📡 Battlefield map sent")

    except Exception as e:
        logger.error(f"Error sending battlefield map: {e}")

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
            logger.warning(f"⚠️ Setup alert sent for {level_name}")

    except Exception as e:
        logger.error(f"Error in send_setup_alert: {e}")

async def send_battle_signal(direction, level_name, level_price, entry, stop_loss, targets, confidence, score, trade_type="Breakout"):
    try:
        knight = assign_knight(trade_type)
        color = discord.Color.green() if direction == "Long" else discord.Color.red()
        trade_id = str(uuid.uuid4())[:8]

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
            logger.warning(f"✅ Battle signal sent: {direction} at {level_name}")

        # Store in active trades for exit monitoring
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

        # NEW: Log to tracking system
        if trade_tracker:
            trade_data = {
                "id": trade_id,
                "entry_price": entry,
                "tp1": targets[0],
                "tp2": targets[1],
                "sl": stop_loss,
                "direction": direction,
                "level_name": level_name,
                "score": score,
                "knight": knight,
                "rating": confidence
            }
            await trade_tracker.log_trade_entry(trade_data)

    except Exception as e:
        logger.error(f"Error in send_battle_signal: {e}")

async def send_exit_alert(reason, price, thread_id, direction, alert_id):
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
            logger.warning(f"✅ 100x alert sent at ${price:.2f}")

    except Exception as e:
        logger.error(f"Error sending 100x alert: {e}")

async def send_proximity_warning(level_name, level_price, current_price, rsi, volume_ratio, trend):
    try:
        distance = current_price - level_price
        distance_pct = (distance / current_price) * 100
        direction = "🔼 Approaching from Below" if current_price < level_price else "🔽 Approaching from Above"

        if trend == "up" and rsi > 55 and volume_ratio > 1.2:
            outcome = "🟢 Likely Reversal"
        elif trend == "down" and rsi < 45 and volume_ratio > 1.2:
            outcome = "🔴 Likely Break"
        else:
            outcome = "⚪ Unclear / 50/50"

        embed = discord.Embed(
            title="🕰️ Knight's Warning – ETH",
            description=f"*Price is nearing {level_name}*",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="📍 Level", value=f"{level_name} – ${level_price:.2f}", inline=True)
        embed.add_field(name="💰 Current Price", value=f"${current_price:.2f}", inline=True)
        embed.add_field(name="📉 Distance", value=f"{direction}\nΔ {distance:+.2f} ({distance_pct:+.2f}%)", inline=False)
        embed.add_field(name="📊 RSI", value=f"{rsi:.1f}", inline=True)
        embed.add_field(name="🔊 Volume Ratio", value=f"{volume_ratio:.2f}x", inline=True)
        embed.add_field(name="🧠 Trend", value=f"{trend.title()}", inline=True)
        embed.add_field(name="🔮 Outlook", value=outcome, inline=False)

        now = datetime.now(timezone.utc)
        ct = now.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = now.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct}")

        channel = bot.get_channel(KNIGHTS_WATCH_ID)
        if channel:
            await channel.send(embed=embed)
            logger.warning(f"⚠️ Proximity warning sent for {level_name}")

    except Exception as e:
        logger.error(f"Error in send_proximity_warning: {e}")

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
            logger.warning("✅ Battleground update sent")

    except Exception as e:
        logger.error(f"Error in send_battleground_embed: {e}")

# ============================================
# SCANNER TASKS
# ============================================

@tasks.loop(minutes=2)
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

        closest = min(cam.items(), key=lambda x: abs(price - x[1]))
        level_name, level_price = closest
        level_dist_pct = abs(price - level_price) / price * 100
        direction = "Long" if price > level_price else "Short"

        body = abs(price - open_)
        range_ = high_ - low_
        body_ratio = body / range_ if range_ > 0 else 0
        volume_ok = volume > avg_volume * 1.2

        breakout_confirmed = (
            ((price > level_price and open_ < level_price) or
             (price < level_price and open_ > level_price)) and
            body_ratio > 0.5 and
            volume_ok
        )

        reversal_confirmed = (
            level_dist_pct <= 0.2 and
            ((high_ > level_price > price and direction == "Short") or
             (low_ < level_price < price and direction == "Long")) and
            body_ratio > 0.5 and
            volume_ok and
            ((rsi < 40 and direction == "Long") or (rsi > 60 and direction == "Short"))
        )

        score = score_trade(rsi, rsi_trend, direction, price, level_price, volume, avg_volume, price_trend)
        confidence = get_tier_label(score)

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

        key = f"{level_name}_{direction}"
        now = datetime.now(timezone.utc)

        if breakout_confirmed or reversal_confirmed:
            last_alert = CAMARILLA_COOLDOWN.get(key)
            if last_alert and (now - last_alert < timedelta(minutes=CAMARILLA_COOLDOWN_MINUTES)):
                return

            CAMARILLA_COOLDOWN[key] = now

            trade_type = "Breakout" if breakout_confirmed else "Reversal"

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

        else:
            missing = []
            if body_ratio <= 0.5:
                missing.append("🧱 Weak Candle Body")
            if not volume_ok:
                missing.append("🔇 Volume Below Threshold")
            if (price > level_price and open_ > level_price) or (price < level_price and open_ < level_price):
                missing.append("📉 No Breakout Structure")

            setup_key = f"{level_name}_{direction}_setup"
            last_setup = setup_alert_cooldowns.get(setup_key)
            if last_setup and (now - last_setup).total_seconds() < SETUP_ALERT_COOLDOWN_MINUTES * 60:
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
    global trade_tracker
    try:
        if "ETH" not in active_trades:
            return

        df = fetch_ohlc("ETH", interval=1)
        if df is None or len(df) < 1:
            return

        latest = df.iloc[-1]
        price = latest["close"]
        trade = active_trades["ETH"]

        tp1, tp2 = trade["tp1"], trade["tp2"]
        sl = trade["sl"]
        direction = trade["side"]
        alert_id = trade["id"]
        entry_price = trade["entry"]

        exit_reason = None
        
        if direction == "Long":
            if price >= tp2:
                exit_reason = "TP2 HIT"
            elif price >= tp1:
                exit_reason = "TP1 HIT"
            elif price <= sl:
                exit_reason = "SL HIT"
        elif direction == "Short":
            if price <= tp2:
                exit_reason = "TP2 HIT"
            elif price <= tp1:
                exit_reason = "TP1 HIT"
            elif price >= sl:
                exit_reason = "SL HIT"
        
        if exit_reason:
            # Calculate PnL
            if direction == "Long":
                pnl = ((price - entry_price) / entry_price) * 100
            else:
                pnl = ((entry_price - price) / entry_price) * 100
            
            # Send exit alert
            await send_exit_alert(exit_reason, price, trade.get("thread_id"), direction, alert_id)
            
            # Log to tracking system
            if trade_tracker:
                await trade_tracker.log_trade_exit(alert_id, price, exit_reason, pnl)
            
            # Remove from active trades
            del active_trades["ETH"]
            
            logger.warning(f"Trade {alert_id} closed: {exit_reason} at ${price:.2f} ({pnl:+.2f}%)")

    except Exception as e:
        logger.error(f"Error in monitor_trade_exits: {e}")

@tasks.loop(minutes=5)
async def memory_cleanup():
    try:
        ohlc_cache.cleanup()
        
        if trade_tracker:
            today = datetime.now(timezone.utc).date()
            if today > trade_tracker.daily_stats['date']:
                trade_tracker.daily_stats = {
                    'date': today,
                    'trades': 0,
                    'wins': 0,
                    'total_pnl': 0.0
                }
        
        gc.collect()
        
        cache_size = len(ohlc_cache.cache)
        if cache_size > 8:
            logger.warning(f"Cache size: {cache_size}/{MAX_CACHE_SIZE}")
            
    except Exception as e:
        logger.error(f"Memory cleanup error: {e}")

@tasks.loop(minutes=1)
async def battleground_loop():
    now = datetime.now(timezone.utc)
    if now.minute in [7, 23, 37, 52]:
        await send_battleground_embed()

@tasks.loop(minutes=1)
async def chronicle_loop():
    if datetime.now(timezone.utc).minute % 15 == 0:
        await send_enhanced_scorecard()

@tasks.loop(minutes=15)
async def battlefield_map_loop():
    await send_battlefield_map()

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
            value="✅ All systems operational\n✅ API connections stable\n✅ All channels active\n✅ Trade tracking online",
            inline=False
        )

        embed.add_field(
            name="📈 Activity Summary",
            value="Chronicle: Every 15min\nSignals: Real-time\nEagle: 100x high-conviction\nWatch: Level proximity\nTracking: Full logging",
            inline=True
        )

        embed.set_footer(text=f"🕒 {now.strftime('%H:%M UTC')} | {ct_now.strftime('%I:%M %p CT')}")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)
            logger.warning("✅ Performance report sent")

    except Exception as e:
        logger.error(f"Error sending performance report: {e}")

# ============================================
# ENHANCED COMMANDS WITH TRADE TRACKING
# ============================================

@bot.command(name='report')
async def performance_report_command(ctx, days: int = 7):
    """Generate detailed performance report"""
    if not trade_tracker:
        await ctx.send("❌ Tracking system not initialized")
        return
    
    try:
        stats = await trade_tracker.generate_performance_report(days)
        
        if not stats or 'error' in stats:
            await ctx.send(f"❌ {stats.get('error', 'No data available')}")
            return
        
        embed = discord.Embed(
            title=f"📊 Performance Report ({days} days)",
            description=f"*Analysis of trading performance over the last {days} days*",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📈 Trade Overview",
            value=f"**Total Trades:** {stats['total_trades']}\n"
                  f"**Completed:** {stats['closed_trades']}\n"
                  f"**Pending:** {stats['pending_trades']}\n"
                  f"**Avg Score:** {stats['avg_score']:.1f}/6",
            inline=True
        )
        
        if stats['closed_trades'] > 0:
            embed.add_field(
                name="🎯 Performance",
                value=f"**Win Rate:** {stats['win_rate']:.1f}%\n"
                      f"**Wins:** {stats['winning_trades']}\n"
                      f"**Losses:** {stats['losing_trades']}\n"
                      f"**Avg PnL:** {stats['avg_pnl']:+.2f}%",
                inline=True
            )
            
            embed.add_field(
                name="💰 PnL Breakdown",
                value=f"**Total PnL:** {stats['total_pnl']:+.2f}%\n"
                      f"**Best Trade:** {stats['best_trade']:+.2f}%\n"
                      f"**Worst Trade:** {stats['worst_trade']:+.2f}%\n"
                      f"**Risk/Reward:** {abs(stats['total_pnl']/max(abs(stats['worst_trade']), 0.01)):.1f}",
                inline=True
            )
        
        if stats.get('exit_reasons'):
            reasons_text = "\n".join([f"**{reason}:** {count}" for reason, count in stats['exit_reasons'].items()])
            embed.add_field(name="🏁 Exit Analysis", value=reasons_text, inline=False)
        
        if stats['closed_trades'] > 0:
            if stats['win_rate'] >= 60 and stats['total_pnl'] > 0:
                rating = "🟢 Excellent"
            elif stats['win_rate'] >= 50 and stats['total_pnl'] > -5:
                rating = "🟡 Good"
            elif stats['win_rate'] >= 40:
                rating = "🟠 Needs Improvement"
            else:
                rating = "🔴 Poor"
            
            embed.add_field(name="📊 Overall Rating", value=rating, inline=False)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error in performance report: {e}")
        await ctx.send(f"❌ Error generating report: {e}")

@bot.command(name='stats')
async def quick_stats(ctx):
    """Quick performance overview"""
    if not trade_tracker:
        await ctx.send("❌ Tracking system not initialized")
        return
    
    try:
        today_stats = trade_tracker.daily_stats
        week_stats = await trade_tracker.generate_performance_report(7)
        
        embed = discord.Embed(
            title="⚡ Quick Stats",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📅 Today",
            value=f"**Trades:** {today_stats['trades']}\n"
                  f"**Wins:** {today_stats['wins']}\n"
                  f"**PnL:** {today_stats['total_pnl']:+.2f}%",
            inline=True
        )
        
        if week_stats and 'closed_trades' in week_stats and week_stats['closed_trades'] > 0:
            embed.add_field(
                name="📈 This Week",
                value=f"**Trades:** {week_stats['closed_trades']}\n"
                      f"**Win Rate:** {week_stats['win_rate']:.1f}%\n"
                      f"**Total PnL:** {week_stats['total_pnl']:+.2f}%",
                inline=True
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name='export')
async def export_trades(ctx, days: int = 30):
    """Export trades as CSV file"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("⚠️ Administrator permissions required")
        return
    
    if not trade_tracker:
        await ctx.send("❌ Tracking system not initialized")
        return
    
    try:
        await ctx.send("📊 Generating trade export...")
        
        csv_file = await trade_tracker.export_trades_csv(days)
        
        if csv_file:
            embed = discord.Embed(
                title="📁 Trade Export",
                description=f"Trade data for the last {days} days",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            await ctx.send(embed=embed, file=csv_file)
        else:
            await ctx.send("❌ No trades found to export")
            
    except Exception as e:
        await ctx.send(f"❌ Export error: {e}")

@bot.command(name='trades')
async def list_recent_trades(ctx, count: int = 5):
    """List recent trades"""
    if not trade_tracker:
        await ctx.send("❌ Tracking system not initialized")
        return
    
    try:
        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if not channel:
            await ctx.send("❌ Trading log channel not found")
            return
        
        trades = []
        async for message in channel.history(limit=50):
            if message.embeds and "Trade" in message.embeds[0].title:
                trades.append(message)
                if len(trades) >= count:
                    break
        
        if not trades:
            await ctx.send("❌ No recent trades found")
            return
        
        embed = discord.Embed(
            title=f"📋 Recent Trades (Last {len(trades)})",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        for i, trade in enumerate(trades[:count], 1):
            trade_embed = trade.embeds[0]
            status = "🟢" if "Complete" in trade_embed.title else "🟡"
            
            trade_id = trade_tracker._extract_trade_id(trade)
            created_time = trade.created_at.strftime('%m/%d %H:%M')
            
            embed.add_field(
                name=f"{status} {i}. {trade_id}",
                value=f"**Time:** {created_time}\n{trade_embed.title}",
                inline=False
            )
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Error listing trades: {e}")

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
            f"📜 Chronicle: {'✅' if chronicle_loop.is_running() else '❌'}\n"
            f"⚔️ Signals: {'✅' if scan_camarilla_trades.is_running() else '❌'}\n"
            f"🦅 Eagle: {'✅' if trade_100x_scan.is_running() else '❌'}\n"
            f"👁️ Watch: {'✅' if check_camarilla_warning.is_running() else '❌'}\n"
            f"🏰 Battleground: {'✅' if battleground_loop.is_running() else '❌'}\n"
            f"📊 Tracking: {'✅' if trade_tracker else '❌'}"
        )

        embed.add_field(name="🔄 Active Tasks", value=task_status, inline=False)
        
        if trade_tracker:
            embed.add_field(
                name="📈 Today's Activity", 
                value=f"Trades: {trade_tracker.daily_stats['trades']}\nWins: {trade_tracker.daily_stats['wins']}\nPnL: {trade_tracker.daily_stats['total_pnl']:+.2f}%", 
                inline=True
            )
        
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in !status command: {e}")
        await ctx.send("❌ Error checking status")

@bot.command(name='memory')
async def memory_usage(ctx):
    """Check memory usage and cache status"""
    if ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="💾 Memory Status",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📊 Cache Usage",
            value=f"Items: {len(ohlc_cache.cache)}/{MAX_CACHE_SIZE}\nActive Trades: {len(active_trades)}",
            inline=True
        )
        
        if trade_tracker:
            stats = trade_tracker.daily_stats
            embed.add_field(
                name="📈 Today's Performance",
                value=f"Total Trades: {stats['trades']}\nWins: {stats['wins']}\nPnL: {stats['total_pnl']:+.2f}%",
                inline=True
            )
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("⚠️ Administrator permissions required")

@bot.command(name='cleanup')
async def manual_cleanup(ctx):
    """Manually trigger memory cleanup"""
    if ctx.author.guild_permissions.administrator:
        old_cache_size = len(ohlc_cache.cache)
        ohlc_cache.cleanup()
        gc.collect()
        new_cache_size = len(ohlc_cache.cache)
        
        await ctx.send(f"🧹 Cleanup complete: {old_cache_size} → {new_cache_size} cached items")
    else:
        await ctx.send("⚠️ Administrator permissions required")

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

@bot.command(name='health')
async def system_health(ctx):
    """Comprehensive system health check"""
    if ctx.author.guild_permissions.administrator:
        embed = discord.Embed(title="🏥 System Health", color=discord.Color.green())
        
        # Basic bot health
        uptime = datetime.now(timezone.utc) - bot_start_time if bot_start_time else timedelta(0)
        embed.add_field(name="⏱️ Uptime", value=str(uptime).split('.')[0], inline=True)
        
        # Tracking system health
        if trade_tracker:
            embed.add_field(name="📊 Tracking", value="✅ Online", inline=True)
            embed.add_field(name="💾 Trade Messages", value=f"{len(trade_tracker.trade_messages)} tracked", inline=True)
        
        # Memory usage
        cache_size = len(ohlc_cache.cache)
        embed.add_field(name="🧠 Memory", value=f"{cache_size}/{MAX_CACHE_SIZE} cache slots", inline=True)
        embed.add_field(name="🔄 Active Trades", value=str(len(active_trades)), inline=True)
        
        # Google Sheets integration
        sheets_status = "✅ Configured" if trade_tracker and trade_tracker.sheets_webhook else "❌ Not configured"
        embed.add_field(name="📊 Google Sheets", value=sheets_status, inline=True)
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("⚠️ Administrator permissions required")

# ============================================
# BOT STARTUP AND EVENTS
# ============================================

@bot.event
async def on_ready():
    global trade_tracker, bot_start_time
    bot_start_time = datetime.now(timezone.utc)
    
    # Initialize tracking system
    trade_tracker = IntegratedTradeTracker(bot)
    
    logger.warning(f"🟢 Bot logged in as {bot.user}")

    try:
        # Start all monitoring tasks
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
        if not battlefield_map_loop.is_running():
            battlefield_map_loop.start()
        if not memory_cleanup.is_running():
            memory_cleanup.start()

        # Send startup notification
        embed = discord.Embed(
            title="🏰 Control Tower Activated - v9.0",
            description="*Complete trade tracking and alert systems online*",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        
        embed.add_field(
            name="📊 New Features", 
            value="✅ Full Trade Tracking\n✅ Performance Analytics\n✅ CSV Export\n✅ Real-time Stats\n✅ Google Sheets Integration", 
            inline=True
        )
        embed.add_field(
            name="💾 Storage", 
            value="Discord Messages\nZero Cost\nFully Persistent\nBackup Ready", 
            inline=True
        )
        embed.add_field(
            name="📈 Commands", 
            value="`!report [days]`\n`!stats`\n`!export [days]`\n`!trades [count]`\n`!health`", 
            inline=True
        )
        
        embed.add_field(
            name="🔧 System Info",
            value=f"**Deployment:** Render Free Tier\n**Memory:** Optimized for 512MB\n**Uptime:** UptimeRobot monitoring\n**Cost:** $0.00/month",
            inline=False
        )
        
        sheets_msg = "✅ Enabled" if trade_tracker.sheets_webhook else "❌ Add GOOGLE_SHEETS_WEBHOOK env var"
        embed.add_field(name="📊 Google Sheets", value=sheets_msg, inline=False)
        
        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)

    except Exception as e:
        logger.error(f"Error during startup: {e}")

@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"Discord event error in {event}: {args}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ You don't have permission to use this command")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid arguments. Check the command usage.")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(f"❌ An error occurred: {str(error)}")

# ============================================
# FLASK THREAD STARTER
# ============================================

def start_flask_thread():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == "__main__":
    logger.warning("🚀 Starting Control Tower ETH Camarilla Bot v9.0 - Production Ready")
    logger.warning("📊 Features: Full trade tracking, performance analytics, zero-cost deployment")
    logger.warning("🔧 Optimized for: Render free tier + UptimeRobot monitoring")
    
    # Start Flask server for health checks
    start_flask_thread()
    
    # Start Discord bot
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")
        exit(1)