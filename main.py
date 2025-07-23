import os
import threading
import requests
from flask import Flask
from dotenv import load_dotenv

import discord
from discord.ext import commands

# === Load Environment Variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")

# === Flask Uptime Server ===
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is live!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

threading.Thread(target=run_flask).start()

# === Discord Bot Setup ===
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# === Supported Coins ===
COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple",
    "BNB": "binancecoin", "SOL": "solana", "DOGE": "dogecoin",
    "ADA": "cardano", "SUI": "sui", "HBAR": "hedera-hashgraph",
    "AVAX": "avalanche-2", "PNIC": "phoenic-token"
}

# === Price Fetching ===
def fetch_price(symbol):
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        response = requests.get(url)
        data = response.json()
        return data[coin_id]['usd']
    except Exception as e:
        print(f"[ERROR] Failed to fetch {symbol}: {e}")
        return None

# === Trade Strategy Message ===
def generate_trade_message(symbol, price):
    if price is None:
        return "❌ Failed to fetch price."

    breakout_entry = round(price * 1.01, 2)
    breakout_stop = round(price * 0.97, 2)
    breakout_tp1 = round(price * 1.006, 2)
    breakout_tp2 = round(price * 1.03, 2)

    pullback_zone = (round(price * 0.965, 2), round(price * 0.975, 2))
    pullback_stop = round(price * 0.96, 2)

    breakdown_entry = round(price * 0.99, 2)
    breakdown_tp2 = round(price * 0.94, 2)

    short_pullback_zone = (round(price * 1.025, 2), round(price * 1.03, 2))
    short_stop = round(price * 1.035, 2)

    return f"""
{symbol.upper()} Trade Strategies (On-Demand)

📈 Current Price: ${price:,.2f}

✅ Breakout Long
• Entry: Above ${breakout_entry}
• Stop: Below ${breakout_stop}
• TP1: ${breakout_tp1}
• TP2: ${breakout_tp2}

🟩 Pullback Long Zone: ${pullback_zone[0]} - ${pullback_zone[1]}
• Stop: Below ${pullback_stop}
• TP1: ${breakout_tp1}
• TP2: ${breakout_tp2}

⛔ Breakdown Short
• Entry: Below ${breakdown_entry}
• Stop: Above ${breakout_entry}
• TP1: ${breakout_stop}
• TP2: ${breakdown_tp2}

🔻 Pullback Short Zone: ${short_pullback_zone[0]} - ${short_pullback_zone[1]}
• Stop: Above ${short_stop}
• TP1: ${breakdown_entry}
• TP2: ${breakout_stop}
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
    await ctx.send("✅ Bot is online and responding!")

# === Start Bot ===
if TOKEN:
    bot.run(TOKEN)
else:
    print("[FATAL] Discord bot TOKEN not found in environment variables.")
