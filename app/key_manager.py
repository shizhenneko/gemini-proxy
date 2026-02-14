# pyright: reportMissingImports=false, reportUnknownVariableType=false

"""Key pool management."""

import asyncio
import time
from datetime import datetime, timedelta, tzinfo
from typing import Dict, Optional, cast

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore

from app.config import Config
from app.models import ApiKey, PoolState, STATUS_ACTIVE, STATUS_EXHAUSTED


class KeyManager:
    """Manages API key pool with rate limits."""

    def __init__(self, config: Config):
        self.pool: PoolState = PoolState()
        self._lock: asyncio.Lock = asyncio.Lock()

        for index, api_key in enumerate(config.api_keys, start=1):
            key_id = f"key_{index}"
            self.pool.keys[key_id] = ApiKey(
                id=key_id,
                key=api_key,
                rpd_limit=config.default_rpd_limit,
                rpm_limit=config.default_rpm_limit,
            )

        pacific_tz = cast(tzinfo, ZoneInfo("America/Los_Angeles"))
        now_pacific = datetime.now(pacific_tz)
        self.pool.last_reset_date = now_pacific.date()

    async def select_key(self) -> Optional[ApiKey]:
        await self.check_and_reset_daily()

        async with self._lock:
            cutoff_time = time.time() - 60
            for key in self.pool.keys.values():
                key.rpm_timestamps = [
                    ts for ts in key.rpm_timestamps if ts > cutoff_time
                ]

            available_keys = [
                key
                for key in self.pool.keys.values()
                if key.status == STATUS_ACTIVE
                and key.rpd_remaining > 0
                and key.rpm_current < key.rpm_limit
            ]

            if not available_keys:
                return None

            available_keys.sort(key=lambda item: item.rpd_remaining, reverse=True)
            return available_keys[0]

    async def record_request(self, key_id: str) -> None:
        async with self._lock:
            key = self.pool.keys.get(key_id)
            if not key:
                return

            key.rpd_used += 1
            key.rpm_timestamps.append(time.time())
            key.last_used = datetime.now()

            if key.rpd_used >= key.rpd_limit:
                key.status = STATUS_EXHAUSTED

    async def record_error(self, key_id: str, is_rpd_limit: bool = False) -> None:
        async with self._lock:
            key = self.pool.keys.get(key_id)
            if not key:
                return

            key.last_error = datetime.now()
            key.consecutive_failures += 1

            if is_rpd_limit:
                key.status = STATUS_EXHAUSTED

    async def check_and_reset_daily(self) -> None:
        async with self._lock:
            pacific_tz = cast(tzinfo, ZoneInfo("America/Los_Angeles"))
            now_pacific = datetime.now(pacific_tz)
            if (
                self.pool.last_reset_date
                and now_pacific.date() <= self.pool.last_reset_date
            ):
                return

            for key in self.pool.keys.values():
                key.rpd_used = 0
                key.consecutive_failures = 0
                if key.status == STATUS_EXHAUSTED:
                    key.status = STATUS_ACTIVE

            self.pool.last_reset_date = now_pacific.date()

    async def add_key(
        self, api_key: str, rpd_limit: int = 250, rpm_limit: int = 10
    ) -> str:
        async with self._lock:
            if any(existing.key == api_key for existing in self.pool.keys.values()):
                raise ValueError("API key already exists")

            key_id = self._next_key_id()
            self.pool.keys[key_id] = ApiKey(
                id=key_id,
                key=api_key,
                rpd_limit=rpd_limit,
                rpm_limit=rpm_limit,
            )
            return key_id

    async def remove_key(self, key_id: str) -> bool:
        async with self._lock:
            if key_id not in self.pool.keys:
                return False
            del self.pool.keys[key_id]
            return True

    def get_status(self) -> Dict[str, object]:
        available_keys = 0
        exhausted_keys = 0

        for key in self.pool.keys.values():
            if key.status == STATUS_EXHAUSTED:
                exhausted_keys += 1
            if (
                key.status == STATUS_ACTIVE
                and key.rpd_remaining > 0
                and key.rpm_current < key.rpm_limit
            ):
                available_keys += 1

        next_reset = None
        if self.pool.last_reset_date:
            next_reset_date = self.pool.last_reset_date + timedelta(days=1)
            next_reset = next_reset_date.isoformat()

        return {
            "total_keys": len(self.pool.keys),
            "available_keys": available_keys,
            "exhausted_keys": exhausted_keys,
            "next_reset": next_reset,
            "keys": [self._format_key_status(key) for key in self.pool.keys.values()],
        }

    def get_key_status(self, key_id: str) -> Optional[Dict[str, object]]:
        key = self.pool.keys.get(key_id)
        if not key:
            return None
        return self._format_key_status(key)

    def _format_key_status(self, key: ApiKey) -> Dict[str, object]:
        return {
            "id": key.id,
            "key_prefix": key.key_prefix(),
            "status": key.status,
            "rpd_used": key.rpd_used,
            "rpd_limit": key.rpd_limit,
            "rpd_remaining": key.rpd_remaining,
            "rpm_limit": key.rpm_limit,
            "rpm_current": key.rpm_current,
            "last_used": key.last_used,
            "last_error": key.last_error,
            "consecutive_failures": key.consecutive_failures,
        }

    def _next_key_id(self) -> str:
        max_id = 0
        for key_id in self.pool.keys.keys():
            if key_id.startswith("key_"):
                suffix = key_id[4:]
                if suffix.isdigit():
                    max_id = max(max_id, int(suffix))
        return f"key_{max_id + 1}"
