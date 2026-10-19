#!/bin/sh
set -eu

required_vars="IMAP_HOST IMAP_PORT IMAP_USER IMAP_PASSWORD IMAP_ACCOUNT_NAME"
missing_vars=""
for name in $required_vars; do
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    missing_vars="$missing_vars $name"
  fi
done

if [ -n "$missing_vars" ]; then
  echo "Missing required environment variables:$missing_vars" >&2
  exit 1
fi

MAILDIR_ROOT="${MAILDIR_ROOT:-/workspace/.mailattic-work/maildir}"
LOG_DIR="${LOG_DIR:-/workspace/.mailattic-work/logs}"
MBSYNC_CONFIG_PATH="${MBSYNC_CONFIG_PATH:-/workspace/.mailattic-work/mbsync/mbsyncrc}"
MBSYNC_TEMPLATE_PATH="${MBSYNC_TEMPLATE_PATH:-/opt/mailattic/mbsyncrc.template}"
MBSYNC_PATTERNS_LINE="${MBSYNC_PATTERNS_LINE:-Patterns *}"
SYNC_CRON_SCHEDULE="${SYNC_CRON_SCHEDULE:-45 8 * * *}"

export MAILDIR_ROOT MBSYNC_PATTERNS_LINE
mkdir -p "$(dirname "$MBSYNC_CONFIG_PATH")" "$MAILDIR_ROOT" "$LOG_DIR" "$MAILDIR_ROOT/$IMAP_ACCOUNT_NAME"
envsubst '${IMAP_HOST} ${IMAP_PORT} ${IMAP_USER} ${IMAP_PASSWORD} ${IMAP_ACCOUNT_NAME} ${MAILDIR_ROOT} ${MBSYNC_PATTERNS_LINE}' \
  < "$MBSYNC_TEMPLATE_PATH" \
  > "$MBSYNC_CONFIG_PATH"

crontab_file="/tmp/mbsync.crontab"
printf '%s %s\n' "$SYNC_CRON_SCHEDULE" "/usr/local/bin/run-sync.sh" > "$crontab_file"
crontab "$crontab_file"

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec cron -f
