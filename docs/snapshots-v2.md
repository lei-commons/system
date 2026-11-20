# Snapshots v2: daily changes, periodic full snapshots, shared parts, incremental loads

Owner request, 2026-11-15. Live after the M2 gate (2026-11-17).

## Why

Until v2, every day wrote a full copy of the directory (2.05 GB at 24.18M records), kept
forever, and the database was reloaded from scratch every night (about 43 minutes). On a
typical day about 0.06% of records change.

## The settings: `config/publishing.yaml`

| Setting | Default | What it does |
|---|---|---|
| `snapshots.full_every_days` (x) | 7 | A full snapshot at least every x days; changes only on the days between. 1 means a full snapshot every day. |
| `snapshots.retain_days` (y) | 30 | Day folders older than y days are deleted, except those a kept day still needs to be rebuilt. |
| `snapshots.noncurrent_retain_days` (z) | 7 | Old versions of overwritten or deleted objects are deleted after z days (a bucket lifecycle rule; applied by the bootstrap). |
| `snapshots.part_target_records` | 2,000,000 | Records per part file, roughly. Keeps every file far below GitHub's 2 GiB limit. |
| `snapshots.change_file_max_rows` | 1,000,000 | Rows per change file. |
| `snapshots.full_when_changes_exceed` | 0.25 | A day whose changes exceed this share of the records becomes a full snapshot. |
| `snapshots.state_kept` | 3 | How many days of build state to keep (internal, in the data bucket). |
| `snapshots.prune_grace_days` | 2 | A part is deleted only once unreferenced for longer than this. |
| `snapshots.prune_dry_run` | false | Log what pruning would delete, without deleting. |
| `storage.data_noncurrent_retain_days` | 30 | The data bucket is versioned too, and every overwrite of an ingestion shard kept its old copy forever. Old versions older than this are deleted (lifecycle rule; the ledgers keep the history). |
| `load.incremental` | true | Apply each day's changes to the database instead of reloading. |
| `load.incremental_max_days` | 14 | More days behind than this, and the database is reloaded in full. |
| `load.incremental_max_fraction` | 0.2 | Changes larger than this share of the records are loaded in full. |
| `load.fallback_to_full` | true | If an incremental load fails a check, roll it back and load in full. |
| `load.vacuum_after_incremental` | true | VACUUM ANALYZE after applying changes. |

Settings are validated when they are read: a missing, unknown or out-of-range setting stops
the job before it writes anything. A test validates the file in CI too.

## Layout

```
gs://<project>-snapshots/
  latest.json                         pointer to the newest day (written last)
  parts/<sha256>.parquet              full-state parts, content-addressed, shared across days
  2026-11-18/manifest.json            every day: the manifest (and its Sigstore bundle, copied back by the release)
  2026-11-18/NOTICE.txt
  2026-11-18/changes.000.parquet      every day with a base: the records that changed since the previous day
  2026-11-18/relationship-changes.000.parquet
gs://<project>-data/publish/state/<date>/   build state: (directoryId, hash) per record, to find tomorrow's changes
```

**Parts, split by source as well as country.** A record goes to the part named for its
country and its exact combination of sources, e.g. `BR.br-rfb.p3of8.parquet` (Brazil register
only), `GB.gb-coh_gleif.p0of1.parquet` (Companies House and GLEIF), `DE.gleif.p0of1.parquet`
(GLEIF only). A large group is split into 2, 4, 8… parts by the record's shard. Register-only
parts change only when the register publishes, so between fulls they are stored once and
shared. A part's object name is the SHA-256 of its bytes, so identical files are never stored
twice. The number of parts for a group is chosen at each full snapshot from the previous
day's counts, and kept until the next full one.

**Changes.** `changes.NNN.parquet` has one row per record that was added, changed or deleted,
with an `op` column (`add`, `change`, `delete`) and the whole new record (only the id for a
delete), ordered like the parts. Relationships likewise. A full day also writes its changes
when it has a base, so the database can still load it incrementally.

**Manifest (schemaVersion 2).** `kind` (`full` or `changes`), `base` (the previous day's date
and root, or null), `anchor` (the full snapshot this day's state starts from), `snapshotRoot`
(the root of the whole state that day), `parts` (every part of that day's state with its
record count and Merkle root; on full days also its SHA-256, size and object), `changes`
(the change files and counts), `files` (the files of that day's release), `layout`, `stats`
(the dashboard counts), sources, decision-log seal, the settings used.

**Roots.** A record's leaf is the SHA-256 of its published row (exactly the Parquet columns),
so anyone can recompute it from the files. A part's root is the Merkle root of its leaves in
file order; the snapshot root is the Merkle root of the part roots in name order. The release
signs the manifest (Sigstore), so the signed snapshot root covers every record of the day.

## Rebuilding and checking any day

`python -m lc_publish.rebuild --date D (--bucket B | --dir DIR) [--out OUT] [--require-signature]`

1. Read D's manifest and walk `base` back to the full snapshot it starts from.
2. Check every file against its SHA-256, and each manifest's signature where present.
3. Stream the full parts and apply each day's changes in order (a merge on shard and id; an
   `add` of an existing record, or a `change` or `delete` of a missing one, is an error).
4. Recompute every part root and the snapshot root for D and compare them with D's signed
   manifest. Optionally write D's parts.

## Fetching a day's whole directory

Most days publish only their changes, so every fetch path can assemble any kept day:
- **Site API** (owner login): `GET /api/v1/snapshots/<date>/recipe` lists the full snapshot's files, then each
  later day's change files in order, each with its download URL (`/download/<date>/<name>`), size and SHA-256,
  plus each manifest. The Data page shows it on every changes day. A broken chain answers 409, never a guess.
- **GitHub releases**: a full day's release holds its parts; a changes day's holds its changes. Download the
  full snapshot's release and every later one, one folder per day, and run `lc_publish.rebuild --dir`.
- **The bucket**: `lc_publish.rebuild --bucket`.
Tested end to end: for every day of a simulated run (and after pruning), the recipe was fetched over HTTP,
every file downloaded and checked, the day rebuilt from only those files and compared with the true records;
the release workflow's own download step was run against every day and every day rebuilt from releases alone.

## Database load

- **Incremental.** If the database holds a day in the latest day's `base` chain (same root),
  no more than `incremental_max_days` behind and with changes under `incremental_max_fraction`:
  in one transaction, apply each day's changes in order. Deletes must remove exactly the
  rows expected and inserts must add exactly the rows expected. The final counts must match
  the manifest. Then update `meta` and commit. Readers see the old day until the commit.
  If any check fails it rolls back and, with `fallback_to_full`, loads in full.
- **Full.** As before (blue/green, atomic swap), from the anchor's parts, then the changes
  since the anchor, checked by counts before the swap.
- The dashboards' stats come from the manifest, so neither path recounts 24M rows.

## Retention (pruning)

Run by build-snapshot after it publishes:
- keep every day from the last `retain_days`, plus every earlier day a kept day needs
  (back to its full snapshot);
- delete other day folders;
- delete parts that no kept full snapshot uses and that are older than `prune_grace_days`;
- keep the newest `state_kept` days of build state;
- if any kept manifest can't be read, delete nothing.

A plain age rule on the bucket can't do this: it would delete shared parts still in use and
full snapshots that kept days depend on. The bucket is versioned, so a delete leaves an old
version. A lifecycle rule (Terraform, from `noncurrent_retain_days`) deletes old versions
after that many days.

## Releases (GitHub)

A full day's release holds its parts, changes, manifest and signature. A changes day's release
holds only its changes, manifest and signature, about 1–2 MB on a GLEIF-only day. Release
retention keeps the 14 newest days, the newest of each month, and every full release that a
kept release needs. The signature bundle is also copied back to the bucket.

## Status category (owner decision 2026-11-20)

Every register words a status its own way: GLEIF writes `ACTIVE`, Companies House `Active`,
Colorado `Good Standing`, Ireland `Normal`, Brazil (as lc_ingest translates its code) `Closed`.
Each record keeps its register's word in `legalStatus`, untouched (Article 4.3; until this change
GLEIF's `ACTIVE`/`INACTIVE` were respelled `Active`/`Inactive`, and no longer are). Next to it,
`statusCategory` is one of `config/labels.yaml` `legal_status`: active, inactive, dissolved,
in-liquidation, struck-off, merged or unknown.

- **The map:** `config/status.yaml`, per source: `{category: [register words]}`. Words match
  case-insensitively and trimmed; a word ending in `*` matches by prefix (Japan's `Closed: …`).
  It is checked when build-snapshot starts (`lc_core.status`): a category outside `legal_status`,
  or a word mapped twice, stops the job before it writes anything.
- **Which word:** the category comes from the map of the source whose `legalStatus` the merged
  record keeps (a register's, when it gives one; otherwise GLEIF's).
- **Unmapped words** get `unknown` and are counted in the manifest, `stats.unmappedStatus`
  (`"<source>: <word>"`, top 50), so they can be added to the map. A record with no word is
  `unknown` and isn't counted there.
- **Stats:** `stats.byStatusCategory` (every category) beside `stats.byStatus` (the registers'
  words, top 15).
- **Database and site:** column `status_category`; the search filter `status=<category>`, its index
  term (`s=<category>`), the status facet and the dashboards' "Entities by status" use the
  category. Search results, the entity page and the graph show the category with the register's
  word ("Active (register: Good Standing)"); the entity page lists the word on its own too.
- **Changing the map** changes the category of every record it touches, so those records are that
  day's changes. A large change (more than `full_when_changes_exceed`) makes the day a full snapshot.

**Columns.** The manifest lists the parts' columns (`columns`: entities, relationships). When the
previous day's columns differ from today's (as on the day `statusCategory` arrived, whose previous
manifest had no `columns`), build-snapshot starts a new chain: a full snapshot with no base
(event `no-previous-state`, reason "the record columns changed"), rather than publish every
record as a change. Every leaf changes with a new column, so a changes file would have held the
whole directory again. The days before stay rebuildable from their own files: a leaf is the hash
of the row as published that day.

## GLEIF identity and the re-key (owner decision 2026-11-20)

A company with an LEI merges with its register record when both mint the same directory ID: country,
GLEIF's code for the register and the register's number. Where GLEIF writes these differently from the
register, `config/sources.yaml` `gleif_identity` says how to read GLEIF's form (`lc_core.gleif_identity`,
checked when read):

| GLEIF code | Read as | Why |
|---|---|---|
| RA000585/6/7 | GB-COH, country GB | Companies House (as before) |
| RA000631 | leading zeros dropped, country US | Oregon writes 5749114 where GLEIF writes "057491-14" |
| RA000072 | leading zeros dropped, country CA | Canada writes 463116 where GLEIF writes "046311-6" |
| RA000473 | RA000472, country NO | GLEIF files Brønnøysund numbers under both codes |
| RA000412 | RA001075 with the check digit added, country JP | the commercial registration number is the corporate number without its check digit |

(Checked 2026-11-20 against data.oregon.gov and Corporations Canada's files: the earlier notes that Oregon's
"-NN" suffix and Canada's check digit were missing from the registers were wrong; both registers keep them,
and only the leading zeros differ.) A number a rule doesn't fit is minted as GLEIF writes it.

**The re-key.** A day's GLEIF file holds only the LEIs that changed, so new rules would reach the rest of
the store slowly. After its entity file, ingest-gleif re-keys every stored record once per set of rules
(`lc_ingest.rekey`; the rules' 12-character fingerprint names the pass, and a marker
`markers/rekey-<fingerprint>.json` in the gleif store records it as done). A record whose ID changes:
- moves to its new ID, every field kept. If another LEI already holds that ID, the two are kept apart as on
  any day (Article 4.2): it gets its own LEI-based ID and `possible-duplicate:<ID>`;
- leaves a **tombstone** at its old ID (`lc_core.tombstone`): the name, jurisdiction, registration and
  country it was published with, and the label `merged:<new ID>` (labels.yaml `merged`). No LEI: the LEI
  belongs to the record it merged into, so an LEI always resolves to one record.

The pass is held by the volume guard like any publication (key `rekey <fingerprint>` for
`mapping_upgrades`). It logs one decision, `gleif-rekeyed`: `moved`, `movedByAuthority`, `tombstones`,
`added`, `thereAlready` (the day's file had already put the record at its new ID), `keptApart`, `changes`
and the store's recounted `total`. Its ledger lines are in `changes/rekey-<fingerprint>.jsonl.gz`. A
second run does nothing; a run cut short before its marker finds its own work done and finishes the rest.
Measured locally on a full GLEIF store (the 2026-11-07 golden copy, 3,361,146 records): 6,135 records
moved (RA000412 3,523, RA000473 2,058, RA000072 386, RA000631 162, RA000472 6), 18 kept apart, 3 relabelled,
in 115 s and 210 MB; 0.18% of the store, far under the guard's 10%. A second run moved nothing.

**In the snapshot.** A tombstone is never merged with anything: it is published as its own row, sources
`gleif`, `statusCategory` `merged`, no `legalStatus` and no `lei`. (Should a live record of any source
hold the same ID, the live record keeps it.) No column is added, so the manifest's `columns` don't change
and the day of the re-key is an ordinary changes day: each moved record is two changed rows (its old ID,
now a tombstone, and the register record it joined, or an added row where there is none). Relationships
link LEIs to IDs afresh every day, so they follow the move. `records` and `recordsPerSource.gleif` count
tombstones too; `stats.byStatusCategory.merged` says how many.

**Serving.** `GET /api/v1/entities/<old ID>` answers with the tombstone and `mergedInto`; the entity page
says it merged and links to the record. Search and suggestions leave tombstones out, except a search for
the old ID itself or with `status=merged`. Dashboards count them under `merged`. The database needs no
change (load version 6 stays).

## Compatibility

Readers accept both formats: days built before v2 (schemaVersion 1, a full copy per day) stay
readable, downloadable and loadable until they age out. The first v2 build is a full snapshot
with no base. The load version is 6 (5 was v2; 6 adds `status_category` and makes the status
filter terms the category), so the database reloads once in full. The site's in-memory path
serves a snapshot from before `statusCategory` with every record `unknown`.

## Measured (local, 2026-11-15, about 0.93M records, 0.1% changing)

| | v1 | v2 |
|---|---|---|
| Build | 10.8 s | 11.8 s (full), 13.3 s (changes day) |
| Published that day | 21.7 MB full copy | 0.98 MB of changes |
| Database | 18 s full load | 1.1 s incremental |
| Rebuild and check a changes day | – | 9.1 s, roots match |

## Going live (after the M2 gate, 2026-11-17)

1. Merge (system, then the release workflow in `releases`). The deploys run build-snapshot and
   load-db once: build-snapshot writes the first v2 day, a full snapshot with no base. Load-db
   reloads once in full (load version 5).
2. Check by counts: `status.yml` (kind, why, records, parts, uploaded), then
   `python -m lc_publish.rebuild --bucket <project>-snapshots --date <day>`.
3. The next cycle publishes changes only and loads them incrementally: check `mode: incremental`
   and the counts.
4. Owner: rerun the bootstrap, so the lifecycle rules for old versions apply.
