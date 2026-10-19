---
name: mailattic
description: >
  Cross-project interface to Sean's personal mail server (mailattic.test) history: three capabilities - pull fresh mail, find old mail + extract prices, reply to an existing mail.
  Runs on the global `bes` CLI + the `MAILATTIC_DB` env var, so no cd into the mailattic repo is needed.
  Capabilities: sync (bes sync-imap), find (bes search/show/thread; bes attachments for attachment-first lookup/export; price and unanswered-mail (ball-in-court) extraction; bes contact-history for a multi-year correspondence summary; bes search --full-body --json for batch retrieval), reply (bes draft-reply -> present to the user -> bes send-draft/send-batch with the draft fingerprint).
  Use when the user asks to search historical mail (查歷史信件), look up a quote (報價), find a commitment (承諾), summarise years of correspondence with a contact (歷年往來摘要), check whether a given mail arrived today (確認今天有沒有收到某封信), draft a reply to an existing mail (擬回覆), or check the latest sync state (同步狀態).
  Do not trigger for composing a mail from scratch to a new recipient (the platform has no compose-from-scratch; see "Unsupported capabilities").
---

# mailattic - cross-project interface to Sean's mail

This skill is the interface layer that agents in other projects use to reach mailattic. The platform is a SQLite + FTS5 mail DB (tens of thousands of mails, years of accumulation) + daily IMAP sync + an outbound reply pipeline. The CLI is `bes`, installed as a global tool (install steps: project CLAUDE.md); it resolves the central DB path from the `MAILATTIC_DB` env var.

## Reporting problems

When this skill or the `bes` CLI hits a bug, the docs disagree with the implementation, query results look clearly wrong, or a capability is missing, open an issue in the source repo:

```bash
gh issue create --repo sean3808/mailattic
```

Include the reproducing command, key output, expected behaviour, and actual behaviour; do not leave the problem only in the current session.

Three capabilities:
- **A. Sync**: pull the mailbox's latest state into the DB (call when needed, not on every task)
- **B. Find**: turn a vague memory step by step into a db_id -> fetch the original -> extract prices / commitments / flag unanswered mail. Also covers `bes attachments` (attachment-first search + `--copy-to` export; the extension semantics come from `attachment_name` / `mime_type`, never guessed from the CAS path), `bes contact-history` (multi-year correspondence summary, the whole relationship), and `bes search --full-body --json` (batch retrieval of full bodies + thread linkage, ingestion-grade, removes N+1 `bes show`). For mail forensics headers (source IP / Received chain / SPF / DKIM) use `bes show <id> --headers` (details, including the privacy rules, in find.md)
- **C. Reply**: draft a reply-all to an existing mail in the DB (`--attach` can add attachments) -> **present the draft to the user and wait for the user to say send** (mandatory step) -> SMTP-send with the draft fingerprint

The three often chain: Sync -> Find -> Reply. Each capability's full content sits in the matching file under `references/`; load it per the routing table below (MANDATORY).

---

## Shared background (all capabilities)

> Assumes the global `bes` is installed and `MAILATTIC_DB` points at the central DB (install / env var setup belongs to the project layer, see project CLAUDE.md). This skill covers operation only.

- DB scale: several GB, tens of thousands of mails, years of accumulation. Ignore the 64 KB `prompt-import*.db` files (early smoke-test leftovers) and the `*.bak-*` / `*.pre-mbsync-*` historical backups.
- Environment: Windows 11 / Python 3.13 / `uv`.
- Bundled tools are versioned against the released BES: `ball-in-court.py` finds the global BES through `uv tool dir --bin` and calls `thread` / `show`; run it directly with `uv run {SKILL_DIR}/scripts/ball-in-court.py`, independent of the platform working tree. A release syncs and verifies this skill's SSOT content.

## Core DB schema summary

```
messages(id, subject, sender_email, sent_at, body_text, message_id,
         in_reply_to, references_header, pst_source, folder_path, ...)
message_recipients(message_id, recipient_type{to|cc|bcc}, email, display_name)
message_fts(...)        -- FTS5 over subject/body/recipients/attachments
message_attachments(...)
```

`messages.id` is the db_id (int); `messages.message_id` is the RFC822 Message-ID string (may be None). `message_recipients.message_id` points at `messages.id` (not the RFC822 string) - confusing name, but that is how it is. `bes show <id>` / `bes thread <id>` locate by integer db_id; `bes draft-reply <message_id>` takes the original mail's stored RFC822 `messages.message_id` and rejects DB / archive row ids.

## Fatal pitfalls (read before acting)

These cost turns in past practice. All three capabilities share this list.

1. **cp950 UnicodeEncodeError**: `bes` already runs `sys.stdout/stderr.reconfigure(encoding='utf-8')` at the top of `main()`, so **calling `bes search ...` directly needs no `PYTHONIOENCODING` prefix**. Raw Python (`uv run python -c "..."` reading the DB / printing Chinese) still needs it:
   ```bash
   PYTHONIOENCODING=utf-8 uv run python -c "..."
   ```
   `$env:PYTHONIOENCODING=...` is PowerShell syntax; the Bash tool's `/usr/bin/bash` does not understand it.
2. **HTML body is 80% CSS noise**: an Outlook-exported body_text carries full `@font-face` / `<style>` / `<!-- -->`. Read the cleaned version with `bes show <id> --format text` (internally `query.render_text()`) rather than the raw body. For batches (agent / ingestion) use the `rendered_body_text` field of `bes search --full-body --json` (same cleaning) instead of `bes show` per mail.
3. **FTS5 `--keyword` syntax**: space-separated terms mean AND; combine groups with uppercase `AND` / `OR` / `NOT` and parentheses, e.g. `A AND (B OR C OR D)`. Operators are **case-sensitive**: lowercase `and` / `or` / `not` are literal terms. Double-quoted phrases work; a token with a hyphenated number (e.g. `PEN-100`) needs a phrase quote: `'"PEN-100"'`. **Unsupported**: `*` prefix, column filters, `NEAR`.
4. **Repeated-subject threads flood results**: a thread like "RE: RE: RE: [customer]Order#xxx" repeats the same keywords in every mail. **With `--keyword`, `bes search` ranks by relevance (bm25) instead of newest-first**, and every hit carries `match_in` (`new` / `quoted`) saying whether the snippet's match sits in this mail's new content or in quoted history - to find the earliest quote / original source, look for `match_in: new` instead of reading each body. This does **not replace** the cross-filter: for "MUST term and CONTEXT term co-occur in the same mail", use `scripts/find-cross-filter.py` (see Stage 3).
5. **One keyword, several customers** (customer disambiguation): product / item codes are often asked about by several customers at once, so a keyword hit alone is not a reportable answer. Split by customer with `--participants-domain`, confirm with the user which side to pursue, then read bodies. The three domain flags (`--sender-domain` / `--recipient-domain` / `--participants-domain`) are all **repeatable, values OR-ed** (e.g. a customer group spanning domains: `--participants-domain a.com --participants-domain b.com`); single-value use stays fully backward compatible; details in `references/capability-b-find.md`.
6. **Ball-in-court (unanswered) blind spot**: when the user asks "did I quote X to Y", the last mail of the matching thread may be **the other side chasing with no reply from us**; taking only the latest mail would wrongly conclude "replied". A Capability B report always includes an "unanswered" section, using `scripts/ball-in-court.py`. **That script reads the `direction` field that `bes` emits to decide ours vs. theirs** (it no longer compares domains itself; the `--our-domains` flag is removed). `direction` is computed from the operator-declared set of self addresses, **not from domains** - a colleague on the same domain counts as inbound, deliberately. With the set unset there is no built-in address to fall back on (`bes identity list` shows `source` = `unset`), and every mail is then judged inbound without any error; when unanswered results look wrong, run `bes identity list` first to check the set; details in `references/capability-b-find.md`. When a thread hits the CLI's 100-mail cap the script sees only the earliest 100, prints `[indeterminate]`, exits 3, and draws no "whose turn" conclusion (narrow by date with `bes search` and look up the newest mail instead); a missing direction prints `[unknown-direction]` and is not treated as ours.
7. **The DB cutoff is not necessarily now**: the newest `sent_at` in the DB depends on the IMAP sync cycle. If a query involves "today / 24h / recent / just now", Capability B first triggers Capability A's `--pre-task`. **That wording trigger has a blind spot**: the real condition for a stale DB is "the target mail is new", not "the user said a time word". A query worded like history search whose target just arrived misses the trigger and hits a stale DB. So **when the main five-stage search returns 0 hits, never assert "no such mail"**: first run one throttle-gated `bes sync-imap --pre-task` and branch on the first line of its stdout. `mbsync: pulled` -> re-search; still 0 is a true negative. `sync-imap: skipped` whose parentheses hold only a time (none of `last sync failed` / `did not complete` / `status unknown`) -> the last sync succeeded, the DB is fresh, and 0 hits is a true negative. If skipped carries any of those markers, or exit != 0, or `mbsync: ERROR` -> report "cannot confirm; the last sync failed / sync failed, the mail store may be incomplete" and do not claim the mail does not exist. Full procedure: the "0-hit sync verification" section under failure modes in `capability-b-find.md`.
8. **Ask the user before sending, and in Claude Code always send with the fingerprint**: the user decides in conversation whether to send. Present the draft id, sending account (`From:`), subject, full To/Cc/Bcc, body, attachments, and fingerprint; send only after the user says send (several mails: one summary table, one authorisation). bes cannot verify "was the user asked", only "what is sent equals what was seen": `send-draft <draft_id> --fingerprint <sha>` / `send-batch <id>=<sha> ...`, and a fingerprint that differs from the current draft refuses the send. **Claude Code's `!` is not a TTY**, and neither is the agent's shell tool; without a fingerprint, sends there are always refused. Never import `_prepare_draft_for_send` / `_submit_prepared_draft` to bypass this. `send-draft` printing `Partially sent` (exit 3) / `send-batch` outcome `partial` / MCP `status="partial"` = the mail went out but SMTP refused some recipients: **never resend the whole mail**, open a new draft for the `refused` addresses only (#224). stderr showing `SMTP close failed after accept` / a result carrying `close_warning` = the server accepted the mail and only closing the connection failed: **the mail is sent, never resend** (#230). A draft stuck in `sending` (`list-drafts` shows `status: sending`, `send-draft` refuses, `send-batch` skips) = the last send did not finish and the mail may have gone out: follow the marker path and steps the message prints; the steps are in `references/capability-c-reply.md` under "Draft stuck in `sending`" (#304). Full flow: `references/capability-c-reply.md` Step 3-4.
   **bes does not wire in an LLM to write content**: Capability C does mechanical steps only (header assembly / SMTP / DB insert / fingerprint check or TTY authorisation). bes resolves the sending settings file in order: `--env-file` -> `MAILATTIC_SMTP_ENV_FILE` -> built-in default (`.env.sean3808` at the repo root); no flag is normally needed. When the resolved file does not exist, all three outbound commands exit 2 before I/O and list three ways out. archive-scrape's `MAIL_SERVER_ENV_PATH` is not an SMTP settings source. The caller AI writes the body into a file itself and feeds it through `--body-from <file>` to `draft-reply`. Recipients: add with repeatable `--to` / `--cc` / `--bcc`, drop derived addresses with `--exclude`, or replace the reply-all derivation entirely with `--replace-recipients`. An explicit `--to` / `--cc` bucket takes priority over the derived placement; when the same address is listed explicitly in both To and Cc, To wins. In every mode the final To must be non-empty, otherwise exit 2 and no draft. `--exclude` only filters reply-all-derived recipients and must not be combined with `--replace-recipients`; the contradiction exits 2 before the DB is read, with this exact stderr: `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.`. Also, `--exclude` must not overlap (case-insensitively) with any explicit `--to` / `--cc` / `--bcc` address; an overlap likewise exits 2 before env / DB I/O with no draft, and stderr lists the sorted normalised conflicting addresses: `--exclude conflicts with explicit recipient address(es): <addresses>. Remove each address from --exclude or from --to/--cc/--bcc.`. The caller must remove each conflict from the exclusion or the explicit placement; `--exclude` cannot be expected to filter the explicit list. Each explicit address must be a single, syntactically complete address whose addr-spec is ASCII addr-spec; malformed raw syntax or a non-ASCII addr-spec gets exit 2 before env / DB I/O with no draft, while a Unicode display name with an ASCII addr-spec is supported. `message_id` still only drives the thread's `In-Reply-To` / `References`; on success compare the final To/Cc/Bcc in stdout against the approved envelope. The subject defaults to "`Re: ` + original subject"; `--subject <text>` replaces it verbatim (no `Re: ` added, thread unchanged, blank or multi-line exits 2) - use it when the anchor mail's subject does not match what this mail is about. There is no way to make bes generate content itself; do not try.
9. **Cross-project calling contract**: a subagent in another project calling `bes` must follow all four rules below; missing any one couples the call to the mailattic dev venv, and when the dev venv breaks (e.g. a broken reparse point from mixing WSL/Windows) the downstream crashes with exit 2:

   1. **Use the global `bes`** (the uv-tool wheel snapshot executable), with the DB resolved through `MAILATTIC_DB`. The bin dir comes from `uv tool dir --bin` (on Windows the local default is `C:\Users\example\.local\bin\bes.exe`). Confirm the resolved binary is the uv-tool one, **not** the caller venv's `.venv/Scripts/bes` shadow (if the caller project installs `mailattic` into its own venv as a dependency, the `bes` entry point appears in `.venv/Scripts/` and PATH-shadows the global binary) - an absolute path is the most reliable. In code, build the path with `Path.home()` / `os.path.expanduser` to expand the home directory, add `.exe` on Windows, and **never pass a literal `~`** (`subprocess.run(..., shell=False)` does not expand tilde; copying it verbatim gives file-not-found).
   2. **Never use `uv run bes` for cross-project calls** (`uv run` binds execution to the project-local `.venv`). This ban covers the `bes` CLI only; bundled helper scripts via `uv run python ...` are fine.
   3. **Never set the child process `cwd` to the mailattic repo** (or any directory where `uv` would discover that repo's `.venv`).
   4. **Scrub the inherited `VIRTUAL_ENV` environment variable before the call**, so the caller's own environment does not interfere with the global tool.

   **The `uv-run-with-cwd` anti-pattern (named and banned)**: `["uv","run","bes",...]` + `cwd=<mailattic repo>` is explicitly banned by this contract. Example: a downstream script assembled this call, and when the dev `.venv` broke from WSL/Windows mixing, the downstream scan crashed with exit 2.
10. **Sent mail: `sync-imap` already covers it**: since 2026-07-14 Outlook runs over IMAP, sent mail lands in the server's `寄件備份` folder, and `bes sync-imap --pre-task` syncs it too; the `scrape-archive` schedule is disabled and is only a manual fallback - do not call it automatically. 0-hit verification: `references/capability-b-find.md`.
11. **An archive-scrape sent row is not a `draft-reply` anchor by default**: the archive sometimes splits one sent mail into rows by delivery batch, so the DB's To/Cc may hold only one domain, and a reply-all from it silently drops cross-domain recipients. Prefer the **inbound** mail of the same thread; `bes draft-reply` refuses an archive-scrape anchor by default. Only when the caller has verified the final recipients by hand may it pass `--allow-archive-anchor`; then check the To/Cc/Bcc in stdout, and with a hand-approved list also use `--replace-recipients --to ...`. Full send flow and the 90-second paced restart rules: `references/capability-c-reply.md`.
12. **Only a human abandons an attachment failure; the agent never does**: `unresolved_attachment_failures` > 0 in `bes db doctor` means some attachments were not recovered (including old records with no filename). When they cannot be recovered, the user decides, and only then run `bes db abandon-attachment-failure --id <id> --reason "..."` (destructive, needs user confirmation, never add `--yes` on the user's behalf). The original mail stays in quarantine (by default under `.mailattic-work/quarantine/<database file name>/` next to the database, travelling with it; doctor's `quarantined_mail` counts every `.eml` in that directory) until recovered or abandoned. Details: Capability A failure modes.

---

## Capability routing (MANDATORY load triggers)

Load the reference file that matches the user's task. Reference paths are relative to this skill's root.

### Sync task

Trigger: the user says "同步", "拉信", "更新 DB", "sync mailbox", or only wants to confirm the latest sync state.

**MANDATORY** - read `references/capability-a-sync.md` in full. **Do NOT load** `capability-b-find.md` / `capability-c-reply.md`.

### Find task

Trigger: the user wants to find / list mail, look up historical quotes, extract commitments, confirm "did X arrive today", or check a customer's unanswered mail.

**MANDATORY** - read `references/capability-b-find.md` in full.

Sub-cases:
- If Stage 0 decides a sync prelude is needed (the query has "today / 24h / recent / just now / latest") -> also load `references/capability-a-sync.md`, run `--pre-task`, then return to the Find flow
- Otherwise **Do NOT load** the sync and reply files

### Reply task

Trigger: the user says "回覆", "draft 回信", "對 X 那封回信".

**MANDATORY** - read `references/capability-c-reply.md` in full.

Prerequisite: the RFC822 `message_id` of the mail to reply to must already be obtained through Find.
- If the caller already has the original's RFC822 `message_id` (the user gave it in an earlier turn, or it comes from outside) -> go straight into the Reply flow
- If the caller has no RFC822 `message_id` -> first load `references/capability-b-find.md`, find the original with `bes search ... --full-body --json`, and take the hit's `message_id` field, then return to the Reply flow; never use the integer `id` / `db_id` as the `draft-reply` anchor
- **Do NOT load** `capability-a-sync.md` (reply does not depend on a fresh sync)

---

## Capability chain table

Common user intents and their chains:

| User says | Chain | Load order |
|---|---|---|
| "Did customer X's mail arrive today" | A -> B (time = today) | sync.md -> find.md |
| "How much was that earlier Y quote" | B (includes Stage 3.5 price extraction) | find.md |
| "How long have X and I corresponded / who contacts me there" | B contact-history aggregate mode | find.md |
| "Find Z's mail, then I want to reply to him" | B -> C | find.md -> reply.md |
| "Any new mail from W in the last 24h; if so, reply" | A -> B -> C | sync.md -> find.md -> reply.md |
| "List all of today's mail" | A -> B Stage 0.5 browse mode | sync.md -> find.md |
| "Sync now" | A only | sync.md |
| "Write a reply to Message-ID `<abc123@example.com>`" | C only (caller already gave the RFC822 `message_id`) | reply.md |

At the end of each chain, ask the user for the next step: after a find, "Reply to it?" Yes -> C; no -> wrap up.

---

## Unsupported capabilities (explicit)

Spare the caller AI turns on things the platform does not do:

- **Compose from scratch / start a new thread to a new To**: the platform's outbound-send capability still needs an existing DB mail's `<message_id>` as the thread anchor. `draft-reply --replace-recipients --to ...` only replaces that reply's envelope and does not create an anchorless new thread; there is still **no** `bes draft-new` / `bes compose`. When the user wants to send a brand-new mail, tell the user "please send it yourself from Outlook"; do not try to work around this
- **Editing an existing draft's content**: before `send-draft` you may delete the `.eml` and regenerate it, but **never edit the `.eml`** (bes does not recompute In-Reply-To / References / Message-ID)
- **Sending every draft in the folder**: `send-batch <draft_id>=<sha> ...` sends an **explicitly enumerated draft_id list** with one review and one authorisation (fingerprints verified per mail; send-once / from-invariant / SMTP applied per mail; a partial failure does not abort; already-sent ones are skipped automatically); there is **no** "send everything in outbound/drafts" mode (to avoid sending leftover drafts), so every draft_id must be listed
- **Semantic / vector search**: Capability B uses FTS5 keyword + SQL LIKE only; there is no embedding / semantic search
- **Attachment OCR / content extraction**: `message_attachments` indexes file names only; attachment content is not in the DB
- **MCP server / API service**: **your (host subagent's) only interface is the CLI + Python API**; there is no HTTP service, and do not try the MCP route. The one exception is a **read-only** MCP bridge serving the CoWork sandbox only (the sandbox cannot see the host CLI / DB), with six read-only tools live - you cannot reach it and do not need to; use the `bes` subcommands for the same queries. See project [D-20260721-1](../../docs/decisions/D-20260721-1-revisit-mcp-for-cowork.md) and the [MCP server contract](../../docs/mcp-server-contract.md)
- **Asking "how fresh is the data / when did the last sync run"**: the CoWork MCP route has `mail_sync_status`, but the CLI has **no matching subcommand**. To confirm freshness, run Capability A's `bes sync-imap --pre-task` directly (within 15 minutes it skips by itself and prints the last sync time and whether it failed; add `--json` for a structured result and read `status` and `last_sync_failed` (always present when skipped, a bool)); do not read log files or DB tables to piece it together

---

## Wrap-up discipline (all capabilities)

- Delete temp files (such as `.tmp_search.json`) right after the search: `rm .tmp_*.json`
- If the user will keep referencing an original mail, save it to `originals_<topic>_<keyword>.txt` in **the user's current project root** and tell the user the path; **never** write it back into the mailattic repo (cross-project use keeps the source repo read-only)
- The reply body file (`reply_<topic>.txt`) likewise goes in the user's current project root
- Do not write found mail into MEMORY - the canonical mail lives in the DB; a pointer is enough

---

## Why this skill exists (value beyond calling bes directly)

The bes CLI already covers most mechanical steps: HTML cleaning (`bes show --format text`), thread reconstruction (`bes thread`), IMAP sync (`bes sync-imap`), and the reply pipeline (`bes draft-reply` / `list-drafts [--json]` / `send-draft`). A cross-project subagent still does not know:

- **When to sync and when not to** (Capability A)
- **When to use five stages and when browse mode** (Capability B Stage 0.5 escape hatch)
- **How to split mixed customers + extract prices** (Capability B Stage 3.5)
- **The unanswered / negative-evidence reporting discipline** (Capability B Stage 4 report format)
- **The reply flow and its prerequisites** (Capability C: mandatory review + draft fingerprint, `!` is not a TTY, SMTP / BCC-self / sent-folder visibility, the `--body-from` file convention)
- **The capabilities that do not exist** (compose-from-scratch, bulk send, semantic search)

The skill writes these **judgment rules + failure modes + boundaries** into reference files + bundled scripts so each run need not rediscover bes's bottlenecks. What the CLI already does is delegated purely, not re-implemented.
