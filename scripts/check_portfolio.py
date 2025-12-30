#!/usr/bin/env python3
"""
Polymarket Portfolio Checker

Check current portfolio positions and available cash in your Polymarket account.

Usage:
    python check_portfolio.py
"""

import json
import requests
from py_clob_client.client import ClobClient
from datetime import datetime

# Try to import web3 for blockchain queries
try:
    from web3 import Web3
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

# ============ CONFIGURATION ============
# These values are from your notebook.ipynb
HOST = "https://clob.polymarket.com"
PRIVATE_KEY = "0x9c0da044d867dfd36f7be2df417fda7b8557f9f4ea32056299561b0beffd1c08"
CHAIN_ID = 137
POLYMARKET_PROXY_ADDRESS = "0xf54f4FF134f2A96E7E63674e561Cde110dE4f282"
DATA_API_BASE = "https://data-api.polymarket.com"

# Blockchain configuration for direct queries
POLYGON_RPC_URL = "https://polygon-rpc.com"  # Public RPC endpoint
# Alternative RPC endpoints (fallback):
# "https://rpc.ankr.com/polygon"
# "https://polygon.llamarpc.com"

# USDC token contract address on Polygon
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ERC20 ABI for balanceOf function (minimal ABI)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]
# =======================================


def get_positions_from_api(proxy_address):
    """Fetch positions from Polymarket Data API."""
    url = f"{DATA_API_BASE}/positions"
    params = {"user": proxy_address}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching positions: {e}")
        return []


def get_balance_from_clob(client):
    """Get available cash balance from CLOB client."""
    # Skip this method for now as it requires specific parameters
    # and may not be the right method for getting cash balance
    return None


def get_current_prices(client, token_ids):
    """Fetch current market prices for token IDs from CLOB API."""
    prices = {}
    if not client:
        return prices
    
    for token_id in token_ids:
        if token_id and token_id != 'N/A':
            try:
                # Try to get the midpoint price (average of bid and ask)
                # For long positions, we want the bid price (what we can sell for)
                # For short positions, we want the ask price (what we'd pay to cover)
                # Using midpoint as a reasonable estimate
                try:
                    # Try get_midpoint first (if available)
                    midpoint = client.get_midpoint(token_id)
                    if midpoint:
                        # Handle different response formats
                        if isinstance(midpoint, dict):
                            price = midpoint.get('midpoint') or midpoint.get('price')
                        elif isinstance(midpoint, (int, float, str)):
                            price = midpoint
                        else:
                            price = None
                        
                        if price:
                            prices[token_id] = float(price)
                            continue
                except:
                    pass
                
                # Fallback: try get_price with 'BUY' side (bid price - what we can sell for)
                try:
                    from py_clob_client.order_builder.constants import BUY
                    price_data = client.get_price(token_id, BUY)
                    if price_data:
                        # Price data structure may vary, try common fields
                        price = None
                        if isinstance(price_data, dict):
                            price = price_data.get('price') or price_data.get('bid') or price_data.get('bestBid')
                        elif isinstance(price_data, (int, float, str)):
                            price = float(price_data)
                        
                        if price:
                            prices[token_id] = float(price)
                            continue
                except Exception as e:
                    pass
                
                # Last resort: try get_last_trade_price
                try:
                    last_trade = client.get_last_trade_price(token_id)
                    if last_trade:
                        if isinstance(last_trade, dict):
                            price = last_trade.get('price') or last_trade.get('lastPrice')
                        else:
                            price = last_trade
                        if price:
                            prices[token_id] = float(price)
                except:
                    pass
            except Exception as e:
                # Silently continue if price fetch fails
                pass
    
    return prices


def format_portfolio_summary(positions, client=None):
    """Format and display portfolio summary with real-time price updates."""
    if not positions:
        print("No positions found.")
        return
    
    total_value = 0.0
    total_value_realtime = 0.0
    total_pnl = 0.0
    total_pnl_realtime = 0.0
    position_count = 0
    
    print("\n" + "="*80)
    print("POLYMARKET PORTFOLIO SUMMARY")
    print("="*80)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Proxy Address: {POLYMARKET_PROXY_ADDRESS}")
    print("-"*80)
    
    # Collect all token IDs for batch price fetching
    token_ids = []
    for pos in positions:
        token_id = pos.get('tokenId') or pos.get('token_id')
        if token_id and token_id != 'N/A':
            token_ids.append(token_id)
    
    # Fetch real-time prices
    current_prices = {}
    if client and token_ids:
        print("\nFetching real-time market prices...")
        current_prices = get_current_prices(client, token_ids)
        if current_prices:
            print(f"✓ Retrieved prices for {len(current_prices)}/{len(token_ids)} tokens")
        else:
            print("⚠ Could not fetch real-time prices, using API values only")
    
    # Group positions by market/condition
    positions_by_market = {}
    for pos in positions:
        condition_id = pos.get('conditionId') or pos.get('condition_id', 'Unknown')
        if condition_id not in positions_by_market:
            positions_by_market[condition_id] = []
        positions_by_market[condition_id].append(pos)
    
    print(f"\nTotal Markets: {len(positions_by_market)}")
    print(f"Total Positions: {len(positions)}")
    print("\n" + "-"*80)
    
    for condition_id, market_positions in positions_by_market.items():
        print(f"\nCondition ID: {condition_id}")
        print("-"*80)
        
        for pos in market_positions:
            outcome = pos.get('outcome', 'N/A')
            size = float(pos.get('size', 0))
            avg_price = float(pos.get('avgPrice', 0))
            current_value_api = float(pos.get('currentValue', 0))
            pnl_api = float(pos.get('cashPnl', 0))
            token_id = pos.get('tokenId') or pos.get('token_id', 'N/A')
            
            # Calculate real-time values if we have current price
            current_price = current_prices.get(token_id) if token_id in current_prices else None
            current_value_realtime = None
            pnl_realtime = None
            
            if current_price is not None and size > 0:
                # Current value = size * current_price
                current_value_realtime = size * current_price
                # P&L = (current_price - avg_price) * size
                pnl_realtime = (current_price - avg_price) * size
                
                # Use real-time values for totals
                total_value_realtime += current_value_realtime
                total_pnl_realtime += pnl_realtime
            
            # Use API values for totals if no real-time price
            total_value += current_value_api
            total_pnl += pnl_api
            position_count += 1
            
            print(f"  Outcome: {outcome}")
            print(f"  Token ID: {token_id}")
            print(f"  Size: {size:.4f} shares")
            print(f"  Avg Price: ${avg_price:.4f}")
            
            if current_price is not None:
                print(f"  Current Price (real-time): ${current_price:.4f}")
                print(f"  Current Value (API): ${current_value_api:.4f}")
                print(f"  Current Value (real-time): ${current_value_realtime:.4f}")
                print(f"  P&L (API): ${pnl_api:.4f} {'(Profit)' if pnl_api >= 0 else '(Loss)'}")
                print(f"  P&L (real-time): ${pnl_realtime:.4f} {'(Profit)' if pnl_realtime >= 0 else '(Loss)'}")
            else:
                print(f"  Current Value: ${current_value_api:.4f}")
                print(f"  P&L: ${pnl_api:.4f} {'(Profit)' if pnl_api >= 0 else '(Loss)'}")
                print(f"  (Real-time price unavailable)")
            print()
    
    print("="*80)
    print("PORTFOLIO TOTALS")
    print("="*80)
    print(f"Total Positions: {position_count}")
    
    if total_value_realtime > 0:
        print(f"Total Portfolio Value (API): ${total_value:.4f}")
        print(f"Total Portfolio Value (real-time): ${total_value_realtime:.4f}")
        print(f"Total P&L (API): ${total_pnl:.4f} {'(Profit)' if total_pnl >= 0 else '(Loss)'}")
        print(f"Total P&L (real-time): ${total_pnl_realtime:.4f} {'(Profit)' if total_pnl_realtime >= 0 else '(Loss)'}")
    else:
        print(f"Total Portfolio Value: ${total_value:.4f}")
        print(f"Total P&L: ${total_pnl:.4f} {'(Profit)' if total_pnl >= 0 else '(Loss)'}")
        print("(Real-time prices unavailable - showing API values)")
    
    print("="*80)


def get_available_cash_from_api(proxy_address):
    """Get available cash balance from Polymarket Data API."""
    try:
        # Try the balance endpoint with user parameter
        url = f"{DATA_API_BASE}/balance"
        params = {"user": proxy_address}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        pass
    
    # Alternative: try the value endpoint for total holdings
    try:
        url = f"{DATA_API_BASE}/value"
        params = {"user": proxy_address}
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {"value": data.get("value", 0), "type": "holdings"}
    except Exception as e:
        pass
    
    return None


def get_usdc_balance_from_blockchain(address):
    """Query blockchain directly for USDC balance."""
    if not WEB3_AVAILABLE:
        return None
    
    try:
        # Connect to Polygon network
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
        
        if not w3.is_connected():
            # Try fallback RPC
            fallback_rpcs = [
                "https://rpc.ankr.com/polygon",
                "https://polygon.llamarpc.com"
            ]
            for rpc in fallback_rpcs:
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc))
                    if w3.is_connected():
                        break
                except:
                    continue
            
            if not w3.is_connected():
                return None
        
        # Get USDC contract
        usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),
            abi=ERC20_ABI
        )
        
        # Get balance
        balance_wei = usdc_contract.functions.balanceOf(
            Web3.to_checksum_address(address)
        ).call()
        
        # Get decimals (USDC has 6 decimals)
        try:
            decimals = usdc_contract.functions.decimals().call()
        except:
            decimals = 6  # USDC standard decimals
        
        # Convert to human-readable format
        balance = balance_wei / (10 ** decimals)
        
        return {
            "balance": balance,
            "balance_raw": balance_wei,
            "decimals": decimals,
            "source": "blockchain"
        }
    except Exception as e:
        print(f"  Blockchain query error: {e}")
        return None


def get_available_cash(client):
    """Get available cash balance."""
    try:
        address = client.get_address()
        collateral_address = client.get_collateral_address()
        
        print(f"Account Address: {address}")
        print(f"Collateral Address: {collateral_address}")
        print(f"Proxy Address: {POLYMARKET_PROXY_ADDRESS}")
        
        # First, try blockchain query (most reliable)
        # Use PROXY_ADDRESS as that's where the USDC balance is held
        if WEB3_AVAILABLE:
            print("\nQuerying blockchain for USDC balance...")
            print(f"  Checking USDC balance at proxy address: {POLYMARKET_PROXY_ADDRESS}")
            blockchain_balance = get_usdc_balance_from_blockchain(POLYMARKET_PROXY_ADDRESS)
            if blockchain_balance:
                return blockchain_balance
        
        # Try to get balance from CLOB client
        try:
            balance_info = get_balance_from_clob(client)
            if balance_info:
                return balance_info
        except Exception as e:
            pass
        
        # Try to get balance from Data API
        balance_info = get_available_cash_from_api(POLYMARKET_PROXY_ADDRESS)
        if balance_info:
            return balance_info
        
        return None
    except Exception as e:
        print(f"Note: Could not retrieve cash balance directly: {e}")
        return None


def main():
    """Main function to check portfolio."""
    print("Initializing Polymarket client...")
    
    # Initialize CLOB client
    try:
        client = ClobClient(
            HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=2,
            funder=POLYMARKET_PROXY_ADDRESS
        )
        
        # Set API credentials
        client.set_api_creds(client.create_or_derive_api_creds())
        
        print("✓ Client initialized successfully")
    except Exception as e:
        print(f"✗ Error initializing client: {e}")
        return
    
    # Get positions from Data API
    print(f"\nFetching positions for proxy address: {POLYMARKET_PROXY_ADDRESS}...")
    positions = get_positions_from_api(POLYMARKET_PROXY_ADDRESS)
    
    if positions:
        print(f"✓ Found {len(positions)} position(s)")
        format_portfolio_summary(positions, client)
    else:
        print("No positions found or error fetching positions.")
    
    # Try to get available cash
    print("\n" + "="*80)
    print("AVAILABLE CASH / BALANCE")
    print("="*80)
    cash_info = get_available_cash(client)
    
    if cash_info:
        if isinstance(cash_info, dict):
            # Check if it's blockchain balance (most reliable)
            if cash_info.get('source') == 'blockchain':
                balance = cash_info.get('balance', 0)
                print(f"✓ Available Cash (from blockchain): ${balance:.4f} USDC")
                print(f"  Raw balance: {cash_info.get('balance_raw', 0)} (with {cash_info.get('decimals', 6)} decimals)")
            # Check if it's balance info from API
            elif 'balance' in cash_info:
                balance = cash_info.get('balance', 0)
                print(f"Available Cash: ${balance:.4f} USDC")
            # Check if it's holdings value info
            elif 'value' in cash_info:
                value = cash_info.get('value', 0)
                print(f"Total Holdings Value: ${value:.4f} USDC")
            else:
                # Try common field names
                available = cash_info.get('available', cash_info.get('availableBalance', None))
                total = cash_info.get('total', cash_info.get('totalBalance', None))
                if available is not None:
                    print(f"Available Cash: ${available:.4f} USDC")
                if total is not None and total != available:
                    print(f"Total Balance: ${total:.4f} USDC")
                if available is None and total is None:
                    print(f"Balance Info: {cash_info}")
        else:
            print(f"Available Cash: {cash_info}")
    else:
        print("Note: Could not retrieve available cash balance.")
        if not WEB3_AVAILABLE:
            print("\nTo enable blockchain queries, install web3:")
            print("  pip install web3")
        print("\nAlternative methods to check balance:")
        print("1. Check the Polymarket web interface")
        print("2. Use a blockchain explorer (e.g., polygonscan.com)")
        print(f"3. Check USDC balance at proxy address: {POLYMARKET_PROXY_ADDRESS}")
        print(f"   USDC Contract: {USDC_CONTRACT_ADDRESS}")
        print(f"   Explorer: https://polygonscan.com/address/{POLYMARKET_PROXY_ADDRESS}")
        try:
            collateral = client.get_collateral_address()
            print(f"   (Collateral Address: {collateral})")
        except:
            pass
        print("\nThe portfolio value above represents your current positions' value.")
    
    print("\n" + "="*80)


if __name__ == "__main__":
    main()

