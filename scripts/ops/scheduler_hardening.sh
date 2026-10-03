#!/usr/bin/env bash
# Raise Cloud Scheduler attempt deadlines and set per-job retry policy.
#
# Dry-run by default: prints each job's current deadline/retry config and
# the exact one-line gcloud command it would run. Pass --apply to execute.
#
#   bash scripts/ops/scheduler_hardening.sh            # dry run
#   bash scripts/ops/scheduler_hardening.sh --apply    # make the changes
#
# Only attemptDeadline and retryConfig are touched. Headers, URIs, schedules
# and time zones are left as they are. Every gcloud call uses an explicit
# --format field list so the X-Scheduler-Secret header is never printed.
#
# Why (observed 2026-09-18..10-02 from Cloud Run request logs):
#   - Every job had retryCount unset (= 0 retries), so a failed run was simply
#     lost, despite code comments and ADR-0006 assuming Scheduler retries.
#   - Deadlines of 60-120s were below observed runtimes: refresh-calendar
#     (max 164s vs 120s), run-reminders (145s vs 60s), run-technical-checks
#     (109s vs 60s), run-earnings-checks (97s vs 60s). Scheduler logged
#     DEADLINE_EXCEEDED while the server went on to return 200.
#
# Deadline rule: ~2x observed max, rounded up to 30s, min 180s, max 1800s.
# A retry is only safe if the endpoint is idempotent, because Scheduler
# retries on DEADLINE_EXCEEDED too, while the first attempt may still be
# running server-side.
#
# PowerShell: run the script with `bash scripts/ops/scheduler_hardening.sh`
# (Git Bash is on PATH), or copy the single-line commands the dry run prints
# and paste them straight into PowerShell. They contain no line continuations.

set -euo pipefail

PROJECT="daneshire"
LOCATION="us-central1"
MIN_BACKOFF="60s"
MAX_BACKOFF="300s"

APPLY=0
case "${1:-}" in
  --apply) APPLY=1 ;;
  ""|--dry-run) ;;
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  *) echo "unknown argument: $1 (use --apply or --dry-run)" >&2; exit 2 ;;
esac

# job | attempt deadline | retry attempts | rationale
JOBS=(
  # 15-min cadence: the next run is the retry. On master a retry that overlaps
  # a still-running attempt can double-push (status is committed only at the
  # end of AlertEngine.run). Revisit to 1 once PR #89's claim-before-send
  # (UPDATE ... WHERE status='active') is merged.
  "alert-price-checks|180s|0|15-min cadence; next run is the retry; overlap could double-push until #89"
  "alert-technical-checks|240s|0|15-min cadence; next run is the retry; overlap could double-push until #89"
  # Daily: a missed run loses the day's pushes, so take one retry. Worst case
  # on master is a duplicate push, which beats a silent miss. #89 makes it safe.
  "alert-reminders|300s|1|daily; a miss loses the reminder; duplicate push < missed push"
  "earnings-run-alert-checks|240s|1|daily; a miss loses the earnings alert; duplicate push < missed push"
  # Pure UPDATE ... WHERE expires_at < now(): idempotent.
  "alert-expire-stale|180s|2|idempotent UPDATE"
  # Upsert (on_conflict_do_update) into earnings_events: idempotent.
  "earnings-refresh-calendar|360s|2|idempotent upsert"
  # on_conflict_do_nothing per (ticker, date): a retry fills only missing rows.
  # 15:45 + 180s + backoff (60s, 120s) still lands before the 16:00 close.
  "earnings-refresh-snapshots|180s|2|on_conflict_do_nothing; retry fills gaps"
  # Hourly; a retry after a partial run can re-send a worsening push.
  "portfolio-plateau-checks|180s|0|hourly; next run is the retry; partial-run retry can re-push"
  # Same-day rerun 500s by design: the runtime SA has objectCreator only and
  # cannot overwrite today's object. Retries would only add noise. Deadline is
  # kept at 360s (above 2x the 103s max) since pg_dump grows with the DB.
  "db-nightly-backup|360s|0|same-day rerun 500s by design (objectCreator cannot overwrite)"
)

SAFE_FMT='value(name.basename(),attemptDeadline,retryConfig.retryCount,retryConfig.minBackoffDuration,retryConfig.maxBackoffDuration)'

if [[ $APPLY -eq 1 ]]; then
  echo "== APPLY mode: project=$PROJECT location=$LOCATION"
else
  echo "== DRY RUN (pass --apply to execute): project=$PROJECT location=$LOCATION"
fi
echo

for row in "${JOBS[@]}"; do
  IFS='|' read -r job deadline retries why <<<"$row"

  # csv keeps empty fields (an unset retryCount) in place; tab-separated
  # value() output collapses them under `read`.
  current=$(gcloud scheduler jobs describe "$job" --location="$LOCATION" --project="$PROJECT" --format="csv[no-heading](name.basename(),attemptDeadline,retryConfig.retryCount,retryConfig.minBackoffDuration,retryConfig.maxBackoffDuration)" 2>/dev/null || true)
  if [[ -z "$current" ]]; then
    echo "!! $job: not found, skipping"
    echo
    continue
  fi
  current=${current%$'\r'}  # gcloud on Windows ends lines with CRLF
  IFS=',' read -r _ cur_deadline cur_retries cur_min cur_max <<<"$current"

  args=(scheduler jobs update http "$job" "--location=$LOCATION" "--project=$PROJECT" "--attempt-deadline=$deadline" "--max-retry-attempts=$retries")
  if [[ "$retries" -gt 0 ]]; then
    args+=("--min-backoff=$MIN_BACKOFF" "--max-backoff=$MAX_BACKOFF")
  fi
  # update prints the job resource by default; restrict it so the header
  # (which carries the scheduler secret) never reaches the terminal.
  args+=("--format=$SAFE_FMT")

  echo "-- $job"
  echo "   now:  deadline=${cur_deadline:-?} retries=${cur_retries:-0} backoff=${cur_min:-?}..${cur_max:-?}"
  echo "   want: deadline=$deadline retries=$retries$([[ $retries -gt 0 ]] && echo " backoff=$MIN_BACKOFF..$MAX_BACKOFF")"
  echo "   why:  $why"
  # Printed form quotes --format so it pastes cleanly into bash or PowerShell.
  printable="gcloud ${args[*]:0:${#args[@]}-1} --format=\"$SAFE_FMT\""
  echo "   cmd:  $printable"

  if [[ $APPLY -eq 1 ]]; then
    gcloud "${args[@]}"
  fi
  echo
done

if [[ $APPLY -eq 1 ]]; then
  echo "== Done. Verify with:"
else
  echo "== Dry run only. Nothing changed. Verify current state with:"
fi
echo "gcloud scheduler jobs list --location=$LOCATION --project=$PROJECT --format=\"table(name.basename(),attemptDeadline,retryConfig.retryCount,retryConfig.minBackoffDuration,retryConfig.maxBackoffDuration)\""
