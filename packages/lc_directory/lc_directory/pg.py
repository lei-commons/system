"""Read-only queries on the serving database, with the same interface as the
site's in-memory snapshot, so the site works against either.

Every query takes parameters, never formatted strings, every result is capped,
and every connection has a statement timeout, so one heavy query can't hang
the site. Match counts are exact up to COUNT_CAP and reported as capped above
it, so a common word across tens of millions of records stays fast.
"""

from __future__ import annotations

import re

from psycopg.rows import dict_row

MAX_LIMIT = 100
MAX_PAGE = 100
COUNT_CAP = 10_000
FACET_LIMIT = 15
GRAPH_LIMIT = 40
GRAPH_PER_NODE = 60
GRAPH_MAX_EDGES = 400
ID_LIKE = re.compile(r"^[0-9A-Z]{20}$")
CAMEL = {"directory_id": "directoryId", "legal_name": "legalName", "legal_form": "legalForm",
         "legal_jurisdiction": "legalJurisdiction", "registration_authority": "registrationAuthority",
         "registration_number": "registrationNumber", "legal_status": "legalStatus",
         "incorporation_date": "incorporationDate", "dissolution_date": "dissolutionDate", "record_mode": "recordMode",
         "registry_url": "registryUrl", "other_names": "otherNames", "legal_address": "legalAddress",
         "headquarters_address": "headquartersAddress", "industry_codes": "industryCodes"}
UPWARD = {"IS_DIRECTLY_CONSOLIDATED_BY": "direct parent", "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimate parent",
          "IS_FUND-MANAGED_BY": "fund manager", "IS_SUBFUND_OF": "umbrella fund", "IS_FEEDER_TO": "master fund",
          "IS_INTERNATIONAL_BRANCH_OF": "head office"}
DOWNWARD = {"IS_DIRECTLY_CONSOLIDATED_BY": "direct subsidiary", "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimately owned",
            "IS_FUND-MANAGED_BY": "managed fund", "IS_SUBFUND_OF": "sub-fund", "IS_FEEDER_TO": "feeder fund",
            "IS_INTERNATIONAL_BRANCH_OF": "branch"}
SUMMARY = ("directory_id AS \"directoryId\", legal_name AS \"legalName\", country, registration_number AS "
           "\"registrationNumber\", lei, legal_status AS \"legalStatus\", array_to_string(sources, ',') AS sources")


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Directory:
    """``pool`` is a psycopg ConnectionPool (or anything with a ``connection()`` context manager)."""

    def __init__(self, pool):
        self._pool = pool
        meta = None
        if self._one("SELECT 1 AS ok FROM information_schema.tables WHERE table_schema = 'live' AND table_name = 'meta'", []):
            meta = self._one("SELECT date, snapshot_root, records, relationships FROM live.meta", [])
        self.meta = {"date": meta["date"], "snapshotRoot": meta["snapshot_root"]} if meta else {}
        self.count = meta["records"] if meta else 0
        self.relationship_count = meta["relationships"] if meta else 0

    def _rows(self, query, params):
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                return cur.fetchall()

    def _one(self, query, params):
        rows = self._rows(query, params)
        return rows[0] if rows else None

    def search(self, q: str, country: str = "", status: str = "", source: str = "", limit: int = 25, page: int = 1):
        q = (q or "").strip()[:200]
        if not q:
            return {"results": [], "total": 0, "totalCapped": False, "facets": {}}
        limit = max(1, min(int(limit), MAX_LIMIT))
        page = max(1, min(int(page), MAX_PAGE))
        where, params = [], []
        if ID_LIKE.match(q.upper()) or (q.isalnum() and len(q) >= 6 and any(c.isdigit() for c in q)):
            where.append("(directory_id = %s OR lei = %s OR registration_number = %s)")
            params += [q.upper(), q.upper(), q.upper()]
            order, order_params = "legal_name", []
        else:
            where.append("search @@ websearch_to_tsquery('simple', %s)")
            params.append(q)
            order = ("(upper(legal_name) = upper(%s)) DESC, starts_with(upper(legal_name), upper(%s)) DESC, "
                     "ts_rank(search, websearch_to_tsquery('simple', %s)) DESC, legal_name")
            order_params = [q, q, q]
        for column, value in (("country", country.strip().upper()[:2]), ("legal_status", status.strip()[:60])):
            if value:
                where.append(f"{column} = %s")
                params.append(value)
        if source:
            where.append("%s = ANY(sources)")
            params.append(source.strip()[:40])
        cond = " AND ".join(where)
        matched = f"SELECT * FROM live.entities WHERE {cond}"
        total = self._one(f"SELECT count(*) AS n FROM (SELECT 1 FROM live.entities WHERE {cond} LIMIT {COUNT_CAP + 1}) x", params)["n"]
        results = self._rows(f"SELECT {SUMMARY} FROM ({matched}) m ORDER BY {order} LIMIT %s OFFSET %s",
                             params + order_params + [limit, (page - 1) * limit])
        sample = f"SELECT country, legal_status, sources FROM live.entities WHERE {cond} LIMIT {COUNT_CAP}"
        facets = {}
        for name, expr in (("country", "country"), ("status", "legal_status"), ("source", "unnest(sources)")):
            rows = self._rows(f"SELECT value, count(*) AS records FROM (SELECT {expr} AS value FROM ({sample}) s) v "
                              f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {FACET_LIMIT}", params)
            facets[name] = rows
        return {"results": results, "total": min(total, COUNT_CAP), "totalCapped": total > COUNT_CAP, "facets": facets}

    def suggest(self, q: str, limit: int = 8):
        q = (q or "").strip()[:100]
        if len(q) < 2:
            return []
        return self._rows("SELECT directory_id AS \"directoryId\", legal_name AS \"legalName\", country FROM live.entities "
                          "WHERE lower(legal_name) LIKE %s ORDER BY length(legal_name), legal_name LIMIT %s",
                          [_like(q.lower()) + "%", max(1, min(int(limit), 20))])

    def entity(self, directory_id: str):
        row = self._one("SELECT * FROM live.entities WHERE directory_id = %s", [directory_id.strip().upper()])
        if not row:
            return None
        record = {CAMEL.get(k, k): v for k, v in row.items() if v is not None and k not in ("search", "names_text")}
        return record

    def relationships(self, directory_id: str):
        directory_id = directory_id.strip().upper()
        base = ("SELECT r.relationship_type AS \"relationshipType\", r.relationship_status AS \"relationshipStatus\", "
                "r.percentage, r.{other}_node AS \"otherLei\", r.{other}_id AS \"otherId\", e.legal_name AS \"otherName\", "
                "e.country AS \"otherCountry\" FROM live.relationships r LEFT JOIN live.entities e ON e.directory_id = r.{other}_id "
                "WHERE r.{this}_id = %s ORDER BY r.relationship_type, e.legal_name LIMIT %s")
        up = self._rows(base.format(other="end", this="start"), [directory_id, GRAPH_LIMIT + 1])
        down = self._rows(base.format(other="start", this="end"), [directory_id, GRAPH_LIMIT + 1])
        down_total = self._one("SELECT count(*) AS n FROM live.relationships WHERE end_id = %s", [directory_id])["n"]
        for row in up:
            row["role"] = UPWARD.get(row["relationshipType"], row["relationshipType"].lower())
        for row in down:
            row["role"] = DOWNWARD.get(row["relationshipType"], row["relationshipType"].lower())
        return {"up": up[:GRAPH_LIMIT], "down": down[:GRAPH_LIMIT], "downTotal": down_total}

    def graph(self, directory_id: str, depth: int = 1, per_node: int = GRAPH_PER_NODE, max_edges: int = GRAPH_MAX_EDGES):
        directory_id = directory_id.strip().upper()
        depth = max(1, min(int(depth), 3))
        if not self._one("SELECT 1 AS ok FROM live.entities WHERE directory_id = %s", [directory_id]):
            return None
        edges, seen, frontier, visited, truncated = [], set(), [directory_id], {directory_id}, False
        for _ in range(depth):
            if not frontier or len(edges) >= max_edges:
                break
            rows = self._rows(
                "SELECT * FROM (SELECT relationship_id, start_node, end_node, start_id, end_id, relationship_type, "
                "relationship_status, percentage, row_number() OVER (PARTITION BY CASE WHEN start_id = ANY(%(f)s) "
                "THEN start_id ELSE end_id END ORDER BY relationship_type, relationship_id) AS n FROM live.relationships "
                "WHERE start_id = ANY(%(f)s) OR end_id = ANY(%(f)s)) x WHERE n <= %(p)s + 1",
                {"f": frontier, "p": per_node})
            nxt = []
            for row in rows:
                if row["n"] > per_node:
                    truncated = True
                    continue
                if row["relationship_id"] in seen:
                    continue
                if len(edges) >= max_edges:
                    truncated = True
                    break
                seen.add(row["relationship_id"])
                edges.append(row)
                for side in ("start_id", "end_id"):
                    other = row[side]
                    if other and other not in visited:
                        visited.add(other)
                        nxt.append(other)
            frontier = nxt
        known = self._rows("SELECT directory_id, lei, legal_name, country, legal_status FROM live.entities "
                           "WHERE directory_id = ANY(%s)", [sorted(visited)])
        nodes = {k["directory_id"]: {"id": k["directory_id"], "lei": k["lei"], "name": k["legal_name"], "country": k["country"],
                                     "status": k["legal_status"], "inDirectory": True} for k in known}
        out = []
        for row in edges:
            ends = []
            for side, lei in (("start_id", row["start_node"]), ("end_id", row["end_node"])):
                node_id = row[side] or f"LEI:{lei}"
                nodes.setdefault(node_id, {"id": node_id, "lei": lei, "name": None, "country": None, "status": None,
                                           "inDirectory": False})
                ends.append(node_id)
            out.append({"id": row["relationship_id"], "source": ends[0], "target": ends[1], "type": row["relationship_type"],
                        "status": row["relationship_status"], "percentage": row["percentage"]})
        return {"focus": directory_id, "nodes": list(nodes.values()), "edges": out, "truncated": truncated}

    def stats(self):
        meta = self._one("SELECT stats FROM live.meta", [])
        stats = (meta or {}).get("stats") or {}
        return {"snapshot": self.meta, "records": self.count, "relationships": self.relationship_count,
                "byCountry": stats.get("byCountry", []), "bySources": stats.get("bySources", []),
                "byStatus": stats.get("byStatus", []), "relationshipTypes": stats.get("relationshipTypes", [])}


def token_pool(host: str, dbname: str, user: str, min_size: int = 1, max_size: int = 8):
    """A pool that signs in with the service's own Google identity (IAM database authentication).

    Each new connection gets a fresh OAuth token as its password, and connections
    are recycled well inside the token's hour, so nothing holds a stale credential.
    """
    import google.auth
    import psycopg
    from google.auth.transport.requests import Request
    from psycopg_pool import ConnectionPool

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/sqlservice.login"])

    class TokenConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo="", **kwargs):
            if not credentials.valid:
                credentials.refresh(Request())
            return super().connect(conninfo, password=credentials.token, **kwargs)

    conninfo = f"host={host} dbname={dbname} user={user} sslmode=require options='-c statement_timeout=5000'"
    return ConnectionPool(conninfo, connection_class=TokenConnection, min_size=min_size, max_size=max_size,
                          max_lifetime=1800, open=True)
