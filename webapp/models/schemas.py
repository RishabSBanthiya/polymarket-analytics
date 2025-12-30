"""
Pydantic schemas for API requests and responses.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class TradeResponse(BaseModel):
    """Trade execution response"""
    id: Optional[int] = None
    agent_id: str
    market_id: str
    token_id: str
    side: str
    shares: float
    price: float
    filled_price: float
    signal_score: Optional[float]
    success: bool
    error_message: Optional[str]
    timestamp: datetime
    wallet_address: Optional[str] = None
    is_orphan: bool = False
    transaction_type: Optional[str] = "trade"  # "trade", "deposit", "withdrawal"


class PositionResponse(BaseModel):
    """Position response"""
    id: Optional[int]
    agent_id: str
    market_id: str
    token_id: str
    outcome: str
    shares: float
    entry_price: float
    entry_time: Optional[datetime]
    current_price: Optional[float]
    status: str
    cost_basis: float
    current_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float


class AlertResponse(BaseModel):
    """Flow alert response"""
    id: int
    alert_type: str
    market_id: str
    token_id: str
    question: str
    timestamp: datetime
    severity: str
    reason: str
    details: Dict[str, Any]
    category: str
    score: Optional[float]


class PerformanceMetrics(BaseModel):
    """Performance metrics response"""
    agent_id: str
    total_trades: int
    successful_trades: int
    failed_trades: int
    win_rate: float
    total_pnl: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    avg_trade_size: float
    total_volume: float


class AlertStats(BaseModel):
    """Alert statistics response"""
    total: int
    by_type: Dict[str, int]
    by_severity: Dict[str, int]

