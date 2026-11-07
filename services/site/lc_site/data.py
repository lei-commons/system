"""The latest snapshot, loaded into DuckDB, and the read-only queries on it.

Every query takes parameters, never formatted strings, and every result is
capped, so nothing a caller types can change what runs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading

import duckdb

MAX_LIMIT = 100
NESTED = ("otherNames", "legalAddress", "headquartersAddress", "industryCodes", "labels")
SUMMARY = "directoryId, legalName, country, registrationNumber, lei, legalStatus, sources"
ID_LIKE = re.compile(r"^[0-9A-Z]{20}$")


class Snapshot:
    """Loads the snapshot into a DuckDB database file.

    DuckDB compresses tables on disk but not purely in memory: 3.35 million
    records took 9.6 GB in memory and 2.7 GB this way, with searches under 100 ms.
    """

    def __init__(self, parquet_glob: str, meta: dict, path: str = None):
        self.meta = meta
        path = path or os.path.join(tempfile.mkdtemp(prefix="site-db-"), "snapshot.duckdb")
        self._con = duckdb.connect(path)
        for setting in ("SET memory_limit='2GB'", "SET threads=2", "SET preserve_insertion_order=false"):
            self._con.execute(setting)
        self._con.execute(
            "CREATE TABLE e AS SELECT *, regexp_extract(filename, '([A-Z]{2})\\.parquet$', 1) AS country "
            "FROM read_parquet(?, filename = true)", [parquet_glob])
        self._con.execute("CHECKPOINT")
        self.count = self._con.execute("SELECT count(*) FROM e").fetchone()[0]
        self._lock = threading.Lock()

    def _rows(self, sql, params):
        with self._lock:
            cur = self._con.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def search(self, q: str, country: str = "", limit: int = 25):
        q = (q or "").strip()[:200]
        limit = max(1, min(int(limit), MAX_LIMIT))
        if not q:
            return []
        country = country.strip().upper()[:2]
        where, params = [], []
        if ID_LIKE.match(q.upper()):
            where.append("(directoryId = ? OR lei = ?)")
            params += [q.upper(), q.upper()]
        else:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(legalName ILIKE ? ESCAPE '\\' OR registrationNumber = ?)")
            params += [pattern, q.upper()]
        if country:
            where.append("country = ?")
            params.append(country)
        sql = f"SELECT {SUMMARY} FROM e WHERE {' AND '.join(where)} ORDER BY legalName LIMIT ?"
        return self._rows(sql, params + [limit])

    def entity(self, directory_id: str):
        rows = self._rows("SELECT * EXCLUDE (filename) FROM e WHERE directoryId = ?", [directory_id.strip().upper()])
        if not rows:
            return None
        record = {k: v for k, v in rows[0].items() if v is not None}
        for field in NESTED:
            if field in record:
                record[field] = json.loads(record[field])
        record["sources"] = record["sources"].split(",")
        return record

    def stats(self):
        def top(column, n):
            return self._rows(f"SELECT {column} AS value, count(*) AS records FROM e GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?", [n])

        return {"snapshot": self.meta, "records": self.count, "byCountry": top("country", 30),
                "bySources": top("sources", 10), "byStatus": top("legalStatus", 15)}
