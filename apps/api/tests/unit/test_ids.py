import time
from uuid import UUID

from app.core.ids import uuid7


def test_uuid7_reports_version_7():
    assert uuid7().version == 7


def test_uuid7_is_a_uuid():
    assert isinstance(uuid7(), UUID)


def test_uuid7_values_are_unique():
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000


def test_uuid7_is_time_ordered():
    """The point of v7 over v4: sequential inserts stay index-local."""
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert first < second


def test_uuid7_encodes_current_timestamp():
    """The leading 48 bits are unix milliseconds."""
    before_ms = int(time.time() * 1000)
    value = uuid7()
    after_ms = int(time.time() * 1000)
    encoded_ms = value.int >> 80
    assert before_ms <= encoded_ms <= after_ms
