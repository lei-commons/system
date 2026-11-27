# Owner-supplied sources

Some registers publish files that only a person may download: no API, and pages that refuse scripted access (a
refusal is never worked around). For these, the owner downloads the files by hand, and the system takes them from
the private repository `lei-commons/data-downloads` (owner decision 2026-11-27). The first is India's Ministry of
Corporate Affairs (`in-mca`).

```
owner's machine                 lei-commons/data-downloads (private)          data bucket                 daily cycle
raw downloads ── clean ──PR──▶  <source>/ cleaned CSVs + manifest  ──copy──▶  intake/in-<source>/   ──▶   ingest job
(deleted once cleaned)          tools/check.py on every push                  <tree>.tar.gz              (trial, then
                                (kept: the source of record)                  current.json               production)
```

1. **Clean.** The source's tool in that repository (`tools/clean_<source>.py`) turns the raw downloads into one CSV
   per kind and month with only the columns the system uses. Columns that hold contact details (email) are never
   read; addresses lose email addresses, phone numbers and any part about a person (C/O, S/O, Mr, Shri, ...).
   The raw downloads are never committed, and are deleted once their pull request is merged.
2. **Guard.** `tools/check.py` runs on every push and pull request: only cleaned files of known sources, exactly
   their columns, a valid identifier on every row, no email address, no phone number or person in an address,
   and a manifest that matches. Its workflows are sensitive paths (owner approval, `config/approvals.yaml`).
3. **Copy.** On main, the `copy` workflow bundles each source's folder (`<git tree id>.tar.gz`, reproducible) and
   writes it, with `current.json` naming it, to `intake/in-<source>/` in the data bucket. It signs in through
   workload identity as `supplier`, which may write `intake/` and nothing else (`infra/terraform/iam.tf`), from
   `data-downloads@main` only (`wif.tf`). No key, no new egress host: the cycle never reaches GitHub.
4. **Ingest.** The source's job reads `current.json`, and processes the bundle whose key it hasn't processed, as
   one complete publication, through the usual runner (`lc_ingest.runner`: diff, volume guard, trial measures).
   In production it deletes the bundle once processed; at every run it deletes any bundle `current.json` no
   longer names. So a processed copy is gone within a day, and the repository stays the source of record.

The repository keeps the cleaned files for good (owner, 2026-11-27): a register such as MCA offers only a window of
its past reports, and the snapshots must be reproducible from what was read.

Adding a month: the owner downloads the new reports into a folder outside the workspace; the session cleans them
into the repository by pull request (`python3 -I tools/clean_<source>.py <folder> <source>`) and merges it once the
check passes. The raw downloads stay on the owner's machine until the owner deletes them (owner, 2026-11-27).

MCA has two kinds of download (2026-11-27). Its **master data** (every company, LLP and foreign company on the
register with its status today) is kept as one snapshot, the latest, in parts of 150,000 rows; its **monthly
reports** (registrations since 2016, strike-offs since October 2023) are kept month by month. A company is in
several of these files; the job lists each identifier once: from the master snapshot when it lists it, else from
the first report that does (struck off when a strike-off report lists it), else from a strike-off report alone.
