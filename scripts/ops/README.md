# scripts/ops

One-off GCP ops scripts for the `daneshire` project (Cloud Run service
`danecast-trades`, us-central1). Both scripts are dry-run by default and only
change anything with `--apply`. Neither prints Scheduler job headers: those
carry the `X-Scheduler-Secret` value, so every `gcloud scheduler` call here
uses an explicit `--format` field list.

Run from the repo root in Git Bash. From PowerShell, `bash scripts/ops/<script>`
works if Git Bash is on PATH. The dry run also prints single-line gcloud
commands that can be pasted straight into PowerShell.

## Run order

1. `bash scripts/ops/scheduler_hardening.sh` (dry run), then `--apply`
2. `bash scripts/ops/monitoring_alerts.sh --email <address>` (dry run), then add `--apply`.
   Add `--with-cloud-run-5xx` to also alert on any 5xx from the service.

Do the deadlines first. The DEADLINE_EXCEEDED failures that are happening now
would otherwise trigger the new Scheduler alert every night.

## scheduler_hardening.sh

Sets `attemptDeadline` and `retryConfig` on all nine jobs. Headers, URIs,
schedules and time zones are left alone.

- **Deadline:** about 2x the observed max runtime, rounded up to 30s, with a
  floor of 180s and a cap of 1800s. Scheduler marks an attempt as failed
  (504 DEADLINE_EXCEEDED) when the deadline passes, even though Cloud Run keeps
  running the request and usually returns 200.
- **Retries:** only for idempotent endpoints. Scheduler also retries on
  DEADLINE_EXCEEDED, so a retry can overlap a first attempt that is still
  running. Backoff for the jobs that retry is 60s to 300s.

The per-job values and the reason for each are in the `JOBS` table at the top of
the script. Revisit `alert-price-checks` / `alert-technical-checks` (0 to 1
retry) once PR #89 (claim-before-send in `AlertEngine.run`) is merged.

`db-nightly-backup` must never retry. A same-day re-run returns 500 by design,
because the runtime SA has `objectCreator` only and cannot overwrite the day's
object. Do not "fix" that by broadening the role.

## monitoring_alerts.sh

Creates an email notification channel for the address you pass (or reuses an
existing one) and log-based alert policies from the JSON files in this folder:

| File | Filter | Notes |
|------|--------|-------|
| `policy_scheduler_failures.json` | `resource.type="cloud_scheduler_job" AND severity>=ERROR` | Failed attempts: 401, 504, 5xx. Labels: `job_id`, `status` |
| `policy_cloud_run_5xx.json` (optional) | `resource.type="cloud_run_revision" AND resource.labels.service_name="danecast-trades" AND httpRequest.status>=500` | Also catches user-facing 5xx |

Both policies have a 1h notification rate limit and auto-close after 24h.
Policies that already exist (matched by display name) are skipped. Needs the
gcloud alpha component (`gcloud components install alpha`).

## Verify

```bash
# One job
gcloud scheduler jobs describe alert-reminders --location=us-central1 --project=daneshire --format="yaml(attemptDeadline,retryConfig)"

# All jobs (never use a bare `describe` or `--format=json`: the output includes the secret header)
gcloud scheduler jobs list --location=us-central1 --project=daneshire --format="table(name.basename(),attemptDeadline,retryConfig.retryCount,retryConfig.minBackoffDuration,retryConfig.maxBackoffDuration)"

# Failed attempts in the last day
gcloud logging read 'resource.type="cloud_scheduler_job" AND severity>=ERROR' --project=daneshire --freshness=1d --format="value(timestamp,resource.labels.job_id,jsonPayload.status,httpRequest.status)"

# Alerting
gcloud beta monitoring channels list --project=daneshire --format="table(name,type,displayName)"
gcloud alpha monitoring policies list --project=daneshire --format="table(name,displayName,enabled)"
```

To test the Scheduler policy end to end, force one job run with
`gcloud scheduler jobs run alert-expire-stale --location=us-central1 --project=daneshire`.
It is idempotent but succeeds, so it does not fire the alert. Seeing an alert
needs a real failure, so it is simplest to wait for the next one.

## Rotating `scheduler-secret`

Each Scheduler job stores a literal copy of the `X-Scheduler-Secret` header. A
job does not read Secret Manager. Rotating the secret therefore means updating
**every** job's header in the same change. In March 2026 a v1-to-v2 rotation
missed four alert jobs, and they returned 401 for about six months before
anyone noticed (see the 2026-09-24 session log in `TODO.md`, which later moves
to `TODO_ARCHIVE.md`). The Scheduler failure policy above is the alert that
would have caught it on day one.
