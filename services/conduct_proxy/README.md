# conduct-policy egress gateway

The only way out to the internet for lei-commons jobs (constitution Article 2). It enforces the conduct rules in infrastructure, so they never depend on a model behaving. This directory is a protected path.

## How jobs use it

Send the request to `/fetch` and put the destination in `X-LC-Target`. Set `X-LC-Actor` to your component ID.

```
GET /fetch HTTP/1.1
X-LC-Target: https://api.gleif.org/api/v1/lei-records?page[size]=10
X-LC-Actor: job:ingest-gleif
```

It is not a `CONNECT` proxy, because a tunnel would hide everything but the host name. The gateway makes the outbound TLS connection itself, so it can see each request in full without an interception CA. Redirects come back to the caller, so every hop is checked as a new request.

## What it enforces

| Traffic | Hosts | Methods | Credentials | robots.txt | Pacing |
| --- | --- | --- | --- | --- | --- |
| crawler | any other public host | GET, HEAD | refused | yes | 1 req/s per host, or Crawl-delay if slower |
| registry | `registry_hosts` | GET, HEAD | refused | no: published for programmatic use | 1 req/s per host |
| owner-key | `owner_key_hosts` | GET, HEAD, POST | forwarded | no | 1 req/s per host |
| model | `model_hosts` | POST | forwarded | no | none: our own Vertex AI quota |

For all traffic:

- The profile comes from the target host, never from the caller.
- Owner-key hosts are refused until the owner has approved that source.
- IP-literal targets, non-standard ports and credentials in URLs are refused.
- Hosts resolving to any non-public address are refused (metadata server, VPC, loopback). The connection is pinned to the checked address, which blocks DNS rebinding.
- Login, sign-up and account paths are refused, after undoing encoding tricks. `BUILTIN_DENY_PATHS` is a floor that config cannot remove.
- Request and response headers are allow-listed, so cookies go neither way, and the user agent is always ours.
- When the system is not `running`, nothing leaves (503). If the control state cannot be read, nothing leaves.
- A config that would weaken Article 2 refuses to load, and the gateway will not start.

Every decision is one JSON line on stdout, for the security agent. Query strings and headers are never logged, because they can hold credentials.

## Running

| Variable | Meaning |
| --- | --- |
| `LC_EGRESS_CONFIG` | Path to `egress-allowlist.yaml` |
| `LC_APPROVED_KEY_SOURCES` | Comma-separated source IDs the owner has approved; empty by default |
| `LC_CONTROL_STATE_URI` | Control state location, as in `lc_core.control` (needs `lc-core[gcp]` for `gs://`) |
| `PORT` | Listen port, default 8080 |

```bash
python -m conduct_proxy.server
```

## Deployment requirements

These are not met yet. They arrive with the infrastructure change, after GCP day zero.

- **One instance only.** Pacing and the robots cache are in memory, so a second instance would double the rate to each host and break Article 2.4.
- **Firewall.** The gateway is only enforcement if other egress is denied, so jobs must have no other route out.
- **Caller authentication.** `X-LC-Actor` is what the caller claims, so it is only useful for logs until access to the gateway is restricted by IAM.
