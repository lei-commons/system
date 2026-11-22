"""Every bulk register's profile, by registry id: lc_ingest.bulk (to batch 2) and lc_ingest.bulk_batch3."""

from .bulk import PROFILES as _BULK
from .bulk_batch3 import BATCH3

PROFILES = {**_BULK, **{p.source: p for p in BATCH3}}
