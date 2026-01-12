"""
Telegram Control Bot for Polymarket Analytics.

Provides remote control of trading bots and manual trade execution.
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

from telegram import Update
from telegram.ext import Application, ContextTypes, CommandHandler
from telegram.request import HTTPXRequest

from ..core.config import Config, get_config
from ..core.api import PolymarketAPI
from ..trading.storage.sqlite import SQLiteStorage
from .subprocess_manager import SubprocessManager
from .handlers import register_bot_control_handlers, register_trading_handlers

logger = logging.getLogger(__name__)


class TelegramControlBot:
    """
    Telegram bot for controlling Polymarket trading bots.

    Features:
    - Start/stop trading bots
    - View wallet and bot status
    - Search markets and place manual trades
    - View positions
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        config: Optional[Config] = None,
        db_path: str = "data/risk_state.db",
    ):
        """
        Initialize Telegram control bot.

        Args:
            token: Telegram bot token from @BotFather
            chat_id: Authorized chat ID for commands
            config: Polymarket configuration
            db_path: Path to SQLite database
        """
        self.token = token
        self.chat_id = chat_id
        self.config = config or get_config()
        self.db_path = db_path

        # Components
        self.storage = SQLiteStorage(db_path)
        self.subprocess_manager = SubprocessManager(
            pid_file="data/bot_pids.json",
            db_path=db_path,
        )
        self.api: Optional[PolymarketAPI] = None
        self._clob_client = None

        # Telegram application
        self.app: Optional[Application] = None

    async def start(self) -> None:
        """Start the Telegram bot."""
        logger.info("Starting Telegram control bot...")

        # Initialize API
        self.api = PolymarketAPI(self.config)
        await self.api.connect()
        logger.info("API connected")

        # Build Telegram application with extended timeouts
        request = HTTPXRequest(
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
        )
        self.app = (
            Application.builder()
            .token(self.token)
            .request(request)
            .get_updates_request(request)
            .build()
        )

        # Store reference to self for handlers
        self.app.bot_data["control_bot"] = self

        # Register handlers
        register_bot_control_handlers(self.app)
        register_trading_handlers(self.app)

        # Add authorization check
        self.app.add_handler(
            CommandHandler("id", self._cmd_id),
            group=-1,  # Run before other handlers
        )

        # Start polling
        logger.info(f"Starting Telegram polling (authorized chat: {self.chat_id})")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

        logger.info("Telegram control bot started")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        logger.info("Stopping Telegram control bot...")

        if self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.warning(f"Error during shutdown: {e}")

        if self.api:
            await self.api.close()

        logger.info("Telegram control bot stopped")

    async def run_forever(self) -> None:
        """Run the bot until interrupted."""
        try:
            # Keep running
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show chat ID (useful for setup)."""
        chat_id = str(update.effective_chat.id)
        await update.message.reply_text(f"Chat ID: {chat_id}")

    def _is_authorized(self, update: Update) -> bool:
        """Check if the update is from an authorized chat."""
        chat_id = str(update.effective_chat.id)
        return chat_id == self.chat_id

    # ==================== Wallet Methods ====================

    async def get_wallet_state(self) -> Dict[str, Any]:
        """Get current wallet state."""
        with self.storage.transaction() as txn:
            wallet_state = txn.get_wallet_state(self.config.proxy_address)

        return {
            "usdc_balance": wallet_state.usdc_balance,
            "positions_value": wallet_state.total_positions_value,
            "total_equity": wallet_state.usdc_balance + wallet_state.total_positions_value,
            "reserved": wallet_state.total_reserved,
            "available": wallet_state.available_capital,
        }

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        with self.storage.transaction() as txn:
            positions = txn.get_api_positions(self.config.proxy_address)
        return positions

    # ==================== Market Methods ====================

    async def search_markets(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search markets by name.

        Args:
            query: Search query (case-insensitive)
            limit: Maximum results to return

        Returns:
            List of matching markets with tokens
        """
        if not self.api:
            raise RuntimeError("API not connected")

        # Fetch all markets
        all_markets = await self.api.fetch_all_markets(max_markets=2000)

        # Filter by query
        query_lower = query.lower()
        matches = []

        for market_raw in all_markets:
            question = market_raw.get("question", "")
            if query_lower in question.lower():
                # Parse market
                market = self.api.parse_market(market_raw)
                if market and not market.closed and not market.resolved:
                    matches.append({
                        "condition_id": market.condition_id,
                        "question": market.question,
                        "slug": market.slug,
                        "tokens": [
                            {
                                "token_id": t.token_id,
                                "outcome": t.outcome,
                                "price": t.price,
                            }
                            for t in market.tokens
                        ],
                    })

                if len(matches) >= limit:
                    break

        return matches

    async def get_token_price(self, token_id: str) -> float:
        """Get current price for a token."""
        if not self.api:
            raise RuntimeError("API not connected")

        orderbook = await self.api.fetch_orderbook(token_id)
        if orderbook and orderbook.get("bids") and orderbook.get("asks"):
            best_bid = float(orderbook["bids"][0]["price"]) if orderbook["bids"] else 0
            best_ask = float(orderbook["asks"][0]["price"]) if orderbook["asks"] else 1
            return (best_bid + best_ask) / 2

        return 0

    # ==================== Trading Methods ====================

    def _get_clob_client(self):
        """Get or create CLOB client."""
        if self._clob_client is None:
            from py_clob_client.client import ClobClient
            from py_clob_client.http_helpers import helpers as http_helpers

            # Monkey-patch the User-Agent to avoid Cloudflare blocks
            original_overload = http_helpers.overloadHeaders
            def patched_overload(method: str, headers: dict) -> dict:
                headers = original_overload(method, headers)
                headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                return headers
            http_helpers.overloadHeaders = patched_overload

            self._clob_client = ClobClient(
                self.config.clob_host,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                signature_type=2,
                funder=self.config.proxy_address,
            )
            self._clob_client.set_api_creds(
                self._clob_client.create_or_derive_api_creds()
            )

        return self._clob_client

    async def execute_trade(
        self,
        token_id: str,
        side: str,
        amount_usd: float,
        price: float,
    ) -> Dict[str, Any]:
        """
        Execute a limit order trade.

        Args:
            token_id: Token ID to trade
            side: BUY or SELL
            amount_usd: Amount in USD
            price: Limit price (0-1)

        Returns:
            Result dict with success, order_id, filled_shares, etc.
        """
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY, SELL

        try:
            client = self._get_clob_client()

            # Calculate shares
            shares = amount_usd / price if price > 0 else 0

            # Build order
            clob_side = BUY if side.upper() == "BUY" else SELL

            order_args = OrderArgs(
                price=price,
                size=shares,
                side=clob_side,
                token_id=token_id,
            )

            # Create and post order
            signed_order = client.create_order(order_args)
            from py_clob_client.clob_types import OrderType
            response = client.post_order(signed_order, OrderType.GTC)

            # Parse response
            success = False
            order_id = ""
            error = ""

            if isinstance(response, dict):
                success = response.get("success", False)
                order_id = response.get("orderID", "")
                error = response.get("errorMsg", "")
            else:
                success = getattr(response, "success", False)
                order_id = getattr(response, "order_id", "")

            if success:
                logger.info(f"Trade executed: {side} {shares:.2f} @ ${price:.4f}")
                return {
                    "success": True,
                    "order_id": order_id,
                    "filled_shares": shares,
                    "filled_price": price,
                }
            else:
                logger.error(f"Trade failed: {error}")
                return {
                    "success": False,
                    "error": error or "Order placement failed",
                }

        except Exception as e:
            logger.error(f"Trade execution error: {e}")
            return {
                "success": False,
                "error": str(e),
            }
