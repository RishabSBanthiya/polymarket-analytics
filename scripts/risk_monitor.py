#!/usr/bin/env python3
"""
Risk Monitor CLI.

Monitor and manage multi-agent risk coordinator.

Usage:
    # View current status
    python risk_monitor.py status
    
    # View drawdown status
    python risk_monitor.py drawdown
    
    # View all agents
    python risk_monitor.py agents
    
    # Cleanup stale data
    python risk_monitor.py cleanup
    
    # Emergency stop (mark all agents as stopped)
    python risk_monitor.py stop-all
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from polymarket.core.config import Config, get_config
from polymarket.core.models import AgentStatus
from polymarket.trading.storage.sqlite import SQLiteStorage


def get_storage(config: Optional[Config] = None) -> SQLiteStorage:
    """Get storage instance"""
    config = config or get_config()
    return SQLiteStorage(config.db_path)


def cmd_status(args):
    """Show overall status"""
    storage = get_storage()
    
    # Get wallet address from config
    config = get_config()
    wallet = config.proxy_address or "unknown"
    
    with storage.transaction() as txn:
        wallet_state = txn.get_wallet_state(wallet)
    
    print("\n" + "="*60)
    print("RISK MONITOR STATUS")
    print("="*60)
    
    print(f"\nWallet: {wallet[:10]}...{wallet[-6:] if len(wallet) > 16 else ''}")
    print(f"USDC Balance: ${wallet_state.usdc_balance:,.2f}")
    print(f"Positions Value: ${wallet_state.total_positions_value:,.2f}")
    print(f"Reserved: ${wallet_state.total_reserved:,.2f}")
    print(f"Available: ${wallet_state.available_capital:,.2f}")
    print(f"Total Exposure: ${wallet_state.total_exposure:,.2f} ({wallet_state.exposure_pct:.1%})")
    
    print(f"\nActive Agents: {len([a for a in wallet_state.agents if a.status == AgentStatus.ACTIVE])}")
    print(f"Open Positions: {len([p for p in wallet_state.positions])}")
    print(f"Active Reservations: {len([r for r in wallet_state.reservations if r.is_active])}")
    
    print("="*60 + "\n")


def cmd_agents(args):
    """Show all agents"""
    storage = get_storage()
    
    with storage.transaction() as txn:
        agents = txn.get_all_agents()
    
    print("\n" + "="*60)
    print("REGISTERED AGENTS")
    print("="*60)
    
    if not agents:
        print("\nNo agents registered.")
    else:
        print(f"\n{'ID':<20} {'Type':<10} {'Status':<10} {'Last Heartbeat':<25}")
        print("-"*65)
        
        for agent in agents:
            hb_ago = agent.seconds_since_heartbeat
            if hb_ago < 60:
                hb_str = f"{hb_ago:.0f}s ago"
            elif hb_ago < 3600:
                hb_str = f"{hb_ago/60:.0f}m ago"
            else:
                hb_str = f"{hb_ago/3600:.1f}h ago"
            
            status_emoji = {
                AgentStatus.ACTIVE: "🟢",
                AgentStatus.STOPPED: "⚪",
                AgentStatus.CRASHED: "🔴",
            }.get(agent.status, "❓")
            
            print(f"{agent.agent_id:<20} {agent.agent_type:<10} {status_emoji} {agent.status.value:<8} {hb_str:<25}")
    
    print("="*60 + "\n")


def cmd_positions(args):
    """Show all positions"""
    storage = get_storage()
    config = get_config()
    wallet = config.proxy_address or "unknown"
    
    with storage.transaction() as txn:
        positions = txn.get_all_positions(wallet)
    
    print("\n" + "="*60)
    print("POSITIONS")
    print("="*60)
    
    if not positions:
        print("\nNo positions found.")
    else:
        print(f"\n{'Agent':<15} {'Token':<15} {'Shares':<12} {'Entry':<10} {'Current':<10} {'P&L':<12} {'Status':<8}")
        print("-"*85)
        
        for pos in positions:
            pnl = pos.unrealized_pnl
            pnl_str = f"${pnl:+.2f}" if pnl else "N/A"
            
            print(
                f"{pos.agent_id:<15} "
                f"{pos.token_id[:12]+'...':<15} "
                f"{pos.shares:<12.2f} "
                f"${pos.entry_price:<9.4f} "
                f"${(pos.current_price or 0):<9.4f} "
                f"{pnl_str:<12} "
                f"{pos.status.value:<8}"
            )
    
    print("="*60 + "\n")


def cmd_reservations(args):
    """Show active reservations"""
    storage = get_storage()
    config = get_config()
    wallet = config.proxy_address or "unknown"
    
    with storage.transaction() as txn:
        reservations = txn.get_all_reservations(wallet)
    
    print("\n" + "="*60)
    print("RESERVATIONS")
    print("="*60)
    
    active = [r for r in reservations if r.is_active]
    
    if not active:
        print("\nNo active reservations.")
    else:
        print(f"\n{'Agent':<15} {'Amount':<12} {'Market':<15} {'Expires':<20} {'Status':<10}")
        print("-"*75)
        
        for res in active:
            expires_in = (res.expires_at - datetime.now(timezone.utc)).total_seconds()
            expires_str = f"{expires_in:.0f}s" if expires_in > 0 else "EXPIRED"
            
            print(
                f"{res.agent_id:<15} "
                f"${res.amount_usd:<11.2f} "
                f"{res.market_id[:12]+'...':<15} "
                f"{expires_str:<20} "
                f"{res.status.value:<10}"
            )
    
    print(f"\nTotal reserved: ${sum(r.amount_usd for r in active):,.2f}")
    print("="*60 + "\n")


def cmd_drawdown(args):
    """Show drawdown status"""
    storage = get_storage()
    config = get_config()
    wallet = config.proxy_address or "unknown"
    
    with storage.transaction() as txn:
        wallet_state = txn.get_wallet_state(wallet)
    
    total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
    
    print("\n" + "="*60)
    print("DRAWDOWN STATUS")
    print("="*60)
    
    print(f"\nCurrent Equity: ${total_equity:,.2f}")
    print(f"\nLimits from config:")
    print(f"  Max Daily Drawdown: {config.risk.max_daily_drawdown_pct:.1%}")
    print(f"  Max Total Drawdown: {config.risk.max_total_drawdown_pct:.1%}")
    
    print("\n⚠️  Note: Historical drawdown tracking requires the trading bot to be running.")
    print("    This view shows current state only.")
    
    print("="*60 + "\n")


def cmd_cleanup(args):
    """Cleanup stale data"""
    storage = get_storage()
    config = get_config()
    
    with storage.transaction() as txn:
        # Cleanup expired reservations
        expired_res = txn.cleanup_expired_reservations()
        
        # Cleanup stale agents
        stale_agents = txn.cleanup_stale_agents(
            config.risk.stale_agent_threshold_seconds
        )
    
    print("\n" + "="*60)
    print("CLEANUP RESULTS")
    print("="*60)
    
    print(f"\nExpired reservations cleaned: {expired_res}")
    print(f"Stale agents marked crashed: {stale_agents}")
    
    print("\n✅ Cleanup complete")
    print("="*60 + "\n")


def cmd_stop_all(args):
    """Emergency stop all agents"""
    if not args.yes:
        confirm = input("\n⚠️  This will mark ALL agents as STOPPED. Continue? [y/N]: ")
        if confirm.lower() != 'y':
            print("Cancelled.")
            return
    
    storage = get_storage()
    
    with storage.transaction() as txn:
        agents = txn.get_all_agents()
        stopped = 0
        
        for agent in agents:
            if agent.status == AgentStatus.ACTIVE:
                txn.update_agent_status(agent.agent_id, AgentStatus.STOPPED)
                stopped += 1
        
        # Release all reservations
        released = txn.release_all_reservations()
    
    print("\n" + "="*60)
    print("EMERGENCY STOP")
    print("="*60)
    
    print(f"\n🛑 Agents stopped: {stopped}")
    print(f"🔓 Reservations released: {released}")
    
    print("\n✅ All agents stopped")
    print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Risk Monitor CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  status       Show overall status
  agents       List all agents
  positions    Show all positions
  reservations Show active reservations
  drawdown     Show drawdown status
  cleanup      Cleanup stale data
  stop-all     Emergency stop all agents
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # Status
    subparsers.add_parser("status", help="Show overall status")
    
    # Agents
    subparsers.add_parser("agents", help="List all agents")
    
    # Positions
    subparsers.add_parser("positions", help="Show all positions")
    
    # Reservations
    subparsers.add_parser("reservations", help="Show active reservations")
    
    # Drawdown
    subparsers.add_parser("drawdown", help="Show drawdown status")
    
    # Cleanup
    subparsers.add_parser("cleanup", help="Cleanup stale data")
    
    # Stop all
    stop_parser = subparsers.add_parser("stop-all", help="Emergency stop all agents")
    stop_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Setup logging
    logging.basicConfig(level=logging.WARNING)
    
    # Run command
    commands = {
        "status": cmd_status,
        "agents": cmd_agents,
        "positions": cmd_positions,
        "reservations": cmd_reservations,
        "drawdown": cmd_drawdown,
        "cleanup": cmd_cleanup,
        "stop-all": cmd_stop_all,
    }
    
    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

