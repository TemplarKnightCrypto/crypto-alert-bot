# =====================================================================
# Control Tower - v11.9
# =====================================================================
# What's included
# - Channel routing (Scorecard, Battle, 100x, Proximity, Battleground, Setup)
# - Breakout detection w/ confirmation (H5 / L5)
# - Continuation setups ABOVE H5 / BELOW L5
# - Pullback & Reversal trade setups around H4 / L4
# - Tiering (S/A/B/C), persona fields (Leonis, Lucien, Orion)
# - Rehydrate + Google Sheets writeback for entries/exits
# - Health endpoint (Flask), 60s scan loop, 15m scorecard loop
# - Basic cooldowns for proximity + 100x + per-signal-type
#
# Env vars (ints):
#   SCRIBES_KEEP_ID, BATTLE_SIGNALS_ID, EAGLE_SIGNAL_ID,
#   KNIGHTS_WATCH_ID, ETH_BATTLEGROUND_ID, SETUP_ALERTS_ID
#
# Tunables:
#   PROXIMITY_PCT=0.2, PROXIMITY_COOLDOWN_MIN=10
#   HUNDRED_X_COOLDOWN_MIN=15
#   SIGNAL_COOLDOWN_MIN=3
#   INTERVAL_MIN=5, PAIR=ETHUSD
#
# Notes:
# - Indicators use pandas when TA-lib not available.
# - Replace placeholder scoring logic with your deeper confluence model if desired.
# =====================================================================

import os
import asyncio
import json
import sqlite3
import logging
import threading
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

# TA imports (optional)
try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD
    TA_AVAILABLE = True
except Exception:
    RSIIndicator = None
    EMAIndicator = None
    MACD = None
    TA_AVAILABLE = False

# -------- Logging --------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
log = logging.getLogger("control_tower")

# -------- Flask --------
app = Flask(__name__)

@app.route('/')
def root():
    return jsonify(ok=True, service="Control Tower v11.7 Advanced + Channels",
                   timestamp=datetime.now(timezone.utc).isoformat())

@app.route('/health')
def health():
    return jsonify(status="healthy", version="11.7-advanced", ta_library=TA_AVAILABLE,
                   timestamp=datetime.now(timezone.utc).isoformat())

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

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

    # Channel IDs
    scribes_keep_id: Optional[int] = None
    battle_signals_id: Optional[int] = None
    eagle_signal_id: Optional[int] = None
    knights_watch_id: Optional[int] = None
    eth_battleground_id: Optional[int] = None
    setup_alerts_id: Optional[int] = None

    # Tunables
    proximity_pct: float = 0.2
    proximity_cooldown_min: int = 10
    hundred_x_cooldown_min: int = 15
    signal_cooldown_min: int = 3

    @staticmethod
    def _read_int(name: str) -> Optional[int]:
        val = os.getenv(name, "").strip()
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            log.warning(f"Env {name} not an int: {val!r}")
            return None

    @staticmethod
    def from_env():
        load_dotenv()
        token = os.getenv("TOKEN", "").strip()
        if not token:
            raise ValueError("Discord TOKEN is required")

        tm = os.getenv("TRAIL_MODE", "none").lower()
        trail_mode = TrailMode.ATR if tm == "atr" else TrailMode.CHAND if tm == "chand" else TrailMode.NONE

        def _float(name, default): 
            try: return float(os.getenv(name, str(default)))
            except: return default
        def _int(name, default): 
            try: return int(os.getenv(name, str(default)))
            except: return default

        cfg = BotConfig(
            token=token,
            sheets_url=os.getenv("GOOGLE_SHEETS_WEBHOOK") or None,
            sheets_token=os.getenv("SHEETS_TOKEN") or None,
            partial_fraction=_float("PARTIAL_FRACTION", 0.5),
            be_after_tp1=os.getenv("BE_AFTER_TP1", "true").lower() in ("1","true","y","yes"),
            be_offset_pct=_float("BE_OFFSET_PCT", 0.0),
            trail_mode=trail_mode,
            trail_atr_period=_int("TRAIL_ATR_PERIOD", 14),
            trail_atr_mult=_float("TRAIL_ATR_MULT", 3.0),
            chand_lookback=_int("CHAN_LOOKBACK", 22),
            pair=(os.getenv("PAIR","ETHUSD").upper()),
            interval_min=_int("INTERVAL_MIN", 5),
            scribes_keep_id=BotConfig._read_int("SCRIBES_KEEP_ID"),
            battle_signals_id=BotConfig._read_int("BATTLE_SIGNALS_ID"),
            eagle_signal_id=BotConfig._read_int("EAGLE_SIGNAL_ID"),
            knights_watch_id=BotConfig._read_int("KNIGHTS_WATCH_ID"),
            eth_battleground_id=BotConfig._read_int("ETH_BATTLEGROUND_ID"),
            setup_alerts_id=BotConfig._read_int("SETUP_ALERTS_ID"),
            proximity_pct=_float("PROXIMITY_PCT", 0.2),
            proximity_cooldown_min=_int("PROXIMITY_COOLDOWN_MIN", 10),
            hundred_x_cooldown_min=_int("HUNDRED_X_COOLDOWN_MIN", 15),
            signal_cooldown_min=_int("SIGNAL_COOLDOWN_MIN", 3)
        )
        return cfg

# -------- DB --------
class DatabaseManager:
    def __init__(self, path="trades.db"):
        self.path = path
        self._ensure()

    def _ensure(self):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS trades(
                id TEXT PRIMARY KEY,
                asset TEXT,
                direction TEXT,
                entry REAL, sl REAL, tp1 REAL, tp2 REAL,
                status TEXT,
                opened_at TEXT, closed_at TEXT,
                be_active INTEGER DEFAULT 0,
                trail_mode TEXT,
                extra TEXT
            );
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS partial_exits(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT, fraction REAL, price REAL, time TEXT
            );
            """)
            conn.commit()

    def save_trade(self, t):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""
            INSERT OR REPLACE INTO trades(id,asset,direction,entry,sl,tp1,tp2,status,opened_at,closed_at,be_active,trail_mode,extra)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t.id, t.asset, t.direction.name, t.entry_price, t.sl, t.tp1, t.tp2, t.status.name,
                t.opened_at.isoformat() if t.opened_at else None,
                t.closed_at.isoformat() if t.closed_at else None,
                1 if t.be_active else 0,
                t.trail_mode.value if t.trail_mode else TrailMode.NONE.value,
                json.dumps(t.enhanced_data or {})
            ))
            conn.commit()

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

    # Enrichment
    rating: Optional[str] = None  # S/A/B/C
    score: Optional[int] = None   # 1..6
    knight: Optional[str] = None
    trade_type: Optional[str] = None
    level_name: Optional[str] = None
    level_price: Optional[float] = None
    enhanced_data: Optional[Dict[str, Any]] = field(default_factory=dict)

# -------- Sheets --------
class GoogleSheetsIntegration:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token
        # Optional JSON mapping from env to rename fields, e.g. {"entry_price":"Entry","tp1":"TP1"}
        import os, json
        self.field_map = {}
        try:
            raw = os.getenv("SHEETS_FIELD_MAP", "")
            if raw.strip():
                self.field_map = json.loads(raw)
        except Exception:
            self.field_map = {}

    def _apply_field_map(self, payload: dict) -> dict:
        if not self.field_map:
            return payload
        out = {}
        for k, v in payload.items():
            out[self.field_map.get(k, k)] = v
        return out

    async def _post(self, session: aiohttp.ClientSession, payload: Dict[str, Any]):
        if not self.url or not self.token:
            return {"status": "skipped", "reason": "no_config"}
        headers = {"x-app-secret": self.token, "content-type": "application/json"}
        try:
            async with session.post(self.url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                txt = await r.text()
                return {"status": r.status, "body": txt}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    
    async def send_trade_entry(self, session, t: TradeData):
        # Flatten metrics to explicit fields
        ed = t.enhanced_data or {}
        # Derive a few if missing
        try:
            rsi = float(ed.get("rsi_level", 50))
        except Exception:
            rsi = 50.0
        # Market Status from RSI
        market_status = ed.get("market_status")
        if not market_status:
            if rsi >= 75:
                market_status = "OVERBOUGHT"
            elif rsi <= 25:
                market_status = "OVERSOLD"
            else:
                market_status = "NORMAL"
        trend_bias = ed.get("trend_bias") or ed.get("market_bias") or "-"
        # vwap position if available in ed; leave blank otherwise
        vwap_position = ed.get("vwap_position") or "-"
        macd_status = ed.get("macd_status") or "-"
        # Risk & RR from entry/sl/tp1 (fallbacks)
        try:
            risk_pct = abs((t.entry_price - t.sl) / t.entry_price) * 100.0
        except Exception:
            risk_pct = None
        try:
            reward_pct = abs(((t.tp1 or t.tp2) - t.entry_price) / t.entry_price) * 100.0 if (t.tp1 or t.tp2) else None
            rr_ratio = (reward_pct / risk_pct) if (reward_pct and risk_pct) else None
        except Exception:
            rr_ratio = None
        # Body strength heuristic (if present in ed)
        candle_body_strength = ed.get("candle_body_strength") or "-"
        # Setup age
        setup_age_minutes = ed.get("setup_age_minutes") or ""
        # Breakout structure/confluence
        breakout_structure = ed.get("breakout_structure") or ""
        confluence_count = ed.get("confluence_count") or ""
        market_session = ed.get("market_session") or ""
        distance_from_level_pct = ed.get("distance_from_level_pct") or ""
        recent_news_events = ed.get("recent_news_events") or "No"
        volatility_state = ed.get("volatility_state") or ""
        trend_strength = ed.get("trend_strength") or (f"Moderate {trend_bias}" if trend_bias not in ("-", "") else "")

        base = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trade_id": t.id,
            "asset": t.asset,
            "direction": t.direction.name.title(),
            "status": "OPEN",
            "level_name": t.level_name or "",
            "trade_type": t.trade_type or "",
            "score": t.score or 0,                         # Original Score (legacy)
            "enhanced_score": ed.get("enhanced_score", t.score or 0),
            "confidence": t.rating or "",
            "knight": t.knight or "",
            # Flat metrics for your sheet
            "rsi_level": rsi,
            "volume_ratio": ed.get("volume_ratio"),
            "market_status": market_status,
            "vwap_position": vwap_position,
            "macd_status": macd_status,
            "trend_bias": trend_bias,
            "risk_pct": round(risk_pct, 2) if risk_pct is not None else "",
            "rr_ratio": round(rr_ratio, 2) if rr_ratio is not None else "",
            "setup_age_minutes": setup_age_minutes,
            "breakout_structure": breakout_structure,
            "confluence_count": confluence_count,
            "candle_body_strength": candle_body_strength,
            "market_session": market_session,
            "distance_from_level_pct": distance_from_level_pct,
            "recent_news_events": recent_news_events,
            "volatility_state": volatility_state,
            "trend_strength": trend_strength,
            "enhanced_data": ed,   # still include the blob for compatibility
        }
        price_block = {
            "entry_price": t.entry_price,
            "entry": t.entry_price,          # alias
            "stop_loss": t.sl,
            "stop": t.sl,                    # alias
            "tp1": t.tp1,
            "target1": t.tp1,                # alias
            "tp2": t.tp2,
            "target2": t.tp2,                # alias
        }
        payload = {**base, **price_block}
        payload = self._apply_field_map(payload)
        return await self._post(session, payload)

    async def rehydrate_open_trades(self, session) -> List[TradeData]:
        if not self.url or not self.token:
            return []
        params = {"action": "open", "key": self.token}
        try:
            async with session.get(self.url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                txt = await r.text()
                js = json.loads(txt) if txt else {}
        except Exception:
            return []
        out = []
        for row in js.get("rows", []):
            try:
                dir_raw = str(row.get("direction","Long")).upper()
                direction = TradeDirection.LONG if dir_raw.startswith("L") else TradeDirection.SHORT
                out.append(TradeData(
                    id=str(row.get("trade_id") or f"rehyd_{len(out)}"),
                    asset=str(row.get("asset") or "ETHUSD"),
                    direction=direction,
                    entry_price=float(row.get("entry_price") or 0),
                    sl=float(row.get("stop_loss") or 0),
                    tp1=float(row.get("tp1") or row.get("target1") or 0),
                    tp2=float(row.get("tp2") or row.get("target2") or 0),
                    score=int(row.get("score") or 0),
                    rating=str(row.get("confidence") or ""),
                    knight=str(row.get("knight") or ""),
                    level_name=str(row.get("level_name") or ""),
                    trade_type=str(row.get("trade_type") or ""),
                ))
            except Exception:
                continue
        return out

    async def send_trade_exit(self, session, trade_id: str, reason: str, price: float, time_iso: str, pnl_pct: float):
        base = {
            "action": "update",
            "trade_id": trade_id,
            "exit_price": price,
            "exit": price,             # alias
            "exit_reason": reason,
            "pnl_pct": pnl_pct,
            "pnl": pnl_pct,            # alias
            "exit_time": time_iso,
            "closed_at": time_iso,     # alias
            "status": "CLOSED",
        }
        payload = self._apply_field_map(base)
        return await self._post(session, payload)


# -------- Trade Manager --------
class TradeManager:
    def __init__(self, cfg: BotConfig, db: DatabaseManager, sheets: GoogleSheetsIntegration):
        self.cfg = cfg
        self.db = db
        self.sheets = sheets
        self.active: Dict[str, TradeData] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def rehydrate(self):
        await self.start()
        rows = await self.sheets.rehydrate_open_trades(self.session)
        for t in rows:
            t.trail_mode = self.cfg.trail_mode
            self.active[t.id] = t
            self.db.save_trade(t)
        log.info(f"Rehydrated: {len(rows)} open trades")

    async def open_trade(self, t: TradeData):
        await self.start()
        t.trail_mode = self.cfg.trail_mode
        self.active[t.id] = t
        self.db.save_trade(t)
        await self.sheets.send_trade_entry(self.session, t)

# -------- Market Data --------
class MarketDataProvider:
    KRAKEN_PAIR_MAP = {"ETHUSD": "ETHUSD", "BTCUSD": "XBTUSD", "SOLUSD": "SOLUSD"}

    def __init__(self, pair: str, interval_min: int):
        self.pair = self.KRAKEN_PAIR_MAP.get(pair, pair)
        self.interval_min = interval_min
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def fetch_ohlc(self, n=500) -> pd.DataFrame:
        await self.start()
        url = "https://api.kraken.com/0/public/OHLC"
        params = {"pair": self.pair, "interval": self.interval_min}
        async with self.session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Kraken error: {resp.status}")
            data = await resp.json()
        key = [k for k in data["result"].keys() if k != "last"][0]
        rows = data["result"][key][-n:]
        df = pd.DataFrame(rows, columns=["time","open","high","low","close","vwap","volume","count"])
        df = df.astype({"time":int,"open":float,"high":float,"low":float,"close":float,"volume":float})
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

# -------- Indicators & Scoring --------
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if TA_AVAILABLE:
        try:
            out["ema_fast"] = EMAIndicator(close=out["close"], window=21).ema_indicator()
            out["ema_slow"] = EMAIndicator(close=out["close"], window=50).ema_indicator()
            out["rsi"] = RSIIndicator(close=out["close"], window=14).rsi()
        except Exception:
            out["ema_fast"] = ema(out["close"], 21)
            out["ema_slow"] = ema(out["close"], 50)
            delta = out["close"].diff()
            up = delta.clip(lower=0).rolling(14).mean()
            down = (-delta.clip(upper=0)).rolling(14).mean()
            rs = (up / (down.replace(0, np.nan))).replace([np.inf,-np.inf], np.nan).fillna(1.0)
            out["rsi"] = 100 - (100 / (1 + rs))
    else:
        out["ema_fast"] = ema(out["close"], 21)
        out["ema_slow"] = ema(out["close"], 50)
        delta = out["close"].diff()
        up = delta.clip(lower=0).rolling(14).mean()
        down = (-delta.clip(upper=0)).rolling(14).mean()
        rs = (up / (down.replace(0, np.nan))).replace([np.inf,-np.inf], np.nan).fillna(1.0)
        out["rsi"] = 100 - (100 / (1 + rs))
    out["vol_avg10"] = out["volume"].rolling(10).mean()
    return out

def calc_camarilla(df: pd.DataFrame) -> Dict[str, float]:
    if len(df) < 2:
        return {}
    prev = df.iloc[-2]
    H, L, C = float(prev["high"]), float(prev["low"]), float(prev["close"])
    r = H - L
    if r <= 0:
        return {}
    L3 = C - (r * 1.1/12); H3 = C + (r * 1.1/12)
    L4 = C - (r * 1.1/6 ); H4 = C + (r * 1.1/6 )
    L5 = C - (r * 1.1/2 ); H5 = C + (r * 1.1/2 )
    return {"L3":L3,"L4":L4,"L5":L5,"H3":H3,"H4":H4,"H5":H5,"P":C}

def confirm_breakout(c,o,h,l,vol,avg_vol,level:float,direction:TradeDirection)->Tuple[bool,Dict[str,Any]]:
    rng = max(h-l, 1e-9)
    body_ratio = abs(c-o)/rng
    close_beyond = (direction==TradeDirection.LONG and c>level) or (direction==TradeDirection.SHORT and c<level)
    vol_ok = vol > (avg_vol*1.2 if avg_vol>0 else vol)
    return (body_ratio>0.5 and close_beyond and vol_ok), {"body_ratio":body_ratio,"close_beyond":close_beyond,"vol_ok":vol_ok}

def compute_confluence(last: pd.Series, levels: Dict[str,float]) -> Dict[str, Any]:
    c = float(last["close"]); v = float(last["volume"])
    avg_vol = float(last.get("vol_avg10", v)) or v
    rsi = float(last.get("rsi", 50))
    ema_fast = float(last.get("ema_fast", c))
    ema_slow = float(last.get("ema_slow", c))
    vwap = float(last.get("vwap", c)) if "vwap" in last else c
    trend_up = ema_fast > ema_slow
    volume_ratio = v/avg_vol if avg_vol>0 else 1.0
    bias = "Bullish" if trend_up else "Bearish"
    score = 2
    if volume_ratio>1.2: score += 1
    if (trend_up and c>vwap) or ((not trend_up) and c<vwap): score += 1
    if (rsi>55 and trend_up) or (rsi<45 and not trend_up): score += 1
    score = min(score, 6)
    def tier_map(s):
        return "S" if s>=5 else "A" if s==4 else "B" if s==3 else "C"
    return {
        "enhanced_score": int(score),
        "tier": tier_map(score),
        "volume_ratio": float(volume_ratio),
        "rsi_level": round(rsi,2),
        "trend_bias": bias,
    }

def knight_for(trade_type: str) -> str:
    # Leonis: momentum/breakouts/continuations; Lucien: pullbacks & structure; Orion: reversals
    if "Breakout" in trade_type or "Continuation" in trade_type:
        return "Sir Leonis Ironhart"
    if "Pullback" in trade_type:
        return "Sir Lucien Frostveil"
    if "Reversal" in trade_type:
        return "Orion Vellum"
    return "Sir Leonis Ironhart"

# -------- Discord Routing --------
INTENTS = discord.Intents.default()
INTENTS.message_content = True

cfg: Optional[BotConfig] = None
bot: Optional[commands.Bot] = None
db: Optional[DatabaseManager] = None
sheets: Optional[GoogleSheetsIntegration] = None
trade_manager: Optional[TradeManager] = None
mdp: Optional[MarketDataProvider] = None
_channel_cache: Dict[int, discord.abc.GuildChannel] = {}

# Cooldowns
_prox_cooldown: Dict[str, datetime] = {}
_hundred_x_cooldown_at: Optional[datetime] = None
_signal_last_ts: Dict[str, datetime] = {}

async def resolve_channel(channel_id: Optional[int]) -> Optional[discord.TextChannel]:
    if not channel_id: return None
    ch = _channel_cache.get(channel_id) or bot.get_channel(channel_id)
    if ch:
        _channel_cache[channel_id] = ch  # type: ignore
        return ch  # type: ignore
    try:
        ch = await bot.fetch_channel(channel_id)
        _channel_cache[channel_id] = ch  # type: ignore
        return ch  # type: ignore
    except Exception:
        return None

async def send_to_channel(channel_id: Optional[int], embed: discord.Embed):
    ch = await resolve_channel(channel_id)
    if not ch: return False
    try:
        await ch.send(embed=embed)
        return True
    except Exception as e:
        log.warning(f"Send failed for {channel_id}: {e}")
        return False

def tier_emoji(tier: str) -> str:
    return {"S":"🟣","A":"🟢","B":"🟡","C":"⚪"}.get(tier,"⚪")

def routed_battle_embed(t: TradeData) -> discord.Embed:
    color = discord.Color.green() if t.direction==TradeDirection.LONG else discord.Color.red()
    title = f"{'⚔️' if 'Breakout' in (t.trade_type or '') or 'Continuation' in (t.trade_type or '') else '🛡️'} {t.trade_type or 'Signal'} — {t.asset} {t.direction.name}"
    e = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Entry", value=f"{t.entry_price:.2f}", inline=True)
    e.add_field(name="Stop", value=f"{t.sl:.2f}", inline=True)
    e.add_field(name="TP1 / TP2", value=f"{t.tp1:.2f} / {t.tp2:.2f}", inline=True)
    e.add_field(name="Level", value=f"{t.level_name or '-'} @ {t.level_price:.2f}" if t.level_price else (t.level_name or "-"), inline=True)
    if t.score is not None and t.rating:
        e.add_field(name="Score", value=f"{t.score}/6 {tier_emoji(t.rating)}", inline=True)
    if t.knight:
        e.add_field(name="Knight", value=t.knight, inline=True)
    if t.enhanced_data:
        ed = t.enhanced_data
        extras = f"RSI {ed.get('rsi_level','-')}, Vol× {round(ed.get('volume_ratio',1.0),2)}, Bias {ed.get('trend_bias', ed.get('market_bias','-'))}"
        e.add_field(name="Confluence", value=extras, inline=False)
    return e

async def route_battle_signal(t: TradeData):
    e = routed_battle_embed(t)
    await send_to_channel(cfg.battle_signals_id, e)

async def route_100x_alert(t: TradeData):
    e = discord.Embed(
        title=f"🦅 100x Signal — {t.asset} {t.direction.name}",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
        description="High-confluence opportunity detected (≥5/6)."
    )
    e.add_field(name="Entry", value=f"{t.entry_price:.2f}", inline=True)
    e.add_field(name="Stop", value=f"{t.sl:.2f}", inline=True)
    e.add_field(name="TP2", value=f"{t.tp2:.2f}", inline=True)
    e.add_field(name="Tier", value=t.rating or "-", inline=True)
    e.add_field(name="Knight", value=t.knight or "-", inline=True)
    await send_to_channel(cfg.eagle_signal_id, e)

async def route_exit_alert(t: TradeData, exit_price: float, reason: str, pnl_pct: float):
    color = discord.Color.light_grey() if "TP2" in reason else discord.Color.dark_red()
    title_icon = "🏁" if "TP2" in reason else "⛔"
    e = discord.Embed(
        title=f"{title_icon} Exit — {t.asset} {t.direction.name} ({reason})",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Entry", value=f"{t.entry_price:.2f}", inline=True)
    e.add_field(name="Exit", value=f"{exit_price:.2f}", inline=True)
    e.add_field(name="PnL %", value=f"{pnl_pct:.2f}", inline=True)
    e.add_field(name="Level", value=f"{t.level_name or '-'}", inline=True)
    e.set_footer(text=f"Trade ID: {t.id}")
    await send_to_channel(cfg.battle_signals_id, e)

async def route_proximity_warning(side: str, level_name: str, level_price: float, price: float, distance_pct: float):
    e = discord.Embed(title=f"🕰️ Knight's Warning — {cfg.pair} near {level_name}",
                      color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Side", value=side, inline=True)
    e.add_field(name="Price → Level", value=f"{price:.2f} → {level_price:.2f}", inline=True)
    e.add_field(name="Distance", value=f"{distance_pct:.3f}%", inline=True)
    await send_to_channel(cfg.knights_watch_id, e)

async def route_setup_alert(zone: str, info: Dict[str, Any]):
    e = discord.Embed(title=f"🗺️ Setup Intel — {cfg.pair} {zone}",
                      color=discord.Color.teal(), timestamp=datetime.now(timezone.utc))
    for k, v in list(info.items())[:6]:
        e.add_field(name=k, value=str(v), inline=True)
    await send_to_channel(cfg.setup_alerts_id, e)

async def route_battleground_report(price: float, levels: Dict[str, float]):
    e = discord.Embed(title=f"🏰 ETH Battleground — {cfg.pair}",
                      color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Price", value=f"{price:.2f}", inline=True)
    e.add_field(name="H5 / L5", value=f"{levels.get('H5',0):.2f} / {levels.get('L5',0):.2f}", inline=True)
    e.add_field(name="H4 / L4", value=f"{levels.get('H4',0):.2f} / {levels.get('L4',0):.2f}", inline=True)
    await send_to_channel(cfg.eth_battleground_id, e)

async def route_market_scorecard(df: pd.DataFrame, levels: Dict[str, float]):
    last = df.iloc[-1]
    e = discord.Embed(title=f"📜 Market Scorecard — {cfg.pair} ({cfg.interval_min}m)",
                      color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Close", value=f"{float(last['close']):.2f}", inline=True)
    e.add_field(name="Vol×10", value=f"{(float(last['volume'])/(float(last.get('vol_avg10',1.0)) or 1.0)):.2f}", inline=True)
    e.add_field(name="H5 / L5", value=f"{levels.get('H5',0):.2f} / {levels.get('L5',0):.2f}", inline=True)
    await send_to_channel(cfg.scribes_keep_id, e)

# ---------- Advanced Detection Blocks ----------
def detect_continuation(last: pd.Series, levels: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Continuation above H5 / below L5: momentum follow-through after prior breakout."""
    c, o, h, l = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"])
    v = float(last["volume"])
    avg_vol = float(last.get("vol_avg10", v))
    ema_fast, ema_slow = float(last.get("ema_fast", c)), float(last.get("ema_slow", c))
    H5, L5 = levels.get("H5"), levels.get("L5")
    if H5 and c > H5 and ema_fast > ema_slow and v > (avg_vol * 1.1):
        return {"type": "H5_Continuation", "direction": TradeDirection.LONG, "level": "H5", "level_price": H5}
    if L5 and c < L5 and ema_fast < ema_slow and v > (avg_vol * 1.1):
        return {"type": "L5_Continuation", "direction": TradeDirection.SHORT, "level": "L5", "level_price": L5}
    return None


def detect_pullback(df: pd.DataFrame, levels: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Pullback near H4/L4 with retake + body/volume confirmation."""
    if len(df) < 2:
        return None
    last = df.iloc[-1]; prev = df.iloc[-2]
    c, o, h, l = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"])
    v = float(last["volume"])
    avg_vol = float(last.get("vol_avg10", v))
    rsi = float(last.get("rsi", 50))
    H4, L4 = levels.get("H4"), levels.get("L4")
    rng = max(h - l, 1e-9)
    body_ratio = abs(c - o) / rng
    # Long pullback: wick into/near H4 then close back above
    if H4 and (l <= H4 * 1.001) and (c > H4) and body_ratio > 0.4 and v > (avg_vol * 1.1) and rsi > 50:
        return {"type": "H4_Pullback_Long", "direction": TradeDirection.LONG, "level": "H4", "level_price": H4}
    # Short pullback: wick into/near L4 then close back below
    if L4 and (h >= L4 * 0.999) and (c < L4) and body_ratio > 0.4 and v > (avg_vol * 1.1) and rsi < 50:
        return {"type": "L4_Pullback_Short", "direction": TradeDirection.SHORT, "level": "L4", "level_price": L4}
    return None


def detect_reversal(df: pd.DataFrame, levels: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """Structure failure at H4/L4 -> reversal back inside range."""
    if len(df) < 2:
        return None
    last = df.iloc[-1]; prev = df.iloc[-2]
    c, o, h, l = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"])
    v = float(last["volume"])
    avg_vol = float(last.get("vol_avg10", v))
    H4, L4 = levels.get("H4"), levels.get("L4")
    rng = max(h - l, 1e-9)
    body_ratio = abs(c - o) / rng

    # From above H4 failing back under -> short reversal
    if H4 and prev["close"] > H4 and c < H4 and body_ratio > 0.5 and v > (avg_vol * 1.2):
        return {"type": "H4_Reversal_Short", "direction": TradeDirection.SHORT, "level": "H4", "level_price": H4}
    # From below L4 failing back over -> long reversal
    if L4 and prev["close"] < L4 and c > L4 and body_ratio > 0.5 and v > (avg_vol * 1.2):
        return {"type": "L4_Reversal_Long", "direction": TradeDirection.LONG, "level": "L4", "level_price": L4}
    return None

def build_trade(last: pd.Series, levels: Dict[str,float], signal: Dict[str,Any]) -> TradeData:
    c = float(last["close"])
    direction: TradeDirection = signal["direction"]
    trade_type = signal["type"]
    level_name = signal["level"]
    level_price = float(signal["level_price"])

    # Risk model (simple % for demo; replace with your calc)
    if direction==TradeDirection.LONG:
        sl = c * 0.99; tp1 = c * 1.015; tp2 = c * 1.03
    else:
        sl = c * 1.01; tp1 = c * 0.985; tp2 = c * 0.97

    # Scoring + tier
    conf = compute_confluence(last, levels)
    score = int(conf["enhanced_score"])
    tier = str(conf["tier"])
    knight = knight_for(trade_type)

    return TradeData(
        id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        asset=cfg.pair,
        direction=direction,
        entry_price=c, sl=sl, tp1=tp1, tp2=tp2,
        trade_type=trade_type,
        level_name=level_name, level_price=level_price,
        rating=tier, score=score, knight=knight,
        enhanced_data=conf
    )

# --- test helper (put below build_trade) ---
def build_test_trade(
    direction: str = "LONG",
    entry: float = 2700.0,
    sl: float = 2650.0,
    tp1: float = 2740.0,
    tp2: float = 2790.0,
    score: int = 5,
    tier: str = "S",
    trade_type: str | None = None,
    level_name: str | None = None,
    level_price: float | None = None,
) -> TradeData:
    direction_enum = TradeDirection.LONG if str(direction).upper().startswith("L") else TradeDirection.SHORT
    trade_type = trade_type or ("H5_Breakout" if direction_enum == TradeDirection.LONG else "L5_Breakout")
    level_name = level_name or ("H5" if direction_enum == TradeDirection.LONG else "L5")
    level_price = level_price if level_price is not None else entry

    t = TradeData(
        id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        asset=cfg.pair,
        direction=direction_enum,
        entry_price=float(entry),
        sl=float(sl),
        tp1=float(tp1),
        tp2=float(tp2),
        trade_type=trade_type,
        level_name=level_name,
        level_price=float(level_price),
        rating=tier,
        score=int(score),
        knight=knight_for(trade_type),
        enhanced_data={
            "enhanced_score": int(score),
            "volume_ratio": 1.5,
            "rsi_level": 62.0 if direction_enum == TradeDirection.LONG else 38.0,
            "trend_bias": "Bullish" if direction_enum == TradeDirection.LONG else "Bearish",
        },
    )
    return t

async def _close_trade_and_notify(t: TradeData, exit_price: float, reason: str):
    # Compute PnL %
    try:
        if t.direction == TradeDirection.LONG:
            pnl_pct = (exit_price / t.entry_price - 1.0) * 100.0
        else:
            pnl_pct = (1.0 - exit_price / t.entry_price) * 100.0
    except Exception:
        pnl_pct = 0.0
    # Mark closed & persist
    t.status = TradeStatus.CLOSED
    t.closed_at = datetime.now(timezone.utc)
    db.save_trade(t)  # upsert with CLOSED
    # Sheets update
    try:
        await trade_manager.start()
        time_iso = t.closed_at.isoformat()
        await sheets.send_trade_exit(trade_manager.session, t.id, reason, exit_price, time_iso, round(pnl_pct, 2))
    except Exception as e:
        log.warning(f"Sheets exit update failed: {e}")
    # Alert channel
    await route_exit_alert(t, exit_price, reason, round(pnl_pct, 2))
    # Remove from active
    try:
        trade_manager.active.pop(t.id, None)
    except Exception:
        pass
# -------- Schedulers --------
@tasks.loop(seconds=60)
async def scan_loop():
    global _hundred_x_cooldown_at
    await mdp.start()
    await trade_manager.start()

    df = await mdp.fetch_ohlc(120)
    df = compute_indicators(df)
    levels = calc_camarilla(df)
    if not levels: return
    last = df.iloc[-1]
    c,o,h,l = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"])
    v = float(last["volume"])
    avg_vol = float(last.get("vol_avg10", v))
    H5, L5 = levels.get("H5"), levels.get("L5")
    H4, L4 = levels.get("H4"), levels.get("L4")

    # ---------- Proximity warnings ----------
    for name in ("H5","L5"):
        if levels.get(name):
            dist_pct = abs(c - levels[name]) / c * 100
            if dist_pct <= cfg.proximity_pct:
                now = datetime.now(timezone.utc)
                last_ts = _prox_cooldown.get(name)
                if not last_ts or (now - last_ts) >= timedelta(minutes=cfg.proximity_cooldown_min):
                    side = "Approaching Resistance" if name=="H5" else "Approaching Support"
                    await route_proximity_warning(side, name, levels[name], c, dist_pct)
                    _prox_cooldown[name] = now

    # ---------- Setup intel zones ----------
    if H4 and H5 and H4 < c < H5:
        await route_setup_alert("H4→H5 (pre-breakout)",
                                {"Close":f"{c:.2f}","Vol×":f"{(v/(avg_vol or 1)):.2f}","Bias": "Bull"})
    if L4 and L5 and L5 < c < L4:
        await route_setup_alert("L5→L4 (pre-breakdown)",
                                {"Close":f"{c:.2f}","Vol×":f"{(v/(avg_vol or 1)):.2f}","Bias": "Bear"})

    # ---------- Primary: Breakouts ----------
    if H5 and c>H5:
        ok,_ = confirm_breakout(c,o,h,l,v,avg_vol,H5,TradeDirection.LONG)
        if ok and not should_throttle("H5_Breakout", cfg.signal_cooldown_min):
            t = build_trade(last, levels, {"type":"H5_Breakout","direction":TradeDirection.LONG,"level":"H5","level_price":H5})
            await trade_manager.open_trade(t)
            await route_battle_signal(t)
            # 100x gate
            if (t.score or 0) >= 5:
                now = datetime.now(timezone.utc)
                if not _hundred_x_cooldown_at or (now - _hundred_x_cooldown_at) >= timedelta(minutes=cfg.hundred_x_cooldown_min):
                    await route_100x_alert(t); _hundred_x_cooldown_at = now

    if L5 and c<L5:
        ok,_ = confirm_breakout(c,o,h,l,v,avg_vol,L5,TradeDirection.SHORT)
        if ok and not should_throttle("L5_Breakdown", cfg.signal_cooldown_min):
            t = build_trade(last, levels, {"type":"L5_Breakout","direction":TradeDirection.SHORT,"level":"L5","level_price":L5})
            await trade_manager.open_trade(t)
            await route_battle_signal(t)
            if (t.score or 0) >= 5:
                now = datetime.now(timezone.utc)
                if not _hundred_x_cooldown_at or (now - _hundred_x_cooldown_at) >= timedelta(minutes=cfg.hundred_x_cooldown_min):
                    await route_100x_alert(t); _hundred_x_cooldown_at = now

    # ---------- Secondary: Continuations ----------
    cont = detect_continuation(last, levels)
    if cont:
        key = cont["type"]
        if not should_throttle(key, cfg.signal_cooldown_min):
            t = build_trade(last, levels, cont)
            await trade_manager.open_trade(t)
            await route_battle_signal(t)
            if (t.score or 0) >= 5:
                now = datetime.now(timezone.utc)
                if not _hundred_x_cooldown_at or (now - _hundred_x_cooldown_at) >= timedelta(minutes=cfg.hundred_x_cooldown_min):
                    await route_100x_alert(t); _hundred_x_cooldown_at = now

    # ---------- Tertiary: Pullbacks & Reversals ----------
    pb = detect_pullback(df, levels)
    if pb:
        key = pb["type"]
        if not should_throttle(key, cfg.signal_cooldown_min):
            t = build_trade(last, levels, pb)
            await trade_manager.open_trade(t)
            await route_battle_signal(t)

    rv = detect_reversal(df, levels)
    if rv:
        key = rv["type"]
        if not should_throttle(key, cfg.signal_cooldown_min):
            t = build_trade(last, levels, rv)
            # Orion handles reversals
            t.knight = "Orion Vellum"
            await trade_manager.open_trade(t)
            await route_battle_signal(t)

    
    # ---------- Manage exits for active trades (TP2 / SL) ----------
    to_check = list(trade_manager.active.values())
    for t in to_check:
        if t.status != TradeStatus.OPEN:
            continue
        # Use high/low of last bar for intrabar hits
        if t.direction == TradeDirection.LONG:
            if h >= (t.tp2 or float('inf')):
                await _close_trade_and_notify(t, t.tp2, "TP2 Hit")
                continue
            if l <= (t.sl or 0):
                await _close_trade_and_notify(t, t.sl, "Stop Loss Hit")
                continue
        else:  # SHORT
            if l <= (t.tp2 or 0):
                await _close_trade_and_notify(t, t.tp2, "TP2 Hit")
                continue
            if h >= (t.sl or float('inf')):
                await _close_trade_and_notify(t, t.sl, "Stop Loss Hit")
                continue

    # ---------- Battleground heartbeat ----------
    await route_battleground_report(c, levels)

@tasks.loop(minutes=15)
async def market_scorecard_loop():
    await mdp.start()
    df = await mdp.fetch_ohlc(120)
    df = compute_indicators(df)
    levels = calc_camarilla(df)
    if levels:
        await route_market_scorecard(df, levels)

def should_throttle(key: str, minutes: int) -> bool:
    now = datetime.now(timezone.utc)
    last = _signal_last_ts.get(key)
    if last and (now - last) < timedelta(minutes=minutes):
        return True
    _signal_last_ts[key] = now
    return False

def status_embed() -> discord.Embed:
    e = discord.Embed(title="🛡️ Control Tower Status", color=discord.Color.blurple(),
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Pair", value=cfg.pair if cfg else "N/A", inline=True)
    e.add_field(name="Interval", value=f"{cfg.interval_min}m" if cfg else "N/A", inline=True)
    e.add_field(name="Active Trades", value=str(len(trade_manager.active)) if trade_manager else "0", inline=True)
    e.add_field(name="Sheets", value="ON" if (cfg and cfg.sheets_url) else "OFF", inline=True)
    return e

def create_bot():
    bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)

    # ---- helpers (local to create_bot) ----
    def _build_test_trade(
        direction: str = "LONG",
        entry: float = 2700.0,
        sl: float = 2650.0,
        tp1: float = 2740.0,
        tp2: float = 2790.0,
        score: int = 5,
        tier: str = "S",
        trade_type: str | None = None,
        level_name: str | None = None,
        level_price: float | None = None,
    ) -> TradeData:
        direction_enum = TradeDirection.LONG if str(direction).upper().startswith("L") else TradeDirection.SHORT
        trade_type = trade_type or ("H5_Breakout" if direction_enum == TradeDirection.LONG else "L5_Breakout")
        level_name = level_name or ("H5" if direction_enum == TradeDirection.LONG else "L5")
        level_price = float(level_price) if level_price is not None else float(entry)

        return TradeData(
            id=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
            asset=cfg.pair,
            direction=direction_enum,
            entry_price=float(entry),
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2),
            trade_type=trade_type,
            level_name=level_name,
            level_price=level_price,
            rating=tier,
            score=int(score),
            knight=knight_for(trade_type),
            enhanced_data={
                "enhanced_score": int(score),
                "volume_ratio": 1.5,
                "rsi_level": 62.0 if direction_enum == TradeDirection.LONG else 38.0,
                "trend_bias": "Bullish" if direction_enum == TradeDirection.LONG else "Bearish",
            },
        )

    async def _send_exit_inline(t: TradeData, exit_price: float, reason: str):
        # Compute PnL %
        try:
            if t.direction == TradeDirection.LONG:
                pnl_pct = (exit_price / t.entry_price - 1.0) * 100.0
            else:
                pnl_pct = (1.0 - exit_price / t.entry_price) * 100.0
        except Exception:
            pnl_pct = 0.0

        # Close & persist
        t.status = TradeStatus.CLOSED
        t.closed_at = datetime.now(timezone.utc)
        db.save_trade(t)

        # Sheets update (if method exists)
        try:
            await trade_manager.start()
            time_iso = t.closed_at.isoformat()
            if hasattr(sheets, "send_trade_exit"):
                await sheets.send_trade_exit(trade_manager.session, t.id, reason, exit_price, time_iso, round(pnl_pct, 2))
        except Exception as e:
            log.warning(f"Sheets exit update failed: {e}")

        # Send exit alert (use route_exit_alert if present, else inline)
        if "route_exit_alert" in globals():
            await route_exit_alert(t, exit_price, reason, round(pnl_pct, 2))
        else:
            color = discord.Color.light_grey() if "TP2" in reason else discord.Color.dark_red()
            title_icon = "🏁" if "TP2" in reason else "⛔"
            e = discord.Embed(
                title=f"{title_icon} Exit — {t.asset} {t.direction.name} ({reason})",
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            e.add_field(name="Entry", value=f"{t.entry_price:.2f}", inline=True)
            e.add_field(name="Exit", value=f"{exit_price:.2f}", inline=True)
            e.add_field(name="PnL %", value=f"{pnl_pct:.2f}", inline=True)
            e.add_field(name="Level", value=f"{t.level_name or '-'}", inline=True)
            e.set_footer(text=f"Trade ID: {t.id}")
            await send_to_channel(cfg.battle_signals_id, e)

        # Remove from active list
        trade_manager.active.pop(t.id, None)

    # ---- existing commands ----
    @bot.command(name="status")
    async def _status(ctx):
        await ctx.send(embed=status_embed())

    @bot.command(name="config")
    async def _config(ctx):
        e = discord.Embed(title="⚙️ Bot Configuration", color=discord.Color.blue())
        e.add_field(name="Pair", value=cfg.pair, inline=True)
        e.add_field(name="Interval", value=f"{cfg.interval_min}m", inline=True)
        e.add_field(name="Trail Mode", value=cfg.trail_mode.value, inline=True)
        e.add_field(name="Sheets", value="✅" if cfg.sheets_url else "❌", inline=True)
        e.add_field(name="Signal Cooldown", value=f"{cfg.signal_cooldown_min}m", inline=True)
        e.add_field(name="Channels", value=str({
            "scribes": cfg.scribes_keep_id,
            "battle": cfg.battle_signals_id,
            "eagle": cfg.eagle_signal_id,
            "watch": cfg.knights_watch_id,
            "battleground": cfg.eth_battleground_id,
            "setup": cfg.setup_alerts_id
        }), inline=False)
        await ctx.send(embed=e)

    @bot.command(name="rehydrate")
    async def _rehydrate(ctx):
        before = len(trade_manager.active)
        await trade_manager.rehydrate()
        after = len(trade_manager.active)
        e = discord.Embed(title="🔄 Rehydration", color=discord.Color.blue())
        e.add_field(name="Before", value=str(before), inline=True)
        e.add_field(name="After", value=str(after), inline=True)
        await ctx.send(embed=e)

    # ---- new commands ----
    @bot.command(name="test_entry")
    async def _test_entry(
        ctx,
        direction: str = "LONG",
        entry: float = 2700.0,
        sl: float = 2650.0,
        tp1: float = 2740.0,
        tp2: float = 2790.0,
        score: int = 5,
        tier: str = "S",
        show_payload: str = "no",
    ):
        """Send a test trade entry, route an alert, and write to Sheets. Add 'yes' to preview the mapped JSON."""
        t = _build_test_trade(direction, entry, sl, tp1, tp2, score, tier)
        await trade_manager.open_trade(t)     # persists & writes to Sheets
        await route_battle_signal(t)          # alert

        # Optional 100x test gate
        try:
            if (t.score or 0) >= 5 and not should_throttle("__100x__", cfg.hundred_x_cooldown_min):
                await route_100x_alert(t)
        except Exception:
            pass

        # Payload preview (after mapping)
        if show_payload.lower() in ("yes", "y", "true", "1"):
            ed = t.enhanced_data or {}
            base = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trade_id": t.id, "asset": t.asset,
                "direction": t.direction.name.title(),
                "status": "OPEN",
                "level_name": t.level_name or "",
                "trade_type": t.trade_type or "",
                "score": t.score or 0,
                "enhanced_score": ed.get("enhanced_score", t.score or 0),
                "confidence": t.rating or "",
                "knight": t.knight or "",
                "rsi_level": ed.get("rsi_level"),
                "volume_ratio": ed.get("volume_ratio"),
                "market_status": ("OVERBOUGHT" if (ed.get("rsi_level", 50) >= 75) else "OVERSOLD" if (ed.get("rsi_level", 50) <= 25) else "NORMAL"),
                "vwap_position": ed.get("vwap_position", "-"),
                "macd_status": ed.get("macd_status", "-"),
                "trend_bias": ed.get("trend_bias") or ed.get("market_bias") or "-",
                "risk_pct": round(abs((t.entry_price - t.sl) / t.entry_price) * 100.0, 2) if t.entry_price else "",
                "rr_ratio": round(
                    (abs(((t.tp1 or t.tp2) - t.entry_price) / t.entry_price) * 100.0) /
                    (abs((t.entry_price - t.sl) / t.entry_price) * 100.0)
                , 2) if (t.entry_price and t.sl and (t.tp1 or t.tp2)) else "",
                "setup_age_minutes": ed.get("setup_age_minutes", ""),
                "breakout_structure": ed.get("breakout_structure", ""),
                "confluence_count": ed.get("confluence_count", ""),
                "candle_body_strength": ed.get("candle_body_strength", "-"),
                "market_session": ed.get("market_session", ""),
                "distance_from_level_pct": ed.get("distance_from_level_pct", ""),
                "recent_news_events": ed.get("recent_news_events", "No"),
                "volatility_state": ed.get("volatility_state", ""),
                "trend_strength": ed.get("trend_strength", f"Moderate {ed.get('trend_bias', '-')}".strip()),
                "enhanced_data": ed,
            }
            price_block = {
                "entry_price": t.entry_price, "entry": t.entry_price,
                "stop_loss": t.sl, "stop": t.sl,
                "tp1": t.tp1, "target1": t.tp1,
                "tp2": t.tp2, "target2": t.tp2,
            }
            try:
                mapped = sheets._apply_field_map({**base, **price_block})  # type: ignore[attr-defined]
            except Exception:
                mapped = {**base, **price_block}

            import json as _json
            j = _json.dumps(mapped, indent=2, default=str)
            if len(j) > 1900:
                j = j[:1900] + "... (truncated)"
            await ctx.send(f"```json\n{j}\n```")

        await ctx.send(f"✅ Test entry sent. Trade ID: `{t.id}`")

    @bot.command(name="test_exit")
    async def _test_exit(ctx, reason: str = "TP2"):
        """Close the most recent active trade as TP2 or SL and send exit alert + Sheets update."""
        t = None
        if trade_manager.active:
            # pick the most recent OPEN trade
            open_trades = [x for x in trade_manager.active.values() if x.status == TradeStatus.OPEN]
            t = sorted(open_trades, key=lambda x: x.opened_at)[-1] if open_trades else None
        if t is None:
            # If none active, create one so you can test exit path
            t = _build_test_trade()
            await trade_manager.open_trade(t)
            await route_battle_signal(t)

        label = "TP2 Hit" if str(reason).upper().startswith("TP") else "Stop Loss Hit"
        price = t.tp2 if "TP" in label else t.sl

        # Prefer module helper if available; otherwise do inline close
        if "_close_trade_and_notify" in globals():
            await _close_trade_and_notify(t, price, label)
        else:
            await _send_exit_inline(t, price, label)

        await ctx.send(f"🏁 Test exit sent for `{t.id}` ({label}).")

    @bot.command(name="scorecard_now")
    async def _scorecard_now(ctx):
        """Force a scorecard to post immediately."""
        await mdp.start()
        df = await mdp.fetch_ohlc(120)
        df = compute_indicators(df)
        levels = calc_camarilla(df)
        if levels:
            await route_market_scorecard(df, levels)
            await ctx.send("📜 Scorecard sent.")
        else:
            await ctx.send("No levels computed; scorecard skipped.")

    @bot.event
    async def on_ready():
        log.info(f"Logged in as {bot.user}")
        try:
            await trade_manager.rehydrate()
            if not scan_loop.is_running(): scan_loop.start()
            if not market_scorecard_loop.is_running(): market_scorecard_loop.start()
            # Pre-resolve channels
            for cid in [cfg.scribes_keep_id, cfg.battle_signals_id, cfg.eagle_signal_id,
                        cfg.knights_watch_id, cfg.eth_battleground_id, cfg.setup_alerts_id]:
                if cid:
                    try:
                        await resolve_channel(cid)
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"on_ready error: {e}")

    return bot

# -------- Main --------
def main():
    global cfg, bot, db, sheets, trade_manager, mdp
    cfg = BotConfig.from_env()
    db = DatabaseManager("trades.db")
    sheets = GoogleSheetsIntegration(cfg.sheets_url, cfg.sheets_token)
    trade_manager = TradeManager(cfg, db, sheets)
    mdp = MarketDataProvider(cfg.pair, cfg.interval_min)

    # Start Flask alongside Discord
    threading.Thread(target=run_flask, daemon=True).start()

    # Launch bot
    globals()["bot"] = create_bot()
    bot.run(cfg.token)

if __name__ == "__main__":
    main()
