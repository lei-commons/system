# lei-commons / system

lei-commons is an open, autonomous directory of legal entities, kept in LEI-compatible format and published as a public good. AI agents build, run and maintain it. A human owner stays in charge and can pause, stop, restart or take over at any time.

This repository holds the system itself: code, rules, agent definitions and infrastructure. The data changes live in the `ledger-*` repositories, and published snapshots live in `releases`.

- Concept: lei-commons — Concept (Final v1.2)
- Design: lei-commons — Phase 1 System Design v0.3

## Layout

| Path | Holds |
| --- | --- |
| `constitution.md` | The non-negotiable rules every agent and job follows. Tier 3, protected. |
| `config/fields.yaml` | Allow-list of record fields, including the personal-data rule. Protected. |
| `config/labels.yaml` | Verification label vocabulary and staleness windows. Protected. |
| `config/models.yaml` | Model routing per task (maker and checker). Protected. |
| `config/sources.yaml` | Every data source, its terms, access mode and whether it needs owner-provided access. |
| `config/egress-allowlist.yaml` | Domains the conduct-policy proxy may reach for registry and API traffic. Protected; owner approval needed. |
| `packages/lc_core` | Shared library: directory IDs, decision log, control-state gate. |
| `scripts/conduct_check.py` | The `conduct-policy` check run on every pull request. |
| `bootstrap/` | Day-zero script the owner runs once from Cloud Shell. |
| `infra/terraform` | All Google Cloud infrastructure. |
| `docs/` | Day-zero guide, runbooks, architecture decision records. |

## Status

Milestone M1 (foundations) in progress.

## Licence

Code: Apache-2.0. Data licence: parked (decision 23); intent is share-alike with attribution.
