import time
from datetime import datetime, date

from app.models import (
    ApiKey,
    PoolState,
    STATUS_ACTIVE,
    STATUS_EXHAUSTED,
    STATUS_DISABLED,
    STATUS_COOLDOWN,
)


def test_api_key_defaults():
    key = ApiKey(id="key_1", key="test-key-123")

    assert key.id == "key_1"
    assert key.key == "test-key-123"
    assert key.project_id == ""
    assert key.rpd_limit == 250
    assert key.rpm_limit == 10
    assert key.rpd_used == 0
    assert key.rpm_timestamps == []
    assert key.last_used is None
    assert key.last_error is None
    assert key.consecutive_failures == 0
    assert key.status == STATUS_ACTIVE


def test_api_key_rpd_remaining():
    key = ApiKey(id="key_1", key="test-key-123", rpd_limit=250, rpd_used=42)

    assert key.rpd_remaining == 208


def test_api_key_rpm_current():
    current_time = time.time()

    key = ApiKey(
        id="key_1",
        key="test-key-123",
        rpm_timestamps=[
            current_time - 30,
            current_time - 45,
            current_time - 70,
            current_time - 120,
        ],
    )

    assert key.rpm_current == 2


def test_api_key_key_prefix():
    key1 = ApiKey(id="key_1", key="AIzaSyABC")
    assert key1.key_prefix() == "AIzaSyABC"

    key2 = ApiKey(id="key_2", key="AIzaSyABCDEF123456789")
    assert key2.key_prefix() == "AIzaSyAB...789"


def test_pool_state_creation():
    pool = PoolState()

    assert pool.keys == {}
    assert pool.last_reset_date is None

    pool_with_data = PoolState(
        keys={"key_1": ApiKey(id="key_1", key="test-key")},
        last_reset_date=date(2026, 2, 13),
    )

    assert len(pool_with_data.keys) == 1
    assert "key_1" in pool_with_data.keys
    assert pool_with_data.last_reset_date == date(2026, 2, 13)
