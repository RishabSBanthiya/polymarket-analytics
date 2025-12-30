"""
Storage backends for multi-agent state coordination.

Provides:
- StorageBackend: Abstract interface for storage implementations
- SQLiteStorage: SQLite-based storage (default)
- RedisStorage: Redis-based storage (future, for distributed systems)
"""

from .base import StorageBackend, StorageTransaction
from .sqlite import SQLiteStorage

__all__ = [
    "StorageBackend",
    "StorageTransaction",
    "SQLiteStorage",
]


