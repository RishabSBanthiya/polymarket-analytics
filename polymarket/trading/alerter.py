"""
Telegram Alert System for Trade Notifications
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class TradeAlert:
    """Trade alert data"""
    agent_id: str
    market_name: str
    side: str  # BUY or SELL
    amount_usd: float
    price: float
    shares: float
    pnl: Optional[float] = None
    alert_level: AlertLevel = AlertLevel.SUCCESS

    def to_telegram_message(self) -> str:
        """Format as Telegram message with emoji"""
        if self.side == "BUY":
            emoji = "🟢"
            action = "BOUGHT"
        else:
            emoji = "🔴"
            action = "SOLD"

        # Truncate market name if too long
        market = self.market_name[:50] + "..." if len(self.market_name) > 50 else self.market_name

        msg = (
            f"{emoji} <b>TRADE EXECUTED</b>\n\n"
            f"<b>Agent:</b> {self.agent_id}\n"
            f"<b>Action:</b> {action}\n"
            f"<b>Market:</b> {market}\n"
            f"<b>Amount:</b> ${self.amount_usd:.2f}\n"
            f"<b>Price:</b> {self.price:.4f}\n"
            f"<b>Shares:</b> {self.shares:.2f}\n"
        )

        if self.pnl is not None:
            pnl_emoji = "📈" if self.pnl >= 0 else "📉"
            msg += f"<b>PnL:</b> {pnl_emoji} ${self.pnl:.2f}\n"

        msg += f"\n<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        return msg


@dataclass
class SystemAlert:
    """System alert data"""
    title: str
    message: str
    alert_level: AlertLevel = AlertLevel.INFO

    def to_telegram_message(self) -> str:
        """Format as Telegram message"""
        level_emoji = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.SUCCESS: "✅",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.ERROR: "🚨",
        }
        emoji = level_emoji.get(self.alert_level, "ℹ️")

        return (
            f"{emoji} <b>{self.title}</b>\n\n"
            f"{self.message}\n\n"
            f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        )


class TelegramAlerter:
    """Telegram bot alerter for trade notifications"""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.bot_token) and bool(self.chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

        if self.enabled:
            logger.info(f"Telegram alerter enabled (chat_id: {self.chat_id[:4]}...)")
        else:
            if not self.bot_token:
                logger.warning("Telegram alerter disabled: TELEGRAM_BOT_TOKEN not set")
            elif not self.chat_id:
                logger.warning("Telegram alerter disabled: TELEGRAM_CHAT_ID not set")

    async def start(self):
        """Start the alerter background worker"""
        if not self.enabled:
            return

        self._session = aiohttp.ClientSession()
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Telegram alerter started")

    async def stop(self):
        """Stop the alerter"""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()

        logger.info("Telegram alerter stopped")

    async def _worker(self):
        """Background worker to send messages"""
        while True:
            try:
                message = await self._queue.get()
                await self._send_message(message)
                self._queue.task_done()
                # Rate limit: max 1 message per second
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram worker error: {e}")
                await asyncio.sleep(5)

    async def _send_message(self, text: str) -> bool:
        """Send message via Telegram API"""
        if not self._session or not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.debug("Telegram message sent successfully")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Telegram API error: {resp.status} - {error}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_trade_alert(self, alert: TradeAlert):
        """Queue a trade alert for sending"""
        if not self.enabled:
            return
        self._queue.put_nowait(alert.to_telegram_message())

    def send_system_alert(self, alert: SystemAlert):
        """Queue a system alert for sending"""
        if not self.enabled:
            return
        self._queue.put_nowait(alert.to_telegram_message())

    def send_raw(self, message: str):
        """Queue a raw message for sending"""
        if not self.enabled:
            return
        self._queue.put_nowait(message)

    # Convenience methods
    def trade_executed(
        self,
        agent_id: str,
        market_name: str,
        side: str,
        amount_usd: float,
        price: float,
        shares: float,
        pnl: Optional[float] = None,
    ):
        """Send trade execution alert"""
        alert = TradeAlert(
            agent_id=agent_id,
            market_name=market_name,
            side=side,
            amount_usd=amount_usd,
            price=price,
            shares=shares,
            pnl=pnl,
        )
        self.send_trade_alert(alert)

    def bot_started(self, agent_id: str, mode: str = "LIVE"):
        """Send bot started alert"""
        self.send_system_alert(SystemAlert(
            title=f"Bot Started: {agent_id}",
            message=f"Mode: {mode}\nBot is now running and monitoring markets.",
            alert_level=AlertLevel.SUCCESS,
        ))

    def bot_stopped(self, agent_id: str, reason: str = "Manual shutdown"):
        """Send bot stopped alert"""
        self.send_system_alert(SystemAlert(
            title=f"Bot Stopped: {agent_id}",
            message=f"Reason: {reason}",
            alert_level=AlertLevel.WARNING,
        ))

    def error(self, agent_id: str, error_msg: str):
        """Send error alert"""
        self.send_system_alert(SystemAlert(
            title=f"Error: {agent_id}",
            message=error_msg,
            alert_level=AlertLevel.ERROR,
        ))


# Global alerter instance
_alerter: Optional[TelegramAlerter] = None


def get_alerter() -> TelegramAlerter:
    """Get or create the global alerter instance"""
    global _alerter
    if _alerter is None:
        _alerter = TelegramAlerter()
    return _alerter


async def init_alerter() -> TelegramAlerter:
    """Initialize and start the global alerter"""
    alerter = get_alerter()
    await alerter.start()
    return alerter


async def shutdown_alerter():
    """Shutdown the global alerter"""
    global _alerter
    if _alerter:
        await _alerter.stop()
        _alerter = None
