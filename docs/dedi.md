# DeDi: lei-commons as signed DeDi files

Owner request, 2026-11-19. lei-commons publishes signed files that describe itself, following the
Decentralized Directory protocol (DeDi, LF Decentralized Trust,
github.com/LF-Decentralized-Trust-labs/decentralized-directory-protocol, docs/publishing-dedi-files.md:
the origin-hosted "protocol v2" that dedi.global crawls; `dedi_version` 0.1).

## What is published

| File (path on the site) | Registry | One record per | What a record says |
|---|---|---|---|
| `/.well-known/dedi.index.json` | (the manifest) | | our signing key(s), the files below, the digests file |
| `/dedi/dedi.data-sources.json` | `data-sources` | source in `config/sources.yaml` | licence, credit, URLs, the owner's approval, whether it is in use and its records |
| `/dedi/dedi.countries.json` | `countries` | country | the latest full snapshot's files for that country (permanent content-addressed URL, SHA-256, size, records, Merkle root, sources), today's count and root, and the recipe to bring the files to today |
| `/dedi/dedi.snapshots.json` | `snapshots` | kept day | the day's manifest (URL, SHA-256, Sigstore bundle), root, kind, anchor, base, decision-log seal, recipe |
| `/dedi/dedi.digests.json` | (digests) | | the SHA-256 of each file above, re-issued daily |
| `/dedi/schemas/*.v1.json` | | | the record schemas (also inline in each file) |

Every country record is also a valid DeDi `Public-Data-Set` record (its `data_url` is the full snapshot's
manifest, with its checksum), so generic DeDi consumers understand it; the extra fields are lei-commons'.
The data files themselves are served at `/data/parts/<sha256>.parquet` (named by their own hash, so a URL
never changes meaning) and `/data/<date>/manifest.json` (+ `.sigstore.json`, `NOTICE.txt`).

## How it is signed and checked (lc_dedi)

`packages/lc_dedi` implements the protocol: JCS (RFC 8785), the fixed JWS profile
(`{"alg":"EdDSA","b64":false,"crit":["b64"]}`, detached), building the DeDi file, the manifest and the
digests file, the official envelope schemas (vendored from the protocol repo at commit e63c71e), and
the five verification steps (shape, integrity, authenticity, freshness, registry state), plus digests.
CI checks conformance with the protocol's own tool, dedi-cli (pinned commit): it must accept what we
sign, and we must accept what it signs (byte-identical signatures).

The key is Ed25519, kid `lc-dedi-1`, kept in Secret Manager (`dedi-signing-key`) and created by the first
publication, so nobody ever sees it. Rotation: add the old key's public JWK to `key.also_list`, change
`kid` (a new secret version or a new secret), let a day publish with both listed, then drop the old one.
Revocation: remove the key from the manifest; every file it signed stops verifying at once.

## When, and what happens when something fails

1. **Publish** (end of build-snapshot, daily ~03:30 UTC): build every file, verify the whole set in memory
   (all five steps against the new manifest); write registry files and schemas, then the digests, then
   the manifest (the pointer last), reading each back; verify the whole set again from the bucket; copy
   it under `<date>/dedi/`; write `dedi/status.json`. Byte-identical files are not rewritten; the
   manifest is re-signed only when keys or the file list change, or within 72 hours of going stale.
   `updated_at` moves only when content changes; `next_update` is 36 hours ahead, so one late or failed
   cycle is absorbed.
2. **Failures.** Deterministic problems (no snapshot, a broken anchor, a record that doesn't fit its
   schema, bad config) are not retried. Transient ones (storage, Secret Manager, a write that doesn't read
   back, a set that fails its re-check) are retried 3 times with backoff. Anything that fails after
   writing began puts the previous set back (manifest last), so a half-written or unverifiable set is
   never left published. `status.json` records the failure and the last good publication; the decision
   log gets `dedi-failed` / `dedi-retry` / `dedi-restored`. **A DeDi failure never fails the snapshot.**
3. **Check** (job `dedi-check`, after load-db in the cycle and again at 14:17 UTC): verifies the published
   set as a DeDi server would, and fails it if anything would go stale within 12 hours or the countries or
   snapshots files describe an older day than `latest.json`. With `--repair` a failing check republishes
   and checks again (this also keeps the files fresh on a day the cycle failed). It writes
   `dedi/check.json` and `dedi-check-ok` / `dedi-check-failed`; a check that still fails exits non-zero,
   so the job fails and its alert emails the owner.
4. **System page:** the DeDi card at the top links every file, with its records, when its content last
   changed and until when it is fresh, the last publication and the last check.

## Private until the owner says otherwise

The namespace is the private site's own host for now, behind the owner's login: the manifest is truthful
where it is served, and the owner can verify everything end to end (browser, `dedi-cli verify`). Going
public (owner decision) means: a domain we control in `config/dedi.yaml` (`domain`, `base_url`,
`data_base_url`); a public route for `/.well-known/dedi.index.json`, `/dedi/*` and `/data/*` only; then
adding the domain to the protocol's `domains.txt` so dedi.global crawls it. The files are already served
with what DeDi asks (`application/json`, `Access-Control-Allow-Origin: *`, a cache lifetime well inside
`next_update`).

## Settings: `config/dedi.yaml`

Domain and URLs; key (`kid`, `signer`: seed or kms, `ref`, `also_list`); freshness (files 25–96 hours,
manifest longer than 72); registries (on/off; data sources: `include_pending`, `include_assessment`,
off by default as Claude's reading of terms is the owner's to publish; snapshots: `max_records`).
Validated when read; a bad value stops the DeDi step, never the snapshot.

## Not yet (proposals)

- ~~Keep the full snapshot of each month for good~~: built (`snapshots.keep_monthly_full`, config/publishing.yaml),
  so the monthly editions' URLs stay valid forever, not only while referenced.
- Anchor each day's digests file in Rekor too (as the snapshot manifest is): a public, append-only record
  of every key and content change, which closes the protocol's acknowledged "host compromise" gap.
- More registries: registration authorities (GLEIF RA codes used in directory ids), the agents that run
  lei-commons (DeDi's agent-registry use case), negative lists (entities removed or struck off).
- Entity-level lookups (`/dedi/lookup/{ns}/{registry}/{id}`) answered from the database with a Merkle
  inclusion proof against the signed snapshot root; and contributing that sharding design upstream (the
  protocol lists Merkle-committed sharding as an open question).
