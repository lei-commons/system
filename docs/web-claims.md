# The web-asserted facts pilot (`web-claims`)

Owner approval 2026-11-23. Design: `lei-commons/docs` `04-designs/web-asserted-facts.md`. Settings:
`config/web_claims.yaml`, and the pilot's line in `config/models.yaml` (`tasks.web_claims`). Code:
`services/web_claims`. Deployed by `.github/workflows/deploy-web-claims.yml`.

**Measure-only.** The pilot finds official websites and parent companies on companies' own pages and measures how
accurate that is. It keeps a report and its claims under `trials/web-claims/` in the data bucket, and nothing
else. Nothing reaches the snapshot, the database, the site's public pages or the releases. The admin **Trials**
tab shows the report.

## What it does

1. **Candidates, once, from the published snapshot** (the newest full snapshot that has the `website` column).
   Active records only, LEI holders first, then a stable pseudo-random order:
   - 10,000 records **with** a published website, Finland first;
   - 10,000 records **without** one: about 2,500 each from the US, Singapore, the EU (round-robin across member
     states) and the UK.
   For LEI holders, GLEIF's direct and ultimate parents in the same snapshot are the truth set for parent claims.
2. **Find the site in Common Crawl** (`index.commoncrawl.org`, one index query per domain, newest crawl):
   - with a website: its own domain;
   - without one: up to 8 addresses made from the legal name without its legal-form words, under the country's
     usual endings and `.com`, stopping at the first with evidence.
   A sample of 1,000 with-website records also goes through this discovery, as if it had no website. The address
   found is scored against the published one; that is the truth set for discovery.
3. **Read the evidence pages from the archive** (`data.commoncrawl.org`, one ranged request per page): the home
   page and then the legal, imprint, about and contact pages, three at most. The companies' own sites are never
   asked.
4. **Hard match evidence, no model:** the record's LEI on a page, its register number (with or without
   separators or leading zeros, and Finland's business ID as its `FI` VAT number), or its exact legal name plus
   its country. With no evidence, there is no claim.
5. **Maker and checker** (records with evidence only). Claude (`claude-haiku`) reads the page lines into a strict
   schema: the operator's name, numbers and LEI, and its parent company if the page states one. Gemini
   (`gemini-flash`), the other vendor, says whether the same lines support each claim. A parent is kept only if
   the checker agrees, and only if it is an entity: it needs a legal-form word, a register number or an LEI, and
   no contact details.
6. **Report** (`report.json`):
   - coverage and evidence rates per country;
   - discovery yield per group;
   - discovery precision against the truth sample;
   - parent precision against GLEIF;
   - model use and estimated spend;
   - parents dropped by the person guard;
   - a sample of 25 claims.

   The public log gets the counts only (`web-claims-report`).

## People's data

Owner, 2026-11-23: "make sure NO people data is processed or stored."

- **Page text is never stored or logged.** Only each page's SHA-256 is kept.
- **What reaches a model is minimised first.** Only lines that name a company, a register number or a group link
  are sent. A line is dropped if it holds:
  - a person's role or title (CEO, director, Geschäftsführer, toimitusjohtaja, Mr, Dr…);
  - an email address;
  - a phone number.
- **The models can't return a person.** Neither schema has a field for one.
- **The person guard** drops a "parent" that might be a person. The report counts how many it dropped.

## Running

- **Daily cycle:** the cycle starts the job as its last step and doesn't wait for it (`skip_polling`).
  - A run works for at most `run.minutes` (330), then saves its progress. The next day's run picks up from there.
  - The job's own limit is `timeouts_minutes.web-claims` in `config/cycle.yaml`.
- **Pace:** four records are in flight at a time. The gateway still asks each Common Crawl host one request at a
  time, at most one a second.
- **Spend:** model spend is capped per run (USD 25) and for the whole pilot (USD 150). These are estimates from
  `prices_usd_per_mtok`; the budget guard and the owner's monthly budget remain the hard limits.
- **Stops:** after 5 model errors in a row the run stops, and so it does on 25 fetch errors or 5 refusals in a
  row. The records it hadn't finished stay pending. Such a stop is reported in the run's events. It is not a
  failed execution, which is kept for crashes.
- **Keeping the files alive:** each run rewrites its four files (`candidates.json.gz`, `progress.json.gz`,
  `claims.jsonl.gz`, `report.json`). This way the `trials/` lifecycle rule never takes a live pilot's files.
- **Ending the pilot:** set `enabled: false`. On the next deploy, the job runs once with `LC_CLEANUP=1`, which
  deletes `trials/web-claims/` in every version, and the job is then deleted. The `trials/` lifecycle rule
  (2 days) is the net.

## Access

The job runs on the sealed jobs network as the crawler identity:

- **Vertex AI user:** to call the models, through Private Google Access;
- **snapshots, read:** to pick the candidates;
- **data bucket, object user:** for its files.

All of these were granted by Terraform in system#142. Both Common Crawl hosts are on the egress allow-list.
