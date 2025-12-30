"""
Centralized configuration with validation.

All configuration is loaded from environment variables with sensible defaults.
Configuration is validated on instantiation to fail fast on invalid values.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class RiskConfig:
    """Risk management configuration - validated on creation"""
    
    # Wallet-level limits
    max_wallet_exposure_pct: float = 0.80
    max_per_agent_exposure_pct: float = 0.40
    max_per_market_exposure_pct: float = 0.15
    
    # Per-trade limits
    min_trade_value_usd: float = 5.0
    max_trade_value_usd: float = 1000.0
    max_spread_pct: float = 0.03
    max_slippage_pct: float = 0.01
    
    # Safety limits
    max_daily_drawdown_pct: float = 0.10
    max_total_drawdown_pct: float = 0.25
    circuit_breaker_failures: int = 5
    circuit_breaker_reset_seconds: int = 300
    
    # Timing
    reservation_ttl_seconds: int = 60
    heartbeat_interval_seconds: int = 30
    stale_agent_threshold_seconds: int = 120
    
    # Rate limiting (Polymarket allows 9,000 requests per 10 seconds for CLOB API)
    api_rate_limit_per_10s: int = 9000
    api_rate_limit_window_seconds: int = 10
    
    def __post_init__(self):
        """Validate configuration on creation"""
        errors = []
        
        # Wallet exposure validation
        if not 0 < self.max_wallet_exposure_pct <= 1.0:
            errors.append(f"max_wallet_exposure_pct must be in (0, 1], got {self.max_wallet_exposure_pct}")
        
        if not 0 < self.max_per_agent_exposure_pct <= self.max_wallet_exposure_pct:
            errors.append(
                f"max_per_agent_exposure_pct must be in (0, {self.max_wallet_exposure_pct}], "
                f"got {self.max_per_agent_exposure_pct}"
            )
        
        if not 0 < self.max_per_market_exposure_pct <= self.max_per_agent_exposure_pct:
            errors.append(
                f"max_per_market_exposure_pct must be in (0, {self.max_per_agent_exposure_pct}], "
                f"got {self.max_per_market_exposure_pct}"
            )
        
        # Trade value validation
        if self.min_trade_value_usd <= 0:
            errors.append(f"min_trade_value_usd must be positive, got {self.min_trade_value_usd}")
        
        if self.max_trade_value_usd <= self.min_trade_value_usd:
            errors.append(
                f"max_trade_value_usd must be > min_trade_value_usd, "
                f"got {self.max_trade_value_usd} <= {self.min_trade_value_usd}"
            )
        
        # Spread and slippage
        if not 0 < self.max_spread_pct < 1.0:
            errors.append(f"max_spread_pct must be in (0, 1), got {self.max_spread_pct}")
        
        if not 0 < self.max_slippage_pct < 1.0:
            errors.append(f"max_slippage_pct must be in (0, 1), got {self.max_slippage_pct}")
        
        # Drawdown limits
        if not 0 < self.max_daily_drawdown_pct < 1.0:
            errors.append(f"max_daily_drawdown_pct must be in (0, 1), got {self.max_daily_drawdown_pct}")
        
        if not 0 < self.max_total_drawdown_pct < 1.0:
            errors.append(f"max_total_drawdown_pct must be in (0, 1), got {self.max_total_drawdown_pct}")
        
        # Timing validation
        if self.reservation_ttl_seconds <= 0:
            errors.append(f"reservation_ttl_seconds must be positive, got {self.reservation_ttl_seconds}")
        
        if self.heartbeat_interval_seconds <= 0:
            errors.append(f"heartbeat_interval_seconds must be positive, got {self.heartbeat_interval_seconds}")
        
        if self.stale_agent_threshold_seconds <= self.heartbeat_interval_seconds:
            errors.append(
                f"stale_agent_threshold_seconds must be > heartbeat_interval_seconds, "
                f"got {self.stale_agent_threshold_seconds} <= {self.heartbeat_interval_seconds}"
            )
        
        # Rate limiting
        if self.api_rate_limit_per_10s <= 0:
            errors.append(f"api_rate_limit_per_10s must be positive, got {self.api_rate_limit_per_10s}")
        if self.api_rate_limit_window_seconds <= 0:
            errors.append(f"api_rate_limit_window_seconds must be positive, got {self.api_rate_limit_window_seconds}")
        
        if errors:
            raise ValueError("Invalid RiskConfig:\n" + "\n".join(f"  - {e}" for e in errors))
    
    @classmethod
    def from_env(cls) -> "RiskConfig":
        """Load configuration from environment variables"""
        return cls(
            max_wallet_exposure_pct=float(os.getenv("MAX_WALLET_EXPOSURE_PCT", "0.80")),
            max_per_agent_exposure_pct=float(os.getenv("MAX_PER_AGENT_EXPOSURE_PCT", "0.40")),
            max_per_market_exposure_pct=float(os.getenv("MAX_PER_MARKET_EXPOSURE_PCT", "0.15")),
            min_trade_value_usd=float(os.getenv("MIN_TRADE_VALUE_USD", "5.0")),
            max_trade_value_usd=float(os.getenv("MAX_TRADE_VALUE_USD", "1000.0")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.03")),
            max_slippage_pct=float(os.getenv("MAX_SLIPPAGE_PCT", "0.01")),
            max_daily_drawdown_pct=float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.10")),
            max_total_drawdown_pct=float(os.getenv("MAX_TOTAL_DRAWDOWN_PCT", "0.25")),
            circuit_breaker_failures=int(os.getenv("CIRCUIT_BREAKER_FAILURES", "5")),
            circuit_breaker_reset_seconds=int(os.getenv("CIRCUIT_BREAKER_RESET_SECONDS", "300")),
            reservation_ttl_seconds=int(os.getenv("RESERVATION_TTL_SECONDS", "60")),
            heartbeat_interval_seconds=int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "30")),
            stale_agent_threshold_seconds=int(os.getenv("STALE_AGENT_THRESHOLD_SECONDS", "120")),
            api_rate_limit_per_10s=int(os.getenv("API_RATE_LIMIT_PER_10S", "9000")),
            api_rate_limit_window_seconds=int(os.getenv("API_RATE_LIMIT_WINDOW_SECONDS", "10")),
        )


@dataclass
class Config:
    """Main application configuration"""
    
    # API endpoints
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"
    
    # Blockchain
    polygon_rpc_url: str = "https://polygon-rpc.com"
    usdc_contract_address: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    chain_id: int = 137
    
    # Credentials (loaded from env)
    private_key: Optional[str] = None
    proxy_address: Optional[str] = None
    
    # Storage
    db_path: str = "data/risk_state.db"
    
    # Risk configuration
    risk: RiskConfig = field(default_factory=RiskConfig)
    
    # Logging
    log_level: str = "INFO"
    
    def __post_init__(self):
        """Load credentials from environment"""
        if self.private_key is None:
            self.private_key = os.getenv("PRIVATE_KEY")
        if self.proxy_address is None:
            self.proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
    
    @classmethod
    def from_env(cls) -> "Config":
        """Load full configuration from environment variables"""
        return cls(
            gamma_api_base=os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com"),
            clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
            data_api_base=os.getenv("DATA_API_BASE", "https://data-api.polymarket.com"),
            polygon_rpc_url=os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com"),
            chain_id=int(os.getenv("CHAIN_ID", "137")),
            db_path=os.getenv("RISK_DB_PATH", "data/risk_state.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            risk=RiskConfig.from_env(),
        )
    
    def validate_credentials(self) -> bool:
        """Check if required credentials are present"""
        return bool(self.private_key and self.proxy_address)
    
    def require_credentials(self):
        """Raise error if credentials are missing"""
        if not self.validate_credentials():
            raise ValueError(
                "Missing credentials. Please create a .env file with:\n"
                "PRIVATE_KEY=0x...\n"
                "POLYMARKET_PROXY_ADDRESS=0x..."
            )


# Global default configuration
_default_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance"""
    global _default_config
    if _default_config is None:
        _default_config = Config.from_env()
    return _default_config


def set_config(config: Config):
    """Set the global configuration instance"""
    global _default_config
    _default_config = config


