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
import datetime
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
STATUS_CHANNEL_ID = 1397320600359272469

KRAKEN_PAIRS = {
    "BTC": "XXBTZUSD", "ETH": "XETHZUSD", "XRP": "XXRPZUSD", "SOL": "SOLUSD",
    "DOGE": "DOGEUSD", "ADA": "ADAUSD", "SUI": "SUIUSD",
    "HBAR": "HBARUSD", "AVAX": "AVAXUSD"
}

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

# [apply_indicators and detect_trade unchanged from previous step]

# ==== ACTIVE ALERTS SETUP ====
last_alerts = {}
active_alerts = {}  # format: symbol: (entry, tp1, tp2, stop, alert_time)

@tasks.loop(minutes=1)
async def scan_coins():
    channel = bot.get_channel(CHANNEL_ID)

    for symbol in KRAKEN_PAIRS:
        df = fetch_ohlc(symbol)
        if df is None:
            continue

        latest = df.iloc[-1]
        price = latest['close']

        if symbol in active_alerts:
            entry, tp1, tp2, stop, alert_time = active_alerts[symbol]
            time_elapsed = (datetime.datetime.utcnow() - alert_time).total_seconds()
            direction = "Long" if entry < stop else "Short"
            tp_hit = sl_hit = False

            if direction == "Long":
                tp_hit = price >= tp1 or price >= tp2
                sl_hit = price <= stop
            else:
                tp_hit = price <= tp1 or price <= tp2
                sl_hit = price >= stop

            if tp_hit or sl_hit:
                result = "🎯 Take Profit Hit!" if tp_hit else "💥 Stop Loss Hit!"
                color = discord.Color.green() if tp_hit else discord.Color.red()
                embed = discord.Embed(
                    title=f"{symbol} {direction} Exit Alert",
                    description=result,
                    color=color
                )
                embed.add_field(name="📈 Price", value=f"${price:,.2f}", inline=True)
                embed.add_field(name="📊 Entry", value=f"${entry:.2f}", inline=True)
                embed.add_field(name="🛑 Stop", value=f"${stop:.2f}", inline=True)
                embed.add_field(name="🎯 TP1", value=f"${tp1:.2f}", inline=True)
                embed.add_field(name="🎯 TP2", value=f"${tp2:.2f}", inline=True)
                utc_now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                embed.set_footer(text=f"Alert generated at {utc_now}")
                await channel.send(embed=embed)
                del active_alerts[symbol]
                continue

            if time_elapsed < 1800:
                continue

        trade = detect_trade(df)
        if trade:
            key = f"{symbol}_{trade['type']}"
            if last_alerts.get(key) != trade['entry']:
                last_alerts[key] = trade['entry']
                active_alerts[symbol] = (
                    trade['entry'], trade['tp1'], trade['tp2'], trade['stop'], datetime.datetime.utcnow()
                )
                await channel.send(embed=format_embed(symbol, trade))

    # Cleanup expired alerts
    for sym in list(active_alerts.keys()):
        if (datetime.datetime.utcnow() - active_alerts[sym][4]).total_seconds() > 3600:
            del active_alerts[sym]

# === On-Demand and Scheduled ETH Report ===
@tasks.loop(minutes=30)
async def eth_status_report():
    channel = bot.get_channel(STATUS_CHANNEL_ID)
    await send_eth_status_report(channel)

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

@bot.command()
async def ethreport(ctx):
    if ctx.channel.id != STATUS_CHANNEL_ID:
        await ctx.send("❌ Please use this command in the ETH report channel.")
        return
    await send_eth_status_report(ctx.channel)

@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online.")
    scan_coins.start()
    eth_status_report.start()

if TOKEN:
    bot.run(TOKEN)
else:
    print("❌ TOKEN not found.")

