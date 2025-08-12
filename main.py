# ============================================
# Control Tower - Fixed v11.1 (H5/L5 + Setup Intel + Rehydrate/BE/Trail)
# ============================================
# Features:
# - Class-based core (Config, TradeData, TradeManager, DB, Sheets)
# - Google Sheets write + REHYDRATE of OPEN trades on startup
# - Trade lifecycle: TP1 partial -> set BE -> optional trailing (ATR/Chandelier)
# - Camarilla scanner with: confirmations, H5/L5 continuation, setup alerts
# - Discord commands: !status, !config, !export, !sheets_test
# - Flask health endpoint for uptime monitors (Render-friendly)
#
# Environment variables:
#   TOKEN                     (Discord)
#   GOOGLE_SHEETS_WEBHOOK     (Apps Script /exec URL)
#   SHEETS_TOKEN              (shared secret; sent in header x-app-secret and GET key)
#   PARTIAL_FRACTION          (0.0-1.0, default 0.5)
#   BE_AFTER_TP1              (true/false)
#   BE_OFFSET_PCT             (e.g., 0.05 for +0.05% over entry at BE)
#   TRAIL_MODE                (none|ATR|CHAND)
#   TRAIL_ATR_PERIOD          (int, e.g., 14)
#   TRAIL_ATR_MULT            (float, e.g., 3.0)
#   CHAN_LOOKBACK             (int for chandelier, e.g., 22)
#   PAIR                      (default: ETHUSD)
#   INTERVAL_MIN              (default: 5) - scan candle interval (minutes)
#
# ============================================

import os
import asyncio
import json
import math
import sqlite3
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import numpy as np
import pandas as pd
from flask import Flask, jsonify
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks

# TA imports with fallbacks for missing libraries
try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD
    from ta.volatility import AverageTrueRange
    TA_AVAILABLE = True
except ImportError:
    RSIIndicator = None
    EMAIndicator = None
    MACD = None
    AverageTrueRange = None
    TA_AVAILABLE = False

# -------- Logging --------
logging.basicConfig(
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log') if os.path.exists('.') else logging.StreamHandler()
    ]
)
log = logging.getLogger('control_tower')

# -------- Flask (health) --------
app = Flask(__name__)

@app.route('/')
def health_root():
    return jsonify(
        ok=True, 
        service="Control Tower Fixed v11.1",
        timestamp=datetime.now(timezone.utc).isoformat(),
        ta_available=TA_AVAILABLE
    )

@app.route('/health')
def health_check():
    global trade_manager, cfg
    try:
        active_count = len(getattr(trade_manager, 'active', {}))
        return jsonify({
            "status": "healthy",
            "version": "11.1-fixed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_trades": active_count,
            "pair": getattr(cfg, 'pair', 'ETHUSD'),
            "ta_library": TA_AVAILABLE,
            "sheets_configured": bool(getattr(cfg, 'sheets_url', None))
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }), 500

# Run Flask in a thread to avoid blocking Discord
def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.error(f"Flask error: {e}")

# -------- Config --------
class TrailMode(Enum):
    NONE = "none"
    ATR = "ATR"
    CHAND = "CHAND"

@dataclass
class BotConfig:
    token: str
    sheets_url: Optional[str] = None
    sheets_token: Optional[str] = None
    partial_fraction: float = 0.5
    be_after_tp1: bool = True
    be_offset_pct: float = 0.0
    trail_mode: TrailMode = TrailMode.NONE
    trail_atr_period: int = 14
    trail_atr_mult: float = 3.0
    chand_lookback: int = 22
    pair: str = "ETHUSD"
    interval_min: int = 5

    @staticmethod
    def from_env() -> "BotConfig":
        load_dotenv()
        token = os.getenv("TOKEN", "").strip()
        if not token:
            raise

def calc_camarilla(df: pd.DataFrame) -> Dict[str, float]:
    """Calculate Camarilla pivot levels with error handling"""
    try:
        if len(df) < 2:
            raise ValueError("Not enough bars")
        
        # Use previous bar for pivot calculation
        prev = df.iloc[-2]
        H = float(prev["high"])
        L = float(prev["low"]) 
        C = float(prev["close"])
        r = H - L
        
        if r <= 0:
            raise ValueError("Invalid range")
        
        # Extension method (commonly used variant)
        L3 = C - (r * 1.1/12)
        H3 = C + (r * 1.1/12)
        L4 = C - (r * 1.1/6)
        H4 = C + (r * 1.1/6)
        L5 = C - (r * 1.1/2)
        H5 = C + (r * 1.1/2)
        # Optional L6/H6 for continuation context
        L6 = C - (r * 1.1*0.67)
        H6 = C + (r * 1.1*0.67)
        
        return {
            "L3": L3, "L4": L4, "L5": L5, "L6": L6,
            "H3": H3, "H4": H4, "H5": H5, "H6": H6,
            "P": C
        }
    except Exception as e:
        log.error(f"Camarilla calculation error: {e}")
        return {}

def confirm_breakout(c, o, h, l, vol, avg_vol, level: float, direction: TradeDirection) -> Tuple[bool, Dict[str, Any]]:
    """Confirm breakout with multiple criteria"""
    try:
        # Body > 50% of range + close beyond level + volume > 1.2x avg
        rng = max(h - l, 1e-9)
        body_ratio = abs(c - o) / rng
        close_beyond = (direction == TradeDirection.LONG and c > level) or (direction == TradeDirection.SHORT and c < level)
        vol_ok = vol > (avg_vol * 1.2 if avg_vol > 0 else vol)
        ok = (body_ratio > 0.5) and close_beyond and vol_ok
        meta = {"body_ratio": body_ratio, "vol_ok": vol_ok, "close_beyond": close_beyond}
        return ok, meta
    except Exception as e:
        log.error(f"Breakout confirmation error: {e}")
        return False, {}

def likely_reversal(c, h, l, level: float, direction: TradeDirection) -> bool:
    """Simple wick test for reversal patterns"""
    try:
        if direction == TradeDirection.LONG:  # reversal up from L5
            return (l < level) and (c > level)
        else:  # reversal down from H5
            return (h > level) and (c < level)
    except Exception:
        return False

# -------- Discord & Bot --------
# Initialize Discord bot
INTENTS = discord.Intents.default()
INTENTS.message_content = True

# Global variables (will be initialized in main())
cfg: Optional[BotConfig] = None
bot: Optional[commands.Bot] = None
db: Optional[DatabaseManager] = None
sheets: Optional[GoogleSheetsIntegration] = None
trade_manager: Optional[TradeManager] = None
mdp: Optional[MarketDataProvider] = None

# -------- Embed Functions --------
def fmt_pct(x: float) -> str:
    return f"{x:.2f}%"

def status_embed() -> discord.Embed:
    try:
        e = discord.Embed(
            title="🛡️ Control Tower Status", 
            color=discord.Color.blurple(), 
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="Pair", value=cfg.pair if cfg else "N/A", inline=True)
        e.add_field(name="Interval", value=f"{cfg.interval_min}m" if cfg else "N/A", inline=True)
        e.add_field(name="Active Trades", value=str(len(trade_manager.active)) if trade_manager else "0", inline=True)
        e.add_field(name="Sheets", value="ON" if (cfg and cfg.sheets_url and cfg.sheets_token) else "OFF", inline=True)
        e.add_field(name="Trail Mode", value=cfg.trail_mode.value if cfg else "N/A", inline=True)
        e.add_field(name="BE after TP1", value="ON" if (cfg and cfg.be_after_tp1) else "OFF", inline=True)
        e.add_field(name="TA Library", value="Available" if TA_AVAILABLE else "Fallback", inline=True)
        return e
    except Exception as e:
        log.error(f"Status embed error: {e}")
        return discord.Embed(title="Status Error", description=str(e), color=discord.Color.red())

def config_embed() -> discord.Embed:
    try:
        e = discord.Embed(
            title="⚙️ Trading Config", 
            color=discord.Color.dark_teal(), 
            timestamp=datetime.now(timezone.utc)
        )
        if cfg:
            e.add_field(name="Partial Fraction", value=str(cfg.partial_fraction), inline=True)
            e.add_field(name="BE Offset %", value=str(cfg.be_offset_pct), inline=True)
            e.add_field(name="Trail", value=cfg.trail_mode.value, inline=True)
            e.add_field(name="ATR Period/Mult", value=f"{cfg.trail_atr_period}/{cfg.trail_atr_mult}", inline=True)
            e.add_field(name="Chandelier Lookback", value=str(cfg.chand_lookback), inline=True)
            e.add_field(name="Sheets URL set", value="Yes" if cfg.sheets_url else "No", inline=True)
        else:
            e.description = "Configuration not loaded"
        return e
    except Exception as e:
        log.error(f"Config embed error: {e}")
        return discord.Embed(title="Config Error", description=str(e), color=discord.Color.red())

async def send_battle_signal(channel: discord.TextChannel, t: TradeData):
    try:
        color = discord.Color.green() if t.direction == TradeDirection.LONG else discord.Color.red()
        e = discord.Embed(
            title=f"⚔️ Battle Signal - {t.asset} {t.direction.name} {t.trade_type or ''}".strip(),
            description=f"*{t.knight or 'Knight'} calls for battle at {t.level_name or 'Level'}*",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="🎯 Level", value=f"{t.level_name} ({t.level_price:.2f})" if t.level_price else (t.level_name or "?"), inline=True)
        e.add_field(name="⚔️ Entry", value=f"{t.entry_price:.2f}", inline=True)
        e.add_field(name="🛑 Stop", value=f"{t.sl:.2f}", inline=True)
        e.add_field(name="🎯 TP1", value=f"{t.tp1:.2f}", inline=True)
        e.add_field(name="🏁 TP2", value=f"{t.tp2:.2f}", inline=True)
        if t.rating: 
            e.add_field(name="📊 Rating", value=t.rating, inline=True)
        if t.score: 
            e.add_field(name="🧮 Score", value=str(t.score), inline=True)
        e.set_footer(text="TP1 sets BE (silent). Only TP2/SL announced.")
        await channel.send(embed=e)
    except Exception as e:
        log.error(f"Battle signal error: {e}")

async def send_setup_alert(channel: discord.TextChannel, title: str, fields: Dict[str, Any], watch_next: List[str]):
    try:
        e = discord.Embed(title=title, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        for k, v in fields.items():
            e.add_field(name=k, value=str(v), inline=True)
        if watch_next:
            e.add_field(name="👀 Watch Next", value="\n".join(f"- {x}" for x in watch_next), inline=False)
        await channel.send(embed=e)
    except Exception as e:
        log.error(f"Setup alert error: {e}")

async def send_exit_alert(channel: discord.TextChannel, t: TradeData, reason: str, price: float, pnl_pct: float):
    try:
        color = discord.Color.green() if "TP2" in reason else discord.Color.red()
        e = discord.Embed(
            title=f"🏁 Exit - {t.asset} {reason}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="Price", value=f"{price:.2f}", inline=True)
        e.add_field(name="PnL", value=fmt_pct(pnl_pct), inline=True)
        e.add_field(name="Trade ID", value=t.id, inline=True)
        await channel.send(embed=e)
    except Exception as e:
        log.error(f"Exit alert error: {e}")

# -------- Signal Generation --------
async def compute_signals(df: pd.DataFrame) -> Dict[str, Any]:
    """Compute trading signals from OHLC data"""
    try:
        levels = calc_camarilla(df)
        if not levels:
            return {"error": "Failed to calculate levels"}
            
        last = df.iloc[-1]
        c, o, h, l, v = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"]), float(last["volume"])
        avg_vol = float(df["volume"].tail(30).mean()) if len(df) >= 30 else v

        # Breakout checks around H5/L5
        sig = {"confirm": None, "setup": None, "levels": levels}
        
        # Long breakout over H5
        ok_long, meta_long = confirm_breakout(c, o, h, l, v, avg_vol, levels["H5"], TradeDirection.LONG)
        # Short breakout under L5
        ok_short, meta_short = confirm_breakout(c, o, h, l, v, avg_vol, levels["L5"], TradeDirection.SHORT)

        if ok_long:
            sig["confirm"] = {
                "direction": "Long", 
                "level": "H5", 
                "level_price": levels["H5"], 
                "meta": meta_long, 
                "type": "Breakout"
            }
        elif ok_short:
            sig["confirm"] = {
                "direction": "Short", 
                "level": "L5", 
                "level_price": levels["L5"], 
                "meta": meta_short, 
                "type": "Breakout"
            }
        else:
            # Setup intelligence (missing criteria)
            miss = []
            rng = max(h-l, 1e-9)
            if abs(c-o)/rng <= 0.5: 
                miss.append("💡 Body < 50%")
            if not (v > avg_vol*1.2): 
                miss.append("📉 Volume < 1.2× avg")
                
            # Likely outcome hint
            watch = []
            if c < levels["H5"] and c > levels["L5"]:
                watch.append(f"Look for break over {levels['H5']:.2f} or under {levels['L5']:.2f}")
            elif c >= levels["H5"]:
                watch.append("Continuation above H5 if momentum/vol holds")
            else:
                watch.append("Continuation below L5 if momentum/vol holds")
            sig["setup"] = {"missing": miss, "watch": watch, "price": c}

        # Reversal hints at H5/L5 (not an entry by itself)
        if likely_reversal(c, h, l, levels["H5"], TradeDirection.SHORT):
            sig["setup"] = sig.get("setup", {})
            sig["setup"]["reversal_hint"] = f"Reversal from H5 observed"
        if likely_reversal(c, h, l, levels["L5"], TradeDirection.LONG):
            sig["setup"] = sig.get("setup", {})
            sig["setup"]["reversal_hint2"] = f"Reversal from L5 observed"
            
        return sig
    except Exception as e:
        log.error(f"Signal computation error: {e}")
        return {"error": str(e)}

# -------- Bot Initialization --------
def create_bot():
    """Create and configure the Discord bot"""
    bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)
    
    # -------- Discord Commands --------
    @bot.command(name="status")
    async def _status(ctx: commands.Context):
        try:
            await ctx.send(embed=status_embed())
        except Exception as e:
            await ctx.send(f"Status error: {e}")

    @bot.command(name="config")
    async def _config(ctx: commands.Context):
        try:
            await ctx.send(embed=config_embed())
        except Exception as e:
            await ctx.send(f"Config error: {e}")

    @bot.command(name="export")
    async def _export(ctx: commands.Context):
        try:
            # Export DB to CSV and upload
            path = "trades_export.csv"
            with sqlite3.connect(db.path) as conn:
                df = pd.read_sql_query("SELECT * FROM trades", conn)
            df.to_csv(path, index=False)
            await ctx.send(file=discord.File(path))
        except Exception as e:
            await ctx.send(f"Export error: {e}")

    @bot.command(name="sheets_test")
    async def _sheets_test(ctx: commands.Context):
        try:
            now = datetime.now(timezone.utc)
            t = TradeData(
                id=now.strftime("TEST%H%M%S"),
                asset=cfg.pair,
                direction=TradeDirection.LONG,
                entry_price=2500.0, sl=2450.0, tp1=2525.0, tp2=2550.0,
                rating="A", score=5, level_name="H4", level_price=2500.0, trade_type="Breakout"
            )
            await trade_manager.open_trade(t)
            await ctx.send("✅ Posted test entry to Google Sheets (if configured).")
        except Exception as e:
            await ctx.send(f"Sheets test error: {e}")

    @bot.command(name="rehydrate")
    async def _rehydrate(ctx: commands.Context):
        try:
            before_count = len(trade_manager.active)
            await trade_manager.rehydrate()
            after_count = len(trade_manager.active)
            rehydrated = after_count - before_count
            
            embed = discord.Embed(
                title="🔄 Manual Rehydration Complete",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Before", value=str(before_count), inline=True)
            embed.add_field(name="After", value=str(after_count), inline=True)
            embed.add_field(name="Rehydrated", value=str(rehydrated), inline=True)
            
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"Rehydration error: {e}")

    # -------- Scanner Task --------
    @tasks.loop(seconds=45)
    async def scan_loop():
        try:
            await mdp.start()
            await trade_manager.start()
            
            try:
                df = await mdp.fetch_ohlc(500)
            except Exception as e:
                log.warning(f"OHLC fetch failed: {e}")
                return

            sig = await compute_signals(df)
            if "error" in sig:
                log.warning(f"Signal computation failed: {sig['error']}")
                return
                
            # Find a channel to send messages
            ch: Optional[discord.TextChannel] = None
            for g in bot.guilds:
                for c in g.text_channels:
                    if c.permissions_for(g.me).send_messages:
                        ch = c
                        break
                if ch: 
                    break
            if ch is None:
                log.warning("No available text channel found")
                return

            # Confirmed → open trade & send Battle Signal
            if sig["confirm"]:
                info = sig["confirm"]
                direction = TradeDirection.LONG if info["direction"] == "Long" else TradeDirection.SHORT
                price = float(df.iloc[-1]["close"])
                
                # Entry/SL/TP scaffolding (example: 1% SL, TP1 1.5%, TP2 3%)
                risk = 0.01
                tp1p = 0.015
                tp2p = 0.03
                
                if direction == TradeDirection.LONG:
                    entry = price
                    sl = entry * (1 - risk)
                    tp1 = entry * (1 + tp1p)
                    tp2 = entry * (1 + tp2p)
                else:
                    entry = price
                    sl = entry * (1 + risk)
                    tp1 = entry * (1 - tp1p)
                    tp2 = entry * (1 - tp2p)

                t = TradeData(
                    id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                    asset=cfg.pair,
                    direction=direction,
                    entry_price=entry, sl=sl, tp1=tp1, tp2=tp2,
                    rating="A", score=6, level_name=info["level"], 
                    level_price=info["level_price"], trade_type=info["type"],
                    enhanced_data=info.get("meta", {}),
                    knight="Sir Camarilla ⚔️"
                )
                await trade_manager.open_trade(t)
                await send_battle_signal(ch, t)

            # Setup alert (not confirmed)
            elif sig["setup"]:
                fields = {
                    "Price": f"{sig['setup']['price']:.2f}" if "price" in sig["setup"] else "-",
                    "Missing": "\n".join(sig["setup"].get("missing", [])) or "—",
                }
                watch_next = sig["setup"].get("watch", [])
                await send_setup_alert(ch, "🛡️ Setup Alert (Awaiting Confirmation)", fields, watch_next)

            # Evaluate exits for active trades (use last candle prices)
            if len(trade_manager.active) > 0:
                highs = df["high"].to_numpy()
                lows = df["low"].to_numpy() 
                closes = df["close"].to_numpy()
                last_price = float(closes[-1])
                now = datetime.now(timezone.utc)

                # Update trailing stops for each active trade and evaluate exit
                for trade_id, t in list(trade_manager.active.items()):
                    # Update trailing
                    trade_manager._update_trail(t, highs, lows, closes)
                    # Evaluate exits
                    result = await trade_manager.evaluate_exit(t, last_price, now)
                    if result:
                        reason, px, pnl = result
                        await send_exit_alert(ch, t, reason, px, pnl)
                        if trade_id in trade_manager.active:
                            del trade_manager.active[trade_id]
                        if trade_manager.session:
                            await sheets.send_trade_exit(trade_manager.session, t.id, reason, px, now.isoformat(), pnl)
                            
        except Exception as e:
            log.error(f"Scan loop error: {e}")

    @scan_loop.before_loop
    async def before_scan():
        await bot.wait_until_ready()
        await trade_manager.start()
        await mdp.start()

    # -------- Bot Events --------
    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user} ({bot.user.id})")
        try:
            # Rehydrate before starting loops
            await trade_manager.rehydrate()
            if not scan_loop.is_running():
                scan_loop.start()
        except Exception as e:
            log.error(f"Bot ready error: {e}")

    @bot.event
    async def on_error(event, *args, **kwargs):
        log.error(f"Discord error in {event}: {args}")

    return bot

# -------- Main Entrypoint --------
def main():
    """Main entry point with proper error handling"""
    try:
        # Initialize global configuration
        global cfg, bot, db, sheets, trade_manager, mdp
        
        log.info("Starting Control Tower Fixed v11.1...")
        
        # Load configuration
        cfg = BotConfig.from_env()
        log.info(f"Configuration loaded: pair={cfg.pair}, interval={cfg.interval_min}m")
        
        # Initialize components
        db = DatabaseManager("trades.db")
        sheets = GoogleSheetsIntegration(cfg.sheets_url, cfg.sheets_token)
        trade_manager = TradeManager(cfg, db, sheets)
        mdp = MarketDataProvider(cfg.pair, cfg.interval_min)
        
        # Create bot
        bot = create_bot()
        
        # Start Flask in a side thread
        log.info("Starting Flask health server...")
        threading.Thread(target=run_flask, daemon=True).start()
        
        # Start Discord bot
        log.info("Starting Discord bot...")
        bot.run(cfg.token)
        
    except Exception as e:
        log.error(f"Main execution error: {e}")
        raise

if __name__ == "__main__":
    main() RuntimeError("Discord TOKEN env var is required")

        sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "").strip() or None
        sheets_token = os.getenv("SHEETS_TOKEN", "").strip() or None
        
        try:
            partial_fraction = float(os.getenv("PARTIAL_FRACTION", "0.5"))
        except ValueError:
            partial_fraction = 0.5
            
        be_after_tp1 = os.getenv("BE_AFTER_TP1", "true").lower() in ("1", "true", "yes", "y")
        
        try:
            be_offset_pct = float(os.getenv("BE_OFFSET_PCT", "0.0"))
        except ValueError:
            be_offset_pct = 0.0
            
        trail_mode = os.getenv("TRAIL_MODE", "none").upper()
        if trail_mode not in ("NONE", "ATR", "CHAND"):
            trail_mode = "NONE"
        # FIX: Convert to lowercase to match enum values
        trail_mode_value = trail_mode.lower()
        trail_mode = TrailMode(trail_mode_value)
        
        try:
            trail_atr_period = int(os.getenv("TRAIL_ATR_PERIOD", "14"))
        except ValueError:
            trail_atr_period = 14
            
        try:
            trail_atr_mult = float(os.getenv("TRAIL_ATR_MULT", "3.0"))
        except ValueError:
            trail_atr_mult = 3.0
            
        try:
            chand_lookback = int(os.getenv("CHAN_LOOKBACK", "22"))
        except ValueError:
            chand_lookback = 22
            
        pair = os.getenv("PAIR", "ETHUSD").upper()
        
        try:
            interval_min = int(os.getenv("INTERVAL_MIN", "5"))
        except ValueError:
            interval_min = 5

        return BotConfig(
            token=token,
            sheets_url=sheets_url,
            sheets_token=sheets_token,
            partial_fraction=partial_fraction,
            be_after_tp1=be_after_tp1,
            be_offset_pct=be_offset_pct,
            trail_mode=trail_mode,
            trail_atr_period=trail_atr_period,
            trail_atr_mult=trail_atr_mult,
            chand_lookback=chand_lookback,
            pair=pair,
            interval_min=interval_min
        )

# -------- DB --------
class DatabaseManager:
    def __init__(self, path: str = "trades.db"):
        self.path = path
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                c.execute("""
                CREATE TABLE IF NOT EXISTS trades(
                    id TEXT PRIMARY KEY,
                    asset TEXT,
                    direction TEXT,
                    entry REAL,
                    sl REAL,
                    tp1 REAL,
                    tp2 REAL,
                    status TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    be_active INTEGER DEFAULT 0,
                    trail_mode TEXT,
                    extra TEXT
                );
                """)
                c.execute("""
                CREATE TABLE IF NOT EXISTS partial_exits(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT,
                    fraction REAL,
                    price REAL,
                    time TEXT
                );
                """)
                conn.commit()
        except Exception as e:
            log.error(f"Database schema error: {e}")

    def save_trade(self, t: "TradeData"):
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                c.execute("""
                INSERT OR REPLACE INTO trades(id, asset, direction, entry, sl, tp1, tp2, status, opened_at, closed_at, be_active, trail_mode, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    t.id, t.asset, t.direction.name, t.entry_price, t.sl, t.tp1, t.tp2, t.status.name,
                    t.opened_at.isoformat() if t.opened_at else None,
                    t.closed_at.isoformat() if t.closed_at else None,
                    1 if t.be_active else 0,
                    t.trail_mode.value if t.trail_mode else TrailMode.NONE.value,
                    json.dumps(t.enhanced_data or {})
                ))
                conn.commit()
        except Exception as e:
            log.error(f"Save trade error: {e}")

    def close_trade(self, trade_id: str, closed_at: datetime):
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                c.execute("UPDATE trades SET status=?, closed_at=? WHERE id=?", ("CLOSED", closed_at.isoformat(), trade_id))
                conn.commit()
        except Exception as e:
            log.error(f"Close trade error: {e}")

    def add_partial(self, trade_id: str, fraction: float, price: float, time: datetime):
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                c.execute("""
                INSERT INTO partial_exits(trade_id, fraction, price, time) VALUES (?,?,?,?)
                """, (trade_id, fraction, price, time.isoformat()))
                conn.commit()
        except Exception as e:
            log.error(f"Add partial error: {e}")

# -------- Trade Model --------
class TradeDirection(Enum):
    LONG = auto()
    SHORT = auto()

class TradeStatus(Enum):
    OPEN = auto()
    CLOSED = auto()

@dataclass
class TradeData:
    id: str
    asset: str
    direction: TradeDirection
    entry_price: float
    sl: float
    tp1: float
    tp2: float
    status: TradeStatus = TradeStatus.OPEN
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    be_active: bool = False
    trail_mode: TrailMode = TrailMode.NONE
    trail_stop: Optional[float] = None
    rating: Optional[str] = None  # e.g., S/A/B/C
    score: Optional[int] = None
    knight: Optional[str] = None
    level_name: Optional[str] = None
    level_price: Optional[float] = None
    trade_type: Optional[str] = None
    enhanced_data: Optional[Dict[str, Any]] = field(default_factory=dict)
    # Add these for proper TP1 tracking
    tp1_done: bool = False
    partial_fraction: float = 0.0

# -------- Sheets Integration --------
class GoogleSheetsIntegration:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token

    async def _post(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.url or not self.token:
            return {"status": "skipped", "reason": "no_config"}
        
        headers = {"x-app-secret": self.token, "content-type": "application/json"}
        
        for attempt in range(1, 4):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with session.post(self.url, headers=headers, json=payload, timeout=timeout) as resp:
                    txt = await resp.text()
                    if resp.status < 300:
                        return {"status": "success", "response": txt}
                    else:
                        log.warning(f"Sheets POST attempt {attempt}: {resp.status} - {txt[:200]}")
                        return {"status": resp.status, "body": txt}
            except Exception as e:
                log.warning(f"Sheets POST attempt {attempt} error: {e}")
                if attempt == 3:
                    return {"status": "error", "error": str(e)}
            await asyncio.sleep(1.0 * attempt)

    async def send_trade_entry(self, session: aiohttp.ClientSession, t: "TradeData"):
        # Apps Script expects *entry_price/stop_loss/target1/target2* etc.
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trade_id": t.id,
            "asset": t.asset,
            "direction": t.direction.name.title(),  # "Long"/"Short"
            "level_name": t.level_name or "",
            "entry_price": t.entry_price,
            "stop_loss": t.sl,
            "target1": t.tp1,
            "target2": t.tp2,
            "score": t.score or 0,
            "knight": t.knight or "",
            "status": "OPEN",
            "trade_type": t.trade_type or "Breakout",
            "confidence": t.rating or "",
            "enhanced_data": t.enhanced_data or {},
        }
        result = await self._post(session, payload)
        if result.get("status") == "success":
            log.info(f"Trade entry sent to sheets: {t.id}")
        else:
            log.warning(f"Sheets entry failed for {t.id}: {result}")
        return result

    async def send_trade_exit(self, session: aiohttp.ClientSession, trade_id: str, reason: str, price: float, time_iso: str, pnl_pct: float):
        # Apps Script exit is an UPDATE action
        payload = {
            "action": "update",
            "trade_id": trade_id,
            "exit_price": price,
            "exit_reason": reason,
            "pnl_pct": pnl_pct,
            "exit_time": time_iso,
            "status": "CLOSED",
        }
        result = await self._post(session, payload)
        if result.get("status") == "success":
            log.info(f"Trade exit sent to sheets: {trade_id}")
        else:
            log.warning(f"Sheets exit failed for {trade_id}: {result}")
        return result

    async def rehydrate_open_trades(self, session) -> List["TradeData"]:
        if not self.url or not self.token:
            log.info("Sheets not configured, skipping rehydration")
            return []
        
        params = {"action": "open", "key": self.token}
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(self.url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    log.warning(f"Rehydrate GET failed: {resp.status}")
                    return []
                txt = await resp.text()
                data = json.loads(txt) if txt else {}
                rows = data.get("rows", [])
                log.info(f"Fetched {len(rows)} trades from sheets for rehydration")
        except Exception as e:
            log.warning(f"Rehydrate GET failed: {e}")
            return []

        out: List[TradeData] = []
        for r in rows:
            try:
                # Direction can be "Long"/"Short" — normalize safely
                dir_raw = str(r.get("direction", "Long")).strip().upper()
                direction = TradeDirection.LONG if dir_raw.startswith("L") else TradeDirection.SHORT
                
                trade = TradeData(
                    id=str(r.get("trade_id") or r.get("id") or f"rehydrated_{len(out)}"),
                    asset=str(r.get("asset") or "ETH"),
                    direction=direction,
                    entry_price=float(r.get("entry_price") or 0),
                    sl=float(r.get("stop_loss") or 0),
                    tp1=float(r.get("tp1") or r.get("target1") or 0),
                    tp2=float(r.get("tp2") or r.get("target2") or 0),
                    score=int(r.get("score") or 0),
                    rating=str(r.get("confidence") or ""),
                    knight=str(r.get("knight") or ""),
                    level_name=str(r.get("level_name") or ""),
                )
                out.append(trade)
                log.debug(f"Rehydrated trade: {trade.id}")
            except Exception as e:
                log.warning(f"Bad row in rehydrate: {e}")
                
        log.info(f"Successfully rehydrated {len(out)} trades")
        return out

# -------- Trade Manager --------
class TradeManager:
    def __init__(self, cfg: BotConfig, db: DatabaseManager, sheets: GoogleSheetsIntegration):
        self.cfg = cfg
        self.db = db
        self.sheets = sheets
        self.active: Dict[str, TradeData] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    # ----------------- lifecycle -----------------
    async def start(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def rehydrate(self):
        try:
            await self.start()
            rows = await self.sheets.rehydrate_open_trades(self.session)
            for t in rows:
                t.trail_mode = self.cfg.trail_mode
                self.active[t.id] = t
                self.db.save_trade(t)
            log.info(f"Rehydrated {len(rows)} trades from Google Sheets")
        except Exception as e:
            log.error(f"Rehydration error: {e}")

    async def open_trade(self, t: TradeData):
        try:
            await self.start()
            t.trail_mode = self.cfg.trail_mode
            self.active[t.id] = t
            self.db.save_trade(t)
            await self.sheets.send_trade_entry(self.session, t)
            log.info(f"Opened trade: {t.id}")
        except Exception as e:
            log.error(f"Open trade error: {e}")

    # ----------------- helpers -----------------
    def _apply_breakeven(self, t: TradeData) -> None:
        """Move stop to BE with optional fee/offset cushion."""
        t.be_active = True
        offset = self.cfg.be_offset_pct / 100.0
        if t.direction == TradeDirection.LONG:
            t.sl = t.entry_price * (1.0 + offset)
        else:
            t.sl = t.entry_price * (1.0 - offset)

    def _pnl_pct(self, entry: float, px: float, direction: TradeDirection) -> float:
        """Signed PnL% for a leg."""
        if direction == TradeDirection.LONG:
            return (px - entry) / entry * 100.0
        return (entry - px) / entry * 100.0

    def _blended_pnl(self, entry: float, tp1: float, final_px: float,
                     direction: TradeDirection, frac: float) -> float:
        """
        frac = portion closed at TP1 (0..1). If frac==0 -> all at final_px.
        """
        try:
            f = max(0.0, min(1.0, float(frac)))
        except Exception:
            f = 0.0
        if f == 0.0:
            return self._pnl_pct(entry, final_px, direction)
        leg1 = self._pnl_pct(entry, tp1, direction)
        leg2 = self._pnl_pct(entry, final_px, direction)
        return leg1 * f + leg2 * (1.0 - f)

    def _calc_atr_fallback(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        """Fallback ATR calculation when TA library not available"""
        try:
            h, l, c = highs[-period-1:], lows[-period-1:], closes[-period-1:]
            if len(h) < period + 1:
                return float(np.mean(h[1:] - l[1:]))  # Simple high-low range
            
            prev_close = c[:-1]
            current_h = h[1:]
            current_l = l[1:]
            
            tr1 = current_h - current_l
            tr2 = np.abs(current_h - prev_close)
            tr3 = np.abs(current_l - prev_close)
            
            tr = np.maximum(tr1, np.maximum(tr2, tr3))
            return float(np.mean(tr[-period:]))
        except Exception as e:
            log.warning(f"ATR fallback error: {e}")
            return 20.0  # Default fallback

    def _calc_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        if not TA_AVAILABLE or AverageTrueRange is None:
            return self._calc_atr_fallback(highs, lows, closes, period)
        
        try:
            df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
            atr = AverageTrueRange(high=df["high"], low=df["low"],
                                   close=df["close"], window=period).average_true_range().iloc[-1]
            return float(atr)
        except Exception as e:
            log.warning(f"TA ATR error: {e}")
            return self._calc_atr_fallback(highs, lows, closes, period)

    def _update_trail(self, t: TradeData, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> None:
        if self.cfg.trail_mode == TrailMode.NONE:
            return

        try:
            if self.cfg.trail_mode == TrailMode.ATR:
                atr = self._calc_atr(highs, lows, closes, self.cfg.trail_atr_period)
                if t.direction == TradeDirection.LONG:
                    trail = closes[-1] - self.cfg.trail_atr_mult * atr
                    t.trail_stop = max(t.trail_stop or -1e9, trail)
                    t.sl = max(t.sl, t.trail_stop)
                else:
                    trail = closes[-1] + self.cfg.trail_atr_mult * atr
                    t.trail_stop = min(t.trail_stop or 1e9, trail)
                    t.sl = min(t.sl, t.trail_stop)

            elif self.cfg.trail_mode == TrailMode.CHAND:
                look = self.cfg.chand_lookback
                if len(highs) < look:
                    return
                if t.direction == TradeDirection.LONG:
                    hh = float(np.max(highs[-look:]))
                    trail = hh - self.cfg.trail_atr_mult * self._calc_atr(highs, lows, closes, self.cfg.trail_atr_period)
                    t.trail_stop = max(t.trail_stop or -1e9, trail)
                    t.sl = max(t.sl, t.trail_stop)
                else:
                    ll = float(np.min(lows[-look:]))
                    trail = ll + self.cfg.trail_atr_mult * self._calc_atr(highs, lows, closes, self.cfg.trail_atr_period)
                    t.trail_stop = min(t.trail_stop or 1e9, trail)
                    t.sl = min(t.sl, t.trail_stop)
        except Exception as e:
            log.warning(f"Trail update error for {t.id}: {e}")

    # ----------------- exit engine -----------------
    async def evaluate_exit(self, t: TradeData, last_price: float, now: datetime) -> Optional[Tuple[str, float, float]]:
        """
        Check TP1/TP2/SL. Returns (reason, exit_price, pnl_pct) when the trade is finalized,
        else returns None to keep monitoring.
        """
        try:
            # 1) TP1 partial (do once), move SL to BE if configured
            if t.status == TradeStatus.OPEN and not t.tp1_done:
                if (t.direction == TradeDirection.LONG and last_price >= t.tp1) or \
                   (t.direction == TradeDirection.SHORT and last_price <= t.tp1):

                    # Persist partial
                    frac = self.cfg.partial_fraction
                    t.tp1_done = True
                    t.partial_fraction = frac
                    self.db.add_partial(t.id, frac, last_price, now)

                    # Move to BE if desired
                    if self.cfg.be_after_tp1:
                        self._apply_breakeven(t)

                    self.db.save_trade(t)
                    log.info(f"TP1 hit for {t.id} at {last_price:.2f}, moved to BE")
                    # Keep the trade OPEN (no finalize yet)
                    return None

            # 2) Finalization on TP2 or SL (SL may be BE if we moved it)
            hit_tp2 = (t.direction == TradeDirection.LONG and last_price >= t.tp2) or \
                      (t.direction == TradeDirection.SHORT and last_price <= t.tp2)
            hit_sl  = (t.direction == TradeDirection.LONG and last_price <= t.sl)  or \
                      (t.direction == TradeDirection.SHORT and last_price >= t.sl)

            if hit_tp2 or hit_sl:
                # Compute blended PnL if TP1 happened
                frac = t.partial_fraction if t.tp1_done else 0.0
                pnl  = self._blended_pnl(t.entry_price, t.tp1, last_price, t.direction, frac)

                t.status = TradeStatus.CLOSED
                t.closed_at = now
                self.db.save_trade(t)
                self.db.close_trade(t.id, now)

                reason = "TP2 HIT" if hit_tp2 else ("SL (BE)" if t.be_active else "SL")
                return (reason, last_price, pnl)

            # keep open
            return None
        except Exception as e:
            log.error(f"Exit evaluation error for {t.id}: {e}")
            return None

# -------- Market Data & Camarilla --------
class MarketDataProvider:
    KRAKEN_PAIR_MAP = {
        "ETHUSD": "ETHUSD",
        "BTCUSD": "XBTUSD",
        "SOLUSD": "SOLUSD"
    }

    def __init__(self, pair: str, interval_min: int):
        self.pair = self.KRAKEN_PAIR_MAP.get(pair, pair)
        self.interval_min = interval_min
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def fetch_ohlc(self, n: int = 500) -> pd.DataFrame:
        await self.start()
        url = "https://api.kraken.com/0/public/OHLC"
        params = {"pair": self.pair, "interval": self.interval_min}
        
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise Exception(f"Kraken API error: {resp.status}")
                data = await resp.json()
                
            if "error" in data and data["error"]:
                raise Exception(f"Kraken API error: {data['error']}")
                
            # Get the first (and usually only) pair key
            result_keys = list(data["result"].keys())
            if not result_keys:
                raise Exception("No data returned from Kraken")
                
            key = result_keys[0]
            rows = data["result"][key][-n:]
            
            df = pd.DataFrame(rows, columns=["time","open","high","low","close","vwap","volume","count"])
            df = df.astype({
                "time": int, 
                "open": float, 
                "high": float, 
                "low": float, 
                "close": float, 
                "volume": float
            })
            df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
            return df
        except Exception as e:
            log.error(f"OHLC fetch error: {e}")