"""
Trading command handlers for Telegram.

Commands:
    /search <query> - Search markets by name
    /positions - Show open positions
    /buy - Buy after search selection
    /sell - Sell a position
    /cancel - Cancel pending trade
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, TYPE_CHECKING

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

if TYPE_CHECKING:
    from ..bot import TelegramControlBot

logger = logging.getLogger(__name__)

# Conversation states
AWAITING_AMOUNT = 1
AWAITING_PRICE = 2
AWAITING_CONFIRMATION = 3


@dataclass
class PendingTrade:
    """A trade awaiting confirmation."""
    user_id: int
    chat_id: int
    token_id: str
    market_id: str
    question: str
    outcome: str
    side: str  # BUY or SELL
    amount_usd: Optional[float] = None
    price: Optional[float] = None
    shares: Optional[float] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.now(timezone.utc) > self.expires_at


# Global state for pending trades and search results
_pending_trades: Dict[int, PendingTrade] = {}
_search_results: Dict[int, List[dict]] = {}


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search markets by name."""
    bot: "TelegramControlBot" = context.bot_data["control_bot"]

    if not context.args:
        await update.message.reply_text(
            "Usage: /search <query>\n"
            "Example: /search manchester united"
        )
        return

    query = " ".join(context.args).lower()
    await update.message.reply_text(f"Searching for '{query}'...")

    try:
        markets = await bot.search_markets(query, limit=10)

        if not markets:
            await update.message.reply_text(f"No markets found for '{query}'")
            return

        # Store search results for this user
        user_id = update.effective_user.id
        _search_results[user_id] = markets

        # Format results with inline buttons
        lines = [f"<b>Found {len(markets)} markets:</b>", ""]

        keyboard = []
        for i, market in enumerate(markets):
            question = market["question"][:60]
            if len(market["question"]) > 60:
                question += "..."

            # Show each outcome with price
            for token in market.get("tokens", []):
                outcome = token.get("outcome", "Yes")
                price = token.get("price", 0)
                token_id = token.get("token_id", "")

                lines.append(f"{i+1}. {question}")
                lines.append(f"   <b>{outcome}</b>: ${price:.2f}")

                # Add buy/sell buttons for this outcome
                keyboard.append([
                    InlineKeyboardButton(
                        f"Buy {outcome} @ ${price:.2f}",
                        callback_data=f"buy:{token_id}:{i}"
                    ),
                    InlineKeyboardButton(
                        f"Sell {outcome}",
                        callback_data=f"sell:{token_id}:{i}"
                    ),
                ])
            lines.append("")

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        logger.error(f"Error searching markets: {e}")
        await update.message.reply_text(f"Error: {e}")


async def callback_trade_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle buy/sell button clicks from search results."""
    query = update.callback_query
    await query.answer()

    bot: "TelegramControlBot" = context.bot_data["control_bot"]
    user_id = update.effective_user.id

    # Parse callback data: buy:token_id:index or sell:token_id:index
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.edit_message_text("Invalid selection")
        return ConversationHandler.END

    side = parts[0].upper()
    token_id = parts[1]
    market_idx = int(parts[2])

    # Get market from search results
    markets = _search_results.get(user_id, [])
    if not markets or market_idx >= len(markets):
        await query.edit_message_text("Search results expired. Please search again.")
        return ConversationHandler.END

    market = markets[market_idx]

    # Find the token
    token = None
    for t in market.get("tokens", []):
        if t.get("token_id") == token_id:
            token = t
            break

    if not token:
        await query.edit_message_text("Token not found. Please search again.")
        return ConversationHandler.END

    # Get current price
    try:
        current_price = await bot.get_token_price(token_id)
    except Exception as e:
        current_price = token.get("price", 0)

    # Create pending trade
    pending = PendingTrade(
        user_id=user_id,
        chat_id=update.effective_chat.id,
        token_id=token_id,
        market_id=market.get("condition_id", ""),
        question=market.get("question", ""),
        outcome=token.get("outcome", ""),
        side=side,
        price=current_price,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
    )
    _pending_trades[user_id] = pending

    # Ask for amount
    await query.edit_message_text(
        f"<b>{side} Order</b>\n\n"
        f"Market: {pending.question[:60]}...\n"
        f"Outcome: {pending.outcome}\n"
        f"Current Price: ${current_price:.4f}\n\n"
        f"Enter amount in USD (e.g., 50):",
        parse_mode="HTML",
    )

    return AWAITING_AMOUNT


async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle amount input for trade."""
    bot: "TelegramControlBot" = context.bot_data["control_bot"]
    user_id = update.effective_user.id

    pending = _pending_trades.get(user_id)
    if not pending or pending.is_expired():
        await update.message.reply_text("Trade session expired. Please start again.")
        return ConversationHandler.END

    # Parse amount
    try:
        amount_text = update.message.text.strip().replace("$", "").replace(",", "")
        amount = float(amount_text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if amount > 10000:
            raise ValueError("Amount too large (max $10,000)")
    except ValueError as e:
        await update.message.reply_text(f"Invalid amount: {e}\nPlease enter a valid USD amount:")
        return AWAITING_AMOUNT

    pending.amount_usd = amount

    # Get fresh price
    try:
        current_price = await bot.get_token_price(pending.token_id)
        pending.price = current_price
    except Exception as e:
        logger.warning(f"Failed to get fresh price: {e}")

    # Calculate shares
    if pending.price and pending.price > 0:
        pending.shares = amount / pending.price
    else:
        pending.shares = 0

    # Ask for price (limit order)
    await update.message.reply_text(
        f"Current price: ${pending.price:.4f}\n\n"
        f"Enter your limit price (or 'market' for current price):",
    )

    return AWAITING_PRICE


async def handle_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle limit price input for trade."""
    user_id = update.effective_user.id

    pending = _pending_trades.get(user_id)
    if not pending or pending.is_expired():
        await update.message.reply_text("Trade session expired. Please start again.")
        return ConversationHandler.END

    price_text = update.message.text.strip().lower()

    if price_text == "market":
        # Use current price
        pass
    else:
        try:
            price = float(price_text.replace("$", ""))
            if price <= 0 or price >= 1:
                raise ValueError("Price must be between 0 and 1")
            pending.price = price
            pending.shares = pending.amount_usd / price
        except ValueError as e:
            await update.message.reply_text(
                f"Invalid price: {e}\n"
                "Enter a price between 0.01 and 0.99 (or 'market'):"
            )
            return AWAITING_PRICE

    # Show confirmation
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data="confirm_trade"),
            InlineKeyboardButton("Cancel", callback_data="cancel_trade"),
        ]
    ])

    await update.message.reply_text(
        f"<b>Confirm {pending.side} Order</b>\n\n"
        f"Market: {pending.question[:50]}...\n"
        f"Outcome: {pending.outcome}\n"
        f"Amount: ${pending.amount_usd:.2f}\n"
        f"Price: ${pending.price:.4f}\n"
        f"Shares: {pending.shares:.2f}\n\n"
        f"<b>Total Cost: ${pending.amount_usd:.2f}</b>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    return AWAITING_CONFIRMATION


async def callback_confirm_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute the confirmed trade."""
    query = update.callback_query
    await query.answer()

    bot: "TelegramControlBot" = context.bot_data["control_bot"]
    user_id = update.effective_user.id

    pending = _pending_trades.get(user_id)
    if not pending or pending.is_expired():
        await query.edit_message_text("Trade session expired. Please start again.")
        return ConversationHandler.END

    await query.edit_message_text("Executing trade...")

    try:
        result = await bot.execute_trade(
            token_id=pending.token_id,
            side=pending.side,
            amount_usd=pending.amount_usd,
            price=pending.price,
        )

        if result.get("success"):
            filled_shares = result.get("filled_shares", pending.shares)
            filled_price = result.get("filled_price", pending.price)
            order_id = result.get("order_id", "N/A")

            await query.edit_message_text(
                f"<b>Trade Executed!</b>\n\n"
                f"Order ID: {order_id}\n"
                f"Side: {pending.side}\n"
                f"Shares: {filled_shares:.2f}\n"
                f"Price: ${filled_price:.4f}\n"
                f"Total: ${filled_shares * filled_price:.2f}",
                parse_mode="HTML",
            )
        else:
            error = result.get("error", "Unknown error")
            await query.edit_message_text(f"Trade failed: {error}")

    except Exception as e:
        logger.error(f"Trade execution error: {e}")
        await query.edit_message_text(f"Error: {e}")

    # Clean up
    if user_id in _pending_trades:
        del _pending_trades[user_id]

    return ConversationHandler.END


async def callback_cancel_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the pending trade."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if user_id in _pending_trades:
        del _pending_trades[user_id]

    await query.edit_message_text("Trade cancelled.")
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any pending trade."""
    user_id = update.effective_user.id

    if user_id in _pending_trades:
        del _pending_trades[user_id]
        await update.message.reply_text("Pending trade cancelled.")
    else:
        await update.message.reply_text("No pending trade to cancel.")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show open positions."""
    bot: "TelegramControlBot" = context.bot_data["control_bot"]

    try:
        positions = await bot.get_positions()

        if not positions:
            await update.message.reply_text("No open positions.")
            return

        lines = ["<b>Open Positions</b>", ""]

        total_value = 0
        total_pnl = 0

        for pos in positions:
            shares = pos.get("shares", 0)
            entry_price = pos.get("avg_price", 0) or pos.get("entry_price", 0)
            current_price = pos.get("current_price", entry_price)
            value = shares * current_price
            pnl = (current_price - entry_price) * shares
            pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0

            total_value += value
            total_pnl += pnl

            pnl_emoji = "📈" if pnl >= 0 else "📉"
            outcome = pos.get("outcome", "")[:20]

            lines.append(f"<b>{outcome}</b>")
            lines.append(f"  Shares: {shares:.2f} @ ${entry_price:.4f}")
            lines.append(f"  Value: ${value:.2f} ({pnl_emoji} ${pnl:.2f} / {pnl_pct:+.1f}%)")
            lines.append("")

        lines.append(f"<b>Total Value: ${total_value:.2f}</b>")
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        lines.append(f"<b>Total PnL: {pnl_emoji} ${total_pnl:.2f}</b>")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        await update.message.reply_text(f"Error: {e}")


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected input during conversation."""
    await update.message.reply_text(
        "I didn't understand that. Use /cancel to cancel the current trade."
    )
    return ConversationHandler.END


def register_trading_handlers(app) -> None:
    """Register trading command handlers."""
    # Search command
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Conversation handler for trade flow
    trade_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_trade_select, pattern=r"^(buy|sell):"),
        ],
        states={
            AWAITING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input),
            ],
            AWAITING_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price_input),
            ],
            AWAITING_CONFIRMATION: [
                CallbackQueryHandler(callback_confirm_trade, pattern=r"^confirm_trade$"),
                CallbackQueryHandler(callback_cancel_trade, pattern=r"^cancel_trade$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(filters.ALL, fallback_handler),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )
    app.add_handler(trade_conv)
