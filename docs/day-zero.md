# Day zero

Everything the human owner does once, by hand, before the agents take over. About 30 minutes. After this, the owner only uses the owner console (pause, stop, restart, manual mode, take over, set budget, approve sources).

The system starts **paused**. Nothing runs and nothing is spent on models until you switch it to running.

## 1. GitHub (done: github.com/lei-commons)

1. Turn on two-factor authentication for your account, and in the org settings require it for all members.
2. Create these empty repositories (public, no README):
   `system`, `ledger-gleif`, `ledger-us`, `ledger-gb`, `ledger-in`, `ledger-br`, `ledger-sg`, `releases`, `intake`, `status`.
   Create `control` as **private**; only you have access to it.
3. Push this repository's contents to `lei-commons/system`.
4. Org settings → Actions → General: allow only actions from GitHub and verified creators, and set workflow permissions to read-only.

The four GitHub Apps (`lei-commons-maker`, `-checker`, `-guardian`, `-release`) and the owner-only `lei-commons-control` are registered in milestone M2 from manifests in this repo. Each is created by you clicking one confirmation link; agents never create Apps themselves.

## 2. Google Cloud

You need a billing account. You do **not** need to create the project first; the script can create it.

1. Open [Cloud Shell](https://shell.cloud.google.com) signed in as the owner account.
2. Find your billing account ID: `gcloud billing accounts list`.
3. Check the billing account's currency (shown in the Billing console). If it isn't USD, pass `--currency INR` (or yours) and `--budget` in that currency.
4. Install Terraform if `terraform version` doesn't print a version. Cloud Shell may only have a stub that prints install instructions:

```bash
mkdir -p ~/bin
curl -fsSLo /tmp/terraform.zip https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip
unzip -o /tmp/terraform.zip terraform -d ~/bin
export PATH="$HOME/bin:$PATH"
hash -r
terraform version
```

5. Run:

```bash
git clone https://github.com/lei-commons/system.git && cd system
./bootstrap/bootstrap.sh --project <project-id> --billing <BILLING-ACCOUNT-ID> --create
```

If the project sits in an organization with a domain-restricted-sharing policy, creating the budget fails with `do not belong to a permitted customer`, because Google's billing service account can't be added to the Pub/Sub topic. Either add an exception for the project, or rerun with `--no-budget-pubsub`. The budget then only emails billing admins, and the budget guard can't pause the system at the cap until Pub/Sub is enabled.

Project IDs are global, so pick a unique one such as `lei-commons-prod-01`. The script shows the Terraform plan and asks before applying. It is safe to re-run.

At the end it prints the GitHub org variables. If you are logged in to `gh` in Cloud Shell (`gh auth login`), it can set them for you; otherwise add them at *Org settings → Secrets and variables → Actions → Variables*.

## Deploy the gateway

Jobs reach the internet only through the conduct gateway. From the root of this repo in Cloud Shell:

```bash
./bootstrap/build-gateway.sh <project-id>
./bootstrap/bootstrap.sh --project <project-id> --billing <BILLING-ACCOUNT-ID> --gateway-image <IMAGE-FROM-THE-LAST-LINE>
gcloud run jobs execute egress-check --region us-central1 --wait
```

Pass the same `--currency`, `--budget` and `--no-budget-pubsub` options as on the first run. The last command must succeed: it tries to reach the internet directly from inside the jobs network and fails if it can.

## 3. What now exists

| Thing | Purpose |
| --- | --- |
| Service accounts `lc-*` | One per role. None can change IAM, create keys, touch billing or delete the project. |
| Workload Identity pool `github` | Lets only `lei-commons/system@main` (deploy) and `lei-commons/releases@main` (publish) act on GCP. No keys exist. |
| Buckets `<project>-data`, `-snapshots`, `-decision-log`, `-frames`, `-control` | All private. `-control/state.json` holds the system state; agents can read it, only you and the console write it. |
| Firestore, BigQuery dataset `lei_commons`, Artifact Registry `lei-commons` | Work queue, dashboards, container images. |
| Secrets `source-gb-coh-api-key`, `source-in-mca-credentials` | Empty. You fill them from the console's Sources page when you have accounts; until then those sources run in web-discovery mode. |
| Budget "lei-commons monthly cap" | Sends spend to Pub/Sub; the budget guard pauses the system at your cap. |

## 4. Optional hardening

- **IAM deny policy.** If your account can grant yourself *Deny Admin* (`roles/iam.denyAdmin`), re-run Terraform with `-var enable_deny_policy=true`. This blocks agents from IAM and key changes even if a role is granted by mistake later.
- **Lock the decision-log retention** after the first month: `gcloud storage buckets update gs://<project>-decision-log --lock-retention-period`. This is irreversible, which is the point.

## 5. Start

When milestone M1 checks are green:

1. Set org variable `SYSTEM_STATE` to `running`.
2. Edit `gs://<project>-control/state.json` and set `"state": "running"` (the console does both in one click from M2 on).

To stop everything at any time, set either back to `paused` or `stopped`. Agents check before every step and fail closed if they cannot read the state.
