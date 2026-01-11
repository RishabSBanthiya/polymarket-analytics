#!/usr/bin/env python3
"""
Telegram Control Bot - Standalone process for controlling trading bots.

Features:
- Start/stop trading bots (bond, flow, arb, stat_arb)
- View wallet and bot status
- Search markets and place manual trades
- View positions

Usage:
    python scripts/run_telegram_control.py

Environment Variables:
    TELEGRAM_BOT_TOKEN - Bot token from @BotFather
    TELEGRAM_CHAT_ID - Your chat ID (use /id command to find it)

Setup:
    1. Create a bot via @BotFather on Telegram
    2. Get your chat ID by sending /id to the bot after running
    3. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env
    4. Run: python scripts/run_telegram_control.py

Commands:
    /help - Show available commands
    /status - Wallet and bot status
    /bots - List registered bots
    /start_bot <type> - Start a bot (bond/flow/arb/stat_arb)
    /stop_bot <agent_id> - Stop a running bot
    /search <query> - Search markets
    /positions - Show open positions
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

# Load environment variables
load_dotenv(project_root / ".env")

from polymarket.core.config import get_config
from polymarket.telegram.bot import TelegramControlBot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Reduce noise from libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


async def main():
    """Main entry point."""
    # Get credentials from environment
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        logger.info("Create a bot via @BotFather and add the token to .env")
        sys.exit(1)

    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not set")
        logger.info("Starting bot - use /id command to get your chat ID")
        logger.info("Then add TELEGRAM_CHAT_ID to .env and restart")
        chat_id = "0"  # Will respond to /id from any chat

    try:
        config = get_config()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)

    # Create bot
    bot = TelegramControlBot(
        token=bot_token,
        chat_id=chat_id,
        config=config,
    )

    # Setup signal handlers
    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(bot.stop())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Start and run
    try:
        await bot.start()
        logger.info("=" * 60)
        logger.info("Telegram Control Bot is running!")
        logger.info("=" * 60)
        logger.info(f"Authorized chat ID: {chat_id}")
        logger.info("Use /help in Telegram to see available commands")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        await bot.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
