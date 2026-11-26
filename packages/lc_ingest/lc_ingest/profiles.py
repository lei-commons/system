"""Every bulk register's profile, by registry id: lc_ingest.bulk (to batch 2), lc_ingest.bulk_batch3 and _batch4."""

from .bulk import PROFILES as _BULK
from .bulk_batch3 import BATCH3
from .bulk_batch4 import BATCH4

PROFILES = {**_BULK, **{p.source: p for p in BATCH3 + BATCH4}}
