"""UUIDv7 (time-ordered) primary keys.

Random UUIDv4 keys scatter inserts across the whole B-tree, which hurts badly on the
multi-million-row chunk/section tables. UUIDv7 keeps the same "non-guessable enough,
no sequential integer leak" property (TZ 22) while inserting near the right edge.
Python 3.13 has no uuid.uuid7 (added in 3.14), hence this helper.
"""
import os
import time
import uuid


def uuid7() -> uuid.UUID:
    ms = time.time_ns() // 1_000_000
    r = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = (r >> 68) & 0xFFF
    rand_b = r & ((1 << 62) - 1)
    value = ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
