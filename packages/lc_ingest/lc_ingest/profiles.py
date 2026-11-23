"""Every bulk register's profile, by registry id: lc_ingest.bulk (to batch 2), lc_ingest.bulk_batch3 and the UK
corporate owners (lc_ingest.psc)."""

from .bulk import PROFILES as _BULK
from .bulk_batch3 import BATCH3
from .psc import UK_PSC

PROFILES = {**_BULK, **{p.source: p for p in BATCH3}, UK_PSC.source: UK_PSC}
