# Source trials: every new source is tested before production

Owner decision 2026-11-22: "Verify and test all the data sources, quality of data, etc within the test mode
before processing them into production database. Some of the data sources may not work well."

## What a trial is

Every pipeline in `config/sources.yaml` names its `stage`: `trial` or `production`. A new source always starts
in `trial`.

- **It runs.** Its job is deployed like any other, runs once on deploy, and then runs every day in the cycle's
  last step (`trials`). That step comes after the snapshot, the load and the DeDi check, so production data is
  never delayed. A failing trial is logged as `trial-failed` and never fails the cycle or affects the
  clean-run count.
- **It is kept apart.** It writes only its own store in the data bucket. The snapshot merges production stores
  only (`lc_core.sources.merge_order`), so nothing from a trial reaches the database, the site, the releases
  or DeDi.
- **It is measured.** Each run measures every record it mapped (`lc_ingest.quality`). The `quality` event in
  `status.yml` carries counts and register vocabulary only, never names, because the logs are public:
  - records, and why rows were skipped;
  - how full each field and address part is;
  - the status words, and how many records have a word `config/status.yaml` doesn't map;
  - legal forms and countries;
  - the shapes of registration numbers;
  - anomalies: repeated IDs, the same name and postcode twice, names that are too short, too long, have no
    letters or hold a web address, dates in the future, before 1600 or out of order, and anything that looks
    like an email or phone number.

  `trial-report.json`, beside the store, holds the same with a sample of 25 records, for the admin page.
- **Its files are checked.** New profiles name the columns they need. A file without them stops the run with
  the missing and present column names, never values. A layout read about in documentation, but never seen
  (the session never downloads), is confirmed or corrected on the first run.

## Moving to production

A source moves to production only through a pull request that changes its `stage` to `production`. That is
a sensitive path (`config/**`), so the owner approves it on the admin page. The pull request quotes the
trial's numbers. A source is ready when:

1. at least one complete run has finished (`done`, with a `quality` event), and the files' columns matched;
2. the record count is plausible for the register, and skips are explained (people dropped, linked records);
3. the fields the register is known for are filled, and the sample reads correctly;
4. every status word is mapped (`statusUnmapped` is 0), so the status filter and dashboards are right;
5. the anomalies are zero or explained, and there are no contact-like values;
6. the registration numbers have one main shape, and the IDs don't collide with another source by mistake.

On promotion, the next snapshot adds the store's records as a normal changes day.
