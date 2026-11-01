import asyncio
import logging
import os

from aiohttp import web
from pyrogram import Client, idle

from info import API_ID, API_HASH, BOT_TOKEN, DATABASE_URI, SESSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

LOGGER = logging.getLogger(__name__)
app = None


async def create_web_app():
    """Create the Render health-check web application."""
    web_app = web.Application()

    async def health_check(request):
        return web.Response(text="DreamxBotz is running!", content_type="text/plain")

    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)
    return web_app


async def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    web_app = await create_web_app()
    runner = web.AppRunner(web_app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
    except Exception:
        await runner.cleanup()
        raise

    LOGGER.info("Web Server started on 0.0.0.0:%s", port)
    return runner


async def start_bot():
    global app
    LOGGER.info("Initializing DreamxBotz...")

    missing = []
    if API_ID <= 0:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not DATABASE_URI:
        missing.append("DATABASE_URI")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    app = Client(
        SESSION,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        plugins={"root": "plugins"},
        workdir=".",
    )

    history_client = None
    runner = None
    app_started = False
    try:
        app.history_client = None
        history_session = os.environ.get("HISTORY_SESSION", "").strip()
        if history_session:
            try:
                history_client = Client(
                    "history_reader",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    session_string=history_session,
                    workdir=".",
                )
                await history_client.start()
                if (await history_client.get_me()).is_bot:
                    raise RuntimeError(
                        "HISTORY_SESSION must belong to a Telegram user"
                    )
                app.history_client = history_client
                LOGGER.info("Telegram history reader started")
            except Exception:
                LOGGER.exception(
                    "History reader could not start; old-post cleaning "
                    "is unavailable"
                )
                if history_client is not None:
                    try:
                        await history_client.stop()
                    except Exception:
                        pass
                history_client = None
        else:
            LOGGER.warning(
                "HISTORY_SESSION is not configured; old-post cleaning "
                "requires a Telegram user session"
            )

        await app.start()
        app_started = True
        bot_info = await app.get_me()
        LOGGER.info("Bot started: %s (@%s)", bot_info.first_name, bot_info.username)

        runner = await start_web_server()
        LOGGER.info("Bot is now running and listening for messages...")
        await idle()
    finally:
        LOGGER.info("Stopping bot...")
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                LOGGER.exception("Web server cleanup failed")
        if history_client is not None:
            try:
                await history_client.stop()
            except Exception:
                LOGGER.exception("History reader stop failed")
        if app_started:
            try:
                await app.stop()
            except Exception:
                LOGGER.exception("Bot stop failed")


if __name__ == "__main__":
    asyncio.run(start_bot())
