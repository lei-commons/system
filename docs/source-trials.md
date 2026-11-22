# Source trials: a new source is tested once, before production

Owner decisions 2026-11-22:
- "Verify and test all the data sources, quality of data, etc within the test mode before processing them into
  production database. Some of the data sources may not work well."
- "Once the source is tested and promoted to production, no need for those sources to be tested again under
  trial mode. Trial mode should be used only when introducing new data sources or an existing data source has
  completely changed." Trial mode must run the latest code, as production does, and must not use any ongoing
  cloud resources when unused.
- "Once the trial is done, left over data/resources should be cleaned up within 24 hrs … maximum 48 hrs."

## When a trial is used

- **For a new source only.** Its pipeline in `config/sources.yaml` says `stage: trial`. Every other pipeline
  says `stage: production`, and a production source is never measured or re-tested again.
- **For an existing source that has changed completely,** for example a new file format, a new publisher or a
  different register behind the same name. Here a second pipeline (its own job, for example `ingest-xx-v2`)
  goes on trial next to the production one, which keeps serving. The promotion pull request then replaces the
  old pipeline with the new one. A production source is never switched back to `trial`, because that would
  take its records out of the next snapshot.

## What a trial does

- **It only measures.** The trial job downloads the source's file through the gateway, checks it, maps every
  row with the real profile, and measures what it mapped (`lc_ingest.quality`). It keeps no records, no
  changes and no copy of the file: only a small report in `trials/<source>/` in the data bucket. So nothing of
  a trial can reach the snapshot, the database, the site, the releases or DeDi.
- **It runs production's code.** A trial job is deployed by the same workflow, from the same image and commit,
  as the production jobs (`deploy-ingest-bulk.yml`, `LC_CODE_VERSION`). On promotion, the very same job starts
  writing the source's store.
- **It runs only when there's something new to measure.** It runs once when it's first deployed, and then in
  the daily cycle's last step (`trials`), after the snapshot, the load and the DeDi check. There it measures a
  publication only if it hasn't measured that publication with this commit yet. An unchanged day is a
  seconds-long check. A failing trial is logged as `trial-failed` and never fails the cycle or touches the
  clean-run count.
- **It uses nothing when unused, and leaves nothing once done** (`lc_ingest.trials`).
  - With no source on trial, the cycle has no `trials` step, and a Cloud Run job that isn't running costs
    nothing.
  - **Promoted:** the job's first production run deletes everything under `trials/<source>/`, every version,
    before it does anything else. The daily cycle runs it within 24 hours of the promotion.
  - **Dropped from the registry:** when the pull request merges, `deploy-ingest-bulk.yml` runs the job once
    with `LC_CLEANUP=1`, which deletes `trials/<source>/` in every version, and then deletes the job. This
    takes minutes. Only jobs whose last deploy labelled them a trial are removed this way.
  - **Safety net:** the data bucket's lifecycle deletes `trials/` objects older than 2 days, and replaced
    versions after 1 day. A live trial rewrites its reports on every daily run (`trial-refreshed`), so the net
    never takes a live trial's report. It only catches leftovers the two paths above missed, for example when
    the system was paused and deploys didn't run.
- **It reports.** The `quality` event in `status.yml` carries counts and register vocabulary only, never
  names, because the logs are public:
  - records, and why rows were skipped;
  - how full each field and address part is;
  - the status words, and how many records have a word `config/status.yaml` doesn't map;
  - legal forms and countries;
  - the shapes of registration numbers;
  - anomalies: repeated IDs, the same name and postcode twice, names that are too short, too long, have no
    letters or hold a web address, dates in the future, before 1600 or out of order, and anything that looks
    like an email or phone number.

  The report in `trials/<source>/` holds the same with a sample of 25 records. The admin **Trials** tab shows
  it, with the checklist below and the last failure.
- **It checks the file's layout.** New profiles name the columns they need. A file without them stops the run
  with the missing and present column names, never values. A layout read about in documentation, but never
  seen (the session never downloads), is confirmed or corrected on the first run.

## Promotion: once

A source moves to production through one pull request that changes its `stage` to `production`. That is a
sensitive path (`config/**`), so the owner approves it on the admin page. The pull request quotes the trial's
numbers against this checklist:

1. at least one complete run has finished (a `quality` event), and the files' columns matched;
2. the record count is plausible for the register, and skips are explained (people dropped, linked records);
3. the fields the register is known for are filled, and the sample reads correctly;
4. every status word is mapped (`statusUnmapped` is 0), so the status filter and dashboards are right;
5. the anomalies are zero or explained, and there are no contact-like values;
6. the registration numbers have one main shape.

After promotion, the job's next run first deletes the trial's reports, then processes the publication into the
source's store. The next snapshot adds those records as a normal changes day.
