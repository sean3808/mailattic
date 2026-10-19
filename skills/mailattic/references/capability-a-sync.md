# Capability A - Sync (pull fresh mail)

> `SKILL.md`'s capability routing table loads this file (MANDATORY).
> When the task is Sync only, **do not load** `capability-b-find.md` / `capability-c-reply.md`.

## One track: sync-imap carries both inbound and outbound (since 2026-07-14)

| Direction | Source | Coverage |
|---|---|---|
| **inbound** | `bes sync-imap` (INBOX) | continuous, no gap |
| **outbound** | `bes sync-imap` (IMAP `寄件備份` folder) | **since 2026-07-14**; up to 2026-05-27 it came from PST import |

Since Outlook moved to IMAP on 2026-07-14, sent mail lands in the server's `寄件備份` folder, and `sync-imap` syncs it directly with the full original headers (`docs/decisions/D-20260714-1-imap-sent-is-sole-outbound-source.md`). The `bes scrape-archive` schedule is disabled and demoted to a manual fallback (run by hand inside WSL only); it is **not** a caller agent's regular outbound path - do not call it automatically.

Residual: mail that was not sent through Outlook never reaches `寄件備份`.

## When to use

- The user's query involves mail from the **last 24 hours** ("did anything arrive today", "did X just email me", "latest", "the last day or two") -> run `--pre-task` first, then go to Capability B
- The user wants mail they **sent** ("the mail I just sent X", "the one I sent today") -> run `--pre-task` as well (it syncs `寄件備份`)
- The user says "同步", "拉信", "更新 DB", or "sync mailbox"
- During Capability B, `SELECT MAX(sent_at) FROM messages` is more than 1 day behind today and the query is time-sensitive
- **Skip it for history lookups** ("that old mail", "some month in 2024"): the DB already covers them and a sync is pure waste

## How to use

The caller agent's entry point is `--pre-task`, which has a built-in 15-minute throttle and is safe to call repeatedly:

```bash
bes sync-imap --pre-task
# or with JSON output:
bes sync-imap --pre-task --json
```

`--pre-task` behaviour: if the last sync **attempt** was less than 15 minutes ago, it skips and prints a throttle message; otherwise it runs a normal sync. The caller never has to decide "should I sync" - just call it. The `--json` flag (valid only with `--pre-task`; alone it exits 2) prints the result as JSON. `status` is `skipped` / `synced` / `failed` (configuration error). When `skipped`, the output always carries `last_sync_failed` (bool), `last_sync_exit_code`, `last_sync_failure_stage`, and `last_sync_failure_message` (`null` when not applicable); reading `last_sync_failed` alone is enough. `synced` / `failed` carry `exit_code`.

**"Attempt" is not "success"**: the throttle reads the previous run's end time without filtering on exit status, so **a failed run also makes the next 15 minutes of calls skip**. When the previous sync failed, the skip output carries `last sync failed`; when a later sync started but left no completion record (interrupted before writing its record, or still running) it carries `last sync did not complete`; when the sync state is unreadable it carries `last sync status unknown` (in JSON all three give `last_sync_failed: true`, with stage = the recorded value / `unfinished` / `state_unreadable`). Only on "skipped with none of the three" may the caller treat a miss as a true miss; otherwise the mail store may be incomplete. For the "IMAP timeout retry" item below, wait the 5-10+ minutes it says: retrying at once only returns the throttle message instead of a real retry.

To estimate how many mails a sync would fetch:

```bash
bes sync-imap --dry-run
```

It only counts and writes nothing to the DB; use it to look before deciding when the sync looks large.

Use `--full-resync` only when bulk_guard blocks (e.g. a folder's UIDs exceed the 500 threshold, so a normal sync exits 1 and prints a `bulk_guard` message):

```bash
bes sync-imap --full-resync
```

It asks for a typed confirmation, clears the JSON watermark mirror (`imap_sync_progress` inside the DB is not wiped first; it is overwritten as the re-fetch advances), and re-fetches everything. **This is an escape hatch, not a regular path.**

## Failure modes

- **`bulk_guard 'INBOX': N UIDs exceeds bulk threshold 500`** -> sync is not broken; the guardrail blocked on purpose. Check N first: below 1000, consider running `--full-resync` after confirming; at 1000 or more, report to the user to judge whether the mailbox has a problem
- **`exit=1` with an IMAP timeout / connection reset message** -> network or server flutter; retry `--pre-task` after 5-10 minutes, and the throttle prevents bursts
- **`exit=1` with a UID whose message cannot be ingested (message-level ingest failure)** -> sync now **stops at that UID on purpose** (fail-loud): the watermark does not pass it, the failure is recorded in `ingest_failures`, and the next sync retries from the same UID. This is deliberate design (it blocks silent mail loss), not a broken sync. If the same UID blocks every time, that mail may be permanently unparseable: report to the user for human intervention (fix the root cause or advance the watermark by hand) and do not bypass automatically. An attachment decode failure does **not** trigger this stop (the mail body is still ingested)
- **`exit=1` with `list: skipped folder: LIST returned a folder name that is not ASCII ...`** (`--dry-run` also prints `ERROR: skipped folder` and exits 1) -> the server sent a folder name in literal form that cannot be decoded; only that folder is skipped, the other folders sync normally and their mail is ingested. The run is recorded as failed (the sync record says `failed`) and the watermark did not move; report the folder name to the user for a human to handle, and do not retry automatically
- **DB just restored from a backup / a different `--database`** -> sync progress lives inside the DB (`imap_sync_progress`), not only in the `.imap_watermarks.json` next to the maildir. A JSON newer than the DB is not trusted, and the next sync back-fills the missing mail automatically, so the watermark needs no manual change. A legacy JSON not bound to a DB is honoured only up to the highest UID this DB actually holds; a folder with no mail at all in the DB rescans from UID 1. Deleting or losing the JSON mirror also rescans from UID 1. When the DB has progress and the JSON mirror is present, any size of gap is back-filled directly; `bulk_guard` exits non-zero only on a rescan from UID 1 (the folder has no progress in the DB, or the JSON mirror is lost) of more than 500 mails, and only then go to `--full-resync`
- **`exit=0` with `new=0 deduped=0`** -> normal, no new mail; not a failure
- **No log at all** (e.g. `.mailattic-work/logs/mbsync-<today>.log` does not exist) -> Windows Task Scheduler did not fire; this is the scheduler layer, not sync itself. Remind the user to check Task Scheduler and do not try to work around it inside the skill

- **Mail whose attachment write failed** (`still failing` > 0 on the `attachment-retry:` line, or `unresolved_attachment_failures` > 0 in `bes db doctor`) -> every sync starts by retrying automatically from the local original mail; an original older than seven days is moved into quarantine (by default `.mailattic-work/quarantine/<database file name>/` next to the database, travelling with it; doctor's `quarantined_mail` counts every `.eml` in that directory) instead of being deleted, until recovered or abandoned. For one that cannot be recovered: `bes db doctor` lists its `id=`, and **a human** decides, then runs `bes db abandon-attachment-failure --id <id> --reason "..."` (destructive, needs user confirmation; an agent in a non-interactive environment must not add `--yes` itself, and the system never abandons automatically)

When a sync fails, never retry automatically many times or escalate to `--full-resync` on your own: it is irreversible and I/O-heavy, and needs user authorisation.
