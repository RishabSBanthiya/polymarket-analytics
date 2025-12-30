"""
Safety components for trading bots.

Contains:
- CircuitBreaker: Stop trading on repeated failures
- DrawdownLimit: Stop trading on excessive losses
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern for trading.
    
    Stops trading after N consecutive failures to prevent
    compounding losses from systemic issues.
    
    States:
    - CLOSED: Normal operation, trades allowed
    - OPEN: Failures exceeded threshold, trades blocked
    - HALF-OPEN: Reset timeout passed, allow one test trade
    
    Usage:
        breaker = CircuitBreaker(failure_threshold=5)
        
        if not breaker.can_execute():
            logger.warning("Circuit breaker is OPEN")
            return
        
        try:
            result = await execute_trade(...)
            breaker.record_success()
        except Exception:
            breaker.record_failure()
    """
    
    failure_threshold: int = 5
    reset_timeout_seconds: int = 300
    failure_count: int = field(default=0, init=False)
    last_failure_time: Optional[datetime] = field(default=None, init=False)
    state: str = field(default="CLOSED", init=False)
    
    def record_success(self):
        """Record a successful operation"""
        self.failure_count = 0
        if self.state != "CLOSED":
            logger.info("Circuit breaker CLOSED (success after recovery)")
        self.state = "CLOSED"
    
    def record_failure(self):
        """Record a failed operation"""
        self.failure_count += 1
        self.last_failure_time = datetime.now(timezone.utc)
        
        if self.failure_count >= self.failure_threshold:
            if self.state != "OPEN":
                logger.error(
                    f"CIRCUIT BREAKER OPEN: {self.failure_count} consecutive failures"
                )
            self.state = "OPEN"
        else:
            logger.warning(
                f"Failure {self.failure_count}/{self.failure_threshold}"
            )
    
    def can_execute(self) -> bool:
        """Check if execution is allowed"""
        if self.state == "CLOSED":
            return True
        
        if self.state == "HALF-OPEN":
            # Already in half-open, allow the test
            return True
        
        # State is OPEN - check if reset timeout has passed
        if self.last_failure_time:
            elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()
            if elapsed > self.reset_timeout_seconds:
                logger.info("Circuit breaker HALF-OPEN (reset timeout passed)")
                self.state = "HALF-OPEN"
                return True
        
        return False
    
    def reset(self):
        """Manually reset the circuit breaker"""
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"
        logger.info("Circuit breaker manually reset")
    
    @property
    def is_open(self) -> bool:
        """Check if circuit breaker is blocking trades"""
        return self.state == "OPEN"
    
    @property
    def seconds_until_reset(self) -> Optional[float]:
        """Seconds until reset timeout (None if not applicable)"""
        if self.state != "OPEN" or not self.last_failure_time:
            return None
        
        elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()
        remaining = self.reset_timeout_seconds - elapsed
        return max(0, remaining)


@dataclass
class DrawdownLimit:
    """
    Drawdown-based trading limit.
    
    Stops trading when losses exceed configured thresholds:
    - Daily drawdown: Losses from start of day
    - Total drawdown: Losses from peak equity
    
    Usage:
        limiter = DrawdownLimit(max_daily=0.10, max_total=0.25)
        
        # Update with current equity
        if not limiter.update(current_equity):
            logger.error("Drawdown limit breached!")
            stop_trading()
    """
    
    max_daily_drawdown_pct: float = 0.10
    max_total_drawdown_pct: float = 0.25
    
    daily_start_equity: Optional[float] = field(default=None, init=False)
    daily_start_date: Optional[datetime] = field(default=None, init=False)
    peak_equity: float = field(default=0.0, init=False)
    current_equity: float = field(default=0.0, init=False)
    is_breached: bool = field(default=False, init=False)
    breach_reason: Optional[str] = field(default=None, init=False)
    
    def update(self, current_equity: float) -> bool:
        """
        Update equity and check limits.
        
        Returns True if trading allowed, False if limit breached.
        """
        now = datetime.now(timezone.utc)
        self.current_equity = current_equity
        
        # Reset daily tracking at midnight UTC
        if (self.daily_start_date is None or 
            now.date() != self.daily_start_date.date()):
            self.daily_start_equity = current_equity
            self.daily_start_date = now
            logger.info(f"Daily drawdown reset: starting equity ${current_equity:.2f}")
        
        # Update peak
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
        
        # Check daily drawdown
        if self.daily_start_equity and self.daily_start_equity > 0:
            daily_drawdown = (self.daily_start_equity - current_equity) / self.daily_start_equity
            if daily_drawdown > self.max_daily_drawdown_pct:
                self.is_breached = True
                self.breach_reason = f"DAILY DRAWDOWN: {daily_drawdown:.1%} > {self.max_daily_drawdown_pct:.1%}"
                logger.error(self.breach_reason)
                return False
        
        # Check total drawdown from peak
        if self.peak_equity > 0:
            total_drawdown = (self.peak_equity - current_equity) / self.peak_equity
            if total_drawdown > self.max_total_drawdown_pct:
                self.is_breached = True
                self.breach_reason = f"TOTAL DRAWDOWN: {total_drawdown:.1%} > {self.max_total_drawdown_pct:.1%}"
                logger.error(self.breach_reason)
                return False
        
        # Clear breach if equity recovered
        if self.is_breached:
            self.is_breached = False
            self.breach_reason = None
            logger.info("Drawdown limits cleared (equity recovered)")
        
        return True
    
    def reset(self, current_equity: Optional[float] = None):
        """Manually reset drawdown tracking"""
        equity = current_equity or self.current_equity
        self.daily_start_equity = equity
        self.daily_start_date = datetime.now(timezone.utc)
        self.peak_equity = equity
        self.is_breached = False
        self.breach_reason = None
        logger.info(f"Drawdown limits manually reset: equity ${equity:.2f}")
    
    @property
    def daily_drawdown_pct(self) -> float:
        """Current daily drawdown as percentage"""
        if not self.daily_start_equity or self.daily_start_equity <= 0:
            return 0.0
        return (self.daily_start_equity - self.current_equity) / self.daily_start_equity
    
    @property
    def total_drawdown_pct(self) -> float:
        """Current total drawdown from peak as percentage"""
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity
    
    @property
    def daily_remaining_pct(self) -> float:
        """Remaining daily drawdown allowance"""
        return max(0, self.max_daily_drawdown_pct - self.daily_drawdown_pct)
    
    @property
    def total_remaining_pct(self) -> float:
        """Remaining total drawdown allowance"""
        return max(0, self.max_total_drawdown_pct - self.total_drawdown_pct)
    
    def get_status(self) -> dict:
        """Get current drawdown status"""
        return {
            "current_equity": self.current_equity,
            "peak_equity": self.peak_equity,
            "daily_start_equity": self.daily_start_equity,
            "daily_drawdown_pct": self.daily_drawdown_pct,
            "total_drawdown_pct": self.total_drawdown_pct,
            "daily_remaining_pct": self.daily_remaining_pct,
            "total_remaining_pct": self.total_remaining_pct,
            "is_breached": self.is_breached,
            "breach_reason": self.breach_reason,
        }


class TradingHalt:
    """
    Comprehensive trading halt that combines multiple safety checks.
    
    Usage:
        halt = TradingHalt()
        halt.add_reason("API_ERROR", "Polymarket API unreachable")
        
        if halt.is_halted:
            logger.error(f"Trading halted: {halt.reasons}")
            return
    """
    
    def __init__(self):
        self._reasons: dict[str, str] = {}
    
    def add_reason(self, key: str, message: str):
        """Add a halt reason"""
        if key not in self._reasons:
            logger.warning(f"Trading halt added: {key} - {message}")
        self._reasons[key] = message
    
    def clear_reason(self, key: str):
        """Clear a halt reason"""
        if key in self._reasons:
            logger.info(f"Trading halt cleared: {key}")
            del self._reasons[key]
    
    def clear_all(self):
        """Clear all halt reasons"""
        self._reasons.clear()
        logger.info("All trading halts cleared")
    
    @property
    def is_halted(self) -> bool:
        """Check if trading is halted"""
        return len(self._reasons) > 0
    
    @property
    def reasons(self) -> dict[str, str]:
        """Get all halt reasons"""
        return self._reasons.copy()
    
    @property
    def reason_summary(self) -> str:
        """Get summary of halt reasons"""
        if not self._reasons:
            return "No halt reasons"
        return "; ".join(f"{k}: {v}" for k, v in self._reasons.items())


