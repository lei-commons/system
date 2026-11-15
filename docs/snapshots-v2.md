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

## Compatibility

Readers accept both formats: days built before v2 (schemaVersion 1, a full copy per day) stay
readable, downloadable and loadable until they age out. The first v2 build is a full snapshot
with no base. The load version is 5, so the database reloads once in full.

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
