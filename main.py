import discord
from discord.ext import commands
import requests
import os
from flask import Flask
import threading
from dotenv import load_dotenv

# === Load environment variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")

# === Flask for Uptime Monitoring ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is live!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

# === Start Flask in a new thread ===
threading.Thread(target=run_flask).start()

# === Intents and Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# === CoinGecko coin ID mapping ===
COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "XRP": "ripple",
    "BNB": "binancecoin",
    "SOL": "solana",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "SUI": "sui",
    "HBAR": "hedera-hashgraph",
    "AVAX": "avalanche-2",
    "PNIC": "phoenic-token"
}

# === Fetch coin price from CoinGecko ===
def fetch_price(coin_symbol):
    coin_id = COIN_IDS.get(coin_symbol.upper())
    if not coin_id:
        return None
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        response = requests.get(url)
        data = response.json()
        return data[coin_id]['usd']
    except Exception as e:
        print(f"[ERROR] Failed to fetch price for {coin_symbol}: {e}")
        return None

# === Generate trade message ===
def generate_trade_message(symbol, price):
    if price is None:
        return "❌ Failed to fetch price."

    # Strategy logic
    breakout_entry = round(price * 1.01, 2)
    breakout_stop = round(price * 0.97, 2)
    breakout_tp1 = round(price * 1.006, 2)
    breakout_tp2 = round(price * 1.03, 2)

    pullback_zone_low = round(price * 0.965, 2)
    pullback_zone_high = round(price * 0.975, 2)
    pullback_stop = round(price * 0.96, 2)
    pullback_tp1 = breakout_tp1
    pullback_tp2 = breakout_tp2

    breakdown_entry = round(price * 0.99, 2)
    breakdown_stop = breakout_entry
    breakdown_tp1 = breakout_stop
    breakdown_tp2 = round(price * 0.94, 2)

    pullback_short_zone_high = round(price * 1.03, 2)
    pullback_short_zone_low = round(price * 1.025, 2)
    pullback_short_stop = round(price * 1.035, 2)
    pullback_short_tp1 = breakdown_entry
    pullback_short_tp2 = breakout_stop

    return f"""
{symbol.upper()} Trade Strategies (On-Demand)

📈 Current {symbol.upper()} Price: ${price:,.2f}

✅ Breakout Long
• Entry: Above ${breakout_entry}
• Stop: Below ${breakout_stop}
• TP1: ${breakout_tp1}
• TP2: ${breakout_tp2}

🟩 Pullback Long Zone: ${pullback_zone_low} - ${pullback_zone_high}
• Stop: Below ${pullback_stop}
• TP1: ${pullback_tp1}
• TP2: ${pullback_tp2}

⛔ Breakdown Short
• Entry: Below ${breakdown_entry}
• Stop: Above ${breakout_entry}
• TP1: ${breakout_stop}
• TP2: ${breakdown_tp2}

🔻 Pullback Short Zone: ${pullback_short_zone_low} - ${pullback_short_zone_high}
• Stop: Above ${pullback_short_stop}
• TP1: ${pullback_short_tp1}
• TP2: ${pullback_short_tp2}
"""

# === !trade Command ===
@bot.command()
async def trade(ctx, symbol: str = "ETH"):
    price = fetch_price(symbol)
    message = generate_trade_message(symbol, price)
    await ctx.send(message)

# === !test Command ===
@bot.command()
async def test(ctx):
    await ctx.send("✅ Bot is online and responding to commands.")

# === Start Bot ===
if TOKEN is None:
    print("[FATAL] Discord bot TOKEN not found in environment variables.")
else:
    bot.run(TOKEN)