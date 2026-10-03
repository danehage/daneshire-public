#!/usr/bin/env bash
# Create an email notification channel and log-based alert policies so a
# failing Cloud Scheduler job pages someone instead of failing silently.
#
# Dry-run by default: shows what exists and what it would create.
#
#   bash scripts/ops/monitoring_alerts.sh --email you@example.com
#   bash scripts/ops/monitoring_alerts.sh --email you@example.com --apply
#   bash scripts/ops/monitoring_alerts.sh --email you@example.com --with-cloud-run-5xx --apply
#
# Policies (checked in next to this script):
#   policy_scheduler_failures.json  resource.type="cloud_scheduler_job" AND severity>=ERROR
#                                   (failed AttemptFinished: 401, 504 DEADLINE_EXCEEDED, 5xx)
#   policy_cloud_run_5xx.json       optional (--with-cloud-run-5xx): any 5xx from danecast-trades
#
# Both are log-match conditions with a 1h notification rate limit and a 24h
# auto-close. Re-running is safe: an existing channel for the same address or
# a policy with the same display name is reused/skipped, not duplicated.
#
# Why: the alert engine 401'd silently from March to September 2026 and
# earnings-refresh-calendar has hit DEADLINE_EXCEEDED nightly since
# 2026-09-30. No alert policy or notification channel existed to say so.
#
# Needs the gcloud alpha component for `monitoring policies`:
#   gcloud components install alpha

set -euo pipefail

PROJECT="daneshire"
HERE="$(cd "$(dirname "$0")" && pwd)"

APPLY=0
EMAIL=""
WITH_RUN_5XX=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dry-run) ;;
    --email) EMAIL="${2:-}"; shift ;;
    --email=*) EMAIL="${1#--email=}" ;;
    --with-cloud-run-5xx) WITH_RUN_5XX=1 ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "$EMAIL" ]]; then
  echo "--email ADDRESS is required (the address alerts are sent to)" >&2
  exit 2
fi

strip_cr() { tr -d '\r'; }

if [[ $APPLY -eq 1 ]]; then
  echo "== APPLY mode: project=$PROJECT email=$EMAIL"
else
  echo "== DRY RUN (pass --apply to execute): project=$PROJECT email=$EMAIL"
fi
echo

if ! gcloud alpha monitoring policies list --project="$PROJECT" --limit=1 --format="value(name)" </dev/null >/dev/null 2>&1; then
  echo "!! 'gcloud alpha monitoring policies' unavailable. Install it with: gcloud components install alpha"
  [[ $APPLY -eq 1 ]] && exit 1
  ALPHA_OK=0
else
  ALPHA_OK=1
fi

# --- 1. Notification channel -------------------------------------------------
CHANNEL=$(gcloud beta monitoring channels list --project="$PROJECT" \
  --filter="type=\"email\" AND labels.email_address=\"$EMAIL\"" \
  --format="value(name)" --limit=1 2>/dev/null | strip_cr)

if [[ -n "$CHANNEL" ]]; then
  echo "-- channel: reusing $CHANNEL"
else
  cmd=(beta monitoring channels create "--project=$PROJECT" --type=email
       "--display-name=Danecast ops ($EMAIL)"
       "--channel-labels=email_address=$EMAIL"
       "--format=value(name)")
  echo "-- channel: would create email channel for $EMAIL"
  echo "   cmd: gcloud beta monitoring channels create --project=$PROJECT --type=email --display-name=\"Danecast ops ($EMAIL)\" --channel-labels=email_address=$EMAIL"
  if [[ $APPLY -eq 1 ]]; then
    CHANNEL=$(gcloud "${cmd[@]}" | strip_cr)
    echo "   created $CHANNEL"
  else
    CHANNEL="projects/$PROJECT/notificationChannels/<new>"
  fi
fi
echo

# --- 2. Policies -------------------------------------------------------------
POLICIES=("policy_scheduler_failures.json")
[[ $WITH_RUN_5XX -eq 1 ]] && POLICIES+=("policy_cloud_run_5xx.json")

RENDERED=""
cleanup() { [[ -n "$RENDERED" && -f "$RENDERED" ]] && rm -f "$RENDERED"; return 0; }
trap cleanup EXIT

for file in "${POLICIES[@]}"; do
  src="$HERE/$file"
  name=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['displayName'])" "$src" 2>/dev/null \
         || sed -n 's/^  "displayName": "\(.*\)",$/\1/p' "$src" | head -1)

  existing=""
  if [[ $ALPHA_OK -eq 1 ]]; then
    existing=$(gcloud alpha monitoring policies list --project="$PROJECT" \
      --filter="displayName=\"$name\"" --format="value(name)" 2>/dev/null | strip_cr | head -1)
  fi

  if [[ -n "$existing" ]]; then
    echo "-- policy '$name': exists ($existing), skipping"
    echo
    continue
  fi

  echo "-- policy '$name': would create from scripts/ops/$file -> $CHANNEL"
  echo "   filter: $(grep -o '"filter": "[^}]*' "$src" | head -1 | sed 's/^"filter": //')"
  if [[ $APPLY -eq 1 ]]; then
    # Relative path: avoids MSYS path translation when gcloud.cmd runs from Git Bash.
    RENDERED="scripts-ops-rendered-$$.json"
    sed "s#__CHANNEL__#$CHANNEL#" "$src" > "$RENDERED"
    gcloud alpha monitoring policies create --project="$PROJECT" \
      --policy-from-file="$RENDERED" --format="value(name)"
    rm -f "$RENDERED"; RENDERED=""
  fi
  echo
done

echo "== Verify:"
echo "gcloud beta monitoring channels list --project=$PROJECT --format=\"table(name,type,displayName)\""
echo "gcloud alpha monitoring policies list --project=$PROJECT --format=\"table(name,displayName,enabled)\""
