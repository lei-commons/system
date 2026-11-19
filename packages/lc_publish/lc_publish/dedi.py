"""DeDi publication: lei-commons describes itself as signed DeDi files (docs/dedi.md).

Every day, after the snapshot is built, the ``publish-dedi`` job (a step of the daily cycle)
writes into the snapshots bucket, under ``dedi/``:

- ``dedi.data-sources.json``  every data source: licence, attribution, approval, records (registry ``data-sources``)
- ``dedi.countries.json``     per country: the latest full snapshot's files, each with a permanent
                              content-addressed URL, SHA-256, size, records and Merkle root, and how to
                              bring them to the current day (registry ``countries``)
- ``dedi.snapshots.json``     the kept daily editions: manifest, root, signature, recipe (registry ``snapshots``)
- ``dedi.digests.json``       the signed digest of each file, re-issued daily
- ``dedi.index.json``         the signed manifest, served at ``/.well-known/dedi.index.json``: our key(s)
                              and the files; re-issued only when keys or the file list change, or before
                              it goes stale
- ``schemas/*.json``          the record schemas the files use
- ``status.json``             what the last publication did (for the System page and the check)

Safety, in order: everything is built and verified in memory with the protocol's five steps before
anything is written; registry files go up first, then the digests, then the manifest (like
latest.json, the pointer last); each object is read back and compared; the whole set is then verified
again from the bucket. A failure leaves the previous set in place, which stays valid until its
``next_update`` (36 hours: one missed day is absorbed), and is recorded, alerted and retried.
``check`` verifies the published set independently (the ``dedi-check`` job and status.yml).
"""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import yaml

from lc_dedi import documents as dd
from lc_dedi import jws

PREFIX = "dedi/"
MANIFEST_OBJECT = PREFIX + "dedi.index.json"
DIGESTS_OBJECT = PREFIX + "dedi.digests.json"
STATUS_OBJECT = PREFIX + "status.json"
REGISTRIES = ("data-sources", "countries", "snapshots")
SCHEMAS = {"data-sources": "data-source.v1.json", "countries": "country-snapshot.v1.json",
           "snapshots": "snapshot-edition.v1.json"}
WELL_KNOWN = "/.well-known/dedi.index.json"
SERVED = {MANIFEST_OBJECT: WELL_KNOWN}  # object -> path on the site; others: "/" + object
MANIFEST_REISSUE_BEFORE = timedelta(hours=72)  # re-sign an unchanged manifest this long before it goes stale
DATE_LEN = 10


class PublishError(RuntimeError):
    """The DeDi set could not be published; the previous one stays in place."""


class TransientError(PublishError):
    """Worth trying again: an object didn't read back as written, or storage or KMS failed."""


# ---- configuration ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    enabled: bool
    domain: str
    base_url: str
    data_base_url: str
    name: str
    description: str
    kid: str
    signer: str
    ref: str
    also_list: tuple
    files_hours: int
    manifest_hours: int
    registries: dict

    def url(self, obj: str) -> str:
        return self.base_url + SERVED.get(obj, "/" + obj)

    def data_url(self, obj: str) -> str:
        return f"{self.data_base_url}/{obj}"

    def file_object(self, registry: str) -> str:
        return f"{PREFIX}dedi.{registry}.json"


def _default_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "dedi.yaml"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("config/dedi.yaml")


def load_config(path=None) -> Config:
    """config/dedi.yaml, checked: anything missing, unknown or out of range stops the DeDi step."""
    raw = yaml.safe_load(Path(path or os.environ.get("LC_DEDI_CONFIG") or _default_path()).read_text(encoding="utf-8"))
    top = {"version", "enabled", "domain", "base_url", "data_base_url", "name", "description", "key", "freshness_hours",
           "registries"}
    _keys(raw, top, "dedi.yaml")
    if raw["version"] != 1:
        raise ValueError("dedi.yaml: version must be 1")
    domain = raw["domain"]
    if not isinstance(domain, str) or "/" in domain or ":" in domain or "." not in domain or domain != domain.lower():
        raise ValueError("dedi.yaml: domain must be a lower-case host name")
    for k in ("base_url", "data_base_url"):
        if not str(raw[k]).startswith("https://") or raw[k].endswith("/"):
            raise ValueError(f"dedi.yaml: {k} must be an https URL without a trailing slash")
    if raw["base_url"].split("/")[2] != domain:
        raise ValueError("dedi.yaml: base_url must be served from the domain (the manifest is authoritative only there)")
    key = raw["key"]
    _keys(key, {"kid", "signer", "ref", "also_list"}, "dedi.yaml key")
    if key["signer"] not in ("kms", "seed"):
        raise ValueError("dedi.yaml: key.signer must be kms or seed")
    fresh = raw["freshness_hours"]
    _keys(fresh, {"files", "manifest"}, "dedi.yaml freshness_hours")
    if not (25 <= fresh["files"] <= 96):
        raise ValueError("dedi.yaml: freshness_hours.files must be 25..96 (longer than a day, so one late cycle is absorbed)")
    if not (fresh["files"] <= fresh["manifest"] <= 24 * 30):
        raise ValueError("dedi.yaml: freshness_hours.manifest must be between files and 720")
    if fresh["manifest"] * 3600 <= MANIFEST_REISSUE_BEFORE.total_seconds():
        raise ValueError("dedi.yaml: freshness_hours.manifest must exceed 72")
    regs = raw["registries"]
    _keys(regs, set(REGISTRIES), "dedi.yaml registries")
    _keys(regs["data-sources"], {"enabled", "include_assessment", "include_pending"}, "dedi.yaml data-sources")
    _keys(regs["countries"], {"enabled"}, "dedi.yaml countries")
    _keys(regs["snapshots"], {"enabled", "max_records"}, "dedi.yaml snapshots")
    if not (1 <= regs["snapshots"]["max_records"] <= 5000):
        raise ValueError("dedi.yaml: snapshots.max_records must be 1..5000")
    return Config(enabled=bool(raw["enabled"]), domain=domain, base_url=raw["base_url"], data_base_url=raw["data_base_url"],
                  name=raw["name"], description=" ".join(str(raw["description"]).split()), kid=key["kid"],
                  signer=key["signer"], ref=key["ref"], also_list=tuple(key["also_list"] or ()),
                  files_hours=int(fresh["files"]), manifest_hours=int(fresh["manifest"]), registries=regs)


def _keys(d, allowed: set, where: str):
    if not isinstance(d, dict):
        raise ValueError(f"{where}: must be a mapping")
    missing, unknown = allowed - set(d), set(d) - allowed
    if missing or unknown:
        raise ValueError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")


def make_signer(cfg: Config, secret_reader=None, secret_writer=None, record=None) -> jws.Signer:
    """The signing key. ``seed``: a private JWK (dedi-cli's key.json format) in Secret Manager, at ``key.ref``
    (a secret name, projects/<p>/secrets/<s>). The first run creates it if the secret has no version yet, so no
    person ever sees the key; Secret Manager keeps it, and only the publishing identity may read or add one.
    ``kms``: a Cloud KMS EC_SIGN_ED25519 key version (KMS signs the raw message, so very large files may exceed
    its request size; the seed signer has no such limit)."""
    if cfg.signer == "kms":
        return jws.KmsSigner(cfg.kid, cfg.ref)
    read, write = secret_reader or _read_secret, secret_writer or _add_secret_version
    try:
        private = json.loads(read(cfg.ref + "/versions/latest"))
    except Exception as exc:
        if type(exc).__name__ != "NotFound":
            raise
        import secrets

        write(cfg.ref, json.dumps(jws.SeedSigner(cfg.kid, secrets.token_bytes(32)).private_jwk()).encode())
        private = json.loads(read(cfg.ref + "/versions/latest"))  # whatever is latest now is the key
        (record or (lambda e: None))({"event": "dedi-key-created", "kid": cfg.kid, "x": private.get("x")})
    if private.get("kid") != cfg.kid:
        raise ValueError(f"the key in {cfg.ref} is {private.get('kid')!r}, but dedi.yaml says {cfg.kid!r}")
    return jws.SeedSigner.from_private_jwk(private)


def _read_secret(name: str) -> bytes:
    from google.cloud import secretmanager

    return secretmanager.SecretManagerServiceClient().access_secret_version(request={"name": name}).payload.data


def _add_secret_version(secret: str, data: bytes) -> None:
    from google.cloud import secretmanager

    secretmanager.SecretManagerServiceClient().add_secret_version(request={"parent": secret, "payload": {"data": data}})


# ---- the records ----------------------------------------------------------------------------------------------

def record_schema(registry: str, cfg: Config) -> dict:
    text = resources.files("lc_publish").joinpath("dedi_data", SCHEMAS[registry]).read_text(encoding="utf-8")
    return json.loads(text.replace("{base_url}", cfg.base_url))


def country_names() -> dict:
    return json.loads(resources.files("lc_publish").joinpath("dedi_data", "countries.json").read_text(encoding="utf-8"))["names"]


def data_source_records(cfg: Config, sources: list, effective: dict, latest: dict) -> list:
    """One record per source in the registry (config/sources.yaml), with the owner's current decision."""
    opts = cfg.registries["data-sources"]
    per_source = (latest or {}).get("recordsPerSource") or {}
    out = []
    for s in sources:
        decision = effective.get(s["id"], {"status": "pending"})
        if decision["status"] != "approved" and not opts["include_pending"]:
            continue
        p = s.get("pipeline") or {}
        records = per_source.get(p.get("records_as")) if p else None
        details = {
            "source_id": s["id"], "name": s["name"], "label": p.get("label"),
            "country": s.get("country") or "*", "access": s.get("access") or "open", "method": s.get("method"),
            "cadence": s.get("cadence"), "approval": decision["status"],
            "approved_at": str(decision["at"]) if decision.get("at") else None,
            "in_use": bool(p) and decision["status"] == "approved" and bool(records),
            "records": int(records) if records is not None else None, "records_as": p.get("records_as"),
            "registration_authority": p.get("registration_authority"), "url": s["url"], "terms_url": s.get("terms_url"),
            "licence": s.get("licence") or "unstated", "licence_verified": bool((s.get("assessment") or {}).get("verified")),
            "attribution": p.get("attribution") or None, "record_url_template": p.get("record_url"),
            "refresh": p.get("refresh"),
        }
        a = s.get("assessment") or {}
        if opts["include_assessment"] and a:
            details["assessment"] = {k2: " ".join(str(a[k1]).split()) for k1, k2 in
                                     (("risk", "risk"), ("commercial", "commercial_use"),
                                      ("attribution", "attribution_requirement"), ("summary", "summary")) if a.get(k1)}
        out.append({"record_name": s["id"], "details": details})
    return out


def country_records(cfg: Config, latest: dict, anchor: dict, anchor_sha256: str, signed: set) -> list:
    """One record per country in the latest full snapshot (``anchor``): its files, and today's state."""
    names, credits = country_names(), {s["id"]: s for s in latest.get("sources", [])}
    current, groups = {}, {}
    for p in latest["parts"]:
        if p.get("kind") == "entities":
            current[p["country"]] = current.get(p["country"], 0) + int(p["records"])
    for p in anchor["parts"]:
        if p.get("kind") == "entities":
            groups.setdefault(p["country"], []).append(p)
    manifest_obj = f"{anchor['date']}/manifest.json"
    out = []
    for country in sorted(groups):
        parts = sorted(groups[country], key=lambda p: p["name"])
        used = sorted({s for p in parts for s in p["sources"]})
        cname = names.get(country, country)
        details = {
            "publisher_id": cfg.domain,
            "name": f"lei-commons: legal entities of {cname} ({country})",
            "description": (f"Every legal entity lei-commons holds for {cname}, from the full snapshot of {anchor['date']}, "
                            f"as Parquet files with their SHA-256 and Merkle roots. data_url is that snapshot's signed "
                            f"manifest; current gives the latest day and the changes to apply."),
            "tags": ["legal-entities", "lei", "company-register", "open-data", f"country:{country}"],
            "type": "json", "data_url": cfg.data_url(manifest_obj),
            "data_url_checksum": anchor_sha256.split(":", 1)[1], "data_url_checksum_type": "sha256",
            "country": country, "country_name": cname, "format": "parquet",
            "schema_url": f"{cfg.base_url}/dedi/schemas/country-snapshot.v1.json",
            "full_snapshot": {"date": anchor["date"], "snapshot_root": anchor["snapshotRoot"],
                              "manifest_url": cfg.data_url(manifest_obj), "manifest_sha256": anchor_sha256,
                              "signature_url": cfg.data_url(manifest_obj + ".sigstore.json") if anchor["date"] in signed else None,
                              "records": sum(int(p["records"]) for p in parts)},
            "parts": [{"name": p["name"], "url": cfg.data_url(p["object"]), "sha256": p["sha256"], "bytes": int(p["bytes"]),
                       "records": int(p["records"]), "root": p["root"], "sources": list(p["sources"])} for p in parts],
            "current": {"date": latest["date"], "records": current.get(country, 0), "snapshot_root": latest["snapshotRoot"],
                        "recipe_url": f"{cfg.base_url}/api/v1/snapshots/{latest['date']}/recipe"},
            "sources": [{"source_id": s, "licence": (credits.get(s) or {}).get("licence", "unstated"),
                         "attribution": (credits.get(s) or {}).get("attribution") or None,
                         "citation": (credits.get(s) or {}).get("citation") or None} for s in used],
            "notice_url": cfg.data_url(f"{anchor['date']}/NOTICE.txt"),
        }
        out.append({"record_name": country, "details": details})
    return out


def edition_records(cfg: Config, editions: list, signed: set) -> list:
    """One record per kept day, newest first. ``editions``: [(manifest, sha256 of its bytes)]."""
    out = []
    keep = cfg.registries["snapshots"]["max_records"]
    for m, sha in sorted(editions, key=lambda e: e[0]["date"], reverse=True)[:keep]:
        obj = f"{m['date']}/manifest.json"
        details = {"date": m["date"], "kind": m["kind"], "anchor": m["anchor"], "base": (m.get("base") or {}).get("date"),
                   "snapshot_root": m["snapshotRoot"], "records": int(m["records"]), "relationships": int(m["relationships"]),
                   "changes": {k: int(v) for k, v in (m.get("changes") or {}).items() if isinstance(v, int)},
                   "manifest_url": cfg.data_url(obj), "manifest_sha256": sha,
                   "signature_url": cfg.data_url(obj + ".sigstore.json") if m["date"] in signed else None,
                   "recipe_url": f"{cfg.base_url}/api/v1/snapshots/{m['date']}/recipe",
                   "decision_log": m.get("decisionLog")}
        if m["kind"] == "full":
            details["relationship_parts"] = [
                {"url": cfg.data_url(p["object"]), "sha256": p["sha256"], "bytes": int(p["bytes"]),
                 "records": int(p["records"]), "root": p["root"]}
                for p in m["parts"] if p.get("kind") == "relationships"]
        out.append({"record_name": m["date"], "details": details})
    return out


REGISTRY_TEXT = {
    "data-sources": ("Every data source lei-commons uses or has considered, with its licence, the credit it asks for, "
                     "the owner's approval and the records it contributes. Only approved sources are fetched.",
                     "Data sources"),
    "countries": ("Per country: the files of the latest full snapshot of lei-commons, each at a permanent "
                  "content-addressed URL with its SHA-256, size, records and Merkle root, and how to bring them to "
                  "the current day.", "Countries"),
    "snapshots": ("The daily editions of lei-commons: each day's signed manifest, root and sealed decision log, "
                  "whether it is a full snapshot or the day's changes, and where to fetch it whole.", "Snapshots"),
}


# ---- building and checking the whole set -------------------------------------------------------------------------

@dataclass
class Inputs:
    sources: list            # config/sources.yaml (approvals.registry())
    effective: dict          # approvals.effective(sources, decisions)
    latest: dict             # the latest day's manifest
    anchor: dict             # its full snapshot's manifest
    anchor_sha256: str       # sha256:<hex> of the anchor manifest as stored
    editions: list           # [(manifest, sha256)] of kept days
    signed: set              # days whose manifest has a Sigstore bundle
    previous: dict           # object -> published document (the previous set), possibly empty


def build_set(cfg: Config, signer: jws.Signer, inputs: Inputs, now: datetime) -> dict:
    """Every object to publish: {object name: bytes}. Built and verified in memory; nothing is written.
    Raises dd.DeDiError (or ValueError) on any problem."""
    if not cfg.enabled:
        raise PublishError("DeDi publication is disabled in config/dedi.yaml")
    files_until = now + timedelta(hours=cfg.files_hours)
    builders = {"data-sources": lambda: data_source_records(cfg, inputs.sources, inputs.effective, inputs.latest),
                "countries": lambda: country_records(cfg, inputs.latest, inputs.anchor, inputs.anchor_sha256, inputs.signed),
                "snapshots": lambda: edition_records(cfg, inputs.editions, inputs.signed)}
    out, docs, entries, digests = {}, {}, [], {}
    for name in REGISTRIES:
        if not cfg.registries[name]["enabled"]:
            continue
        records = builders[name]()
        if not records:
            raise PublishError(f"registry {name} would be empty")
        obj = cfg.file_object(name)
        description, display = REGISTRY_TEXT[name]
        registry = {"name": name, "schema": record_schema(name, cfg), "state": "live", "description": description,
                    "meta": {"display_name": f"lei-commons {display}", "records": len(records),
                             "built_from": inputs.latest["date"]}}
        previous = inputs.previous.get(obj)
        unchanged = dd.content_unchanged(previous, registry | {"updated_at": None}, records)
        registry["updated_at"] = previous["registry"]["updated_at"] if unchanged else dd.utc(now)
        doc = dd.build_file(domain=cfg.domain, signer=signer, source_url=cfg.url(obj), next_update=files_until,
                            registry=registry, records=records)
        data = dd.serialize(doc)
        out[obj], docs[obj] = data, doc
        digests[name] = dd.digest(data)
        entries.append({"registry": name, "url": cfg.url(obj), "schema": registry["schema"]["$id"], "state": "live"})
        out[f"{PREFIX}schemas/{SCHEMAS[name]}"] = dd.serialize(registry["schema"])
    keys = [signer.public_jwk(), *[dict(k) for k in cfg.also_list]]
    prev_digests = inputs.previous.get(DIGESTS_OBJECT)
    same_digests = prev_digests and prev_digests.get("digests") == dict(sorted(digests.items()))
    digests_doc = dd.build_digests(domain=cfg.domain, signer=signer,
                                   updated_at=dd.parse_utc(prev_digests["updated_at"]) if same_digests else now,
                                   next_update=files_until, digests=digests)
    out[DIGESTS_OBJECT], docs[DIGESTS_OBJECT] = dd.serialize(digests_doc), digests_doc
    prev_manifest = inputs.previous.get(MANIFEST_OBJECT)
    manifest_fields = {"keys": keys, "files": entries, "name": cfg.name, "description": cfg.description,
                       "digests": cfg.url(DIGESTS_OBJECT), "domain": cfg.domain}
    reuse = (prev_manifest is not None and all(prev_manifest.get(k) == v for k, v in manifest_fields.items())
             and dd.parse_utc(prev_manifest["next_update"]) - now > MANIFEST_REISSUE_BEFORE)
    if reuse:
        manifest = prev_manifest
    else:
        same_list = prev_manifest is not None and prev_manifest.get("keys") == keys and prev_manifest.get("files") == entries
        manifest = dd.build_manifest(domain=cfg.domain, signer=signer, keys=keys,
                                     updated_at=dd.parse_utc(prev_manifest["updated_at"]) if same_list else now,
                                     next_update=now + timedelta(hours=cfg.manifest_hours), files=entries,
                                     name=cfg.name, description=cfg.description, digests_url=cfg.url(DIGESTS_OBJECT))
    out[MANIFEST_OBJECT], docs[MANIFEST_OBJECT] = dd.serialize(manifest), manifest
    report = check_set(cfg, lambda obj: out.get(obj), now=now, expected_date=inputs.latest["date"])
    if not report["ok"]:
        raise PublishError("the new set failed verification: " + "; ".join(report["problems"]))
    return out


def check_set(cfg: Config, read, *, now: datetime = None, expected_date: str = None, min_hours_left: float = 0) -> dict:
    """Verify a published set end to end, as a DeDi server would, from ``read(object) -> bytes | None``.

    Checks: the manifest (shape, self-signature, domain, freshness), the digests file (signed by a listed key),
    every listed file (the five steps, and its bytes against the committed digest), that each file's source_url
    is where the manifest says it is, that nothing goes stale within ``min_hours_left``, and that the countries
    and snapshots registries describe ``expected_date``. Returns {ok, problems, files, checkedAt}."""
    now = now or datetime.now(timezone.utc)
    problems, files = [], {}

    def load(obj):
        data = read(obj)
        if data is None:
            raise dd.DeDiError("fetch", f"{obj} is missing")
        try:
            return data, json.loads(data)
        except ValueError as exc:
            raise dd.DeDiError("shape", f"{obj} is not JSON") from exc

    try:
        _, manifest = load(MANIFEST_OBJECT)
        dd.verify_manifest(manifest, served_host=cfg.domain, now=now)
    except dd.DeDiError as exc:
        return {"ok": False, "problems": [f"manifest: {exc}"], "files": {}, "checkedAt": dd.utc(now)}
    digests = {}
    try:
        if manifest.get("digests") != cfg.url(DIGESTS_OBJECT):
            raise dd.DeDiError("shape", "the manifest does not point at our digests file")
        _, dg = load(DIGESTS_OBJECT)
        digests = dd.verify_digests(dg, manifest, now=now)
        _left(dg, now, min_hours_left, "digests", problems)
    except dd.DeDiError as exc:
        problems.append(f"digests: {exc}")
    _left(manifest, now, min_hours_left, "manifest", problems)
    by_url = {cfg.url(cfg.file_object(r)): cfg.file_object(r) for r in REGISTRIES}
    for entry in manifest["files"]:
        name = entry.get("registry") if isinstance(entry.get("registry"), str) else None
        obj = by_url.get(entry.get("url"))
        if not name or not obj:
            problems.append(f"files[]: unexpected entry {entry.get('registry')!r} at {entry.get('url')!r}")
            continue
        try:
            data, doc = load(obj)
            if doc.get("source_url") != entry["url"]:
                raise dd.DeDiError("shape", "source_url is not the URL the manifest lists")
            if doc["registry"]["name"] != name:
                raise dd.DeDiError("shape", f"holds registry {doc['registry']['name']!r}, not {name!r}")
            if name not in digests:
                raise dd.DeDiError("authenticity", "no digest committed for it")
            dd.verify_file(doc, manifest, now=now, digests=digests, served_bytes=data)
            _left(doc, now, min_hours_left, name, problems)
            built = (doc["registry"].get("meta") or {}).get("built_from")
            if expected_date and name in ("countries", "snapshots") and built != expected_date:
                problems.append(f"{name}: describes {built}, not the latest day {expected_date}")
            files[name] = {"url": entry["url"], "digest": dd.digest(data), "bytes": len(data),
                           "records": len(doc["records"]), "updatedAt": doc["registry"]["updated_at"],
                           "nextUpdate": doc["next_update"], "builtFrom": built}
        except dd.DeDiError as exc:
            problems.append(f"{name}: {exc}")
        except (KeyError, TypeError) as exc:
            problems.append(f"{name}: malformed ({exc})")
    for name in set(digests) - set(files):
        if not any(p.startswith(f"{name}:") for p in problems):
            problems.append(f"{name}: has a digest but the manifest does not list it")
    return {"ok": not problems, "problems": problems, "files": files, "checkedAt": dd.utc(now),
            "manifest": {"url": cfg.url(MANIFEST_OBJECT), "updatedAt": manifest.get("updated_at"),
                         "nextUpdate": manifest.get("next_update"), "keys": [k["kid"] for k in manifest.get("keys", [])]},
            "digests": {"url": cfg.url(DIGESTS_OBJECT)}}


def _left(doc, now, min_hours, what, problems):
    if min_hours and dd.parse_utc(doc["next_update"]) - now < timedelta(hours=min_hours):
        problems.append(f"{what}: goes stale at {doc['next_update']}, within {min_hours} hours")


# ---- reading inputs from, and writing to, the snapshots bucket ---------------------------------------------------

def _read(bucket, name):
    blob = bucket.get_blob(name)
    return blob.download_as_bytes() if blob is not None else None


def gather(bucket, *, sources: list, effective: dict) -> Inputs:
    """Everything build_set needs, from the snapshots bucket."""
    latest_ptr = json.loads(_read(bucket, "latest.json") or b"null")
    if not latest_ptr:
        raise PublishError("no latest.json yet")
    date = latest_ptr["date"]
    raw = _read(bucket, f"{date}/manifest.json")
    if raw is None:
        raise PublishError(f"latest.json points at {date}, which has no manifest")
    latest = json.loads(raw)
    if latest.get("schemaVersion", 1) < 2:
        raise PublishError("the latest day is not a v2 snapshot")
    listing = bucket.list_blobs(delimiter="/")
    list(listing)
    days = sorted(p.rstrip("/") for p in listing.prefixes if len(p.rstrip("/")) == DATE_LEN and p[:4].isdigit())
    editions, signed = [], set()
    for day in days:
        data = raw if day == date else _read(bucket, f"{day}/manifest.json")
        if data is None:
            continue
        m = json.loads(data)
        if m.get("schemaVersion", 1) < 2:
            continue  # old-format days are not described (they age out)
        editions.append((m, dd.digest(data).replace("sha-256:", "sha256:")))
        if bucket.get_blob(f"{day}/manifest.json.sigstore.json") is not None:
            signed.add(day)
    anchor_entry = next((e for e in editions if e[0]["date"] == latest["anchor"]), None)
    if anchor_entry is None:
        raise PublishError(f"the anchor {latest['anchor']} of {date} is not in the bucket")
    previous = {}
    for obj in [MANIFEST_OBJECT, DIGESTS_OBJECT] + [f"{PREFIX}dedi.{r}.json" for r in REGISTRIES]:
        data = _read(bucket, obj)
        if data is not None:
            try:
                previous[obj] = json.loads(data)
            except ValueError:
                pass  # an unreadable previous file is simply replaced
    return Inputs(sources=sources, effective=effective, latest=latest, anchor=anchor_entry[0],
                  anchor_sha256=anchor_entry[1], editions=editions, signed=signed, previous=previous)


def write_set(bucket, objects: dict) -> dict:
    """Upload in order (schemas and registry files, then digests, then the manifest), reading each back.
    Objects that are byte-for-byte unchanged are not rewritten. Returns {written, unchanged}."""
    written, unchanged = [], []
    for obj in _order(objects):
        data = objects[obj]
        if _read(bucket, obj) == data:
            unchanged.append(obj)
            continue
        bucket.blob(obj).upload_from_string(data, content_type="application/json")
        if _read(bucket, obj) != data:
            raise TransientError(f"{obj} did not read back as written")
        written.append(obj)
    return {"written": written, "unchanged": unchanged}


def _order(objects) -> list:
    return sorted(o for o in objects if o not in (DIGESTS_OBJECT, MANIFEST_OBJECT)) + \
        [o for o in (DIGESTS_OBJECT, MANIFEST_OBJECT) if o in objects]


def restore(bucket, before: dict) -> None:
    """Put back the set as it was before a failed write (same order: the manifest last). New objects that
    had no previous version are removed."""
    for obj in _order(before):
        data = before[obj]
        if data is None:
            blob = bucket.get_blob(obj)
            if blob is not None:
                blob.delete()
        elif _read(bucket, obj) != data:
            bucket.blob(obj).upload_from_string(data, content_type="application/json")


def copy_day(bucket, objects: dict, date: str) -> None:
    """A copy under the day, so every day's published set stays inspectable until the day is pruned."""
    for obj in _order(objects):
        if obj.startswith(PREFIX + "dedi."):
            bucket.blob(f"{date}/{obj}").upload_from_string(objects[obj], content_type="application/json")


def publish(bucket, cfg: Config, signer: jws.Signer, *, sources: list, effective: dict, now=None, record=None,
            retries: int = 3, wait=time.sleep) -> dict:
    """Build, verify, write and re-verify the DeDi set. Transient failures (storage, KMS) are retried with
    backoff; a set that fails verification is never written. Writes status.json either way, then raises
    PublishError on failure so the job (and the cycle) reports it."""
    record = record or (lambda e: None)
    clock = now or (lambda: datetime.now(timezone.utc))
    last = None
    for attempt in range(1, retries + 1):
        at = clock()
        try:
            inputs = gather(bucket, sources=sources, effective=effective)
            objects = build_set(cfg, signer, inputs, at)  # verified in memory; nothing written yet
            before = {obj: _read(bucket, obj) for obj in objects}
            try:
                result = write_set(bucket, objects)
                report = check_set(cfg, lambda o: _read(bucket, o), now=at, expected_date=inputs.latest["date"])
                if not report["ok"]:
                    raise TransientError("published set failed verification: " + "; ".join(report["problems"]))
            except Exception:
                try:
                    restore(bucket, before)  # never leave a half-written or unverifiable set behind
                    record({"event": "dedi-restored", "objects": len(before)})
                except Exception as exc:  # the previous set stays valid until its next_update; the check will see it
                    record({"event": "dedi-restore-failed", "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                raise
            try:
                copy_day(bucket, objects, inputs.latest["date"])
            except Exception as exc:  # a convenience copy; the published set is already good
                record({"event": "dedi-day-copy-skipped", "error": type(exc).__name__})
            status = {"ok": True, "publishedAt": dd.utc(at), "date": inputs.latest["date"], "attempts": attempt,
                      **result, **{k: report[k] for k in ("files", "manifest", "digests")}}
            _status(bucket, status)
            record({"event": "dedi-published", "date": inputs.latest["date"], "attempts": attempt,
                    "written": len(result["written"]), "unchanged": len(result["unchanged"]),
                    **{f"records_{k}": v["records"] for k, v in report["files"].items()}})
            return status
        except TransientError as exc:
            last = exc
        except (dd.DeDiError, PublishError, ValueError, KeyError, TypeError) as exc:
            last = exc
            break  # deterministic: the same inputs would fail the same way, so don't retry
        except Exception as exc:  # storage, KMS or network: transient as far as we can tell
            last = exc
        if attempt < retries:
            record({"event": "dedi-retry", "attempt": attempt, "error": f"{type(last).__name__}: {str(last)[:160]}"})
            wait(10 * attempt)
    message = f"{type(last).__name__}: {last}"
    try:
        previous = json.loads(_read(bucket, STATUS_OBJECT) or b"{}")
    except Exception:
        previous = {}
    _status(bucket, {"ok": False, "failedAt": dd.utc(clock()), "error": message[:500],
                     "lastGood": previous.get("publishedAt") if previous.get("ok") else previous.get("lastGood")})
    record({"event": "dedi-failed", "error": message[:300]})
    raise PublishError(message)


def _status(bucket, status: dict) -> None:
    try:
        bucket.blob(STATUS_OBJECT).upload_from_string(json.dumps(status, indent=2, sort_keys=True),
                                                      content_type="application/json")
    except Exception:
        pass  # the status is a convenience; the decision log has the event


def check(bucket, cfg: Config, *, now=None, min_hours_left: float = 12) -> dict:
    """The independent check (dedi-check job, status.yml): is the published set valid, current and not about
    to go stale?"""
    latest = json.loads(_read(bucket, "latest.json") or b"null") or {}
    return check_set(cfg, lambda o: _read(bucket, o), now=now, expected_date=latest.get("date"),
                     min_hours_left=min_hours_left)
