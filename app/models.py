"""Data models for API key management."""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional
import time

STATUS_ACTIVE = "active"
STATUS_EXHAUSTED = "exhausted"


@dataclass
class ApiKey:
    """Represents a single API key with usage tracking."""

    id: str
    key: str
    rpd_limit: int = 250
    rpm_limit: int = 10
    rpd_used: int = 0
    rpm_timestamps: List[float] = field(default_factory=list)
    last_used: Optional[datetime] = None
    last_error: Optional[datetime] = None
    consecutive_failures: int = 0
    status: str = STATUS_ACTIVE

    @property
    def rpd_remaining(self) -> int:
        return self.rpd_limit - self.rpd_used

    @property
    def rpm_current(self) -> int:
        current_time = time.time()
        cutoff_time = current_time - 60
        return sum(1 for ts in self.rpm_timestamps if ts > cutoff_time)

    def key_prefix(self) -> str:
        if len(self.key) <= 11:
            return self.key
        return f"{self.key[:8]}...{self.key[-3:]}"


@dataclass
class PoolState:
    """Represents the state of the entire API key pool."""

    keys: Dict[str, ApiKey] = field(default_factory=dict)
    last_reset_date: Optional[date] = None
