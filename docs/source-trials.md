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
  seconds-long check. A failing trial is logged as `trial-failed` and never fails the cycle.
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
  - anomalies (a name holding an email address or phone number is dropped before it is measured, because it
    may be a person's): repeated IDs, the same name and postcode twice, names that are too short, too long, have no
    letters or hold a web address, dates in the future, before 1600 or out of order, and anything that looks
    like an email or phone number.

  The report in `trials/<source>/` holds the same with a sample of 25 records. The admin **Trials** tab shows
  it, with the checklist below and the last failure.
- **It checks the file's layout.** New profiles name the columns they need. A file without them stops the run
  with the missing and present column names, never values. A layout read about in documentation, but never
  seen (the session never downloads), is confirmed or corrected on the first run.

## A trial that keeps failing is marked invalid

Owner decision 2026-11-22: "After few trials/tries across few days (say 3 days), if it is established that a
particular data source is invalid, then it must be marked so and not to be processed anymore. In the Data sources
tab, it can be shown as "invalid" with reason given against that data source. ... Allow admin to explicitly
re-approve such invalid ones. If so, it should be tried again over few days (say 3 days) and then if it is still
erroring out, mark again as "Invalid" with date and reason against that data source." (`lc_core.validity`)

- **Counting.** The trial job keeps `trials/<source>/validity.json`. A run that fails (an error, or one that ends
  denied or held without measuring) adds its UTC day to the count; retries on the same day count once. A run that
  measures, or finds nothing new to measure, empties the count. Runs that stop because the system is paused, the
  job is busy, or the source isn't approved say nothing about the source and count nothing.
- **Marking.** On the day the count reaches `trials.invalid_after_failed_days` in `config/sources.yaml` (3), the
  source is marked invalid with the date and the reason: the days, and the last error with its message. The
  public log gets `trial-invalidated` with the count and the error's type only.
- **Stopping.** From then on the job stops as soon as it starts (`trial-invalid`): it fetches nothing and measures
  nothing. It still rewrites `validity.json`, so the trial lifecycle rule never takes the mark. The Data sources
  tab shows the source as **Invalid**, with the date and reason, and the Trials tab says the same.
- **Re-approving.** The owner can re-approve it on the Data sources tab ("Re-approve for a new trial", a reason and
  a fresh authenticator code). That writes an approval newer than the mark, and the server refuses it unless the
  source really is marked invalid. At its next run the job moves the mark into the history (`trial-retried`) and
  starts counting from zero, so the source gets the full number of days again. If it still fails, it is marked
  invalid again, with the new date and reason; the tab lists the earlier marks.
- **Writes.** The job writes the mark in the data bucket. Only the owner's actions write approvals, in the control
  bucket, so a job can never approve itself.
- **Scope.** Only sources on trial are marked. A production source that fails keeps its records and is reported by
  `source-failed` and its health, and the owner decides what to do with it.

## Promotion: once

A source moves to production through one pull request that changes its `stage` to `production`. Owner decision
2026-11-22: "Once a new data source has gone through trial and tested for data access and data structure etc., no
need to ask for approval to promote it to production for each data source. Sensitive code/system changes should
be explicitly approved through admin console." So a promotion needs no approval: the owner-approval workflow
passes it on its own (`lc_core.approval_rules.promotion_only`) when it does nothing else, that is:

- it changes only `config/sources.yaml`, the rendered `infra/workflows/daily-cycle.yaml`, and tests or docs;
- the registry is unchanged except for pipelines whose `stage` goes from `trial` to `production`;
- the cycle is unchanged except for the promoted jobs leaving the trials step (and joining the ingest list,
  which `deploy-cycle` checks against `render_cycle.py` before deploying).

Anything more in the same pull request (a profile fix, a new host, another registry field) needs the owner's
approval as before, so fixes go in their own pull request first. The session merges a promotion once CI is green,
and only when the trial's numbers pass this checklist, which the pull request quotes:

1. at least one complete run has finished (a `quality` event), and the files' columns matched;
2. the record count is plausible for the register, and skips are explained (people dropped, linked records);
3. the fields the register is known for are filled, and the sample reads correctly;
4. every status word is mapped (`statusUnmapped` is 0), so the status filter and dashboards are right;
5. the anomalies are zero or explained, and there are no contact-like values;
6. the registration numbers have one main shape.

After promotion, the job's next run first deletes the trial's reports, then processes the publication into the
source's store. The next snapshot adds those records as a normal changes day.
