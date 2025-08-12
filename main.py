# ============================================
# Control Tower - Merged v11 (H5/L5 + Setup Intel + Rehydrate/BE/Trail)
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

# Optional TA imports (use 'ta' package, not TA-Lib)
try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD
    from ta.volatility import AverageTrueRange
except Exception:
    # Safe fallbacks if 'ta' not installed at build time; scanning still runs with minimal checks
    RSIIndicator = None
    EMAIndicator = None
    MACD = None
    AverageTrueRange = None

# -------- Logging --------
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
log = logging.getLogger('control_tower')

# -------- Flask (health) --------
app = Flask(__name__)

@app.route('/')
def health_root():
    return jsonify(ok=True, service="Control Tower Merged v11")

# Run Flask in a thread to avoid blocking Discord
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# -------- Config --------
class TrailMode(str, Enum):
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
            raise RuntimeError("Discord TOKEN env var is required")

        sheets_url = os.getenv("GOOGLE_SHEETS_WEBHOOK", "").strip() or None
        sheets_token = os.getenv("SHEETS_TOKEN", "").strip() or None
        partial_fraction = float(os.getenv("PARTIAL_FRACTION", "0.5"))
        be_after_tp1 = os.getenv("BE_AFTER_TP1", "true").lower() in ("1", "true", "yes", "y")
        be_offset_pct = float(os.getenv("BE_OFFSET_PCT", "0.0"))
        trail_mode = os.getenv("TRAIL_MODE", "none").upper()
        if trail_mode not in ("NONE", "ATR", "CHAND"):
            trail_mode = "NONE"
        trail_mode = TrailMode(trail_mode)
        trail_atr_period = int(os.getenv("TRAIL_ATR_PERIOD", "14"))
        trail_atr_mult = float(os.getenv("TRAIL_ATR_MULT", "3.0"))
        chand_lookback = int(os.getenv("CHAN_LOOKBACK", "22"))
        pair = os.getenv("PAIR", "ETHUSD").upper()
        interval_min = int(os.getenv("INTERVAL_MIN", "5"))

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

    def save_trade(self, t: "TradeData"):
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

    def close_trade(self, trade_id: str, closed_at: datetime):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("UPDATE trades SET status=?, closed_at=? WHERE id=?", ("CLOSED", closed_at.isoformat(), trade_id))
            conn.commit()

    def add_partial(self, trade_id: str, fraction: float, price: float, time: datetime):
        with sqlite3.connect(self.path) as conn:
            c = conn.cursor()
            c.execute("""
            INSERT INTO partial_exits(trade_id, fraction, price, time) VALUES (?,?,?,?)
            """, (trade_id, fraction, price, time.isoformat()))
            conn.commit()

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

# -------- Sheets Integration --------
class GoogleSheetsIntegration:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token

    async def _post(self, session: aiohttp.ClientSession, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.url or not self.token:
            return {"status": "skipped"}
        headers = {"x-app-secret": self.token, "content-type": "application/json"}
        for attempt in range(1, 4):
            try:
                async with session.post(self.url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    txt = await resp.text()
                    return {"status": resp.status, "body": txt}
            except Exception as e:
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
            "level_name": t.level_name,
            "entry_price": t.entry_price,
            "stop_loss": t.sl,
            "target1": t.tp1,
            "target2": t.tp2,
            "score": t.score,
            "knight": t.knight,
            "status": "OPEN",
            "trade_type": t.trade_type or "Breakout",
            "confidence": t.rating,                 # A/B/C… (optional)
            "enhanced_data": t.enhanced_data or {}, # optional blob
        }
        return await self._post(session, payload)

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
        return await self._post(session, payload)

    async def rehydrate_open_trades(self, session) -> List["TradeData"]:
        if not self.url or not self.token:
            return []
        params = {"action": "open", "key": self.token}
        try:
            async with session.get(self.url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                txt = await resp.text()
                data = json.loads(txt) if txt else {}
                rows = data.get("rows", [])
        except Exception as e:
            log.warning(f"Rehydrate GET failed: {e}")
            return []

        out: List[TradeData] = []
        for r in rows:
            try:
                # Direction can be "Long"/"Short" — normalize safely
                dir_raw = str(r.get("direction", "Long")).strip().upper()
                direction = TradeDirection.LONG if dir_raw.startswith("L") else TradeDirection.SHORT
                out.append(TradeData(
                    id=str(r.get("trade_id") or r.get("id")),
                    asset=str(r.get("asset") or "ETH"),
                    direction=direction,
                    entry_price=float(r.get("entry_price")),
                    sl=float(r.get("stop_loss")),
                    tp1=float(r.get("tp1") or r.get("Take Profit 1") or r.get("target1")),
                    tp2=float(r.get("tp2") or r.get("Take Profit 2") or r.get("target2")),
                    score=int(r.get("score") or 0),
                    rating=str(r.get("confidence") or ""),
                    knight=str(r.get("knight") or ""),
                    level_name=str(r.get("level_name") or ""),
                ))
            except Exception as e:
                log.warning(f"Bad row in rehydrate: {e}")
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
        await self.start()
        rows = await self.sheets.rehydrate_open_trades(self.session)
        for t in rows:
            t.trail_mode = self.cfg.trail_mode
            self.active[t.id] = t
            self.db.save_trade(t)
        log.info(f"Rehydrated {len(rows)} trades from Google Sheets")

    async def open_trade(self, t: TradeData):
        await self.start()
        t.trail_mode = self.cfg.trail_mode
        self.active[t.id] = t
        self.db.save_trade(t)
        await self.sheets.send_trade_entry(self.session, t)

    # ----------------- helpers -----------------
    def _apply_breakeven(self, t: TradeData) -> None:
        """Move stop to BE with optional fee/offset cushion."""
        t.be_active = True
        offset = float(getattr(self.cfg, "be_offset_pct", 0.0)) / 100.0
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

    def _calc_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        if AverageTrueRange is None:
            # naive TR average fallback
            h, l, c = highs, lows, closes
            prev_close = c[-period-1:-1]
            tr = np.maximum(h[-period:] - l[-period:],
                            np.maximum(np.abs(h[-period:] - prev_close),
                                       np.abs(l[-period:] - prev_close)))
            return float(np.mean(tr))
        df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
        atr = AverageTrueRange(high=df["high"], low=df["low"],
                               close=df["close"], window=period).average_true_range().iloc[-1]
        return float(atr)

    def _update_trail(self, t: TradeData, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> None:
        if self.cfg.trail_mode == TrailMode.NONE:
            return

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

    # ----------------- exit engine -----------------
    async def evaluate_exit(self, t: TradeData, last_price: float, now: datetime) -> Optional[Tuple[str, float, float]]:
        """
        Check TP1/TP2/SL. Returns (reason, exit_price, pnl_pct) when the trade is finalized,
        else returns None to keep monitoring.
        """

        # 1) TP1 partial (do once), move SL to BE if configured
        if t.status == TradeStatus.OPEN and not getattr(t, "tp1_done", False):
            if (t.direction == TradeDirection.LONG and last_price >= t.tp1) or \
               (t.direction == TradeDirection.SHORT and last_price <= t.tp1):

                # Persist partial
                frac = float(getattr(self.cfg, "partial_fraction", 0.5))
                t.tp1_done = True
                t.partial_fraction = frac
                self.db.add_partial(t.id, frac, last_price, now)

                # Optional: POST partial to Sheets only if you explicitly enable it
                if getattr(self.cfg, "post_partial_to_sheets", False) and self.session:
                    try:
                        await self.sheets.send_partial_exit(self.session, t.id, frac, last_price, now.isoformat())
                    except Exception as e:
                        log.error("Sheets partial POST failed for %s: %s", t.id, e)

                # Move to BE if desired
                if getattr(self.cfg, "be_after_tp1", True):
                    self._apply_breakeven(t)

                self.db.save_trade(t)
                # Keep the trade OPEN (no finalize yet)
                return None

        # 2) Finalization on TP2 or SL (SL may be BE if we moved it)
        hit_tp2 = (t.direction == TradeDirection.LONG and last_price >= t.tp2) or \
                  (t.direction == TradeDirection.SHORT and last_price <= t.tp2)
        hit_sl  = (t.direction == TradeDirection.LONG and last_price <= t.sl)  or \
                  (t.direction == TradeDirection.SHORT and last_price >= t.sl)

        if hit_tp2 or hit_sl:
            # Compute blended PnL if TP1 happened
            frac = float(getattr(t, "partial_fraction", getattr(self.cfg, "partial_fraction", 0.5))) \
                   if getattr(t, "tp1_done", False) else 0.0
            pnl  = self._blended_pnl(t.entry_price, t.tp1, last_price, t.direction, frac)

            t.status = TradeStatus.CLOSED
            t.closed_at = now
            self.db.save_trade(t)
            self.db.close_trade(t.id, now)

            reason = "TP2 HIT" if hit_tp2 else ("SL (BE)" if getattr(t, "be_active", False) else "SL")
            return (reason, last_price, pnl)

        # keep open
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
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def fetch_ohlc(self, n: int = 500) -> pd.DataFrame:
        await self.start()
        url = "https://api.kraken.com/0/public/OHLC"
        params = {"pair": self.pair, "interval": self.interval_min}
        async with self.session.get(url, params=params) as resp:
            data = await resp.json()
        key = list(data["result"].keys())[0]
        rows = data["result"][key][-n:]
        df = pd.DataFrame(rows, columns=["time","open","high","low","close","vwap","volume","count"])
        df = df.astype({"time": int, "open": float, "high": float, "low": float, "close": float, "volume": float})
        df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

def calc_camarilla(df: pd.DataFrame) -> Dict[str, float]:
    # Use previous day HLC for intraday levels; if not available, use last 288 bars approx
    if len(df) < 2:
        raise ValueError("Not enough bars")
    # Pivot source: prev day
    prev = df.iloc[-2]  # rough; for better accuracy, group by day
    H = float(prev["high"]); L = float(prev["low"]); C = float(prev["close"])
    r = H - L
    # Extension method (commonly used variant)
    L3 = C - (r * 1.1/12); H3 = C + (r * 1.1/12)
    L4 = C - (r * 1.1/6);  H4 = C + (r * 1.1/6)
    L5 = C - (r * 1.1/2);  H5 = C + (r * 1.1/2)
    # Optional L6/H6 for continuation context
    L6 = C - (r * 1.1*0.67); H6 = C + (r * 1.1*0.67)
    return {"L3":L3,"L4":L4,"L5":L5,"H3":H3,"H4":H4,"H5":H5,"L6":L6,"H6":H6}

def confirm_breakout(c, o, h, l, vol, avg_vol, level: float, direction: TradeDirection) -> Tuple[bool, Dict[str, Any]]:
    # Body > 50% of range + close beyond level + volume > 1.2x avg
    rng = max(h - l, 1e-9)
    body_ratio = abs(c - o) / rng
    close_beyond = (direction == TradeDirection.LONG and c > level) or (direction == TradeDirection.SHORT and c < level)
    vol_ok = vol > (avg_vol * 1.2 if avg_vol > 0 else vol)
    ok = (body_ratio > 0.5) and close_beyond and vol_ok
    meta = {"body_ratio": body_ratio, "vol_ok": vol_ok, "close_beyond": close_beyond}
    return ok, meta

def likely_reversal(c, h, l, level: float, direction: TradeDirection) -> bool:
    # simple wick test: price pierces and closes back inside
    if direction == TradeDirection.LONG:  # reversal up from L5
        return (l < level) and (c > level)
    else:  # reversal down from H5
        return (h > level) and (c < level)

# -------- Discord & Bot --------
INTENTS = discord.Intents.default()
INTENTS.message_content = True

cfg = BotConfig.from_env()
bot = commands.Bot(command_prefix="!", intents=INTENTS, help_command=None)

db = DatabaseManager("trades.db")
sheets = GoogleSheetsIntegration(cfg.sheets_url, cfg.sheets_token)
trade_manager = TradeManager(cfg, db, sheets)
mdp = MarketDataProvider(cfg.pair, cfg.interval_min)

# ------- Embeds -------
def fmt_pct(x: float) -> str:
    return f"{x:.2f}%"

def status_embed() -> discord.Embed:
    e = discord.Embed(title="🛡️ Control Tower Status", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Pair", value=cfg.pair, inline=True)
    e.add_field(name="Interval", value=f"{cfg.interval_min}m", inline=True)
    e.add_field(name="Active Trades", value=str(len(trade_manager.active)), inline=True)
    e.add_field(name="Sheets", value="ON" if (cfg.sheets_url and cfg.sheets_token) else "OFF", inline=True)
    e.add_field(name="Trail Mode", value=cfg.trail_mode.value, inline=True)
    e.add_field(name="BE after TP1", value="ON" if cfg.be_after_tp1 else "OFF", inline=True)
    return e

def config_embed() -> discord.Embed:
    e = discord.Embed(title="⚙️ Trading Config", color=discord.Color.dark_teal(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Partial Fraction", value=str(cfg.partial_fraction), inline=True)
    e.add_field(name="BE Offset %", value=str(cfg.be_offset_pct), inline=True)
    e.add_field(name="Trail", value=cfg.trail_mode.value, inline=True)
    e.add_field(name="ATR Period/Mult", value=f"{cfg.trail_atr_period}/{cfg.trail_atr_mult}", inline=True)
    e.add_field(name="Chandelier Lookback", value=str(cfg.chand_lookback), inline=True)
    e.add_field(name="Sheets URL set", value="Yes" if cfg.sheets_url else "No", inline=True)
    return e

async def send_battle_signal(channel: discord.TextChannel, t: TradeData):
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
    if t.rating: e.add_field(name="📊 Rating", value=t.rating, inline=True)
    if t.score: e.add_field(name="🧮 Score", value=str(t.score), inline=True)
    e.set_footer(text="TP1 sets BE (silent). Only TP2/SL announced.")
    await channel.send(embed=e)

async def send_setup_alert(channel: discord.TextChannel, title: str, fields: Dict[str, Any], watch_next: List[str]):
    e = discord.Embed(title=title, color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    for k, v in fields.items():
        e.add_field(name=k, value=str(v), inline=True)
    if watch_next:
        e.add_field(name="👀 Watch Next", value="\n".join(f"- {x}" for x in watch_next), inline=False)
    await channel.send(embed=e)

async def send_exit_alert(channel: discord.TextChannel, t: TradeData, reason: str, price: float, pnl_pct: float):
    color = discord.Color.green() if reason == "TP2" else discord.Color.red()
    e = discord.Embed(
        title=f"🏁 Exit - {t.asset} {reason}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Price", value=f"{price:.2f}", inline=True)
    e.add_field(name="PnL", value=fmt_pct(pnl_pct), inline=True)
    e.add_field(name="Trade ID", value=t.id, inline=True)
    await channel.send(embed=e)

# ------- Discord Commands -------
@bot.command(name="status")
async def _status(ctx: commands.Context):
    await ctx.send(embed=status_embed())

@bot.command(name="config")
async def _config(ctx: commands.Context):
    await ctx.send(embed=config_embed())

@bot.command(name="export")
async def _export(ctx: commands.Context):
    # Export DB to CSV and upload
    path = "trades_export.csv"
    with sqlite3.connect(db.path) as conn:
        df = pd.read_sql_query("SELECT * FROM trades", conn)
    df.to_csv(path, index=False)
    await ctx.send(file=discord.File(path))

@bot.command(name="sheets_test")
async def _sheets_test(ctx: commands.Context):
    now = datetime.now(timezone.utc)
    t = TradeData(
        id=now.strftime("TEST%H%M%S"),
        asset=cfg.pair,
        direction=TradeDirection.LONG,
        entry_price=2500.0, sl=2450.0, tp1=2525.0, tp2=2550.0,
        rating="A", score=5, level_name="H4", level_price=2500.0, trade_type="Breakout"
    )
    await trade_manager.open_trade(t)
    await ctx.send("Posted test entry to Google Sheets (if configured).")

# ------- Scanner Task -------
async def compute_signals(df: pd.DataFrame) -> Dict[str, Any]:
    levels = calc_camarilla(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    c, o, h, l, v = float(last["close"]), float(last["open"]), float(last["high"]), float(last["low"]), float(last["volume"])
    avg_vol = float(df["volume"].tail(30).mean()) if len(df) >= 30 else v

    # Breakout checks around H5/L5
    sig = {"confirm": None, "setup": None, "levels": levels}
    # Long breakout over H5
    ok_long, meta_long = confirm_breakout(c, o, h, l, v, avg_vol, levels["H5"], TradeDirection.LONG)
    # Short breakout under L5
    ok_short, meta_short = confirm_breakout(c, o, h, l, v, avg_vol, levels["L5"], TradeDirection.SHORT)

    if ok_long:
        sig["confirm"] = {"direction": "Long", "level":"H5", "level_price": levels["H5"], "meta": meta_long, "type":"Breakout"}
    elif ok_short:
        sig["confirm"] = {"direction": "Short", "level":"L5", "level_price": levels["L5"], "meta": meta_short, "type":"Breakout"}
    else:
        # Setup intelligence (missing criteria)
        miss = []
        if max(h-l,1e-9) == 1e-9 or abs(c-o)/(max(h-l,1e-9)) <= 0.5: miss.append("💡 Body < 50%")
        if not (v > avg_vol*1.2): miss.append("📉 Volume < 1.2× avg")
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

@tasks.loop(seconds=45)
async def scan_loop():
    await mdp.start()
    await trade_manager.start()
    try:
        df = await mdp.fetch_ohlc(500)
    except Exception as e:
        log.warning(f"OHLC fetch failed: {e}")
        return

    sig = await compute_signals(df)
    ch: Optional[discord.TextChannel] = None
    # Pick the first text channel in the guilds the bot can speak in
    for g in bot.guilds:
        for c in g.text_channels:
            if c.permissions_for(g.me).send_messages:
                ch = c
                break
        if ch: break
    if ch is None:
        return

    # Confirmed → open trade & send Battle Signal
    if sig["confirm"]:
        info = sig["confirm"]
        direction = TradeDirection.LONG if info["direction"] == "Long" else TradeDirection.SHORT
        price = float(df.iloc[-1]["close"])
        # Entry/SL/TP scaffolding (example: 1% SL, TP1 1.5%, TP2 3%)
        risk = 0.01
        tp1p = 0.015; tp2p = 0.03
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
            rating="A", score=6, level_name=info["level"], level_price=info["level_price"], trade_type=info["type"],
            enhanced_data=info.get("meta",{})
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
    highs = df["high"].to_numpy(); lows = df["low"].to_numpy(); closes = df["close"].to_numpy()
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

@scan_loop.before_loop
async def before_scan():
    await bot.wait_until_ready()
    await trade_manager.start()
    await mdp.start()

# ------- Bot Startup -------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} ({bot.user.id})")
    # Rehydrate before starting loops
    await trade_manager.rehydrate()
    if not scan_loop.is_running():
        scan_loop.start()

# ------- Main Entrypoint -------
def main():
    # Start Flask in a side thread
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(cfg.token)

if __name__ == "__main__":
    main()
