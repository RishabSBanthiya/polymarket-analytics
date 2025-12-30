"""
Risk monitoring API endpoints.
"""

import logging
from fastapi import APIRouter, Query
from typing import Optional, List
from datetime import datetime, timezone

from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.core.config import get_config
from polymarket.core.api import PolymarketAPI
from polymarket.core.models import AgentStatus, PositionStatus, ReservationStatus

logger = logging.getLogger(__name__)

router = APIRouter()
config = get_config()
storage = SQLiteStorage(config.db_path)


@router.get("/status")
async def get_risk_status():
    """Get overall risk status - wallet state, exposure, limits."""
    wallet = config.proxy_address or ""
    
    with storage.transaction() as txn:
        wallet_state = txn.get_wallet_state(wallet)
        all_agents = txn.get_all_agents()
    
    # Calculate exposure percentage
    total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
    exposure_pct = wallet_state.total_exposure / total_equity if total_equity > 0 else 0
    
    # Count active agents
    active_agents = len([a for a in all_agents if a.status == AgentStatus.ACTIVE])
    wallet_agents = len([a for a in wallet_state.agents if a.status == AgentStatus.ACTIVE])
    
    # Count positions by status
    open_positions = len([p for p in wallet_state.positions if p.status == PositionStatus.OPEN])
    orphan_positions = len([p for p in wallet_state.positions if p.status == PositionStatus.ORPHAN])
    
    # Active reservations
    active_reservations = len([r for r in wallet_state.reservations if r.is_active])
    
    return {
        "wallet_address": wallet,
        "usdc_balance": wallet_state.usdc_balance,
        "positions_value": wallet_state.total_positions_value,
        "total_reserved": wallet_state.total_reserved,
        "available_capital": wallet_state.available_capital,
        "total_exposure": wallet_state.total_exposure,
        "exposure_pct": exposure_pct,
        "total_equity": total_equity,
        "active_agents": active_agents,
        "wallet_active_agents": wallet_agents,
        "open_positions": open_positions,
        "orphan_positions": orphan_positions,
        "active_reservations": active_reservations,
        "limits": {
            "max_daily_drawdown_pct": config.risk.max_daily_drawdown_pct,
            "max_total_drawdown_pct": config.risk.max_total_drawdown_pct,
            "max_trade_value_usd": config.risk.max_trade_value_usd,
            "max_wallet_exposure_pct": config.risk.max_wallet_exposure_pct,
            "max_per_market_exposure_pct": config.risk.max_per_market_exposure_pct,
        }
    }


@router.get("/agents")
async def get_agents():
    """Get all registered agents with their status."""
    with storage.transaction() as txn:
        agents = txn.get_all_agents()
    
    now = datetime.now(timezone.utc)
    
    return [
        {
            "agent_id": a.agent_id,
            "agent_type": a.agent_type,
            "status": a.status.value,
            "started_at": a.started_at.isoformat(),
            "last_heartbeat": a.last_heartbeat.isoformat(),
            "seconds_since_heartbeat": a.seconds_since_heartbeat,
            "is_healthy": a.seconds_since_heartbeat < 60,  # Healthy if heartbeat < 60s ago
        }
        for a in agents
    ]


@router.get("/positions")
async def get_risk_positions():
    """Get all open/orphan positions with their values."""
    wallet = config.proxy_address or ""
    
    with storage.transaction() as txn:
        open_positions = txn.get_all_positions(wallet, PositionStatus.OPEN)
        orphan_positions = txn.get_all_positions(wallet, PositionStatus.ORPHAN)
    
    positions = open_positions + orphan_positions
    
    return [
        {
            "id": p.id,
            "agent_id": p.agent_id,
            "token_id": p.token_id,
            "market_id": p.market_id,
            "shares": p.shares,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "cost_basis": p.cost_basis,
            "current_value": p.current_value,
            "unrealized_pnl": p.unrealized_pnl,
            "unrealized_pnl_pct": p.unrealized_pnl_pct,
            "status": p.status.value,
            "is_orphan": p.status == PositionStatus.ORPHAN,
        }
        for p in positions
    ]


@router.get("/reservations")
async def get_reservations():
    """Get active capital reservations."""
    wallet = config.proxy_address or ""
    
    with storage.transaction() as txn:
        reservations = txn.get_all_reservations(wallet, ReservationStatus.PENDING)
    
    now = datetime.now(timezone.utc)
    
    return [
        {
            "id": r.id,
            "agent_id": r.agent_id,
            "market_id": r.market_id,
            "token_id": r.token_id,
            "amount_usd": r.amount_usd,
            "reserved_at": r.reserved_at.isoformat(),
            "expires_at": r.expires_at.isoformat(),
            "expires_in_seconds": (r.expires_at - now).total_seconds(),
            "status": r.status.value,
            "is_active": r.is_active,
        }
        for r in reservations
        if r.is_active
    ]


@router.get("/drawdown")
async def get_drawdown_status():
    """Get drawdown status and limits."""
    wallet = config.proxy_address or ""
    
    with storage.transaction() as txn:
        wallet_state = txn.get_wallet_state(wallet)
    
    total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
    
    return {
        "current_equity": total_equity,
        "usdc_balance": wallet_state.usdc_balance,
        "positions_value": wallet_state.total_positions_value,
        "limits": {
            "max_daily_drawdown_pct": config.risk.max_daily_drawdown_pct,
            "max_total_drawdown_pct": config.risk.max_total_drawdown_pct,
        },
        "note": "Historical drawdown tracking requires the trading bot to be running."
    }


@router.post("/cleanup")
async def cleanup_stale_data():
    """Cleanup expired reservations and stale agents."""
    with storage.transaction() as txn:
        expired_res = txn.cleanup_expired_reservations()
        stale_agents = txn.cleanup_stale_agents(
            config.risk.stale_agent_threshold_seconds
        )
    
    return {
        "expired_reservations_cleaned": expired_res,
        "stale_agents_marked_crashed": stale_agents,
    }


@router.post("/refresh")
async def refresh_wallet_data():
    """
    Refresh wallet data from blockchain and API.
    
    Fetches actual USDC balance from Polygon blockchain and
    syncs positions with Polymarket Data API.
    """
    wallet = config.proxy_address or ""
    
    if not wallet:
        return {
            "success": False,
            "error": "No wallet address configured",
            "usdc_balance": 0,
            "positions_synced": 0,
        }
    
    api = PolymarketAPI(config)
    
    try:
        await api.connect()
        
        # Fetch actual USDC balance from blockchain
        actual_balance = await api.fetch_usdc_balance(wallet)
        logger.info(f"Fetched actual USDC balance: ${actual_balance:.2f}")
        
        # Fetch actual positions from API
        actual_positions = await api.fetch_positions(wallet)
        logger.info(f"Fetched {len(actual_positions)} positions from API")
        
        # Update database
        with storage.transaction() as txn:
            # Update cached balance
            txn.update_usdc_balance(wallet, actual_balance)
            
            # Build lookup of current positions by token_id
            db_open = txn.get_all_positions(wallet, PositionStatus.OPEN)
            db_orphan = txn.get_all_positions(wallet, PositionStatus.ORPHAN)
            db_positions = db_open + db_orphan
            db_token_ids = {p.token_id: p for p in db_positions}
            actual_token_ids = {p.token_id: p for p in actual_positions}
            
            positions_added = 0
            positions_updated = 0
            positions_closed = 0
            
            # Sync positions from API
            for token_id, pos in actual_token_ids.items():
                price = pos.current_price or pos.entry_price or 0
                
                if token_id not in db_token_ids:
                    # Add as orphan position
                    txn.add_orphan_position(
                        wallet_address=wallet,
                        token_id=token_id,
                        market_id=pos.market_id,
                        shares=pos.shares,
                        current_price=price
                    )
                    positions_added += 1
                else:
                    # Update existing position price
                    db_pos = db_token_ids[token_id]
                    if db_pos.id and price > 0:
                        txn.update_position_price(db_pos.id, price)
                        positions_updated += 1
            
            # Mark ghost positions as closed
            for token_id, db_pos in db_token_ids.items():
                if token_id not in actual_token_ids:
                    txn.mark_position_closed(db_pos.id)
                    positions_closed += 1
        
        # Calculate totals
        total_positions_value = sum(
            p.shares * (p.current_price or p.entry_price or 0) 
            for p in actual_positions
        )
        
        return {
            "success": True,
            "usdc_balance": actual_balance,
            "positions_value": total_positions_value,
            "total_equity": actual_balance + total_positions_value,
            "positions_synced": len(actual_positions),
            "positions_added": positions_added,
            "positions_updated": positions_updated,
            "positions_closed": positions_closed,
        }
        
    except Exception as e:
        logger.error(f"Error refreshing wallet data: {e}")
        return {
            "success": False,
            "error": str(e),
            "usdc_balance": 0,
            "positions_synced": 0,
        }
    finally:
        await api.close()

