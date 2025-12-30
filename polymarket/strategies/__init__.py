"""
Trading strategies package.

Contains preconfigured trading bot configurations for different strategies.
"""

from .bond_strategy import create_bond_bot
from .flow_strategy import create_flow_bot

__all__ = [
    "create_bond_bot",
    "create_flow_bot",
]


