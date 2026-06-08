"""
Polymarket multi-agent trading bot — entry point.

Configuration: copy ``config.json.example`` → ``config.json`` (database URL, session secret, bootstrap admin).
Bot parameters and API keys live in the SQLite DB and are edited in the Admin panel (not .env).

Optional: ``--ui`` overrides ``ui_mode`` from DB for one run.
"""

import argparse
import asyncio
import logging
import shutil
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("polymarket")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def run(ui_mode_override: str | None = None):
    from bot.config_file import config_path, ensure_upload_dir, project_root
    from bot.db.bootstrap import init_database

    ex = project_root() / "config.json.example"
    cp = config_path()
    if not cp.exists() and ex.exists():
        shutil.copy(ex, cp)
        logging.getLogger("polymarket").warning("Created %s from example — edit session_secret", cp)

    (project_root() / "data").mkdir(parents=True, exist_ok=True)
    cfg = init_database()
    ensure_upload_dir(cfg)

    import server
    from bot.orchestrator import TradingBot
    from bot.terminal_ui import run_terminal_dashboard

    bot = TradingBot()
    s = bot.settings
    if ui_mode_override:
        s.ui_mode = ui_mode_override.lower()
        if s.ui_mode not in ("both", "dashboard", "terminal"):
            s.ui_mode = "both"

    logger.info("=" * 60)
    logger.info("  POLYMARKET MULTI-AGENT BOT")
    logger.info("=" * 60)
    logger.info("  UI_MODE:   %s", s.ui_mode)
    logger.info("  Trade:     %s", "DRY RUN" if s.dry_run else "LIVE")
    logger.info(
        "  Agents:    copy=%s  weather=%s",
        s.agent_copy,
        s.agent_weather,
    )
    if s.ui_mode in ("dashboard", "both"):
        logger.info("  Web UI:    http://%s:%s", s.host, s.port)
    if s.ui_mode in ("terminal", "both"):
        logger.info("  Terminal:  Rich live dashboard (this console)")
    logger.info("=" * 60)

    success = await bot.initialize()
    if not success:
        logger.error("CLOB not ready yet — set keys in Admin → Save → bot will retry every ~12s")
        logger.error("Errors: %s", bot.state.errors)

    server.app.state.trader = bot
    server.trader = bot

    tasks: list[asyncio.Task] = []
    web_server = None

    if s.ui_mode in ("dashboard", "both"):
        import uvicorn

        web_server = uvicorn.Server(
            uvicorn.Config(
                app=server.app,
                host=s.host,
                port=s.port,
                log_level="warning",
            )
        )
        tasks.append(asyncio.create_task(web_server.serve(), name="web"))

    if s.ui_mode in ("terminal", "both"):
        if s.ui_mode == "both":
            logging.getLogger("polymarket.orchestrator").setLevel(logging.WARNING)
        tasks.append(
            asyncio.create_task(run_terminal_dashboard(bot, 3.0), name="terminal")
        )

    tasks.append(asyncio.create_task(bot.run_forever(), name="trader"))

    if not tasks:
        logger.error("Nothing to run — enable UI_MODE=dashboard|terminal|both")
        await bot.aclose()
        return

    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Shutdown requested")
        bot.stop()
        for t in tasks:
            if not t.done():
                t.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, shutdown)
        loop.add_signal_handler(signal.SIGTERM, shutdown)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: shutdown())
        signal.signal(signal.SIGTERM, lambda *_: shutdown())

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Task failed: %s", r, exc_info=r)
    finally:
        await bot.aclose()


def main():
    p = argparse.ArgumentParser(description="Polymarket multi-agent bot")
    p.add_argument(
        "--ui",
        choices=("dashboard", "terminal", "both"),
        default=None,
        help="Override UI_MODE for this run",
    )
    args = p.parse_args()
    try:
        asyncio.run(run(ui_mode_override=args.ui))
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.error("Fatal: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
