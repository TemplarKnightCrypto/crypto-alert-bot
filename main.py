# === Bot Status Command ===
@bot.command(name='status')
async def status(ctx):
    try:
        embed = discord.Embed(
            title="🤖 Knight's Status Report",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )

        embed.add_field(name="⚙️ Mode", value=CONFIRMATION_MODE.upper(), inline=True)
        embed.add_field(name="📊 Tasks", value="✅ All Running", inline=True)
        embed.add_field(name="🌐 API", value="✅ Connected", inline=True)

        task_status = (
            f"📜 Chronicle: {'✅' if send_market_chronicle.is_running() else '❌'}\n"
            f"⚔️ Signals: {'✅' if scan_trade_alerts.is_running() else '❌'}\n"
            f"🦅 Eagle: {'✅' if trade_100x_scan.is_running() else '❌'}\n"
            f"👁️ Watch: {'✅' if check_camarilla_warning.is_running() else '❌'}\n"
            f"⚡ Battleground: {'✅' if battleground_loop.is_running() else '❌'}"
        )

        embed.add_field(name="🔄 Active Tasks", value=task_status, inline=False)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await ctx.send("❌ Error checking status")

# === Manual Chronicle Trigger ===
@bot.command(name='test_chronicle')
async def test_chronicle(ctx):
    if ctx.author.guild_permissions.administrator:
        try:
            await send_enhanced_scorecard()
            await ctx.send("✅ Chronicle sent to scribes-keep")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
    else:
        await ctx.send("⚠️ Administrator permissions required")

# === Alert Mode Command ===
@bot.command(name='alertmode')
async def alertmode(ctx, mode=None):
    global ALERT_SCORE_THRESHOLD
    modes = {"strict": 5, "balanced": 4, "exploratory": 3}
    if mode in modes:
        ALERT_SCORE_THRESHOLD = modes[mode]
        await ctx.send(f"⚙️ Alert mode set to **{mode.upper()}** (score ≥ {ALERT_SCORE_THRESHOLD})")
    else:
        await ctx.send(
            f"⚙️ Current mode: score ≥ **{ALERT_SCORE_THRESHOLD}**\n"
            f"Use: `!alertmode strict` | `balanced` | `exploratory`"
        )

# === Bot Startup ===
@bot.event
async def on_ready():
    logger.info(f"🟢 Bot logged in as {bot.user}")
    logger.info(f"⚙️ Alert Mode: score ≥ {ALERT_SCORE_THRESHOLD}")
    try:
        if not scan_trade_alerts.is_running():
            scan_trade_alerts.start()
        if not send_market_chronicle.is_running():
            send_market_chronicle.start()
        if not trade_100x_scan.is_running():
            trade_100x_scan.start()
        if not check_camarilla_warning.is_running():
            check_camarilla_warning.start()
        if not battleground_loop.is_running():
            battleground_loop.start()
        if not heartbeat.is_running():
            heartbeat.start()
        if not performance_report.is_running():
            performance_report.start()

        embed = discord.Embed(
            title="🏰 Control Tower Activated",
            description="*Trade scanning and alert systems are online.*",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.add_field(name="⚙️ Current Alert Mode", value=f"Score ≥ {ALERT_SCORE_THRESHOLD}", inline=True)
        embed.add_field(name="📡 Strategy", value="Camarilla + RSI/Volume/Trend Confluence", inline=True)
        ct = embed.timestamp.astimezone(CENTRAL_TZ).strftime('%I:%M %p CT')
        utc = embed.timestamp.strftime('%H:%M UTC')
        embed.set_footer(text=f"🕒 {utc} | {ct}")

        channel = bot.get_channel(SCROLLS_ORDER_ID)
        if channel:
            await channel.send(embed=embed)

    except Exception as e:
        logger.error(f"Error in on_ready: {e}")

# === Bot Runner ===
def start_bot():
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")

if __name__ == "__main__":
    logger.info("🚀 Starting Control Tower ETH Camarilla Bot...")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server running in background")
    start_bot()
