# pyright: reportMissingImports=false, reportUnknownVariableType=false

import asyncio
import time
from datetime import date, datetime, tzinfo
from typing import Dict, List, Optional, cast

import pytest

from app.config import Config
from app.key_manager import KeyManager
from app.models import STATUS_ACTIVE, STATUS_EXHAUSTED

try:
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # pragma: no cover - Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore


def make_config(
    api_keys: List[str],
    default_rpd_limit: int = 250,
    default_rpm_limit: int = 10,
) -> Config:
    return Config(
        api_keys=api_keys,
        default_rpd_limit=default_rpd_limit,
        default_rpm_limit=default_rpm_limit,
    )


@pytest.mark.asyncio
async def test_init_creates_keys_from_config():
    config = make_config(["k1", "k2"], default_rpd_limit=123, default_rpm_limit=7)
    manager = KeyManager(config)

    assert manager.pool.last_reset_date is not None
    assert list(manager.pool.keys.keys()) == ["key_1", "key_2"]
    assert manager.pool.keys["key_1"].key == "k1"
    assert manager.pool.keys["key_1"].rpd_limit == 123
    assert manager.pool.keys["key_1"].rpm_limit == 7


@pytest.mark.asyncio
async def test_select_key_highest_rpd():
    manager = KeyManager(make_config(["k1", "k2", "k3"], default_rpd_limit=100))

    manager.pool.keys["key_1"].rpd_used = 80
    manager.pool.keys["key_2"].rpd_used = 10
    manager.pool.keys["key_3"].rpd_used = 50

    selected = await manager.select_key()
    assert selected is not None
    assert selected.id == "key_2"


@pytest.mark.asyncio
async def test_select_key_skips_exhausted():
    manager = KeyManager(make_config(["k1", "k2"]))

    manager.pool.keys["key_1"].status = STATUS_EXHAUSTED
    selected = await manager.select_key()

    assert selected is not None
    assert selected.id == "key_2"


@pytest.mark.asyncio
async def test_select_key_skips_rpm_limited():
    manager = KeyManager(make_config(["k1", "k2"], default_rpm_limit=2))
    now = time.time()

    manager.pool.keys["key_1"].rpm_timestamps = [now, now]
    selected = await manager.select_key()

    assert selected is not None
    assert selected.id == "key_2"


@pytest.mark.asyncio
async def test_select_key_returns_none_when_all_exhausted():
    manager = KeyManager(make_config(["k1", "k2"]))

    manager.pool.keys["key_1"].status = STATUS_EXHAUSTED
    manager.pool.keys["key_2"].status = STATUS_EXHAUSTED

    selected = await manager.select_key()
    assert selected is None


@pytest.mark.asyncio
async def test_record_request_increments_counters():
    manager = KeyManager(make_config(["k1"]))

    await manager.record_request("key_1")

    key = manager.pool.keys["key_1"]
    assert key.rpd_used == 1
    assert len(key.rpm_timestamps) == 1
    assert key.last_used is not None


@pytest.mark.asyncio
async def test_record_request_marks_exhausted_at_limit():
    manager = KeyManager(make_config(["k1"]))
    manager.pool.keys["key_1"].rpd_limit = 1

    await manager.record_request("key_1")

    assert manager.pool.keys["key_1"].status == STATUS_EXHAUSTED


@pytest.mark.asyncio
async def test_record_error_updates_state():
    manager = KeyManager(make_config(["k1"]))

    await manager.record_error("key_1")

    key = manager.pool.keys["key_1"]
    assert key.last_error is not None
    assert key.consecutive_failures == 1


@pytest.mark.asyncio
async def test_record_error_rpd_marks_exhausted():
    manager = KeyManager(make_config(["k1"]))

    await manager.record_error("key_1", is_rpd_limit=True)

    assert manager.pool.keys["key_1"].status == STATUS_EXHAUSTED


@pytest.mark.asyncio
async def test_daily_reset_resets_counters(monkeypatch: pytest.MonkeyPatch):
    manager = KeyManager(make_config(["k1"]))
    manager.pool.last_reset_date = date(2026, 2, 13)

    key = manager.pool.keys["key_1"]
    key.rpd_used = 9
    key.consecutive_failures = 2
    key.status = STATUS_EXHAUSTED

    fixed_dt = datetime(
        2026,
        2,
        14,
        10,
        0,
        0,
        tzinfo=cast(tzinfo, ZoneInfo("America/Los_Angeles")),
    )

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: Optional[tzinfo] = None) -> datetime:
            if tz:
                return fixed_dt.astimezone(tz)
            return fixed_dt

    import app.key_manager as key_manager_module

    monkeypatch.setattr(key_manager_module, "datetime", FrozenDateTime)

    await manager.check_and_reset_daily()

    assert key.rpd_used == 0
    assert key.consecutive_failures == 0
    assert key.status == STATUS_ACTIVE
    assert manager.pool.last_reset_date == fixed_dt.date()


@pytest.mark.asyncio
async def test_daily_reset_skips_same_day(monkeypatch: pytest.MonkeyPatch):
    manager = KeyManager(make_config(["k1"]))
    manager.pool.last_reset_date = date(2026, 2, 13)

    key = manager.pool.keys["key_1"]
    key.rpd_used = 5
    key.status = STATUS_EXHAUSTED

    fixed_dt = datetime(
        2026,
        2,
        13,
        12,
        0,
        0,
        tzinfo=cast(tzinfo, ZoneInfo("America/Los_Angeles")),
    )

    class FrozenDateTime:
        @classmethod
        def now(cls, tz: Optional[tzinfo] = None) -> datetime:
            if tz:
                return fixed_dt.astimezone(tz)
            return fixed_dt

    import app.key_manager as key_manager_module

    monkeypatch.setattr(key_manager_module, "datetime", FrozenDateTime)

    await manager.check_and_reset_daily()

    assert key.rpd_used == 5
    assert key.status == STATUS_EXHAUSTED
    assert manager.pool.last_reset_date == date(2026, 2, 13)


@pytest.mark.asyncio
async def test_add_key_success():
    manager = KeyManager(make_config(["k1"]))

    key_id = await manager.add_key("k2", rpd_limit=300, rpm_limit=15)

    assert key_id in manager.pool.keys
    assert manager.pool.keys[key_id].key == "k2"
    assert manager.pool.keys[key_id].rpd_limit == 300


@pytest.mark.asyncio
async def test_add_key_duplicate_rejected():
    manager = KeyManager(make_config(["k1"]))

    with pytest.raises(ValueError):
        _ = await manager.add_key("k1")


@pytest.mark.asyncio
async def test_remove_key_success():
    manager = KeyManager(make_config(["k1"]))

    removed = await manager.remove_key("key_1")

    assert removed is True
    assert "key_1" not in manager.pool.keys


@pytest.mark.asyncio
async def test_remove_key_not_found():
    manager = KeyManager(make_config(["k1"]))

    removed = await manager.remove_key("missing")

    assert removed is False


@pytest.mark.asyncio
async def test_concurrent_requests():
    manager = KeyManager(make_config(["k1"], default_rpd_limit=1000))

    _ = await asyncio.gather(*[manager.record_request("key_1") for _ in range(60)])

    key = manager.pool.keys["key_1"]
    assert key.rpd_used == 60
    assert len(key.rpm_timestamps) == 60


@pytest.mark.asyncio
async def test_get_status_format():
    manager = KeyManager(make_config(["k1", "k2"]))
    status = await manager.get_status()
    keys_list = cast(List[Dict[str, object]], status["keys"])

    assert status["total_keys"] == 2
    assert "available_keys" in status
    assert "exhausted_keys" in status
    assert "next_reset" in status
    assert isinstance(status["keys"], list)
    assert len(keys_list) == 2
    assert "id" in keys_list[0]
    assert "key_prefix" in keys_list[0]


@pytest.mark.asyncio
async def test_get_key_status():
    manager = KeyManager(make_config(["k1"]))

    key_status = await manager.get_key_status("key_1")

    assert key_status is not None
    assert key_status["id"] == "key_1"
    assert await manager.get_key_status("missing") is None
