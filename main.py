import os
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask
from dotenv import load_dotenv
import discord
from discord.ext import commands, tasks
from ta.trend import ema_indicator
from ta.momentum import rsi, stochrsi, tsi
from ta.volatility import average_true_range
from ta.volume import on_balance_volume
import pytz
CENTRAL_TZ = pytz.timezone("US/Central")

load_dotenv()
TOKEN = os.getenv("TOKEN")

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8000)).start()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

CHANNEL_ID = 1395604673737789460
STATUS_CHANNEL_ID = 1397320600359272469  # 30-min ETH report channel

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD",
    "HBAR": "HBARUSD", "AVAX": "AVAXUSD"
}

# === Indicator Helpers ===
def fetch_ohlc(symbol, interval=5):
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={KRAKEN_PAIRS[symbol]}&interval={interval}"
        r = requests.get(url)
        raw = r.json()['result']
        pair_key = next(k for k in raw if k != 'last')
        data = pd.DataFrame(raw[pair_key], columns=["time","open","high","low","close","vwap","volume","count"])
        data[['open','high','low','close','volume']] = data[['open','high','low','close','volume']].astype(float)
        return apply_indicators(data)
    except Exception as e:
        print(f"[ERROR] OHLC fetch for {symbol}: {e}")
        return None

def apply_indicators(df):
    df['ema50'] = ema_indicator(df['close'], window=50)
    df['rsi'] = rsi(df['close'], window=14)
    df['stochrsi'] = stochrsi(df['close'], window=14)
    df['tsi'] = tsi(df['close'])
    df['obv'] = on_balance_volume(df['close'], df['volume'])
    df['atr'] = average_true_range(df['high'], df['low'], df['close'], window=14)
    df['macd'] = df['close'].ewm(span=12).mean() - df['close'].ewm(span=26).mean()
    df['signal'] = df['macd'].ewm(span=9).mean()
    df['macd_hist'] = df['macd'] - df['signal']
    df['macd_hist_flip'] = df['macd_hist'].diff().apply(lambda x: x > 0)
    df['volume_spike'] = df['volume'] > df['volume'].rolling(20).mean() * 1.5
    df['supertrend_bull'] = df['close'] > df['high'].rolling(10).mean()
    df['supertrend_bear'] = df['close'] < df['low'].rolling(10).mean()
    df['jaw'] = df['close'].rolling(13).mean()
    df['teeth'] = df['close'].rolling(8).mean()
    df['lips'] = df['close'].rolling(5).mean()
    df['alligator_bullish'] = (df['lips'] > df['teeth']) & (df['teeth'] > df['jaw'])
    df['alligator_bearish'] = (df['lips'] < df['teeth']) & (df['teeth'] < df['jaw'])
    period9_high = df['high'].rolling(9).max()
    period9_low = df['low'].rolling(9).min()
    tenkan_sen = (period9_high + period9_low) / 2
    period26_high = df['high'].rolling(26).max()
    period26_low = df['low'].rolling(26).min()
    kijun_sen = (period26_high + period26_low) / 2
    senkou_span_a = ((tenkan_sen + kijun_sen) / 2).shift(26)
    period52_high = df['high'].rolling(52).max()
    period52_low = df['low'].rolling(52).min()
    senkou_span_b = ((period52_high + period52_low) / 2).shift(26)
    df['ichimoku_bullish'] = (df['close'] > senkou_span_a) & (df['close'] > senkou_span_b)
    df['ichimoku_bearish'] = (df['close'] < senkou_span_a) & (df['close'] < senkou_span_b)
    return df

# === Trade Detection ===
def detect_trade(df):
    latest = df.iloc[-1]
    high20 = df['high'].rolling(20).max().iloc[-2]
    low20 = df['low'].rolling(20).min().iloc[-2]
    atr = latest['atr']
    trade = None

    if latest['close'] > high20 and latest['macd_hist_flip'] and latest['volume_spike']:
        trade = {"type": "Breakout Long"}
    elif low20 < latest['close'] < low20 + atr and latest['rsi'] < 40:
        trade = {"type": "Pullback Long"}
    elif latest['close'] < low20 and not latest['macd_hist_flip'] and latest['volume_spike']:
        trade = {"type": "Breakdown Short"}

    if trade:
        trade.update({
            "entry": latest['close'],
            "stop": latest['close'] - atr if "Long" in trade['type'] else latest['close'] + atr,
            "tp1": latest['close'] + atr * 1.5 if "Long" in trade['type'] else latest['close'] - atr * 1.5,
            "tp2": latest['close'] + atr * 2.5 if "Long" in trade['type'] else latest['close'] - atr * 2.5,
            "confidence": (
                int(latest['supertrend_bull'] and 'Long' in trade['type']) +
                int(latest['supertrend_bear'] and 'Short' in trade['type']) +
                int(latest['alligator_bullish'] and 'Long' in trade['type']) +
                int(latest['alligator_bearish'] and 'Short' in trade['type']) +
                int(latest['ichimoku_bullish'] and 'Long' in trade['type']) +
                int(latest['ichimoku_bearish'] and 'Short' in trade['type'])
            )
        })
    return trade

def format_embed(symbol, trade):
    color = discord.Color.green() if "Long" in trade["type"] else discord.Color.red()
    embed = discord.Embed(title=f"{symbol} {trade['type']} Alert", color=color)
    embed.add_field(name="💥 Entry", value=f"${trade['entry']:.2f}", inline=True)
    embed.add_field(name="🛑 Stop", value=f"${trade['stop']:.2f}", inline=True)
    embed.add_field(name="🎯 TP1", value=f"${trade['tp1']:.2f}", inline=True)
    embed.add_field(name="🎯 TP2", value=f"${trade['tp2']:.2f}", inline=True)
    rr = abs((trade['tp1'] - trade['entry']) / (trade['entry'] - trade['stop']))
    embed.add_field(name="⚖️ Risk/Reward", value=f"{rr:.2f}x", inline=False)
    embed.add_field(name="📊 Confidence", value=f"{trade['confidence']}/6", inline=False)
    return embed

# === Auto Scan Loop ===
last_alerts = {}

@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)
    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None: continue
        trade = detect_trade(df)
        if trade:
            key = f"{symbol}_{trade['type']}"
            if last_alerts.get(key) != trade['entry']:
                last_alerts[key] = trade['entry']
                await channel.send(embed=format_embed(symbol, trade))

@tasks.loop(minutes=30)
async def eth_status_report():
    channel = bot.get_channel(1397320600359272469)
    df = fetch_ohlc("ETH")
    if df is None:
        await channel.send("❌ ETH data fetch failed.")
        return

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    # Price change
    price_now = latest['close']
    price_prev = previous['close']
    pct_change = ((price_now - price_prev) / price_prev) * 100
    direction = "⬆️" if pct_change >= 0 else "⬇️"
    twist = "⚠️ Twist detected" if df['ichimoku_bullish'].iloc[-1] != df['ichimoku_bullish'].iloc[-2] else "No twist"

    embed = discord.Embed(
        title=f"📊 ETH Strategy Status {direction} ({pct_change:+.2f}%) at {pd.Timestamp.now(CENTRAL_TZ).strftime('%Y-%m-%d %I:%M %p %Z')
}",
        color=discord.Color.blue()
    )

    embed.add_field(name="💰 Price", value=f"${price_now:,.2f}", inline=True)
    embed.add_field(name="📈 RSI", value=f"{latest['rsi']:.2f}", inline=True)
    embed.add_field(name="📉 MACD", value=f"{latest['macd']:.4f} | Signal: {latest['signal']:.4f}", inline=True)
    embed.add_field(name="📊 Stoch RSI", value=f"{latest['stochrsi']:.2f}", inline=True)
    embed.add_field(name="📊 EMA50", value=f"${latest['ema50']:.2f}", inline=True)

    # OBV trend
    obv_trend = "📈 Bullish" if latest['obv'] > df['obv'].rolling(5).mean().iloc[-1] else "📉 Bearish"
    embed.add_field(name="📶 OBV", value=obv_trend, inline=True)

    # Signal states
    embed.add_field(name="🧠 Supertrend", value="🟢 Bullish" if latest['supertrend_bull'] else "🔴 Bearish", inline=True)
    embed.add_field(name="🐊 Alligator", value="🟢 Bullish" if latest['alligator_bullish'] else "🔴 Bearish", inline=True)
    embed.add_field(name="☁️ Ichimoku", value="🟢 Bullish" if latest['ichimoku_bullish'] else "🔴 Bearish", inline=True)
    embed.add_field(name="🌪️ Twist Alert", value=twist, inline=True)

    await channel.send(embed=embed)


# === Commands ===
@bot.command()
async def scan(ctx):
    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            await ctx.send(f"❌ {symbol} data fetch failed.")
            continue
        trade = detect_trade(df)
        if trade:
            await ctx.send(embed=format_embed(symbol, trade))
        else:
            await ctx.send(f"🔍 No setup for {symbol}.")

@bot.command()
async def confidence(ctx, symbol: str):
    symbol = symbol.upper()
    if symbol not in KRAKEN_PAIRS:
        await ctx.send("❌ Unsupported symbol.")
        return
    df = fetch_ohlc(symbol)
    if df is not None:
        trade = detect_trade(df)
        if trade:
            await ctx.send(embed=format_embed(symbol, trade))
        else:
            await ctx.send(f"ℹ️ {symbol} has no valid setup right now.")
    else:
        await ctx.send(f"❌ Could not fetch data for {symbol}.")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online.")
    scan_coins.start()
    eth_status_report.start()

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")


