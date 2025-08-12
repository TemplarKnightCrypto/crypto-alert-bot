
# ============================================
# Control Tower - Clean v11.5
# ============================================

import os
import asyncio
import json
import sqlite3
import logging
import threading
from io import BytesIO
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

# TA imports with fallbacks
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
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
log = logging.getLogger('control_tower')

# -------- Flask --------
app = Flask(__name__)

@app.route('/')
def health_root():
    return jsonify(
        ok=True, 
        service="Control Tower Clean v11.2",
        timestamp=datetime.now(timezone.utc).isoformat()
    )

@app.route('/health')
def health_check():
    try:
        return jsonify({
            "status": "healthy",
            "version": "11.2-clean",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ta_library": TA_AVAILABLE
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

def run_flask():
    try:
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        log.error(f"Flask error: {e}")

# -------- Enums --------
class TrailMode(Enum):
    NONE = "none"
    ATR = "atr"
    CHAND = "chand"

class TradeDirection(Enum):
    LONG = auto()
    SHORT = auto()

class TradeStatus(Enum):
    OPEN = auto()
    CLOSED = auto()

# -------- Config --------
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
    def from_env():
        load_dotenv()
        
        # Get token - this is required
        token = os.getenv("TOKEN", "").strip()
        if not token:
            raise ValueError("Discord TOKEN environment variable is required")

        # Get other settings with safe defaults
        sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "").strip() or None
        sheets_token = os.getenv("SHEETS_TOKEN", "").strip() or None
        
        # Parse numeric values safely
        try:
            partial_fraction = float(os.getenv("PARTIAL_FRACTION", "0.5"))
        except (ValueError, TypeError):
            partial_fraction = 0.5
            
        be_after_tp1 = os.getenv("BE_AFTER_TP1", "true").lower() in ("1", "true", "yes", "y")
        
        try:
            be_offset_pct = float(os.getenv("BE_OFFSET_PCT", "0.0"))
        except (ValueError, TypeError):
            be_offset_pct = 0.0
            
        # Handle trail mode safely
        trail_mode_str = os.getenv("TRAIL_MODE", "none").lower()
        if trail_mode_str == "atr":
            trail_mode = TrailMode.ATR
        elif trail_mode_str == "chand":
            trail_mode = TrailMode.CHAND
        else:
            trail_mode = TrailMode.NONE
        
        try:
            trail_atr_period = int(os.getenv("TRAIL_ATR_PERIOD", "14"))
        except (ValueError, TypeError):
            trail_atr_period = 14
            
        try:
            trail_atr_mult = float(os.getenv("TRAIL_ATR_MULT", "3.0"))
        except (ValueError, TypeError):
            trail_atr_mult = 3.0
            
        try:
            chand_lookback = int(os.getenv("CHAN_LOOKBACK", "22"))
        except (ValueError, TypeError):
            chand_lookback = 22
            
        pair = os.getenv("PAIR", "ETHUSD").upper()
        
        try:
            interval_min = int(os.getenv("INTERVAL_MIN", "5"))
        except (ValueError, TypeError):
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

# -------- Database --------
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

    def save_trade(self, t):
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
    rating: Optional[str] = None
    score: Optional[int] = None
    knight: Optional[str] = None
    level_name: Optional[str] = None
    level_price: Optional[float] = None
    trade_type: Optional[str] = None
    enhanced_data: Optional[Dict[str, Any]] = field(default_factory=dict)
    tp1_done: bool = False
    partial_fraction: float = 0.0

# -------- Sheets Integration --------
class GoogleSheetsIntegration:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token

    async def _post(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.url or not self.token:
            log.warning("Sheets not configured - skipping POST")
            return {"status": "skipped", "reason": "no_config"}
        
        headers = {"x-app-secret": self.token, "content-type": "application/json"}
        
        log.info(f"Posting to sheets URL: {self.url}")
        log.info(f"Headers: x-app-secret: {self.token[:10]}...")
        
        for attempt in range(1, 4):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with session.post(self.url, headers=headers, json=payload, timeout=timeout) as resp:
                    txt = await resp.text()
                    log.info(f"Sheets attempt {attempt}: status={resp.status}, response={txt[:200]}")
                    
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

        return {"status": "failed", "reason": "max_retries_exceeded"}

    async def send_trade_entry(self, session: aiohttp.ClientSession, t):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trade_id": t.id,
            "asset": t.asset,
            "direction": t.direction.name.title(),
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
        
        log.info(f"Sending to sheets: {payload}")
        result = await self._post(session, payload)
        log.info(f"Sheets response: {result}")
        
        if result.get("status") == "success":
            log.info(f"Trade entry sent to sheets: {t.id}")
        else:
            log.warning(f"Sheets entry failed for {t.id}: {result}")
        return result

    async def send_trade_exit(self, session: aiohttp.ClientSession, trade_id: str, reason: str, price: float, time_iso: str, pnl_pct: float):
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
        return result

    async def rehydrate_open_trades(self, session) -> List:
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

        out = []
        for r in rows:
            try:
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
            except Exception as e:
                log.warning(f"Bad row in rehydrate: {e}")
                
        log.info(f"Successfully rehydrated {len(out)} trades")
        return out

# -------- Trade Manager --------
class TradeManager:
    def __init__(self, cfg, db, sheets):
        self.cfg = cfg
        self.db = db
        self.sheets = sheets
        self.active = {}
        self.session = None

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

    async def open_trade(self, t):
        try:
            await self.start()
            t.trail_mode = self.cfg.trail_mode
            self.active[t.id] = t
            self.db.save_trade(t)
            await self.sheets.send_trade_entry(self.session, t)
            log.info(f"Opened trade: {t.id}")
        except Exception as e:
            log.error(f"Open trade error: {e}")

# -------- Market Data --------
class MarketDataProvider:
    KRAKEN_PAIR_MAP = {
        "ETHUSD": "ETHUSD",
        "BTCUSD": "XBTUSD",
        "SOLUSD": "SOLUSD"
    }

    def __init__(self, pair: str, interval_min: int):
        self.pair = self.KRAKEN_PAIR_MAP.get(pair, pair)
        self.interval_min = interval_min
        self.session = None

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
            raise

def calc_camarilla(df: pd.DataFrame) -> Dict[str, float]:
    try:
        if len(df) < 2:
            raise ValueError("Not enough bars")
        
        prev = df.iloc[-2]
        H = float(prev["high"])
        L = float(prev["low"]) 
        C = float(prev["close"])
        r = H - L
        
        if r <= 0:
            raise ValueError("Invalid range")
        
        L3 = C - (r * 1.1/12)
        H3 = C + (r * 1.1/12)
        L4 = C - (r * 1.1/6)
        H4 = C + (r * 1.1/6)
        L5 = C - (r * 1.1/2)
        H5 = C + (r * 1.1/2)
        
        return {
            "L3": L3, "L4": L4, "L5": L5,
            "H3": H3, "H4": H4, "H5": H5,
            "P": C
        }
    except Exception as e:
        log.error(f"Camarilla calculation error: {e}")
        return {}

def confirm_breakout(c, o, h, l, vol, avg_vol, level: float, direction) -> Tuple[bool, Dict[str, Any]]:
    try:
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

# -------- Discord Bot --------
INTENTS = discord.Intents.default()
INTENTS.message_content = True

# Global variables
cfg = None
bot = None
db = None
sheets = None
trade_manager = None
mdp = None

def status_embed() -> discord.Embed:
    try:
        e = discord.Embed(
            title="🛡️ Control Tower Status", 
            color=discord.Color.blurple(), 
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="Pair", value=cfg.pair if cfg else "N/A", inline=True)
        e.add_field(name="Active Trades", value=str(len(trade_manager.active)) if trade_manager else "0", inline=True)
        e.add_field(name="Sheets", value="ON" if (cfg and cfg.sheets_url) else "OFF", inline=True)
        return e
    except Exception as e:
        log.error(f"Status embed error: {e}")
        return discord.Embed(title="Status Error", description=str(e), color=discord.Color.red())

async def send_battle_signal(channel, t):
    try:
        color = discord.Color.green() if t.direction == TradeDirection.LONG else discord.Color.red()
        e = discord.Embed(
            title=f"⚔️ Battle Signal - {t.asset} {t.direction.name}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="Entry", value=f"{t.entry_price:.2f}", inline=True)
        e.add_field(name="Stop", value=f"{t.sl:.2f}", inline=True)
        e.add_field(name="TP1/TP2", value=f"{t.tp1:.2f} / {t.tp2:.2f}", inline=True)
        await channel.send(embed=e)
    except Exception as e:
        log.error(f"Battle signal error: {e}")

def create_bot():
    bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)
    
    @bot.command(name="status")
    async def _status(ctx):
        try:
            await ctx.send(embed=status_embed())
        except Exception as e:
            await ctx.send(f"Status error: {e}")

    @bot.command(name="config")
    async def _config(ctx):
        try:
            e = discord.Embed(title="⚙️ Bot Configuration", color=discord.Color.blue())
            e.add_field(name="Pair", value=cfg.pair, inline=True)
            e.add_field(name="Interval", value=f"{cfg.interval_min}m", inline=True)
            e.add_field(name="Trail Mode", value=cfg.trail_mode.value, inline=True)
            e.add_field(name="Sheets", value="✅ Configured" if cfg.sheets_url else "❌ Not configured", inline=True)
            e.add_field(name="Active Trades", value=str(len(trade_manager.active)), inline=True)
            await ctx.send(embed=e)
        except Exception as e:
            await ctx.send(f"Config error: {e}")

    @bot.command(name="trades")
    async def _trades(ctx):
        try:
            if not trade_manager.active:
                await ctx.send("📊 No active trades currently")
                return
            
            e = discord.Embed(
                title="📊 Active Trades", 
                description=f"Currently tracking {len(trade_manager.active)} trade(s)",
                color=discord.Color.green()
            )
            
            for trade_id, trade in list(trade_manager.active.items())[:10]:  # Limit to 10
                trade_info = (
                    f"**Direction:** {trade.direction.name}\n"
                    f"**Entry:** ${trade.entry_price:.2f}\n"
                    f"**TP1/TP2:** ${trade.tp1:.2f} / ${trade.tp2:.2f}\n"
                    f"**Stop:** ${trade.sl:.2f}"
                )
                e.add_field(name=f"🎯 {trade_id[:8]}", value=trade_info, inline=True)
            
            await ctx.send(embed=e)
        except Exception as e:
            await ctx.send(f"Trades error: {e}")

    @bot.command(name="export")
    async def _export(ctx):
        try:
            # Export DB to CSV
            path = "trades_export.csv"
            with sqlite3.connect(db.path) as conn:
                df_trades = pd.read_sql_query("SELECT * FROM trades", conn)
                df_partials = pd.read_sql_query("SELECT * FROM partial_exits", conn)
            
            # Create export file
            with open(path, 'w', newline='') as csvfile:
                df_trades.to_csv(csvfile, index=False)
            
            await ctx.send("📊 Database Export", file=discord.File(path))
            
            # Clean up
            if os.path.exists(path):
                os.remove(path)
                
        except Exception as e:
            await ctx.send(f"Export error: {e}")

    @bot.command(name="enhanced_export")
    async def _enhanced_export(ctx, days: int = 30):
        """Export enhanced trading data"""
        try:
            # Calculate date filter
            since_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            with sqlite3.connect(db.path) as conn:
                query = """
                SELECT 
                    id, asset, direction, entry, sl, tp1, tp2, status,
                    opened_at, closed_at, be_active, trail_mode, extra
                FROM trades 
                WHERE opened_at >= ?
                ORDER BY opened_at DESC
                """
                df = pd.read_sql_query(query, conn, params=(since_date.isoformat(),))
            
            if df.empty:
                await ctx.send(f"❌ No trades found in the last {days} days")
                return
            
            # Create CSV content
            csv_content = df.to_csv(index=False)
            
            # Create file
            filename = f"enhanced_trading_data_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
            
            # Send as Discord file
            file_obj = discord.File(
                fp=BytesIO(csv_content.encode()),
                filename=filename
            )
            
            embed = discord.Embed(
                title="📊 Enhanced Trading Data Export",
                description=f"Complete dataset - Last {days} days",
                color=discord.Color.green()
            )
            embed.add_field(name="Records", value=str(len(df)), inline=True)
            embed.add_field(name="Period", value=f"{days} days", inline=True)
            
            await ctx.send(embed=embed, file=file_obj)
            
        except Exception as e:
            log.error(f"Enhanced export error: {e}")
            await ctx.send(f"❌ Enhanced export failed: {e}")

    @bot.command(name="rehydrate")
    async def _rehydrate(ctx):
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

    @bot.command(name="sheets_debug")
    async def _sheets_debug(ctx):
        """Debug Google Sheets integration"""
        try:
            embed = discord.Embed(title="🔍 Sheets Debug Info", color=discord.Color.orange())
            
            # Check configuration
            embed.add_field(name="URL Configured", value="✅ Yes" if cfg.sheets_url else "❌ No", inline=True)
            embed.add_field(name="Token Configured", value="✅ Yes" if cfg.sheets_token else "❌ No", inline=True)
            
            if cfg.sheets_url:
                embed.add_field(name="Webhook URL", value=f"{cfg.sheets_url[:50]}...", inline=False)
            
            # Test connection
            if cfg.sheets_url and cfg.sheets_token:
                try:
                    await trade_manager.start()
                    params = {"action": "open", "key": cfg.sheets_token}
                    timeout = aiohttp.ClientTimeout(total=10)
                    
                    async with trade_manager.session.get(cfg.sheets_url, params=params, timeout=timeout) as resp:
                        status_text = f"{resp.status} - {'✅ OK' if resp.status == 200 else '❌ Error'}"
                        embed.add_field(name="Connection Test", value=status_text, inline=True)
                        
                        if resp.status == 200:
                            text = await resp.text()
                            embed.add_field(name="Response Preview", value=text[:100] + "...", inline=False)
                        
                except Exception as e:
                    embed.add_field(name="Connection Error", value=str(e), inline=False)
            else:
                embed.add_field(name="Connection Test", value="❌ Cannot test - missing config", inline=True)
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Sheets debug error: {e}")

    @bot.command(name="sheets_test")
    async def _sheets_test(ctx):
        try:
            now = datetime.now(timezone.utc)
            t = TradeData(
                id=now.strftime("TEST%H%M%S"),
                asset=cfg.pair,
                direction=TradeDirection.LONG,
                entry_price=2500.0, sl=2450.0, tp1=2525.0, tp2=2550.0,
                rating="A", score=5, level_name="H4"
            )
            
            # Show what we're sending
            embed = discord.Embed(title="🧪 Testing Sheets Integration", color=discord.Color.blue())
            embed.add_field(name="Trade ID", value=t.id, inline=True)
            embed.add_field(name="Direction", value=t.direction.name, inline=True)
            embed.add_field(name="Entry", value=f"${t.entry_price:.2f}", inline=True)
            
            await ctx.send(embed=embed)
            
            # Send to sheets
            result = await trade_manager.open_trade(t)
            
            # Report result
            if "success" in str(result).lower():
                await ctx.send("✅ Posted test entry to Google Sheets successfully!")
            else:
                await ctx.send(f"⚠️ Sheets result: {result}")
                
        except Exception as e:
            await ctx.send(f"Sheets test error: {e}")

    @tasks.loop(seconds=60)
    async def scan_loop():
        try:
            await mdp.start()
            await trade_manager.start()
            
            df = await mdp.fetch_ohlc(100)
            levels = calc_camarilla(df)
            
            if not levels:
                return
                
            # Simple signal generation
            last = df.iloc[-1]
            c = float(last["close"])
            
            # Find a channel
            ch = None
            for g in bot.guilds:
                for channel in g.text_channels:
                    if channel.permissions_for(g.me).send_messages:
                        ch = channel
                        break
                if ch: 
                    break
            
            if not ch:
                return
                
            # Check for breakout signals (simplified)
            h5 = levels.get("H5")
            l5 = levels.get("L5")
            
            if h5 and c > h5:
                # Potential long signal
                t = TradeData(
                    id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                    asset=cfg.pair,
                    direction=TradeDirection.LONG,
                    entry_price=c,
                    sl=c * 0.99,
                    tp1=c * 1.015,
                    tp2=c * 1.03,
                    level_name="H5",
                    knight="Sir Camarilla"
                )
                await trade_manager.open_trade(t)
                await send_battle_signal(ch, t)
                
        except Exception as e:
            log.error(f"Scan loop error: {e}")

    @scan_loop.before_loop
    async def before_scan():
        await bot.wait_until_ready()
        await trade_manager.start()
        await mdp.start()

    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user}")
        try:
            await trade_manager.rehydrate()
            if not scan_loop.is_running():
                scan_loop.start()
        except Exception as e:
            log.error(f"Bot ready error: {e}")

    return bot

def main():
    try:
        global cfg, bot, db, sheets, trade_manager, mdp
        
        log.info("Starting Control Tower Clean v11.2...")
        
        # Load configuration
        cfg = BotConfig.from_env()
        log.info(f"Configuration loaded successfully")
        
        # Initialize components
        db = DatabaseManager("trades.db")
        sheets = GoogleSheetsIntegration(cfg.sheets_url, cfg.sheets_token)
        trade_manager = TradeManager(cfg, db, sheets)
        mdp = MarketDataProvider(cfg.pair, cfg.interval_min)
        
        # Create bot
        bot = create_bot()
        
        # Start Flask
        log.info("Starting Flask health server...")
        threading.Thread(target=run_flask, daemon=True).start()
        
        # Start Discord bot
        log.info("Starting Discord bot...")
        bot.run(cfg.token)
        
    except Exception as e:
        log.error(f"Main execution error: {e}")
        raise

if __name__ == "__main__":
    main()