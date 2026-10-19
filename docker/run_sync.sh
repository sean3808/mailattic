#!/bin/sh
set -eu

cmd="python /opt/mailattic/run_sync.py --database ${DATABASE_PATH:-/workspace/mailattic.db} --maildir-root ${MAILDIR_ROOT:-/workspace/.mailattic-work/maildir} --log-dir ${LOG_DIR:-/workspace/.mailattic-work/logs}"

if [ -n "${SYNC_FOLDER:-}" ]; then
  cmd="$cmd --folder \"$SYNC_FOLDER\""
fi

if [ -n "${FULL_RESYNC:-}" ]; then
  cmd="$cmd --full-resync"
fi

eval "$cmd"
