import secrets
import time
import uuid

# RFC 9562 §5.7 layout:
#   48 bits unix_ts_ms | 4 bits version (7) | 12 bits rand_a
#   | 2 bits variant (0b10) | 62 bits rand_b
_VERSION = 0x7
_VARIANT = 0b10


def uuid7() -> uuid.UUID:
    """A time-ordered UUID.

    Used instead of uuid4 so that primary keys insert sequentially. Random
    keys scatter B-tree writes across the whole index; time-ordered keys keep
    them at the right edge, which matters once tables are large.
    """
    timestamp_ms = int(time.time() * 1000)
    value = (
        (timestamp_ms << 80)
        | (_VERSION << 76)
        | (secrets.randbits(12) << 64)
        | (_VARIANT << 62)
        | secrets.randbits(62)
    )
    return uuid.UUID(int=value)
