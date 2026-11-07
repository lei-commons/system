"""The shard function, the same as lc_ingest.store.shard_of.

Copied rather than imported so publishing doesn't depend on the ingestion
package; a test checks the two agree.
"""

from __future__ import annotations

import hashlib

SHARDS = 256


def shard_of(directory_id: str) -> int:
    return hashlib.sha256(directory_id.encode()).digest()[0] % SHARDS
