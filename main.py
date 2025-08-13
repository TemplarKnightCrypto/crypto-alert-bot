# ============================================
# Control Tower - Complete v11.11.1 + Trade Closure System
# ============================================

import os
import time
import random
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
from collections import defaultdict

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
        service="Control Tower Complete v11.11",
        timestamp=datetime.now(timezone.utc).isoformat()
    )

@app.route('/health')
def health_check():
    try:
        return jsonify({
            "status": "healthy",
            "version": "11.11-complete-closure",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ta_library": TA_AVAILABLE,
            "alert_system": "enhanced",
            "trade_closure": "implemented"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.route("/debug/last_payload")
def debug_last_payload():
    """Show the last payload sent to sheets"""
    global last_sheets_payload
    if 'last_sheets_payload' in globals():
        return jsonify(last_sheets_payload)
    else:
        return jsonify({"error": "No payload recorded yet"})

# Global to store last payload for debugging
last_sheets_payload = None

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

# -------- Discord Channel Configuration --------
@dataclass 
class ChannelConfig:
    scribes_keep_id: int = 1398691425347961016      # 📜 Market scorecard
    battle_signals_id: int = 1399532925279666278    # ⚔️ Trade alerts  
    eagle_signal_id: int = 1398690647417819198      # 🦅 100x alerts
    knights_watch_id: int = 1399532102571135118     # 🕰️ Proximity warnings
    eth_battleground_id: int = 1399532442075005038  # 🏰 Real-time reports
    scrolls_order_id: int = 1399067396488302623     # 📚 Performance logs
    setup_alerts_id: int = 1402053509490151424      # 🗺️ Setup alerts

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
    channels: ChannelConfig = field(default_factory=ChannelConfig)

    @staticmethod
    def from_env():
        load_dotenv()
        
        token = os.getenv("TOKEN", "").strip()
        if not token:
            raise ValueError("Discord TOKEN environment variable is required")

        sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "").strip() or None
        sheets_token = os.getenv("SHEETS_TOKEN", "").strip() or None
        
        try:
            partial_fraction = float(os.getenv("PARTIAL_FRACTION", "0.5"))
        except (ValueError, TypeError):
            partial_fraction = 0.5
            
        be_after_tp1 = os.getenv("BE_AFTER_TP1", "true").lower() in ("1", "true", "yes", "y")
        
        try:
            be_offset_pct = float(os.getenv("BE_OFFSET_PCT", "0.0"))
        except (ValueError, TypeError):
            be_offset_pct = 0.0
            
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
            chand_lookback = int(os.getenv("CHAN_LOOKBOOK", "22"))
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
            interval_min=interval_min,
            channels=ChannelConfig()
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
                    extra TEXT,
                    tp1_done INTEGER DEFAULT 0,
                    partial_fraction REAL DEFAULT 0.0,
                    exit_reason TEXT,
                    exit_price REAL,
                    pnl_pct REAL
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
                INSERT OR REPLACE INTO trades(id, asset, direction, entry, sl, tp1, tp2, status, opened_at, closed_at, be_active, trail_mode, extra, tp1_done, partial_fraction, exit_reason, exit_price, pnl_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    t.id, t.asset, t.direction.name, t.entry_price, t.sl, t.tp1, t.tp2, t.status.name,
                    t.opened_at.isoformat() if t.opened_at else None,
                    t.closed_at.isoformat() if t.closed_at else None,
                    1 if t.be_active else 0,
                    t.trail_mode.value if t.trail_mode else TrailMode.NONE.value,
                    json.dumps(t.enhanced_data or {}),
                    1 if t.tp1_done else 0,
                    t.partial_fraction,
                    getattr(t, 'exit_reason', None),
                    getattr(t, 'exit_price', None),
                    getattr(t, 'pnl_pct', None)
                ))
                conn.commit()
        except Exception as e:
            log.error(f"Save trade error: {e}")

    def close_trade(self, trade_id: str, closed_at: datetime, exit_reason: str = None, exit_price: float = None, pnl_pct: float = None):
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                c.execute("""
                UPDATE trades SET status=?, closed_at=?, exit_reason=?, exit_price=?, pnl_pct=? WHERE id=?
                """, ("CLOSED", closed_at.isoformat(), exit_reason, exit_price, pnl_pct, trade_id))
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

    def get_trade_performance(self) -> Dict[str, Any]:
        """Get trade performance statistics"""
        try:
            with sqlite3.connect(self.path) as conn:
                c = conn.cursor()
                
                # Basic stats
                c.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")
                total_closed = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND pnl_pct > 0")
                winners = c.fetchone()[0]
                
                c.execute("SELECT AVG(pnl_pct) FROM trades WHERE status='CLOSED' AND pnl_pct IS NOT NULL")
                avg_pnl = c.fetchone()[0] or 0
                
                c.execute("SELECT SUM(pnl_pct) FROM trades WHERE status='CLOSED' AND pnl_pct IS NOT NULL")
                total_pnl = c.fetchone()[0] or 0
                
                win_rate = (winners / total_closed * 100) if total_closed > 0 else 0
                
                return {
                    "total_trades": total_closed,
                    "winners": winners,
                    "losers": total_closed - winners,
                    "win_rate": win_rate,
                    "avg_pnl": avg_pnl,
                    "total_pnl": total_pnl
                }
        except Exception as e:
            log.error(f"Performance stats error: {e}")
            return {}

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
    exit_reason: Optional[str] = None
    exit_price: Optional[float] = None
    pnl_pct: Optional[float] = None

# -------- Sheets Integration --------
class GoogleSheetsIntegration:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token

    async def _post(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.url or not self.token:
            log.warning("Sheets not configured - skipping POST")
            return {"status": "skipped", "reason": "no_config"}
        
        # Store payload for debugging
        global last_sheets_payload
        last_sheets_payload = payload.copy()
        
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
        # Create clean trade ID
        clean_trade_id = t.id
        if len(clean_trade_id) > 16:
            clean_trade_id = clean_trade_id[-8:]
        
        # Build base payload matching sheet structure
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trade_id": clean_trade_id,
            "asset": t.asset,
            "direction": t.direction.name.title(),
            "entry_price": float(t.entry_price),
            "stop_loss": float(t.sl),
            "take_profit_1": float(t.tp1), 
            "take_profit_2": float(t.tp2),
            "status": "OPEN",
            "original_score": int(t.score or 0),
        }
        
        # Enhanced data fields
        enhanced_data = t.enhanced_data or {}
        payload.update({
            "enhanced_score": enhanced_data.get("enhanced_score", t.score or 0),
            "rsi_level": enhanced_data.get("rsi_level", 50.0),
            "volume_ratio": enhanced_data.get("volume_ratio", 1.0),
            "market_status": enhanced_data.get("market_status", "NORMAL"),
            "vwap_position": enhanced_data.get("vwap_position", "Above"),
            "macd_status": enhanced_data.get("macd_status", "Neutral"),
            "market_bias": enhanced_data.get("market_bias", "Neutral"),
            "level_name": t.level_name or "Unknown",
            "knight": t.knight or "Unknown",
            "trade_type": t.trade_type or "Breakout",
            "confidence": t.rating or "A",
            "risk_pct": enhanced_data.get("risk_pct", 1.0),
            "rr_ratio": enhanced_data.get("rr_ratio", 1.5),
        })
        
        log.info(f"Sending to sheets - Trade ID: {clean_trade_id}, Level: {t.level_name}")
        result = await self._post(session, payload)
        log.info(f"Sheets response: {result}")
        
        return result

    async def send_trade_exit(self, session: aiohttp.ClientSession, trade_id: str, reason: str, price: float, time_iso: str, pnl_pct: float):
        payload = {
            "action": "update",
            "trade_id": trade_id,
            "exit_price": float(price),
            "exit_reason": str(reason),
            "pnl_pct": float(pnl_pct),
            "exit_time": time_iso,
        }
        
        log.info(f"Sending trade exit to sheets: {trade_id} - {reason} - {pnl_pct:+.2f}%")
        result = await self._post(session, payload)
        
        if result.get("status") == "success":
            log.info(f"Trade exit sent to sheets: {trade_id}")
        else:
            log.warning(f"Trade exit failed for {trade_id}: {result}")
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
                    tp1_done=bool(r.get("tp1_done", False)),
                    partial_fraction=float(r.get("partial_fraction", 0.0))
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

    async def close_trade(self, trade_id: str, reason: str, exit_price: float):
        """Close a specific trade"""
        try:
            if trade_id not in self.active:
                log.warning(f"Trade {trade_id} not found in active trades")
                return {"success": False, "error": "Trade not found"}
            
            trade = self.active[trade_id]
            closed_at = datetime.now(timezone.utc)
            
            # Calculate P&L
            if trade.direction == TradeDirection.LONG:
                pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price) * 100
            else:
                pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price) * 100
            
            # Update trade object
            trade.status = TradeStatus.CLOSED
            trade.closed_at = closed_at
            trade.exit_reason = reason
            trade.exit_price = exit_price
            trade.pnl_pct = pnl_pct
            
            # Update database
            self.db.close_trade(trade_id, closed_at, reason, exit_price, pnl_pct)
            
            # Send exit to Google Sheets
            await self.start()
            result = await self.sheets.send_trade_exit(
                self.session, 
                trade_id, 
                reason, 
                exit_price, 
                closed_at.isoformat(), 
                pnl_pct
            )
            
            # Remove from active trades
            del self.active[trade_id]
            
            log.info(f"Trade {trade_id} closed: {reason} at ${exit_price:.2f} ({pnl_pct:+.2f}%)")
            return {"success": True, "pnl_pct": pnl_pct, "sheets_result": result}
            
        except Exception as e:
            log.error(f"Close trade error for {trade_id}: {e}")
            return {"success": False, "error": str(e)}

    async def monitor_all_trades(self, current_price: float):
        """Monitor all active trades for exit conditions"""
        try:
            trades_to_close = []
            
            for trade_id, trade in list(self.active.items()):
                exit_reason = None
                exit_price = current_price
                
                if trade.direction == TradeDirection.LONG:
                    if current_price <= trade.sl:
                        exit_reason = "Stop Loss"
                    elif current_price >= trade.tp1 and not trade.tp1_done:
                        exit_reason = "Take Profit 1 (Partial)"
                        # Mark TP1 as done but don't close trade yet
                        trade.tp1_done = True
                        trade.partial_fraction = self.cfg.partial_fraction
                        self.db.save_trade(trade)
                        self.db.add_partial(trade_id, self.cfg.partial_fraction, current_price, datetime.now(timezone.utc))
                        log.info(f"Trade {trade_id} hit TP1 - 50% partial exit")
                        continue  # Don't close the trade, just mark partial
                    elif current_price >= trade.tp2:
                        exit_reason = "Take Profit 2 (Full Exit)"
                else:  # SHORT
                    if current_price >= trade.sl:
                        exit_reason = "Stop Loss"
                    elif current_price <= trade.tp1 and not trade.tp1_done:
                        exit_reason = "Take Profit 1 (Partial)"
                        # Mark TP1 as done but don't close trade yet
                        trade.tp1_done = True
                        trade.partial_fraction = self.cfg.partial_fraction
                        self.db.save_trade(trade)
                        self.db.add_partial(trade_id, self.cfg.partial_fraction, current_price, datetime.now(timezone.utc))
                        log.info(f"Trade {trade_id} hit TP1 - 50% partial exit")
                        continue  # Don't close the trade, just mark partial
                    elif current_price <= trade.tp2:
                        exit_reason = "Take Profit 2 (Full Exit)"
                
                if exit_reason:
                    trades_to_close.append((trade_id, exit_reason, exit_price))
            
            # Close trades that hit full exit conditions
            for trade_id, reason, price in trades_to_close:
                result = await self.close_trade(trade_id, reason, price)
                if result.get("success"):
                    # Send Discord notification
                    await self._send_trade_exit_notification(trade_id, reason, price, result.get("pnl_pct", 0))
                    
        except Exception as e:
            log.error(f"Trade monitoring error: {e}")

    async def _send_trade_exit_notification(self, trade_id: str, reason: str, exit_price: float, pnl_pct: float):
        """Send trade exit notification to Discord"""
        try:
            # Get bot and config from global scope
            global bot, cfg
            
            channel = bot.get_channel(cfg.channels.battle_signals_id)
            if not channel:
                return
            
            color = discord.Color.green() if pnl_pct > 0 else discord.Color.red()
            
            embed = discord.Embed(
                title=f"🏁 Trade Closed - {trade_id}",
                description=f"**{reason}** - {pnl_pct:+.2f}% P&L",
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(name="🏁 Exit Price", value=f"${exit_price:.2f}", inline=True)
            embed.add_field(name="💰 P&L", value=f"{pnl_pct:+.2f}%", inline=True)
            embed.add_field(name="⏱️ Reason", value=reason, inline=True)
            embed.add_field(name="🆔 Trade ID", value=trade_id, inline=True)
            
            # Add performance context
            if pnl_pct > 2:
                embed.add_field(name="🎉 Performance", value="Excellent Trade!", inline=False)
            elif pnl_pct > 0:
                embed.add_field(name="✅ Performance", value="Profitable Trade", inline=False)
            else:
                embed.add_field(name="⚠️ Performance", value="Loss - Review Strategy", inline=False)
            
            await channel.send(embed=embed)
            
        except Exception as e:
            log.error(f"Exit notification error: {e}")

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

# -------- Enhanced Market Analysis Functions --------
async def calculate_enhanced_metrics(df: pd.DataFrame, latest: pd.Series, level_price: float, direction: str) -> Dict[str, Any]:
    """Calculate enhanced metrics for Google Sheets"""
    try:
        # Basic metrics
        rsi = float(latest.get("rsi", 50)) if TA_AVAILABLE else 50.0
        volume = float(latest.get("volume", 0))
        price = float(latest["close"])
        open_price = float(latest.get("open", price))
        high = float(latest["high"])
        low = float(latest["low"])
        
        # Volume ratio
        avg_volume = float(df["volume"].tail(10).mean()) if len(df) >= 10 else volume
        volume_ratio = volume / avg_volume if avg_volume > 0 else 1.0
        
        # Market status from RSI
        if rsi > 75:
            market_status = "OVERBOUGHT"
        elif rsi < 25:
            market_status = "OVERSOLD"
        else:
            market_status = "NORMAL"
        
        # VWAP position
        vwap = float(latest.get("vwap", price))
        vwap_position = "Above" if price > vwap else "Below"
        
        # MACD status
        macd_hist = float(latest.get("macd_hist", 0)) if TA_AVAILABLE else 0
        macd_status = "Bullish" if macd_hist > 0 else "Bearish"
        
        # Market bias from trend
        if len(df) >= 5:
            recent_closes = df["close"].tail(5)
            trend_up = recent_closes.iloc[-1] > recent_closes.iloc[0]
            if direction == "Long":
                market_bias = "Bullish" if trend_up else "Neutral"
            else:
                market_bias = "Bearish" if not trend_up else "Neutral"
        else:
            market_bias = "Neutral"
        
        # Enhanced score calculation
        base_score = 4
        enhanced_score = base_score
        
        # Add points for favorable conditions
        if volume_ratio > 1.2:
            enhanced_score += 1
        if market_status == "NORMAL":
            enhanced_score += 1
        if (direction == "Long" and rsi > 50) or (direction == "Short" and rsi < 50):
            enhanced_score += 1
            
        enhanced_score = min(enhanced_score, 6)  # Cap at 6
        
        # Risk % and R:R Ratio calculations
        entry_price = price
        if direction == "Long":
            sl_price = entry_price * 0.99
            tp1_price = entry_price * 1.015
        else:
            sl_price = entry_price * 1.01
            tp1_price = entry_price * 0.985
            
        risk_pct = abs((entry_price - sl_price) / entry_price) * 100
        reward_pct = abs((tp1_price - entry_price) / entry_price) * 100
        rr_ratio = reward_pct / risk_pct if risk_pct > 0 else 0
        
        return {
            "enhanced_score": enhanced_score,
            "rsi_level": round(rsi, 2),
            "volume_ratio": round(volume_ratio, 2),
            "market_status": market_status,
            "vwap_position": vwap_position,
            "macd_status": macd_status,
            "market_bias": market_bias,
            "risk_pct": round(risk_pct, 2),
            "rr_ratio": round(rr_ratio, 1),
            "tier": "S" if enhanced_score >= 5 else "A" if enhanced_score == 4 else "B"
        }
        
    except Exception as e:
        log.error(f"Enhanced metrics calculation error: {e}")
        return {
            "enhanced_score": 4,
            "rsi_level": 50.0,
            "volume_ratio": 1.0,
            "market_status": "NORMAL",
            "vwap_position": "Above",
            "macd_status": "Neutral",
            "market_bias": "Neutral",
            "risk_pct": 1.0,
            "rr_ratio": 1.5,
            "tier": "A"
        }

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

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add technical indicators to dataframe"""
    try:
        if not TA_AVAILABLE or df is None or len(df) < 20:
            # Add basic indicators without TA library
            df["rsi"] = 50.0
            df["macd_hist"] = 0.0
            df["vwap"] = df["close"]
            return df
        
        df = df.copy()
        
        # RSI
        rsi_indicator = RSIIndicator(close=df["close"], window=14)
        df["rsi"] = rsi_indicator.rsi()
        
        # MACD
        macd_indicator = MACD(close=df["close"])
        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_hist"] = macd_indicator.macd_diff()
        
        # VWAP
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        vwap_num = (typical_price * df["volume"]).cumsum()
        vwap_den = df["volume"].cumsum()
        df["vwap"] = vwap_num / vwap_den
        
        # Fix deprecated fillna method
        df = df.ffill().bfill()
        
        return df
        
    except Exception as e:
        log.error(f"Indicator calculation error: {e}")
        # Return dataframe with basic fallback indicators
        df["rsi"] = 50.0
        df["macd_hist"] = 0.0
        df["vwap"] = df["close"]
        return df

# -------- Trade Monitoring Functions --------
async def monitor_active_trades(df: pd.DataFrame, current_price: float):
    """Monitor active trades for exit conditions"""
    try:
        if not trade_manager or not trade_manager.active:
            return
            
        await trade_manager.monitor_all_trades(current_price)
        
    except Exception as e:
        log.error(f"Trade monitoring error: {e}")

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
        e.add_field(name="Trade Closure", value="✅ Implemented", inline=True)
        return e
    except Exception as e:
        log.error(f"Status embed error: {e}")
        return discord.Embed(title="Status Error", description=str(e), color=discord.Color.red())

async def send_battle_signal(channel, t: TradeData):
    """Send enhanced battle signal"""
    try:
        color = discord.Color.green() if t.direction == TradeDirection.LONG else discord.Color.red()
        e = discord.Embed(
            title=f"⚔️ Battle Signal - {t.asset} {t.direction.name}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        e.add_field(name="🎯 Entry", value=f"${t.entry_price:.2f}", inline=True)
        e.add_field(name="🛑 Stop", value=f"${t.sl:.2f}", inline=True)
        e.add_field(name="🎪 TP1/TP2", value=f"${t.tp1:.2f} / ${t.tp2:.2f}", inline=True)
        e.add_field(name="⚔️ Knight", value=t.knight or "Unknown", inline=True)
        e.add_field(name="📊 Score", value=f"{t.score or 0}/6", inline=True)
        e.add_field(name="🏆 Level", value=t.level_name or "Unknown", inline=True)
        
        # Enhanced data if available
        if t.enhanced_data:
            enhanced_score = t.enhanced_data.get("enhanced_score", t.score or 0)
            tier = t.enhanced_data.get("tier", "A")
            e.add_field(name="✨ Enhanced Score", value=f"{enhanced_score}/6 ({tier})", inline=True)
        
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
            
            for trade_id, trade in list(trade_manager.active.items())[:10]:
                trade_info = (
                    f"**Direction:** {trade.direction.name}\n"
                    f"**Entry:** ${trade.entry_price:.2f}\n"
                    f"**TP1/TP2:** ${trade.tp1:.2f} / ${trade.tp2:.2f}\n"
                    f"**Stop:** ${trade.sl:.2f}\n"
                    f"**TP1 Done:** {'✅' if trade.tp1_done else '❌'}"
                )
                e.add_field(name=f"🎯 {trade_id[:8]}", value=trade_info, inline=True)
            
            await ctx.send(embed=e)
        except Exception as e:
            await ctx.send(f"Trades error: {e}")

    @bot.command(name="performance")
    async def _performance(ctx):
        """Show trading performance statistics"""
        try:
            stats = db.get_trade_performance()
            
            if not stats or stats.get("total_trades", 0) == 0:
                await ctx.send("📊 No completed trades yet")
                return
            
            embed = discord.Embed(
                title="📈 Trading Performance",
                color=discord.Color.gold() if stats.get("win_rate", 0) > 50 else discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(name="📊 Total Trades", value=str(stats.get("total_trades", 0)), inline=True)
            embed.add_field(name="✅ Winners", value=str(stats.get("winners", 0)), inline=True)
            embed.add_field(name="❌ Losers", value=str(stats.get("losers", 0)), inline=True)
            
            embed.add_field(name="🎯 Win Rate", value=f"{stats.get('win_rate', 0):.1f}%", inline=True)
            embed.add_field(name="📈 Avg P&L", value=f"{stats.get('avg_pnl', 0):+.2f}%", inline=True)
            embed.add_field(name="💰 Total P&L", value=f"{stats.get('total_pnl', 0):+.2f}%", inline=True)
            
            # Performance rating
            win_rate = stats.get("win_rate", 0)
            if win_rate >= 70:
                rating = "🌟 Excellent"
            elif win_rate >= 60:
                rating = "✅ Good"
            elif win_rate >= 50:
                rating = "⚖️ Average"
            else:
                rating = "⚠️ Needs Improvement"
            
            embed.add_field(name="🏆 Rating", value=rating, inline=False)
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Performance error: {e}")

    @bot.command(name="test_close")
    async def _test_close(ctx, trade_id: str = None):
        """Test closing a trade manually"""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("⚠️ Administrator permissions required")
            return
        
        try:
            if not trade_id:
                if not trade_manager.active:
                    await ctx.send("❌ No active trades to close")
                    return
                trade_id = list(trade_manager.active.keys())[0]
            
            if trade_id not in trade_manager.active:
                await ctx.send(f"❌ Trade {trade_id} not found")
                return
            
            trade = trade_manager.active[trade_id]
            test_exit_price = trade.entry_price * 1.01  # Simulate small profit
            
            result = await trade_manager.close_trade(trade_id, "Manual Test", test_exit_price)
            
            if result.get("success"):
                pnl = result.get("pnl_pct", 0)
                await ctx.send(f"✅ Test closure completed for trade {trade_id} - P&L: {pnl:+.2f}%")
            else:
                await ctx.send(f"❌ Test close failed: {result.get('error', 'Unknown error')}")
                
        except Exception as e:
            await ctx.send(f"❌ Test close failed: {e}")

    @bot.command(name="force_monitor")
    async def _force_monitor(ctx):
        """Force check all active trades"""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("⚠️ Administrator permissions required")
            return
        
        try:
            df = await mdp.fetch_ohlc(100)
            if df is None:
                await ctx.send("❌ Failed to fetch market data")
                return
            
            current_price = float(df.iloc[-1]["close"])
            before_count = len(trade_manager.active)
            
            await monitor_active_trades(df, current_price)
            
            after_count = len(trade_manager.active)
            closed_count = before_count - after_count
            
            embed = discord.Embed(
                title="🔍 Force Monitor Complete",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(name="💰 Current Price", value=f"${current_price:.2f}", inline=True)
            embed.add_field(name="📊 Trades Before", value=str(before_count), inline=True)
            embed.add_field(name="📊 Trades After", value=str(after_count), inline=True)
            embed.add_field(name="🏁 Trades Closed", value=str(closed_count), inline=True)
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"❌ Force monitor failed: {e}")

    @bot.command(name="market")
    async def _market(ctx):
        """Show current market analysis"""
        try:
            df = await mdp.fetch_ohlc(100)
            if df is None:
                await ctx.send("❌ Failed to fetch market data")
                return
                
            df = add_indicators(df)
            levels = calc_camarilla(df)
            
            if not levels:
                await ctx.send("❌ Failed to calculate Camarilla levels")
                return
                
            latest = df.iloc[-1]
            price = float(latest["close"])
            rsi = float(latest.get("rsi", 50))
            
            embed = discord.Embed(
                title="📊 Current Market Analysis",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            embed.add_field(name="💰 Price", value=f"${price:.2f}", inline=True)
            embed.add_field(name="📈 RSI", value=f"{rsi:.1f}", inline=True)
            embed.add_field(name="🌊 Regime", value="NORMAL", inline=True)
            
            # Show key levels
            level_text = []
            for name, level in levels.items():
                distance = price - level
                distance_pct = (distance / price) * 100
                level_text.append(f"**{name}:** ${level:.2f} ({distance_pct:+.2f}%)")
            
            embed.add_field(
                name="🎯 Camarilla Levels",
                value="\n".join(level_text[:6]),
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"Market analysis error: {e}")

    @tasks.loop(minutes=2)
    async def enhanced_scanner():
        """Enhanced scanner with trade monitoring"""
        try:
            await mdp.start()
            await trade_manager.start()
            
            df = await mdp.fetch_ohlc(100)
            if df is None:
                return
                
            df = add_indicators(df)
            levels = calc_camarilla(df)
            
            if not levels:
                return
                
            latest = df.iloc[-1]
            current_price = float(latest["close"])
            
            # CRITICAL: Monitor existing trades FIRST
            await monitor_active_trades(df, current_price)
            
            # Traditional signal generation
            await scan_for_signals(df, levels)
                
        except Exception as e:
            log.error(f"Enhanced scanner error: {e}")

    async def scan_for_signals(df: pd.DataFrame, levels: Dict[str, float]):
        """Scan for trading signals"""
        try:
            latest = df.iloc[-1]
            current_price = float(latest["close"])
            
            # Check for H5/L5 breakouts
            h5 = levels.get("H5")
            l5 = levels.get("L5")
            
            if h5 and current_price > h5:
                # Potential long signal
                enhanced_data = await calculate_enhanced_metrics(df, latest, h5, "Long")
                
                log.info(f"H5 breakout detected at ${current_price:.2f}")
                
                timestamp = datetime.now(timezone.utc)
                trade_id = f"L{timestamp.strftime('%m%d%H%M')}"
                
                t = TradeData(
                    id=trade_id,
                    asset=cfg.pair.replace("USD", ""),
                    direction=TradeDirection.LONG,
                    entry_price=float(current_price),
                    sl=float(current_price * 0.99),
                    tp1=float(current_price * 1.015),
                    tp2=float(current_price * 1.03),
                    level_name="H5",
                    level_price=float(h5),
                    knight="Sir Camarilla",
                    rating=enhanced_data.get("tier", "A"),
                    score=enhanced_data.get("enhanced_score", 4),
                    trade_type="H5_Breakout",
                    enhanced_data=enhanced_data
                )
                
                await trade_manager.open_trade(t)
                
                # Send battle signal
                channel = bot.get_channel(cfg.channels.battle_signals_id)
                if channel:
                    await send_battle_signal(channel, t)
                    
            elif l5 and current_price < l5:
                # Potential short signal
                enhanced_data = await calculate_enhanced_metrics(df, latest, l5, "Short")
                
                log.info(f"L5 breakout detected at ${current_price:.2f}")
                
                timestamp = datetime.now(timezone.utc)
                trade_id = f"S{timestamp.strftime('%m%d%H%M')}"
                
                t = TradeData(
                    id=trade_id,
                    asset=cfg.pair.replace("USD", ""),
                    direction=TradeDirection.SHORT,
                    entry_price=float(current_price),
                    sl=float(current_price * 1.01),
                    tp1=float(current_price * 0.985),
                    tp2=float(current_price * 0.97),
                    level_name="L5",
                    level_price=float(l5),
                    knight="Sir Camarilla",
                    rating=enhanced_data.get("tier", "A"),
                    score=enhanced_data.get("enhanced_score", 4),
                    trade_type="L5_Breakout",
                    enhanced_data=enhanced_data
                )
                
                await trade_manager.open_trade(t)
                
                # Send battle signal
                channel = bot.get_channel(cfg.channels.battle_signals_id)
                if channel:
                    await send_battle_signal(channel, t)
                
        except Exception as e:
            log.error(f"Signal scanning error: {e}")

    @enhanced_scanner.before_loop
    async def before_scanner():
        await bot.wait_until_ready()
        await trade_manager.start()
        await mdp.start()

    @bot.event
    async def on_ready():
            log.info(f"Logged in as {bot.user}")
            
            try:
                await trade_manager.rehydrate()
                if not enhanced_scanner.is_running():
                    enhanced_scanner.start()
                    
                # Send startup notification
                embed = discord.Embed(
                    title="🏰 Complete Control Tower v11.11 Online",
                    description="*Trade closure system implemented - Full automation ready*",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc)
                )
                
                embed.add_field(
                    name="✅ System Status",
                    value=(
                        "🤖 **Discord Bot**: Connected\n"
                        "📊 **Google Sheets**: Ready\n"
                        "📈 **Market Scanner**: Running\n"
                        "🏁 **Trade Closure**: IMPLEMENTED\n"
                        "🔄 **Trade Monitoring**: Active"
                    ),
                    inline=False
                )
                
                embed.add_field(
                    name="🎯 Trade Closure Logic",
                    value=(
                        "• **Long Stop Loss**: Price ≤ SL\n"
                        "• **Long TP1**: Price ≥ TP1 (50% exit)\n"
                        "• **Long TP2**: Price ≥ TP2 (full exit)\n"
                        "• **Short Stop Loss**: Price ≥ SL\n"
                        "• **Short TP1**: Price ≤ TP1 (50% exit)\n"
                        "• **Short TP2**: Price ≤ TP2 (full exit)"
                    ),
                    inline=False
                )
                
                channel = bot.get_channel(cfg.channels.scrolls_order_id)
                if channel:
                    await channel.send(embed=embed)
                    
                log.info("✅ Bot ready with complete trade closure system")
                    
            except Exception as e:
                log.error(f"Bot ready error: {e}")
    
    return bot


def _run_with_backoff(bot, token, log, max_attempts=7, base_delay=10, max_delay=300):
    """Run bot with exponential backoff on Cloudflare/Discord 429 at login."""
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            bot.run(token)
            return
        except Exception as e:
            # discord.errors.HTTPException has .status; others may not
            status = getattr(e, "status", None)
            if status == 429 or "429" in str(e):
                jitter = random.uniform(0, delay * 0.25)
                wait = min(max_delay, delay + jitter)
                try:
                    log.warning(f"429 on login; backing off {wait:.1f}s (attempt {attempt}/{max_attempts})")
                except Exception:
                    pass
                time.sleep(wait)
                delay = min(max_delay, delay * 2)
                continue
            raise
    raise RuntimeError("Exceeded max login retries due to 429s")


def main():
    try:
        global cfg, bot, db, sheets, trade_manager, mdp
        
        log.info("Starting Complete Control Tower v11.11...")
        
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
        log.info("Starting Discord bot with complete trade closure system...")
        _run_with_backoff(bot, cfg.token, log)
        
    except Exception as e:
        log.error(f"Main execution error: {e}")
        raise

if __name__ == "__main__":
    main()

# End of Control Tower v11.11 - Complete Trade Closure System