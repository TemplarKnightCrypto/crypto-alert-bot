# ============================================
# Production_ControlTower_v12.2.3
# ============================================

import os
import sys
import re
import time
import json
import math
import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Union
from collections import defaultdict, deque

import aiohttp
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks

# ===== Enhanced Logging Configuration =====
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("production_control_tower.log", mode="a", encoding="utf-8"),
        ],
    )
    return logging.getLogger("ProductionControlTower")

log = setup_logging()

# ===== Technical Analysis with Fallbacks =====
try:
    from ta.momentum import RSIIndicator
    from ta.trend import EMAIndicator, MACD
    from ta.volatility import AverageTrueRange
    TA_AVAILABLE = True
    log.info("TA library loaded successfully")
except Exception:
    RSIIndicator = None
    EMAIndicator = None
    MACD = None
    AverageTrueRange = None
    TA_AVAILABLE = False
    log.warning("TA library not available - using fallback indicators")

# ===== Configuration Management =====
@dataclass
class RiskConfig:
    max_position_pct: float = 2.0
    max_daily_loss_pct: float = 5.0
    max_open_trades: int = 3
    min_rr_ratio: float = 1.5
    account_balance: float = 10_000.0

@dataclass
class TradingConfig:
    # Confirmation profile knobs
    confirmation_mode: str = "balanced"  # aggressive|balanced|strict
    body_ratio_threshold: float = 0.55
    volume_multiplier: float = 1.25
    signal_confidence_min: int = 4

    # Throttling
    cooldown_seconds: int = 450                    # short cooldown (v12)
    global_cooldown_seconds: int = 1800            # optional CT-style longer cooldown
    use_global_cooldown: bool = False              # toggle
    token_bucket_max: int = 5
    token_bucket_refill_per_min: float = 1.0       # tokens/minute

    # Lifecycle
    partial_exit_fraction: float = 0.5
    be_after_tp1: bool = True
    be_offset_pct: float = 0.1
    trail_mode: str = "none"  # none|atr|chandelier
    trail_atr_mult: float = 2.0
    chandelier_period: int = 22
    chandelier_mult: float = 3.0

    # Camarilla session controls (NEW)
    camarilla_freq: Optional[str] = "4H"           # default 4-hour buckets
    camarilla_tz: str = "America/Chicago"          # default TZ alignment

@dataclass
class ChannelConfig:
    signals: int = int(os.getenv("CHANNEL_SIGNALS", "1399532925279666278"))
    performance: int = int(os.getenv("CHANNEL_PERFORMANCE", "1399532102571135118"))
    alerts: int = int(os.getenv("CHANNEL_ALERTS", "1398690647417819198"))
    logs: int = int(os.getenv("CHANNEL_LOGS", "1399067396488302623"))
    health: int = int(os.getenv("CHANNEL_HEALTH", "1398691425347961016"))

@dataclass
class BotConfig:
    token: str
    pair: str = "ETHUSD"
    interval_minutes: int = 5

    # Feature toggles
    dry_run: bool = False     # evaluate signals, log, but do not save DB or send Sheets
    no_post: bool = False     # do not send Discord posts (still logs/DB)
    no_trade: bool = False    # never open trades (simulate only)

    # Sheets
    sheets_url: Optional[str] = None
    sheets_token: Optional[str] = None
    sheets_rehydrate: bool = True

    risk: RiskConfig = field(default_factory=RiskConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    channels: ChannelConfig = field(default_factory=ChannelConfig)

    @classmethod
    def from_env(cls) -> "BotConfig":
        load_dotenv()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_TOKEN is required")

        risk = RiskConfig(
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "2.0")),
            max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0")),
            max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
            min_rr_ratio=float(os.getenv("MIN_RR_RATIO", "1.5")),
            account_balance=float(os.getenv("ACCOUNT_BALANCE", "10000.0")),
        )

        confirmation_mode = os.getenv("CONFIRMATION_MODE", "balanced").lower()
        tc = TradingConfig(
            confirmation_mode=confirmation_mode if confirmation_mode in {"aggressive","balanced","strict"} else "balanced",
            body_ratio_threshold=float(os.getenv("BODY_RATIO", "0.55")),
            volume_multiplier=float(os.getenv("VOLUME_MULT", "1.25")),
            signal_confidence_min=int(os.getenv("MIN_CONFIDENCE", "4")),
            cooldown_seconds=int(os.getenv("COOLDOWN_SECONDS", "450")),
            global_cooldown_seconds=int(os.getenv("GLOBAL_COOLDOWN_SECONDS", "1800")),
            use_global_cooldown=os.getenv("USE_GLOBAL_COOLDOWN", "false").lower() == "true",
            token_bucket_max=int(os.getenv("TOKEN_BUCKET_MAX", "5")),
            token_bucket_refill_per_min=float(os.getenv("TOKEN_BUCKET_REFILL_PER_MIN", "1.0")),
            partial_exit_fraction=float(os.getenv("PARTIAL_FRACTION", "0.5")),
            be_after_tp1=os.getenv("BE_AFTER_TP1", "true").lower() == "true",
            be_offset_pct=float(os.getenv("BE_OFFSET_PCT", "0.1")),
            trail_mode=os.getenv("TRAIL_MODE", "none").lower(),
            trail_atr_mult=float(os.getenv("TRAIL_ATR_MULT", "2.0")),
            chandelier_period=int(os.getenv("CHAND_PERIOD", "22")),
            chandelier_mult=float(os.getenv("CHAND_MULT", "3.0")),
            camarilla_freq=os.getenv("CAMARILLA_FREQ", "4H"),
            camarilla_tz=os.getenv("CAMARILLA_TZ", "America/Chicago"),
        )

        # Profile presets
        if tc.confirmation_mode == "aggressive":
            tc.body_ratio_threshold = float(os.getenv("BODY_RATIO", "0.45"))
            tc.volume_multiplier = float(os.getenv("VOLUME_MULT", "1.1"))
            tc.signal_confidence_min = int(os.getenv("MIN_CONFIDENCE", "3"))
        elif tc.confirmation_mode == "strict":
            tc.body_ratio_threshold = float(os.getenv("BODY_RATIO", "0.6"))
            tc.volume_multiplier = float(os.getenv("VOLUME_MULT", "1.4"))
            tc.signal_confidence_min = int(os.getenv("MIN_CONFIDENCE", "5"))

        return cls(
            token=token,
            pair=os.getenv("TRADING_PAIR", "ETHUSD").upper(),
            interval_minutes=int(os.getenv("INTERVAL_MINUTES", "5")),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            no_post=os.getenv("NO_POST", "false").lower() == "true",
            no_trade=os.getenv("NO_TRADE", "false").lower() == "true",
            sheets_url=os.getenv("GOOGLE_SHEETS_URL") or os.getenv("SHEETS_WEBHOOK"),
            sheets_token=os.getenv("SHEETS_TOKEN"),
            sheets_rehydrate=os.getenv("SHEETS_REHYDRATE", "true").lower() == "true",
            risk=risk,
            trading=tc,
            channels=ChannelConfig(),
        )

# ===== Market Data Provider =====
class MarketDataProvider:
    PAIR_MAPPING = {
        "ETHUSD": "ETHUSD",
        "BTCUSD": "XBTUSD",
        "SOLUSD": "SOLUSD",
    }

    def __init__(self, pair: str, interval_minutes: int):

# === Alert flow knobs ===
self.token_bucket_max = int(os.getenv("TOKEN_BUCKET_MAX", str(getattr(self, "token_bucket_max", 0) or 0)))
self.token_bucket_refill_per_min = int(os.getenv("TOKEN_BUCKET_REFILL_PER_MIN", str(getattr(self, "token_bucket_refill_per_min", 0) or 0)))
self.cooldown_seconds = int(os.getenv("COOLDOWN_SECONDS", str(getattr(self, "cooldown_seconds", 60) or 60)))
self.use_global_cooldown = bool(int(os.getenv("USE_GLOBAL_COOLDOWN", "0")))
self.global_cooldown_seconds = int(os.getenv("GLOBAL_COOLDOWN_SECONDS", str(getattr(self, "global_cooldown_seconds", 0) or 0)))
        self.pair = self.PAIR_MAPPING.get(pair.upper(), pair.upper())
        self.interval_minutes = interval_minutes
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.cache_ttl = 30  # seconds

    async def start(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _validate_ohlc_data(self, df: pd.DataFrame) -> bool:
        try:
            for c in ["open","high","low","close","volume"]:
                if c not in df.columns:
                    log.error("Missing OHLC column: %s", c)
                    return False
            if (df["high"] < df["low"]).any():
                log.error("Invalid OHLC: high < low")
                return False
            # Fill NaNs
            if df.isnull().any().any():
                df.fillna(method="ffill", inplace=True)
                df.fillna(method="bfill", inplace=True)
            return True
        except Exception as e:
            log.error("OHLC validation error: %s", e)
            return False

    async def fetch_ohlc(self, limit: int = 500) -> Optional[pd.DataFrame]:
        try:
            cache_key = f"{self.pair}_{self.interval_minutes}_{limit}"
            now = time.time()
            cached = self.cache.get(cache_key)
            if cached and now - cached["ts"] < self.cache_ttl:
                return cached["df"].copy()

            await self.start()
            url = "https://api.kraken.com/0/public/OHLC"
            params = {"pair": self.pair, "interval": self.interval_minutes}
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.error("Kraken HTTP %s", resp.status)
                    return None
                data = await resp.json()

            if data.get("error"):
                log.error("Kraken error: %s", data["error"])
                return None

            result = data.get("result", {})
            pair_key = None
            for k, v in result.items():
                if k != "last" and isinstance(v, list):
                    pair_key = k
                    break
            if not pair_key:
                log.error("No pair key in Kraken response")
                return None

            rows = result[pair_key][-limit:]
            if not rows:
                return None

            columns = ["timestamp","open","high","low","close","vwap","volume","count"]
            df = pd.DataFrame(rows, columns=columns)
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
            for c in ["open","high","low","close","vwap","volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")

            if not self._validate_ohlc_data(df):
                return None

            self.cache[cache_key] = {"df": df.copy(), "ts": now}
            return df
        except Exception as e:
            log.error("Fetch OHLC error: %s", e)
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            if df is None or len(df) < 20:
                return df
            df = df.copy()

            # RSI
            if TA_AVAILABLE and RSIIndicator:
                rsi_ind = RSIIndicator(close=df["close"], window=14)
                df["rsi"] = rsi_ind.rsi()
            else:
                delta = df["close"].diff()
                gain = (delta.clip(lower=0)).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, np.nan)
                df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

            # MACD
            if TA_AVAILABLE and MACD:
                m = MACD(close=df["close"])
                df["macd"] = m.macd()
                df["macd_signal"] = m.macd_signal()
                df["macd_histogram"] = m.macd_diff()
            else:
                ema12 = df["close"].ewm(span=12).mean()
                ema26 = df["close"].ewm(span=26).mean()
                macd = ema12 - ema26
                sig = macd.ewm(span=9).mean()
                df["macd"] = macd
                df["macd_signal"] = sig
                df["macd_histogram"] = macd - sig

            # ATR
            if TA_AVAILABLE and AverageTrueRange:
                atr = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"]).average_true_range()
                df["atr"] = atr
            else:
                hl = df["high"] - df["low"]
                hc = (df["high"] - df["close"].shift()).abs()
                lc = (df["low"] - df["close"].shift()).abs()
                tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
                df["atr"] = tr.rolling(14).mean().bfill().fillna(0.01)

            # VWAP
            tp = (df["high"] + df["low"] + df["close"]) / 3
            df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, np.nan).cumsum()
            df["vwap"] = df["vwap"].ffill().fillna(df["close"])

            return df
        except Exception as e:
            log.error("Indicator calc error: %s", e)
            return df

# ===== Camarilla Levels (bucketed w/ TZ) =====
class CamarillaCalculator:
    @staticmethod
    def calculate_levels(df: pd.DataFrame,
                         freq: Optional[str] = "4H",
                         tz: Optional[str] = "America/Chicago") -> Dict[str, float]:
        """
        Compute Camarilla levels from the last *completed* time bucket.
        - freq: pandas offset alias like "4H", "6H", "1D".
        - tz: timezone name for bucket alignment (e.g., "America/Chicago"); if None, use naive UTC.
        Fallback: if resampling fails, use previous candle.
        """
        try:
            if df is None or len(df) < 2:
                return {}

            if freq:
                dfx = df.copy()
                if "timestamp" not in dfx.columns:
                    return {}
                dfx = dfx.set_index("timestamp")
                # Convert to target TZ (timestamps are UTC-aware)
                try:
                    if tz:
                        dfx = dfx.tz_convert(tz)
                except Exception:
                    # If tz_convert fails (e.g., naive index), try tz_localize first
                    try:
                        dfx = dfx.tz_localize("UTC").tz_convert(tz)
                    except Exception:
                        pass

                norm_freq = str(freq).lower() if freq else None
                grouped = dfx.resample(norm_freq, label="right", closed="right").agg({
                    "high": "max",
                    "low": "min",
                    "close": "last"
                }).dropna()

                if len(grouped) < 2:
                    # Not enough completed buckets; fallback to previous bar
                    prev = df.iloc[-2]
                    high = float(prev["high"]); low = float(prev["low"]); close = float(prev["close"])
                else:
                    prev = grouped.iloc[-2]  # last completed bucket
                    high = float(prev["high"]); low = float(prev["low"]); close = float(prev["close"])
            else:
                prev = df.iloc[-2]
                high = float(prev["high"]); low = float(prev["low"]); close = float(prev["close"])

            rng = high - low
            if rng <= 0:
                return {}

            k = 1.1  # same factor as before
            return {
                "H5": close + (rng * k / 2),
                "H4": close + (rng * k / 6),
                "H3": close + (rng * k / 12),
                "L3": close - (rng * k / 12),
                "L4": close - (rng * k / 6),
                "L5": close - (rng * k / 2),
                "PIVOT": close,
            }
        except Exception as e:
            log.error("Camarilla calc error: %s", e)
            return {}

# ===== Signal model =====
@dataclass
class Signal:
    pair: str
    side: str           # "long" or "short"
    level_name: str     # "H5" or "L5"
    level_price: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    timestamp: int
    signal_type: str    # "breakout" or "continuation"
    reason: str
    confidence: int
    risk_reward_ratio: float
    atr: Optional[float] = None
    volume_ratio: Optional[float] = None

class RateLimiter:
    def __init__(self, max_tokens: int, refill_per_min: float):
        self.max_tokens = max_tokens
        self.tokens = float(max_tokens)
        self.refill_per_min = refill_per_min
        self.last = time.time()

    def acquire(self) -> bool:
        now = time.time()
        elapsed_min = (now - self.last) / 60.0
        self.tokens = min(self.max_tokens, self.tokens + elapsed_min * self.refill_per_min)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class TradeSignalEngine:
    def __init__(self, config: TradingConfig):
        self.cfg = config
        self.cooldown_until: Dict[str, float] = defaultdict(lambda: 0.0)
        self.global_cooldown_until: Dict[str, float] = defaultdict(lambda: 0.0)
        self.recent_signatures: Dict[str, float] = {}  # signature -> expiry ts
        self.recent_ttl = int(os.getenv('ALERT_DEDUPE_TTL_SECONDS', '0'))  # seconds for dedupe entries
        self.rate_limiter = RateLimiter(config.token_bucket_max, config.token_bucket_refill_per_min)

    def _signature(self, sig: Signal) -> str:
        bucket = int(sig.timestamp / (self.cfg.cooldown_seconds or 60))  # coarse time bucket
        return f"{sig.pair}|{sig.side}|{sig.level_name}|{sig.signal_type}|{round(sig.entry, 2)}|{bucket}"

    def _volume_ratio(self, df: pd.DataFrame, lookback: int = 20) -> float:
        if df is None or len(df) < lookback + 1:
            return 1.0
        cur = float(df.iloc[-1]["volume"] or 0.0)
        avg = float(df["volume"].iloc[-(lookback + 1):-1].mean() or 0.0)
        return (cur / avg) if avg > 0 else 1.0

    def _atr(self, df: pd.DataFrame, period: int = 14) -> Optional[float]:
        try:
            if "atr" in df.columns and not math.isnan(df["atr"].iloc[-1]):
                return float(df["atr"].iloc[-1])
            if len(df) < period + 1:
                return None
            hl = df["high"] - df["low"]
            hc = (df["high"] - df["close"].shift()).abs()
            lc = (df["low"] - df["close"].shift()).abs()
            tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
            return float(tr.rolling(period).mean().iloc[-1])
        except Exception:
            return None

    def _confirm_breakout(self, row: pd.Series, level: float, side: str, vol_ratio: float) -> bool:
        o = float(row["open"]); h = float(row["high"]); l = float(row["low"]); c = float(row["close"])
        rng = max(h - l, 1e-9)
        body = abs(c - o) / rng
        if body < self.cfg.body_ratio_threshold:
            return False
        if vol_ratio < self.cfg.volume_multiplier:
            return False
        if side == "long" and c <= level:
            return False
        if side == "short" and c >= level:
            return False
        return True

    def _pullback_ok(self, recent: pd.DataFrame, level: float, side: str) -> bool:
        if recent is None or len(recent) < 5:
            return False
        tol = getattr(self.cfg, "wick_tolerance", 0.0015)  # 0.15% wiggle for wicks
        pulls = 0
        for _, r in recent.iloc[:-1].iterrows():
            lo = float(r["low"]); hi = float(r["high"])
            o = float(r["open"]); c = float(r["close"])
            if side == "long":
                if lo < level * (1 - tol):
                    return False
                if c < o:
                    pulls += 1
            else:
                if hi > level * (1 + tol):
                    return False
                if c > o:
                    pulls += 1
        return pulls >= 1

    def _confidence(self, entry: float, sl: float, tp1: float, signal_type: str) -> int:
        base = 3
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        rr = reward / risk if risk > 0 else 0.0
        if rr >= 2.0:
            base += 1
        if rr >= 3.0:
            base += 1
        if signal_type == "breakout":
            base += 1
        if signal_type == "continuation":
            base += 1
        return int(min(base, 6))

    def _create_long(self, entry: float, level: float, level_name: str, atr: Optional[float], signal_type: str, reason: str) -> Signal:
        sl = max(entry * 0.99, level - (0.5 * (atr or 0.0))) if atr else entry * 0.99
        risk = entry - sl
        tp1 = entry + (risk * 1.5)
        tp2 = entry + (risk * 3.0)
        conf = self._confidence(entry, sl, tp1, signal_type)
        rr = (tp1 - entry) / (entry - sl) if entry != sl else 0.0
        return Signal(pair="ETHUSD", side="long", level_name=level_name, level_price=level, entry=entry,
                      sl=sl, tp1=tp1, tp2=tp2, timestamp=int(time.time()), signal_type=signal_type,
                      reason=reason, confidence=conf, risk_reward_ratio=rr, atr=atr)

    def _create_short(self, entry: float, level: float, level_name: str, atr: Optional[float], signal_type: str, reason: str) -> Signal:
        sl = min(entry * 1.01, level + (0.5 * (atr or 0.0))) if atr else entry * 1.01
        risk = sl - entry
        tp1 = entry - (risk * 1.5)
        tp2 = entry - (risk * 3.0)
        conf = self._confidence(entry, sl, tp1, signal_type)
        rr = (entry - tp1) / (sl - entry) if sl != entry else 0.0
        return Signal(pair="ETHUSD", side="short", level_name=level_name, level_price=level, entry=entry,
                      sl=sl, tp1=tp1, tp2=tp2, timestamp=int(time.time()), signal_type=signal_type,
                      reason=reason, confidence=conf, risk_reward_ratio=rr, atr=atr)

    def _quality_ok(self, sig: Signal) -> bool:
        if sig.confidence < self.cfg.signal_confidence_min:
            return False
        if sig.risk_reward_ratio < 1.5:
            return False
        if self.cfg.token_bucket_max > 0 and self.cfg.token_bucket_refill_per_min > 0 and not self.rate_limiter.acquire():
            return False
        now = time.time()
        key = f"{sig.pair}_{sig.side}"
        if now < self.cooldown_until[key]:
            return False
        if self.cfg.use_global_cooldown and now < self.global_cooldown_until[sig.pair]:
            return False
        sign = self._signature(sig)
        if self.recent_ttl > 0:
            for k, ts in list(self.recent_signatures.items()):
            if now > ts:
                self.recent_signatures.pop(k, None)
        if self.recent_ttl > 0 and sign in self.recent_signatures:
            return False
        self.cooldown_until[key] = now + self.cfg.cooldown_seconds
        if self.cfg.use_global_cooldown:
            self.global_cooldown_until[sig.pair] = now + self.cfg.global_cooldown_seconds
        if self.recent_ttl > 0:
            self.recent_signatures[sign] = now + self.recent_ttl
        return True

    def generate(self, df: pd.DataFrame, levels: Dict[str, float], pair: str) -> Optional[Signal]:
        try:
            if df is None or len(df) < 25 or not levels:
                return None
            row = df.iloc[-1]; prev = df.iloc[-2]
            close = float(row["close"]); prev_close = float(prev["close"])
            atr = self._atr(df); vol_ratio = self._volume_ratio(df)

            # Consider all standard levels bi-directionally
            ordered_levels = [lvl for lvl in ["H5","H4","H3","PIVOT","L3","L4","L5"] if lvl in levels]

            # Breakouts both ways
            for lvl in ordered_levels:
                lv = float(levels[lvl])
                if prev_close <= lv < close and self._confirm_breakout(row, lv, "long", vol_ratio):
                    sig = self._create_long(close, lv, lvl, atr, "breakout", f"{lvl} upward breakout")
                    sig.pair = pair
                    if self._quality_ok(sig): return sig
                if prev_close >= lv > close and self._confirm_breakout(row, lv, "short", vol_ratio):
                    sig = self._create_short(close, lv, lvl, atr, "breakout", f"{lvl} downward breakout")
                    sig.pair = pair
                    if self._quality_ok(sig): return sig

            # Continuations both ways (hold above/below with pullback)
            recent = df.tail(6)
            for lvl in ordered_levels:
                lv = float(levels[lvl])
                if close > lv and self._pullback_ok(recent, lv, "long"):
                    sig = self._create_long(close, lv, lvl, atr, "continuation", f"{lvl} continuation after pullback")
                    sig.pair = pair
                    if self._quality_ok(sig): return sig
                if close < lv and self._pullback_ok(recent, lv, "short"):
                    sig = self._create_short(close, lv, lvl, atr, "continuation", f"{lvl} continuation after pullback")
                    sig.pair = pair
                    if self._quality_ok(sig): return sig

            return None
        except Exception as e:
            log.error("Signal generation error: %s", e)
            return None
class DatabaseManager:
    def __init__(self, path: str = "pct_trades.db"):
        self.path = path
        self._ensure()

    def _ensure(self):
        with sqlite3.connect(self.path) as con:
            con.execute("PRAGMA journal_mode=WAL;")
            con.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit_1 REAL NOT NULL,
                    take_profit_2 REAL NOT NULL,
                    position_size REAL DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    confidence INTEGER DEFAULT 3,
                    signal_type TEXT,
                    level_name TEXT,
                    level_price REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP,
                    exit_price REAL,
                    exit_reason TEXT,
                    pnl_usd REAL DEFAULT 0,
                    pnl_pct REAL DEFAULT 0,
                    tp1_hit INTEGER DEFAULT 0,
                    be_active INTEGER DEFAULT 0,
                    trail_active INTEGER DEFAULT 0,
                    metadata TEXT
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS daily_performance (
                    date TEXT PRIMARY KEY,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_pnl_usd REAL DEFAULT 0,
                    total_pnl_pct REAL DEFAULT 0,
                    max_drawdown_pct REAL DEFAULT 0,
                    account_balance REAL DEFAULT 0
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    description TEXT,
                    metadata TEXT
                );
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);")

    def connect(self):
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        return con

    def save_trade(self, td: Dict[str, Any]) -> bool:
        try:
            with self.connect() as con:
                con.execute("""
                    INSERT OR REPLACE INTO trades (
                        id, pair, side, entry_price, stop_loss, take_profit_1, take_profit_2,
                        position_size, status, confidence, signal_type, level_name, level_price,
                        metadata, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP);
                """, (
                    td["id"], td["pair"], td["side"], td["entry_price"], td["stop_loss"], td["take_profit_1"],
                    td["take_profit_2"], td.get("position_size", 0), td.get("status","OPEN"), td.get("confidence",3),
                    td.get("signal_type",""), td.get("level_name",""), td.get("level_price",0.0),
                    json.dumps(td.get("metadata", {}), ensure_ascii=False),
                ))
                con.commit()
                return True
        except Exception as e:
            log.error("save_trade error: %s", e)
            return False

    def update_trade(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        try:
            if not updates: return True
            sets = []; vals = []
            for k, v in updates.items():
                if k in {"entry_price","stop_loss","take_profit_1","take_profit_2","position_size","exit_price","pnl_usd","pnl_pct","level_price"}:
                    sets.append(f"{k} = ?"); vals.append(float(v) if v is not None else None)
                elif k in {"status","exit_reason","signal_type","level_name"}:
                    sets.append(f"{k} = ?"); vals.append(str(v) if v is not None else None)
                elif k in {"tp1_hit","be_active","trail_active"}:
                    sets.append(f"{k} = ?"); vals.append(1 if v else 0)
                elif k == "metadata":
                    sets.append(f"{k} = ?"); vals.append(json.dumps(v or {}, ensure_ascii=False))
            sets.append("updated_at = CURRENT_TIMESTAMP")
            vals.append(trade_id)
            q = f"UPDATE trades SET {', '.join(sets)} WHERE id = ?"
            with self.connect() as con:
                con.execute(q, vals)
                con.commit()
                return True
        except Exception as e:
            log.error("update_trade error: %s", e)
            return False

    def close_trade(self, trade_id: str, exit_price: float, exit_reason: str, pnl_pct: float) -> bool:
        try:
            with self.connect() as con:
                con.execute("""
                    UPDATE trades SET status='CLOSED', closed_at=CURRENT_TIMESTAMP,
                    exit_price=?, exit_reason=?, pnl_pct=?, updated_at=CURRENT_TIMESTAMP WHERE id=?;
                """, (exit_price, exit_reason, pnl_pct, trade_id))
                con.commit()
                return True
        except Exception as e:
            log.error("close_trade error: %s", e)
            return False

    def get_open_trades(self) -> List[Dict[str, Any]]:
        try:
            with self.connect() as con:
                cur = con.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY created_at DESC;")
                rows = []
                for r in cur.fetchall():
                    d = dict(r)
                    try:
                        d["metadata"] = json.loads(d.get("metadata") or "{}")
                    except Exception:
                        d["metadata"] = {}
                    rows.append(d)
                return rows
        except Exception as e:
            log.error("get_open_trades error: %s", e)
            return []

    def performance_stats(self, days: int = 30) -> Dict[str, Any]:
        try:
            with self.connect() as con:
                cur = con.execute(f"""
                    SELECT COUNT(*) total_trades,
                           SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) winning_trades,
                           AVG(pnl_pct) avg_pnl_pct,
                           SUM(pnl_pct) total_pnl_pct,
                           MAX(pnl_pct) best_trade,
                           MIN(pnl_pct) worst_trade
                    FROM trades
                    WHERE status='CLOSED' AND created_at >= datetime('now', '-{int(days)} days');
                """)
                row = cur.fetchone()
                if not row or row["total_trades"] == 0:
                    return {"total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0}
                total = int(row["total_trades"])
                wins = int(row["winning_trades"] or 0)
                win_rate = (wins / total) * 100.0 if total else 0.0
                return {
                    "total_trades": total,
                    "winning_trades": wins,
                    "losing_trades": total - wins,
                    "win_rate": round(win_rate, 2),
                    "avg_pnl_pct": round(row["avg_pnl_pct"] or 0, 2),
                    "total_pnl_pct": round(row["total_pnl_pct"] or 0, 2),
                    "best_trade": round(row["best_trade"] or 0, 2),
                    "worst_trade": round(row["worst_trade"] or 0, 2),
                }
        except Exception as e:
            log.error("performance_stats error: %s", e)
            return {"total_trades": 0, "win_rate": 0, "avg_pnl_pct": 0}

    def log_event(self, etype: str, desc: str, meta: Dict[str, Any] = None):
        try:
            with self.connect() as con:
                con.execute("INSERT INTO system_events (event_type, description, metadata) VALUES (?,?,?);",
                            (etype, desc, json.dumps(meta or {}, ensure_ascii=False)))
                con.commit()
        except Exception as e:
            log.error("log_event error: %s", e)

# ===== Sheets Integration (Hardened) =====
class GoogleSheetsIntegration:
    def __init__(self, webhook_url: Optional[str], token: Optional[str]):
        self.url = (webhook_url or "").strip()
        self.token = (token or "").strip()
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled = bool(self.url and self.token)
        self._last_payload: Dict[str, Any] = {}
        if self.url and not self.token:
            log.warning("Sheets webhook URL provided but token missing; disabling Sheets integration.")
            self.enabled = False

        # optional field aliasing (env JSON): {"entry_price":"Entry","tp1":"TP1"}
        self.field_map: Dict[str, str] = {}
        try:
            raw = os.getenv("SHEETS_FIELD_MAP", "")
            if raw.strip():
                self.field_map = json.loads(raw)
        except Exception:
            self.field_map = {}

    async def start(self):
        if not self.session and self.enabled:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _apply_field_map(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.field_map:
            return payload
        mapped = {}
        for k, v in payload.items():
            mapped[self.field_map.get(k, k)] = v
        return mapped

    @staticmethod
    def _sanitize(obj: Any) -> Any:
        try:
            if obj is None: return None
            if isinstance(obj, (str, int, float, bool)): return obj
            if isinstance(obj, (np.integer,)): return int(obj)
            if isinstance(obj, (np.floating,)): return float(obj)
            if isinstance(obj, (np.bool_,)): return bool(obj)
            if isinstance(obj, (pd.Timestamp,)):
                return obj.to_pydatetime().isoformat()
            if isinstance(obj, (datetime,)):
                return obj.astimezone(timezone.utc).isoformat()
            if isinstance(obj, (list, tuple)):
                return [GoogleSheetsIntegration._sanitize(x) for x in obj]
            if isinstance(obj, dict):
                return {str(k): GoogleSheetsIntegration._sanitize(v) for k, v in obj.items()}
            return str(obj)
        except Exception:
            return str(obj)

    def last_payload(self) -> Dict[str, Any]:
        return self._last_payload

    async def send_trade(self, trade_payload: Dict[str, Any], action: str) -> bool:
        if not self.enabled:
            return True
        try:
            await self.start()
            base = {"action": action, "timestamp": datetime.now(timezone.utc).isoformat()}
            payload = {**base, **trade_payload}
            payload = self._apply_field_map(payload)
            payload = GoogleSheetsIntegration._sanitize(payload)
            self._last_payload = payload

            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}

            # Retries
            for attempt in range(3):
                try:
                    async with self.session.post(self.url, json=payload, headers=headers) as resp:
                        if resp.status == 200:
                            return True
                        else:
                            log.warning("Sheets POST HTTP %s", resp.status)
                except Exception as e:
                    log.warning("Sheets POST attempt %s failed: %s", attempt+1, e)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            log.error("Sheets POST failed after 3 attempts")
            return False
        except Exception as e:
            log.error("Sheets send_trade error: %s", e)
            return False

    
async def fetch_open_trades(self) -> List[Dict[str, Any]]:
    """GET ?action=open and return normalized snake_case dicts."""
    if not self.enabled:
        return []
    def _num(v):
        try:
            if v is None: return None
            if isinstance(v, (int,float)): return float(v)
            s = str(v).strip().replace(',', '').replace('$','')
            if s == '': return None
            return float(s)
        except Exception:
            return None
    def _get(r, key):
        # try snake_case, then mapped header name from field_map
        if key in r: return r.get(key)
        if self.field_map:
            alt = self.field_map.get(key)
            if alt and alt in r: return r.get(alt)
        return None
    try:
        await self.start()
        params = {"action": "open"}
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.session.get(self.url, params=params, headers=headers) as resp:
            if resp.status != 200:
                log.warning("Sheets GET open trades HTTP %s", resp.status)
                return []
            data = await resp.json(content_type=None)
        rows = data.get("rows") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        norm: List[Dict[str, Any]] = []
        for r in rows:
            try:
                side_txt = str(_get(r, "side") or _get(r, "direction") or "long").lower()
                side = "short" if "short" in side_txt else "long"
                entry = _num(_get(r, "entry_price") or _get(r, "entry") or _get(r, "Entry Price"))
                stop  = _num(_get(r, "stop_loss") or _get(r, "sl") or _get(r, "Stop Loss"))
                tp1   = _num(_get(r, "take_profit_1") or _get(r, "tp1") or _get(r, "Take Profit 1"))
                tp2   = _num(_get(r, "take_profit_2") or _get(r, "tp2") or _get(r, "Take Profit 2"))
                # Fallback compute TP if missing but entry/stop exist
                if tp1 is None and entry is not None and stop is not None:
                    risk = abs(entry - stop)
                    if risk > 0: tp1 = entry + 1.5*risk if side == "long" else entry - 1.5*risk
                if tp2 is None and entry is not None and stop is not None:
                    risk = abs(entry - stop)
                    if risk > 0: tp2 = entry + 3.0*risk if side == "long" else entry - 3.0*risk
                rec = {
                    "id": str(_get(r, "id") or _get(r, "trade_id") or _get(r, "Trade ID") or ""),
                    "pair": str(_get(r, "pair") or _get(r, "asset") or _get(r, "Asset") or "ETHUSD"),
                    "side": side,
                    "entry_price": float(entry) if entry is not None else 0.0,
                    "stop_loss": float(stop) if stop is not None else 0.0,
                    "take_profit_1": float(tp1) if tp1 is not None else 0.0,
                    "take_profit_2": float(tp2) if tp2 is not None else 0.0,
                    "status": "OPEN",
                    "confidence": int(float(_get(r, "confidence") or _get(r, "Original Score") or 3)),
                    "signal_type": (_get(r, "signal_type") or _get(r, "type") or ""),
                    "level_name": (_get(r, "level_name") or _get(r, "Level Name") or ""),
                    "level_price": float(_num(_get(r, "level_price") or _get(r, "Level Price") or 0) or 0),
                    "metadata": r,
                }
                if rec["id"]:
                    norm.append(rec)
            except Exception:
                continue
        return norm
    except Exception as e:
        log.error("Sheets fetch_open_trades error: %s", e)
        return []

# ===== Trade Manager =====
class TradeManager:
    def __init__(self, cfg: BotConfig, db: DatabaseManager):
        self.cfg = cfg
        self.db = db
        self.active: Dict[str, Dict[str, Any]] = {}
        self.daily_pnl_pct = 0.0

    def can_open(self) -> Tuple[bool, str]:
        if self.cfg.no_trade or self.cfg.dry_run:
            return False, "no-trade or dry-run mode active"
        if len(self.active) >= getattr(self.cfg.risk, 'max_open_trades', 0) if getattr(self.cfg.risk, 'max_open_trades', 0) and getattr(self.cfg.risk, 'max_open_trades', 0) > 0 else 999999999:
            return False, f"Max open trades {self.cfg.risk.max_open_trades} reached"
        if self.daily_pnl_pct <= -self.cfg.risk.max_daily_loss_pct:
            return False, f"Daily loss limit {self.cfg.risk.max_daily_loss_pct}% hit"
        return True, "OK"

    def position_size(self, entry: float, sl: float) -> float:
        risk_amount = self.cfg.risk.account_balance * (self.cfg.risk.max_position_pct / 100.0)
        price_risk = abs(entry - sl)
        if price_risk <= 0:
            return 0.0
        size = risk_amount / price_risk
        max_notional = self.cfg.risk.account_balance * 0.10  # cap 10% notional
        return min(size, max_notional)

    async def open_trade(self, sig: Signal) -> Optional[str]:
        ok, reason = self.can_open()
        if not ok:
            log.warning("Cannot open trade: %s", reason)
            return None
        pos_size = self.position_size(sig.entry, sig.sl)
        if pos_size <= 0:
            log.warning("Invalid position size")
            return None

        trade_id = f"{sig.side.upper()[0]}{datetime.now(timezone.utc).strftime('%m%d%H%M%S')}"
        tdata = {
            "id": trade_id,
            "pair": sig.pair,
            "side": sig.side,
            "entry_price": sig.entry,
            "stop_loss": sig.sl,
            "take_profit_1": sig.tp1,
            "take_profit_2": sig.tp2,
            "position_size": pos_size,
            "status": "OPEN",
            "confidence": sig.confidence,
            "signal_type": sig.signal_type,
            "level_name": sig.level_name,
            "level_price": sig.level_price,
            "metadata": {
                "reason": sig.reason,
                "atr": sig.atr,
                "risk_reward_ratio": sig.risk_reward_ratio,
                "timestamp": sig.timestamp,
            },
        }

        if not self.cfg.dry_run:
            if not self.db.save_trade(tdata):
                log.error("DB save failed; aborting open")
                return None
        self.active[trade_id] = tdata
        self.db.log_event("TRADE_OPENED", f"Opened {sig.side} {sig.pair}", {"trade_id": trade_id, "entry": sig.entry})
        return trade_id

    async def monitor(self, current_price: float, df: pd.DataFrame) -> List[Dict[str, Any]]:
        updates = []
        for tid, t in list(self.active.items()):
            u = await self._check_exit(t, current_price, df)
            if u:
                updates.append(u)
        return updates

    async def _check_exit(self, t: Dict[str, Any], price: float, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        try:
            tid = t["id"]; side = t["side"]
            entry = float(t["entry_price"]); sl = float(t["stop_loss"])
            tp1 = float(t["take_profit_1"]); tp2 = float(t["take_profit_2"])
            tp1_hit = bool(t.get("tp1_hit", False))

            # Stops/TPs
            if side == "long":
                if price <= sl: return await self._close(tid, price, "STOP_LOSS")
                if (not tp1_hit) and price >= tp1: return await self._hit_tp1(tid, price)
                if price >= tp2: return await self._close(tid, price, "TAKE_PROFIT_2")
            else:
                if price >= sl: return await self._close(tid, price, "STOP_LOSS")
                if (not tp1_hit) and price <= tp1: return await self._hit_tp1(tid, price)
                if price <= tp2: return await self._close(tid, price, "TAKE_PROFIT_2")

            # Trailing after TP1
            if t.get("tp1_hit", False) and self.cfg.trading.trail_mode != "none":
                upd = await self._update_trailing(t, price, df)
                if upd: return upd

            return None
        except Exception as e:
            log.error("check_exit error: %s", e)
            return None

    async def _hit_tp1(self, tid: str, price: float) -> Optional[Dict[str, Any]]:
        t = self.active[tid]
        entry = float(t["entry_price"]); side = t["side"]
        frac = self.cfg.trading.partial_exit_fraction
        pnl_pct = ((price - entry) / entry) * 100.0 * frac if side == "long" else ((entry - price) / entry) * 100.0 * frac

        new_sl = float(t["stop_loss"])
        if self.cfg.trading.be_after_tp1:
            be_off = entry * (self.cfg.trading.be_offset_pct / 100.0)
            new_sl = max(new_sl, entry + be_off) if side == "long" else min(new_sl, entry - be_off)

        updates = {"tp1_hit": True, "be_active": True, "stop_loss": new_sl, "trail_active": (self.cfg.trading.trail_mode != "none")}
        if not self.cfg.dry_run:
            self.db.update_trade(tid, updates)
        t.update(updates)
        return {"type": "TP1_HIT", "trade_id": tid, "price": price, "pnl_pct": pnl_pct, "new_stop_loss": new_sl, "partial_fraction": frac}

    async def _close(self, tid: str, price: float, reason: str) -> Optional[Dict[str, Any]]:
        t = self.active[tid]
        entry = float(t["entry_price"]); side = t["side"]
        pnl_pct = ((price - entry) / entry) * 100.0 if side == "long" else ((entry - price) / entry) * 100.0
        if t.get("tp1_hit", False):
            pnl_pct *= (1 - self.cfg.trading.partial_exit_fraction)
        pnl_usd = float(t.get("position_size", 0.0)) * (pnl_pct / 100.0)
        if not self.cfg.dry_run:
            self.db.close_trade(tid, price, reason, pnl_pct)
        self.active.pop(tid, None)
        self.daily_pnl_pct += pnl_pct
        self.db.log_event("TRADE_CLOSED", f"Closed {side} trade: {reason}", {"trade_id": tid, "exit_price": price, "pnl_pct": pnl_pct})
        return {"type": "TRADE_CLOSED", "trade_id": tid, "exit_price": price, "exit_reason": reason, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd}

    async def _update_trailing(self, t: Dict[str, Any], price: float, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        try:
            mode = self.cfg.trading.trail_mode
            if mode == "none": return None
            side = t["side"]
            cur_sl = float(t["stop_loss"]); new_sl = None

            if mode == "atr":
                if df is None or len(df) < 15: return None
                atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0
                mult = self.cfg.trading.trail_atr_mult
                if side == "long":
                    cand = price - (atr * mult)
                    if cand > cur_sl: new_sl = cand
                else:
                    cand = price + (atr * mult)
                    if cand < cur_sl: new_sl = cand

            elif mode == "chandelier":
                p = self.cfg.trading.chandelier_period
                mult = self.cfg.trading.chandelier_mult
                if len(df) < p + 1: return None
                if side == "long":
                    hh = float(df["high"].tail(p).max())
                    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0
                    cand = hh - atr * mult
                    if cand > cur_sl: new_sl = cand
                else:
                    ll = float(df["low"].tail(p).min())
                    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0
                    cand = ll + atr * mult
                    if cand < cur_sl: new_sl = cand

            if new_sl is not None:
                if not self.cfg.dry_run:
                    self.db.update_trade(t["id"], {"stop_loss": new_sl})
                t["stop_loss"] = new_sl
                return {"type": "TRAILING_STOP_UPDATE", "trade_id": t["id"], "new_stop_loss": new_sl}
            return None
        except Exception as e:
            log.error("update_trailing error: %s", e)
            return None

# ===== Discord Bot =====
class TradingBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.db = DatabaseManager()
        self.mdp = MarketDataProvider(cfg.pair, cfg.interval_minutes)
        self.engine = TradeSignalEngine(cfg.trading)
        self.tm = TradeManager(cfg, self.db)
        self.sheets = GoogleSheetsIntegration(cfg.sheets_url, cfg.sheets_token)
        self.calculator = CamarillaCalculator()

        intents = discord.Intents.default()
        intents.message_content = True
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self._setup_commands()
        self._setup_events()

        self.last_health = time.time()

        # Flask app visibility
        self.flask_app: Optional[Flask] = None

    # ---------- Commands ----------
    def _setup_commands(self):
        @self.bot.command(name="status")
        async def status(ctx):
            try:
                e = discord.Embed(title="🛡️ Bot Status", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                e.add_field(name="Pair", value=self.cfg.pair, inline=True)
                e.add_field(name="TF", value=f"{self.cfg.interval_minutes}m", inline=True)
                e.add_field(name="Open Trades", value=str(len(self.tm.active)), inline=True)
                e.add_field(name="Daily P&L", value=f"{self.tm.daily_pnl_pct:+.2f}%", inline=True)
                e.add_field(name="Sheets", value="✅" if self.sheets.enabled else "❌", inline=True)
                e.add_field(name="Mode", value=f"dry={self.cfg.dry_run} no_post={self.cfg.no_post} no_trade={self.cfg.no_trade}", inline=False)
                await ctx.send(embed=e)
            except Exception as e:
                await ctx.send(f"❌ {e}")

        @self.bot.command(name="trades")
        async def trades(ctx):
            if not self.tm.active:
                await ctx.send("📊 No active trades")
                return
            e = discord.Embed(title="📈 Active Trades", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            for tid, t in list(self.tm.active.items())[:10]:
                side_emoji = "🟢" if t["side"] == "long" else "🔴"
                tp1_status = "✅" if t.get("tp1_hit", False) else "⏳"
                info = (f"{side_emoji} **{t['side'].upper()}** {t['pair']}\n"
                        f"Entry: ${t['entry_price']:.2f}\n"
                        f"SL: ${t['stop_loss']:.2f}\n"
                        f"TP1: ${t['take_profit_1']:.2f} {tp1_status}\n"
                        f"TP2: ${t['take_profit_2']:.2f}")
                e.add_field(name=f"🎯 {tid}", value=info, inline=True)
            await ctx.send(embed=e)

        @self.bot.command(name="performance")
        async def performance(ctx, days: int = 30):
            st = self.db.performance_stats(days)
            if st.get("total_trades", 0) == 0:
                await ctx.send("📊 No trades in period")
                return
            clr = discord.Color.green() if st["win_rate"] > 50 else discord.Color.red()
            e = discord.Embed(title=f"📈 Performance ({days}d)", color=clr, timestamp=datetime.now(timezone.utc))
            for k in ["total_trades","winning_trades","losing_trades","win_rate","avg_pnl_pct","total_pnl_pct","best_trade","worst_trade"]:
                e.add_field(name=k.replace("_"," ").title(), value=str(st[k]), inline=True)
            await ctx.send(embed=e)

        @self.bot.command(name="market")
        async def market(ctx):
            df = await self.mdp.fetch_ohlc(100)
            if df is None or df.empty:
                await ctx.send("❌ Market data unavailable")
                return
            df = self.mdp.calculate_indicators(df)
            levels = self.calculator.calculate_levels(
                df,
                freq=self.cfg.trading.camarilla_freq,
                tz=self.cfg.trading.camarilla_tz
            )
            if not levels:
                await ctx.send("❌ Levels unavailable")
                return
            last = df.iloc[-1]
            price = float(last["close"]); rsi = float(last.get("rsi", 50)); vol = float(last.get("volume", 0))
            e = discord.Embed(title=f"📊 Market - {self.cfg.pair}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            e.add_field(name="Price", value=f"${price:.2f}", inline=True)
            e.add_field(name="RSI", value=f"{rsi:.1f}", inline=True)
            e.add_field(name="Vol", value=f"{vol:,.0f}", inline=True)
            regime = "🔥 Overbought" if rsi > 70 else ("❄️ Oversold" if rsi < 30 else "⚖️ Neutral")
            e.add_field(name="Regime", value=regime, inline=True)
            key_levels = []
            for k in ["H5","H4","PIVOT","L4","L5"]:
                if k in levels:
                    lp = float(levels[k])
                    dist = ((price - lp)/price) * 100
                    key_levels.append(f"**{k}** ${lp:.2f} ({dist:+.2f}%)")
            e.add_field(name="Levels", value="\n".join(key_levels) or "—", inline=False)
            await ctx.send(embed=e)

        # Admin toggles
        @self.bot.command(name="dryrun")
        async def dryrun(ctx, mode: Optional[str] = None):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admin only")
                return
            if mode in {"on","off"}:
                self.cfg.dry_run = (mode == "on")
            await ctx.send(f"Dry-run is **{self.cfg.dry_run}**")

        @self.bot.command(name="postmode")
        async def postmode(ctx, mode: Optional[str] = None):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admin only")
                return
            if mode in {"on","off"}:
                self.cfg.no_post = (mode == "off")
            await ctx.send(f"Posting enabled: **{not self.cfg.no_post}**")

        @self.bot.command(name="trademode")
        async def trademode(ctx, mode: Optional[str] = None):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admin only")
                return
            if mode in {"on","off"}:
                self.cfg.no_trade = (mode == "off")
            await ctx.send(f"Trade opening enabled: **{not self.cfg.no_trade}**")

        # Test: simulate a one-shot scan without sending public alerts
        @self.bot.command(name="scan_once")
        async def scan_once(ctx):
            try:
                df = await self.mdp.fetch_ohlc(100)
                if df is None or df.empty:
                    await ctx.send("❌ Data unavailable")
                    return
                df = self.mdp.calculate_indicators(df)
                levels = self.calculator.calculate_levels(
                    df,
                    freq=self.cfg.trading.camarilla_freq,
                    tz=self.cfg.trading.camarilla_tz
                )
                sig = self.engine.generate(df, levels, self.cfg.pair) if levels else None
                if not sig:
                    await ctx.send("🔍 No signal")
                    return
                await ctx.send(f"✅ Signal: {sig.side} {sig.pair} {sig.signal_type} @ {sig.entry:.2f} RR~{sig.risk_reward_ratio:.2f} conf {sig.confidence}/6")
            except Exception as e:
                await ctx.send(f"❌ {e}")

        @self.bot.command(name="close")
        async def close_trade(ctx, trade_id: Optional[str] = None):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Admin only")
                return
            if not self.tm.active:
                await ctx.send("No active trades")
                return
            if not trade_id:
                trade_id = list(self.tm.active.keys())[0]
            if trade_id not in self.tm.active:
                await ctx.send(f"Trade {trade_id} not found")
                return
            df = await self.mdp.fetch_ohlc(10)
            if df is None or df.empty:
                await ctx.send("❌ Price unavailable")
                return
            price = float(df.iloc[-1]["close"])
            res = await self.tm._close(trade_id, price, "MANUAL_CLOSE")
            if res:
                e = discord.Embed(title="✅ Trade Closed Manually", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
                e.add_field(name="Trade ID", value=trade_id, inline=True)
                e.add_field(name="Exit Price", value=f"${price:.2f}", inline=True)
                e.add_field(name="P&L", value=f"{res['pnl_pct']:+.2f}%", inline=True)
                await ctx.send(embed=e)
            else:
                await ctx.send("❌ Failed")

        @self.bot.command(name="config")
        async def config_cmd(ctx):
            e = discord.Embed(title="⚙️ Config", color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))
            risk = self.cfg.risk
            tr = self.cfg.trading
            e.add_field(name="Risk", value=f"max_pos {risk.max_position_pct}%\nmax_daily_loss {risk.max_daily_loss_pct}%\nmax_open {risk.max_open_trades}\nmin_RR {risk.min_rr_ratio}", inline=True)
            e.add_field(name="Trading", value=f"mode {tr.confirmation_mode}\nbody_ratio {tr.body_ratio_threshold}\nvol_mult {tr.volume_multiplier}x\nconf_min {tr.signal_confidence_min}\ntrail {tr.trail_mode}", inline=True)
            e.add_field(name="Throttle", value=f"cooldown {tr.cooldown_seconds}s\nglobal({tr.use_global_cooldown}) {tr.global_cooldown_seconds}s\nbucket {tr.token_bucket_max}@{tr.token_bucket_refill_per_min}/min", inline=True)
            e.add_field(name="Camarilla", value=f"freq {tr.camarilla_freq}\ntz {tr.camarilla_tz}", inline=True)
            await ctx.send(embed=e)

    # ---------- Events ----------
    def _setup_events(self):
        @self.bot.event
        async def on_ready():
            log.info("Discord logged in as %s", self.bot.user)
            await self.mdp.start()
            await self.sheets.start()
            # Rehydrate open trades: DB first
            for t in self.db.get_open_trades():
                self.tm.active[t["id"]] = t
            # Optional rehydrate from Sheets
            if self.cfg.sheets_rehydrate and self.sheets.enabled:
                try:
                    rows = await self.sheets.fetch_open_trades()
                    existing = set(self.tm.active.keys())
                    for r in rows:
                        tid = r["id"]
                        if tid in existing:
                            continue
                        self.db.save_trade(r)
                        self.tm.active[tid] = r
                    log.info("Rehydrated %s open trades from Sheets", len(rows))
                except Exception as e:
                    log.warning("Sheets rehydrate failed: %s", e)

            # Start monitoring loop
            if not self.monitor_loop.is_running():
                self.monitor_loop.start()

            # Send startup
            try:
                if not self.cfg.no_post:
                    ch = self.bot.get_channel(self.cfg.channels.health)
                    if ch:
                        e = discord.Embed(title="🟢 Bot Online", description="Production_ControlTower_v12x active", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                        e.add_field(name="Pair", value=self.cfg.pair, inline=True)
                        e.add_field(name="Interval", value=f"{self.cfg.interval_minutes}m", inline=True)
                        e.add_field(name="Mode", value=f"dry={self.cfg.dry_run} no_post={self.cfg.no_post} no_trade={self.cfg.no_trade}", inline=False)
                        e.add_field(name="Camarilla", value=f"{self.cfg.trading.camarilla_freq} ({self.cfg.trading.camarilla_tz})", inline=False)
                        await ch.send(embed=e)
            except Exception as e:
                log.error("Startup notify error: %s", e)

    # ---------- Monitoring Loop ----------
    @tasks.loop(minutes=2)
    async def monitor_loop(self):
        try:
            df = await self.mdp.fetch_ohlc(100)
            if df is None or df.empty:
                log.warning("No market data in loop")
                return
            df = self.mdp.calculate_indicators(df)
            price = float(df.iloc[-1]["close"])

            # Monitor existing trades
            updates = await self.tm.monitor(price, df)
            for u in updates:
                await self._handle_update(u)

            # Check for new signals
            await self._maybe_open_signal(df)

            self.last_health = time.time()
        except Exception as e:
            log.error("monitor_loop error: %s", e)
            if not self.cfg.no_post:
                try:
                    ch = self.bot.get_channel(self.cfg.channels.alerts)
                    if ch:
                        ebd = discord.Embed(title="⚠️ Monitoring Error", description=str(e)[:200], color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
                        await ch.send(embed=ebd)
                except Exception:
                    pass

    @monitor_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_open_signal(self, df: pd.DataFrame):
        levels = self.calculator.calculate_levels(
            df,
            freq=self.cfg.trading.camarilla_freq,
            tz=self.cfg.trading.camarilla_tz
        )
        if not levels:
            return
        sig = self.engine.generate(df, levels, self.cfg.pair)
        if not sig:
            return
        trade_id = await self.tm.open_trade(sig)
        if not trade_id:
            return

        # Unified trade ID everywhere
        if not self.cfg.no_post:
            await self._send_signal(sig, trade_id)

        # Sheets write (strict id discipline)
        if self.sheets.enabled and not self.cfg.dry_run:
            payload = {
                "id": trade_id,
                "pair": sig.pair,
                "side": sig.side,
                "entry_price": sig.entry,
                "stop_loss": sig.sl,
                "take_profit_1": sig.tp1,
                "take_profit_2": sig.tp2,
                "confidence": sig.confidence,
                "signal_type": sig.signal_type,
                "level_name": sig.level_name,
                "level_price": sig.level_price,
                "reason": sig.reason,
                "risk_reward_ratio": sig.risk_reward_ratio,
            }
            await self.sheets.send_trade(payload, "CREATE")

    async def _send_signal(self, sig: Signal, trade_id: str):
        ch = self.bot.get_channel(self.cfg.channels.signals)
        if not ch: return
        side_emoji = "🟢" if sig.side == "long" else "🔴"
        color = discord.Color.green() if sig.side == "long" else discord.Color.red()
        e = discord.Embed(title=f"⚡ New Signal - {sig.pair}", description=f"{side_emoji} **{sig.side.upper()}** opened", color=color, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Trade ID", value=trade_id, inline=True)
        e.add_field(name="Entry", value=f"${sig.entry:.2f}", inline=True)
        e.add_field(name="Stop", value=f"${sig.sl:.2f}", inline=True)
        e.add_field(name="TP1", value=f"${sig.tp1:.2f}", inline=True)
        e.add_field(name="TP2", value=f"${sig.tp2:.2f}", inline=True)
        e.add_field(name="Confidence", value=f"{sig.confidence}/6", inline=True)
        e.add_field(name="R:R", value=f"{sig.risk_reward_ratio:.2f}", inline=True)
        e.add_field(name="Level", value=f"{sig.level_name}", inline=True)
        e.add_field(name="Type", value=sig.signal_type.title(), inline=True)
        e.add_field(name="Reason", value=sig.reason, inline=False)
        try:
            await ch.send(embed=e)
        except Exception as ex:
            log.error("send_signal error: %s", ex)

    async def _handle_update(self, u: Dict[str, Any]):
        typ = u.get("type"); tid = u.get("trade_id")
        if typ == "TP1_HIT":
            await self._send_tp1(u)
        elif typ == "TRADE_CLOSED":
            await self._send_close(u)
        elif typ == "TRAILING_STOP_UPDATE":
            log.info("Trailing updated %s -> %s", tid, u.get("new_stop_loss"))

        # Sheets update for TP1/Close
        if self.sheets.enabled and not self.cfg.dry_run and typ in {"TP1_HIT","TRADE_CLOSED"}:
            await self.sheets.send_trade(u, "UPDATE")

    async def _send_tp1(self, u: Dict[str, Any]):
        ch = self.bot.get_channel(self.cfg.channels.performance)
        if not ch or self.cfg.no_post: return
        e = discord.Embed(title="🎯 TP1 Hit", description=f"Partial exit for {u['trade_id']}", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
        e.add_field(name="Price", value=f"${u['price']:.2f}", inline=True)
        e.add_field(name="Partial P&L", value=f"{u['pnl_pct']:+.2f}%", inline=True)
        e.add_field(name="Partial Size", value=f"{u['partial_fraction']*100:.0f}%", inline=True)
        e.add_field(name="New Stop", value=f"${u['new_stop_loss']:.2f}", inline=True)
        e.add_field(name="Status", value="Moved to BE", inline=True)
        await ch.send(embed=e)

    async def _send_close(self, u: Dict[str, Any]):
        ch = self.bot.get_channel(self.cfg.channels.performance)
        if not ch or self.cfg.no_post: return
        pnl = float(u["pnl_pct"]); color = discord.Color.green() if pnl > 0 else discord.Color.red()
        status = "✅" if pnl > 0 else "❌"
        e = discord.Embed(title=f"{status} Trade Closed", description=f"{u['trade_id']} closed", color=color, timestamp=datetime.now(timezone.utc))
        e.add_field(name="Exit", value=f"${u['exit_price']:.2f}", inline=True)
        e.add_field(name="Final P&L", value=f"{pnl:+.2f}%", inline=True)
        e.add_field(name="Reason", value=(u.get("exit_reason","").replace("_"," ").title()), inline=True)
        if "pnl_usd" in u:
            e.add_field(name="PnL (USD)", value=f"${u['pnl_usd']:+.2f}", inline=True)
        await ch.send(embed=e)

    # ---------- Flask Health Server ----------
    def start_health_server(self):
        app = Flask(__name__)

        @app.route("/health")
        def health():
            try:
                since = time.time() - self.last_health
                healthy = since < 300
                return jsonify({
                    "status": "healthy" if healthy else "degraded",
                    "version": "v12x",
                    "interval_minutes": self.cfg.interval_minutes,
                    "active_trades": len(self.tm.active),
                    "daily_pnl_pct": self.tm.daily_pnl_pct,
                    "last_check_age_sec": since,
                    "dry_run": self.cfg.dry_run,
                    "no_post": self.cfg.no_post,
                    "no_trade": self.cfg.no_trade,
                }), (200 if healthy else 503)
            except Exception as e:
                return jsonify({"status":"error","error":str(e)}), 500

        @app.route("/", methods=["GET","HEAD"])
        def root():
            try:
                return jsonify({"ok": True, "message": "Service up. See /health, /metrics, /debug/last_payload"}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/metrics")
        def metrics():
            try:
                return jsonify(self.db.performance_stats(30))
            except Exception as e:
                return jsonify({"error":str(e)}), 500

        @app.route("/debug/last_payload")
        def last_payload():
            try:
                return jsonify(self.sheets.last_payload())
            except Exception as e:
                return jsonify({"error":str(e)}), 500

        def run():
            port = int(os.environ.get("PORT", "8080"))
            app.run(host="0.0.0.0", port=port, debug=False)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.flask_app = app
        log.info("Health server started")

    async def start(self):
        log.info("Starting Production_ControlTower_v12x...")
        self.db.log_event("BOT_STARTUP", "Trading bot starting")
        self.start_health_server()
        await self.bot.start(self.cfg.token)

    async def stop(self):
        try:
            if self.monitor_loop.is_running():
                self.monitor_loop.cancel()
        except Exception:
            pass
        try:
            await self.mdp.stop()
            await self.sheets.stop()
        except Exception:
            pass
        try:
            await self.bot.close()
        except Exception:
            pass
        self.db.log_event("BOT_SHUTDOWN", "Trading bot shutdown")

# ===== Entrypoint =====
async def main():
    try:
        cfg = BotConfig.from_env()
        bot = TradingBot(cfg)
        await bot.start()
    except Exception as e:
        log.error("Fatal: %s", e)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.error("Fatal error: %s", e)
        sys.exit(1)
