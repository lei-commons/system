"""The latest snapshot in DuckDB, and the read-only queries on it.

Every query takes parameters, never formatted strings, and every result is
capped, so nothing a caller types can change what runs.

Records go into a DuckDB database file, because DuckDB compresses tables on
disk but not purely in memory: 3.35 million records took 9.6 GB in memory and
2.7 GB this way. Search uses DuckDB's full-text index over legal and other
names (BM25), with exact names, then names starting with the query, ranked first.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading

import duckdb

MAX_LIMIT = 100
MAX_PAGE = 100
FACET_LIMIT = 15
NESTED = ("otherNames", "legalAddress", "headquartersAddress", "industryCodes", "labels")
SUMMARY = "directoryId, legalName, country, registrationNumber, lei, legalStatus, sources"
ID_LIKE = re.compile(r"^[0-9A-Z]{20}$")
EXTENSIONS = os.environ.get("LC_DUCKDB_EXTENSIONS", "")
GRAPH_LIMIT = 40
GRAPH_PER_NODE = 60
GRAPH_MAX_EDGES = 400
# Relationship types in plain words, read from the record's own side.
UPWARD = {  # this entity -> the other
    "IS_DIRECTLY_CONSOLIDATED_BY": "direct parent",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimate parent",
    "IS_FUND-MANAGED_BY": "fund manager",
    "IS_SUBFUND_OF": "umbrella fund",
    "IS_FEEDER_TO": "master fund",
    "IS_INTERNATIONAL_BRANCH_OF": "head office",
}
DOWNWARD = {  # the other -> this entity
    "IS_DIRECTLY_CONSOLIDATED_BY": "direct subsidiary",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "ultimately owned",
    "IS_FUND-MANAGED_BY": "managed fund",
    "IS_SUBFUND_OF": "sub-fund",
    "IS_FEEDER_TO": "feeder fund",
    "IS_INTERNATIONAL_BRANCH_OF": "branch",
}


class Snapshot:
    def __init__(self, entity_files: list, meta: dict, relationship_files: list = (), path: str = None):
        self.meta = meta
        path = path or os.path.join(tempfile.mkdtemp(prefix="site-db-"), "snapshot.duckdb")
        self._con = duckdb.connect(path)
        settings = ["SET memory_limit='2GB'", "SET threads=2", "SET preserve_insertion_order=false"]
        if EXTENSIONS:
            settings.append(f"SET extension_directory='{EXTENSIONS}'")
        for setting in settings:
            self._con.execute(setting)
        self._con.execute(
            "CREATE TABLE e AS SELECT * EXCLUDE (filename), regexp_extract(filename, '([A-Z]{2})\\.parquet$', 1) AS country "
            "FROM read_parquet(?, filename = true)", [list(entity_files)])
        self._con.execute("INSTALL fts")
        self._con.execute("LOAD fts")
        # Keep digits: "3M" and "APPLE 12" are names. The default tokenizer drops them.
        self._con.execute("PRAGMA create_fts_index('e', 'directoryId', 'legalName', 'otherNames', "
                          "stemmer='none', stopwords='none', ignore='(\\.|[^a-z0-9])+', overwrite=1)")
        if relationship_files:
            self._con.execute("CREATE TABLE r AS SELECT * FROM read_parquet(?)", [list(relationship_files)])
        else:
            self._con.execute("CREATE TABLE r (relationshipId VARCHAR, startNode VARCHAR, endNode VARCHAR, startId VARCHAR, "
                              "endId VARCHAR, relationshipType VARCHAR, relationshipStatus VARCHAR, percentage VARCHAR, "
                              "periodStart VARCHAR, periodEnd VARCHAR)")
        self._con.execute("CHECKPOINT")
        self.count = self._con.execute("SELECT count(*) FROM e").fetchone()[0]
        self.relationship_count = self._con.execute("SELECT count(*) FROM r").fetchone()[0]
        self._lock = threading.Lock()

    def _rows(self, sql, params):
        with self._lock:
            cur = self._con.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def search(self, q: str, country: str = "", status: str = "", source: str = "", limit: int = 25, page: int = 1):
        """Return {"results", "total", "facets"} for a query, with optional filters."""
        q = (q or "").strip()[:200]
        empty = {"results": [], "total": 0, "facets": {}}
        if not q:
            return empty
        limit = max(1, min(int(limit), MAX_LIMIT))
        page = max(1, min(int(page), MAX_PAGE))
        filters, params = [], []
        for column, value in (("country", country.strip().upper()[:2]), ("legalStatus", status.strip()[:60])):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        if source:
            filters.append("list_contains(string_split(sources, ','), ?)")
            params.append(source.strip()[:40])
        where = "".join(f" AND {f}" for f in filters)

        # IDs and registration numbers are looked up exactly; "3M" is still a name.
        if ID_LIKE.match(q.upper()) or (q.isalnum() and len(q) >= 6 and any(c.isdigit() for c in q)):
            matched = "SELECT *, 1.0 AS score FROM e WHERE (directoryId = ? OR lei = ? OR registrationNumber = ?)"
            match_params = [q.upper(), q.upper(), q.upper()]
            order = "legalName"
        else:
            matched = ("SELECT *, fts_main_e.match_bm25(directoryId, ?, conjunctive := 1) AS score FROM e")
            match_params = [q]
            order = "(upper(legalName) = upper(?)) DESC, starts_with(upper(legalName), upper(?)) DESC, score DESC, legalName"
        base = f"SELECT * FROM ({matched}) WHERE score IS NOT NULL{where}"
        order_params = [] if order == "legalName" else [q, q]

        total = self._rows(f"SELECT count(*) AS n FROM ({base})", match_params + params)[0]["n"]
        results = self._rows(f"SELECT {SUMMARY} FROM ({base}) ORDER BY {order} LIMIT ? OFFSET ?",
                             match_params + params + order_params + [limit, (page - 1) * limit])
        facets = {}
        for name, expr in (("country", "country"), ("status", "legalStatus"), ("source", "unnest(string_split(sources, ','))")):
            # The expression goes in an inner query, because DuckDB can't group by an unnest directly.
            facets[name] = self._rows(f"SELECT value, count(*) AS records FROM (SELECT {expr} AS value FROM ({base})) "
                                      f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {FACET_LIMIT}", match_params + params)
        return {"results": results, "total": total, "facets": facets}

    def entity(self, directory_id: str):
        rows = self._rows("SELECT * FROM e WHERE directoryId = ?", [directory_id.strip().upper()])
        if not rows:
            return None
        record = {k: v for k, v in rows[0].items() if v is not None}
        for field in NESTED:
            if field in record:
                record[field] = json.loads(record[field])
        record["sources"] = record["sources"].split(",")
        return record

    def relationships(self, directory_id: str):
        """The entity's relationships in both directions, each with the other side's name if we hold it."""
        directory_id = directory_id.strip().upper()
        sql = ("SELECT r.relationshipType, r.relationshipStatus, r.percentage, r.{other}Node AS otherLei, "
               "r.{other}Id AS otherId, e.legalName AS otherName, e.country AS otherCountry "
               "FROM r LEFT JOIN e ON e.directoryId = r.{other}Id WHERE r.{this}Id = ? "
               "ORDER BY r.relationshipType, otherName LIMIT ?")
        up = self._rows(sql.format(other="end", this="start"), [directory_id, GRAPH_LIMIT + 1])
        down = self._rows(sql.format(other="start", this="end"), [directory_id, GRAPH_LIMIT + 1])
        down_total = self._rows("SELECT count(*) AS n FROM r WHERE endId = ?", [directory_id])[0]["n"]
        for row in up:
            row["role"] = UPWARD.get(row["relationshipType"], row["relationshipType"].lower())
        for row in down:
            row["role"] = DOWNWARD.get(row["relationshipType"], row["relationshipType"].lower())
        return {"up": up[:GRAPH_LIMIT], "down": down[:GRAPH_LIMIT], "downTotal": down_total}

    def suggest(self, q: str, limit: int = 8):
        """Names starting with the query, shortest first, for search-as-you-type."""
        q = (q or "").strip()[:100]
        if len(q) < 2:
            return []
        prefix = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return self._rows("SELECT directoryId, legalName, country FROM e WHERE legalName ILIKE ? ESCAPE '\\' "
                          "ORDER BY length(legalName), legalName LIMIT ?", [prefix, max(1, min(int(limit), 20))])

    def graph(self, directory_id: str, depth: int = 1, per_node: int = GRAPH_PER_NODE, max_edges: int = GRAPH_MAX_EDGES):
        """The neighbourhood of an entity: nodes and edges ready to draw.

        Walks relationships both ways for up to ``depth`` steps. Each node shows at
        most ``per_node`` of its relationships and the whole graph at most
        ``max_edges``; ``truncated`` says when something was left out. Entities we
        don't hold appear by LEI only, with ``inDirectory`` false.
        """
        directory_id = directory_id.strip().upper()
        depth = max(1, min(int(depth), 3))
        if not self._rows("SELECT 1 FROM e WHERE directoryId = ?", [directory_id]):
            return None
        edges, seen_edges, frontier, visited, truncated = [], set(), [directory_id], {directory_id}, False
        for _ in range(depth):
            if not frontier or len(edges) >= max_edges:
                break
            rows = self._rows(
                "SELECT * FROM (SELECT relationshipId, startNode, endNode, startId, endId, relationshipType, relationshipStatus, "
                "percentage, CASE WHEN list_contains(?, startId) THEN startId ELSE endId END AS via, "
                "row_number() OVER (PARTITION BY CASE WHEN list_contains(?, startId) THEN startId ELSE endId END "
                "ORDER BY relationshipType, relationshipId) AS n "
                "FROM r WHERE list_contains(?, startId) OR list_contains(?, endId)) WHERE n <= ? + 1",
                [frontier, frontier, frontier, frontier, per_node])
            per_via = {}
            for row in rows:
                per_via[row["via"]] = per_via.get(row["via"], 0) + 1
            nxt = []
            for row in rows:
                if row["n"] > per_node:
                    truncated = True
                    continue
                if row["relationshipId"] in seen_edges:
                    continue
                if len(edges) >= max_edges:
                    truncated = True
                    break
                seen_edges.add(row["relationshipId"])
                edges.append(row)
                for side in ("startId", "endId"):
                    other = row[side]
                    if other and other not in visited:
                        visited.add(other)
                        nxt.append(other)
            frontier = nxt
        known = self._rows("SELECT directoryId, lei, legalName, country, legalStatus, legalJurisdiction FROM e "
                           "WHERE list_contains(?, directoryId)", [sorted(visited)])
        nodes = {k["directoryId"]: {"id": k["directoryId"], "lei": k["lei"], "name": k["legalName"], "country": k["country"],
                                    "status": k["legalStatus"], "inDirectory": True} for k in known}
        out_edges = []
        for row in edges:
            ends = []
            for side, lei in (("startId", row["startNode"]), ("endId", row["endNode"])):
                node_id = row[side] or f"LEI:{lei}"
                if node_id not in nodes:
                    nodes[node_id] = {"id": node_id, "lei": lei, "name": None, "country": None, "status": None,
                                      "inDirectory": False}
                ends.append(node_id)
            out_edges.append({"id": row["relationshipId"], "source": ends[0], "target": ends[1],
                              "type": row["relationshipType"], "status": row["relationshipStatus"],
                              "percentage": row["percentage"]})
        return {"focus": directory_id, "nodes": list(nodes.values()), "edges": out_edges, "truncated": truncated}

    def stats(self):
        def top(column, n):
            return self._rows(f"SELECT {column} AS value, count(*) AS records FROM e GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT ?", [n])

        rel_types = self._rows("SELECT relationshipType AS value, count(*) AS records FROM r GROUP BY 1 ORDER BY 2 DESC", [])
        return {"snapshot": self.meta, "records": self.count, "relationships": self.relationship_count,
                "byCountry": top("country", 30), "bySources": top("sources", 10), "byStatus": top("legalStatus", 15),
                "relationshipTypes": rel_types}
