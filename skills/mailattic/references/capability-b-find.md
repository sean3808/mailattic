# Capability B — Find (locate mail + extract prices + ball-in-court)

> `SKILL.md`'s Capability routing table loads this file as MANDATORY.
> A Find-only task does **not** load `capability-c-reply.md`.
> Stage 0 loads `capability-a-sync.md` when it triggers a sync; load it in no other case.

> **`{SKILL_DIR}` convention**: in every `{SKILL_DIR}/scripts/...` below, `{SKILL_DIR}` = the "Base directory for this skill" value the harness shows when this skill loads (on one machine `C:\Users\example\.claude\skills\mailattic`; it differs per machine). Substitute that actual path before running; never hardcode a username-specific absolute path.

## When to use

A user request that pairs a vague memory with a concrete business goal is this capability:
- "that email from client X", "I remember Y discussed Z last year", "find the past emails about W"
- "have I quoted client X for Y", "past quotes", "how did we reply last time"
- "did we get anything from X today" (trigger Capability A `--pre-task` first to pull fresh mail)

## Mental model

The user does not give a clean query; they give a vague memory plus a concrete business goal. Your job has two layers:
1. Narrow the vague memory, step by step, to concrete mail `db_id`s.
2. When the user wants structured information (historical quotes / commitments), extract it from the original mail and report it, including whether the other side has not replied (ball-in-court status).

The flow has five stages: coarse filter, user focus, detailed lookup, customer split + price extraction, structured original-mail report. Do not try to land it in one shot; show the user each stage's result before moving on.

**The five stages are the heavy machinery built for vague memories. Check the fast-path gate below first**: a strong-anchor query converges in one or two commands, so skip the five stages for it.

---

## Fast-path: strong-anchor direct hit (decide first; on a hit, skip the five stages)

Before entering the five stages, ask: **did the user give a concrete anchor code plus the counterparty's domain?** (product code / part number / order number, plus a supplier or customer domain)

If yes, converge in two steps and **skip Stages 1-3.5**:

```bash
# 1. Strong anchor, one shot (domain x keyword narrows to very few threads)
bes search --participants-domain supplier.example --keyword "PRODUCT-A" --limit 50 --json

# 2. Hits converge to a single thread -> read the full text and extract by hand
bes thread <db_id> --format text
```

- When hits converge to a **single thread**, **read the full thread text and extract prices by hand**; this **skips the Stage 3.5 customer split**.
- When hits still span several customers / threads (> 5 mails across several domains), fall back to the five stages and do the Stage 3.5 customer split.
- **A fast-path `bes search` with 0 hits never justifies "no such mail"**: the fast-path skips Stage 0 and the 0-hit verification, but a **new** or **user-sent** target mail may not be synced into the DB yet. On 0 hits, **fall back to "0-hit sync verification" under Failure modes below**: for freshness / new mail run `bes sync-imap --pre-task` (sent mail is synced from `寄件備份`); after exit 0 with a successful last sync, search again, and only a repeated 0 is a verified true negative. **If the last sync failed, 0 hits is never a true negative** (see Failure modes). Never state "not found" straight from the fast-path.

With no concrete anchor (only a vague memory such as "that email from X" or "we discussed Z last year"), expand the five stages below.

---

## Stage 0: Sync prelude (run only for freshness-sensitive queries)

If the user's query contains one of "today / yesterday / 24h / recent / just now / latest" (今天 / 昨天 / 24h / 最近 / 剛剛 / 最新), load `capability-a-sync.md` and run:

```bash
bes sync-imap --pre-task
```

Enter Stage 1 only after confirming exit=0. A throttle skip also counts as OK (a recent sync already happened). On sync failure (exit != 0), stop and report to the user; searching a stale DB gives wrong conclusions.

**Finding outbound mail (mail the user sent)**: `sync-imap --pre-task` has synced the IMAP `寄件備份` folder since 2026-07-14, so no separate track is needed.

For historical lookups (not freshness-sensitive), skip Stage 0 and go straight to Stage 1.

## Stage 0.5: Browse escape hatch (when there is no MUST term)

When the user only wants "list the latest 20", "list today's mail", or "list last week's mail from some domain" (a need with **no vague memory to make concrete**), skip the five stages and run directly:

```bash
bes search --from-date 2026-05-29 --to-date 2026-05-31 --limit 20 --json
bes search --participants-domain client.com --from-date 2026-05-01 --limit 50 --json
```

List the results for the user and the task is done. The five stages exist to turn a vague memory into a concrete db_id; with no vague memory they are unnecessary.

## Aggregate mode: contact-history (multi-year correspondence summary, not per-mail)

When the user wants the whole picture of correspondence with a person / company over the years, not one mail (laying it out before taking a job / replying / negotiating, to avoid inconsistent quotes and duplicate commitments), use the aggregate subcommand. Do **not** run the five stages to pull mails one by one:

```bash
bes contact-history --contact alice@client.example --format json
bes contact-history --domain client.com --date-from 2024-01-01 --date-to 2025-12-31 --format json
```

- `--contact <email>` and `--domain <domain>` are **mutually exclusive, exactly one required**. Dates use `--date-from` / `--date-to` (YYYY-MM-DD; the names differ from `bes search`'s `--from-date`).
- Returns one summary object: time span (first / last contact), total mail count, inbound/outbound ratio, every contact person at the domain (email + display_name + per-person frequency / last contact), attachment overview.
- `--format` defaults to `table` (compact TOON format); for agent consumption use `--format json`. `--attachments-limit N` (default 20) controls the attachment-overview row count.
- Direction (inbound / outbound) comes from the self-address set; contacts exclude bcc and self.
- Count fields: `total_messages = inbound_count + outbound_count + unknown_count`. `unknown_count` counts mails whose sender is missing or blank so direction cannot be determined; they are not in `inbound_count` and yield no sender contact. The `mail_contact_history` (MCP) output carries the same field.
- When to use: for "how long have I corresponded with X / who is in touch with me / inbound-outbound ratio" (a **relationship-overview** question), use contact-history. To find "a specific mail / a specific quote", go back to the five-stage Find. Common chain: contact-history for the overview, then pin down the period / contact person, then drill to a concrete db_id with the five stages.

---

## Five-stage flow (making a vague memory concrete)

Extract three kinds of terms from the user's wording, then converge stage by stage; show the user each stage's result before deciding the next step.

### Stage 1: Capture intent, split into keywords

- **MUST terms**: must appear (customer name, person name, order number, product code; usually 1-2).
- **CONTEXT terms**: topic word group (lead time / spec / price / quote / unit price; usually 3-5, OR-ed).
- **Time range**: when the user says "early 2025", "April-May", "last year", note it; narrow only if the first round finds nothing.

There is no mandatory clarification gate. When the keywords are specific enough (product code + customer / person name + topic term), run directly; ask the user back only when they are truly insufficient (a single generic term such as "quote", or a lone customer name with no topic). Prefer running a coarse filter first and showing the user the hit distribution before deciding whether to narrow.

### Stage 2: FTS coarse filter

```bash
bes search \
  --keyword "<MUST term>" \
  --from-date 2025-04-01 --to-date 2025-07-01 \
  --limit 50 --json > .tmp_search.json
```

The flags are `--from-date` / `--to-date` (**not** `--date-from/--date-to`). Other common flags: `--sender` / `--recipient` / `--sender-domain` / `--recipient-domain` / `--participants-domain` / `--attachment-name` / `--limit`. To target a different DB, override the env var with `--database <path>`.

**Direction**: every record in `bes search --json` and `bes thread --format json` carries a `direction` field (value `outbound` / `inbound` / `null`). It is decided by whether the sender belongs to the self-address set declared with `bes identity`; `null` means the sender is missing (no guessing). **Do not compare sender domains yourself to decide direction**: colleagues and other people at the same domain would be misjudged as ours. Read this field. MCP `mail_search` / `mail_thread` responses have the same shape. There is currently no server-side `--direction inbound|outbound` filter; to filter, do it on the returned JSON.

**What a date bound means**: a plain date (`2026-07-01`) = **the whole day in Asia/Taipei, both ends inclusive**, for the lower and the upper bound alike. `bes search` / `bes attachments` also accept a full timestamp: with an offset (`2026-07-01T12:00:00+08:00`) it selects that instant and is not widened to a whole day; **a timestamp without an offset is read as Taipei time**. `bes contact-history`'s `--date-from` / `--date-to` accept dates only (non-zero-padded forms such as `2026-7-1` are accepted too), not timestamps. A malformed date is an error (it names the bad value and the parameter), never silently zero results. Canonical rule: `docs/decisions/D-20260723-1-taipei-day-date-filter-semantics.md`.

`--keyword` supports a controlled FTS5 boolean syntax: uppercase `AND` / `OR` / `NOT`, parentheses, double-quoted phrases. Operators are case-sensitive; lowercase `and` / `or` / `not` are literal terms. Quote part numbers containing a hyphen as a phrase (example: `'"PEN-100"'`). `*` prefix, column filters, and `NEAR` are not supported.

**Ordering and hit evidence when `--keyword` is given** (same shape in MCP `mail_search`):

- **Ordering is relevance (bm25), not newest-first.** Without `--keyword` it falls back to time order. This changes what `--limit` means: in a long reply chain every mail contains the same keyword, so under time order **the earliest mail (usually the original quote / original source) is always cut by the limit**; relevance ordering uses bm25 length normalization to lift the short original up. For "the newest mail", give no keyword, or re-sort by `sent_at` yourself after searching.
- **`match_in`** is one of `new` / `quoted` / `null`: whether **the hit shown in the snippet** falls in this mail's new content or in quoted history. To find the original source, filter to `new`. It says where "the evidence you are looking at" sits, not where "each query term" appears; the latter would mislabel an original quote mail (new content holds the full quote, the quoted region merely mentions the product name) as matching in both places. `null` means the hit is not in the body (next item).
- **`match_field`** is one of `body` / `subject` / `null`: `null` means the literal term is in neither subject nor body, so the mail matched through its **recipients or attachment file names** (`message_fts` indexes both). Do not treat `null` as a bug; it honestly reports "the hit is not in text you can see". Literal matching is **whole-token** (consistent with the index), so querying `art` does not count the `art` inside `partial` as a body hit; CJK terms follow unigram indexing semantics, where a substring hit counts as a hit.
- **`snippet` is centered on the hit** (+-120 characters, capped at 300); for a multi-term query it picks the stretch with the **most terms co-occurring**, not the earliest term. Without a keyword it still takes the start of the body (capped at 160). So the snippet from `bes search --keyword` can be used directly as evidence; no need for `bes show` just to see the keyword's context.

**The three domain flags are repeatable (multi-value, OR between values)**: to query several domains at once (for example a customer group with several domains), repeat the flag; hits are the union of the domains:

```bash
# sender belongs to a.com or b.com
bes search --sender-domain a.com --sender-domain b.com --limit 50 --json
```

`--recipient-domain` / `--participants-domain` repeat the same way. The Python API takes `str | list[str]` (`SearchFilters(sender_domain=["a.com","b.com"])`). **Single-value use is 100% backward compatible**: passing the flag once, or a single `str`, behaves exactly as before. Values of the same flag are OR-ed; different flags stay AND-ed.

Read `.tmp_search.json` for hits. If they are all topic-duplicate order threads, jump to Stage 3; do not keep tuning keywords at the bes layer (it gets worse).

#### Calling `bes search` from a subprocess: correct vs incorrect

When the caller is another Python script that must call `bes search` as a subprocess, one common mistake is fatal.

**Incorrect call (forbidden)**

```python
import subprocess

# Binds bes to the mailattic dev .venv — when the dev venv breaks, downstream crashes
subprocess.run(["uv", "run", "bes", "search", "--keyword", "..."], cwd="<mailattic repo>")
```

This is the `uv-run-with-cwd` anti-pattern: with a given cwd, `uv run` automatically finds the nearest `pyproject.toml` and binds the run to mailattic's development `.venv`. Once the dev venv is damaged (for example a broken symlink from mixing Windows and WSL), downstream queries immediately crash with exit 2. The full four rules are in SKILL.md fatal pitfall #9.

**Correct call (portable, fail-loud)**

```python
import os
import subprocess
from pathlib import Path

# 1. Get the real bin dir (UV_TOOL_BIN_DIR / XDG can override it, so never hardcode the path)
#    check_output raises FileNotFoundError when uv is absent and CalledProcessError on non-zero exit;
#    swallow neither exception
bin_dir = subprocess.check_output(["uv", "tool", "dir", "--bin"], text=True).strip()
if not bin_dir:
    raise RuntimeError("uv tool dir --bin returned empty — an empty string must not degrade into the relative path bes.exe/bes")

# 2. Build the absolute path of the bes executable (with .exe on Windows)
#    subprocess with shell=False does not expand tilde; a literal "~/.local/bin/bes" fails with file-not-found
bes = str(Path(bin_dir) / ("bes.exe" if os.name == "nt" else "bes"))

# Local default, illustration only (shows what the path looks like; not an identical path across machines;
# never use it as a fallback when uv tool dir --bin fails):
# bes = str(Path.home() / ".local" / "bin" / ("bes.exe" if os.name == "nt" else "bes"))
# i.e. C:\Users\example\.local\bin\bes.exe (a local Windows machine)

# 3. Remove the inherited VIRTUAL_ENV so the caller's environment cannot interfere with the global tool
env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

# 4. Call the global bes, without a repo cwd
result = subprocess.run(
    [bes, "search", "--keyword", "範例客戶", "--limit", "50", "--json"],
    env=env,
    capture_output=True,
    text=True,
    check=True,  # raises CalledProcessError on non-zero exit
)
```

(`範例客戶` above is a placeholder Chinese customer name; it stays Chinese because the mail being searched is Chinese.)

**Key points**:

- The directory `uv tool dir --bin` returns can be overridden by the `UV_TOOL_BIN_DIR` / XDG environment variables and is not a fixed `~/.local/bin`, so fetch it dynamically.
- An empty `bin_dir` must not degrade into the relative path `bes.exe` or `bes`: that falls back to the caller's cwd/PATH shadow, which is exactly what the contract blocks.
- Catch neither `FileNotFoundError` (uv missing) nor `CalledProcessError` (non-zero exit) from `check_output` and swallow it; let the caller learn the environment is abnormal.

**Note**: other helper-script calls on this page such as `uv run python {SKILL_DIR}/scripts/*.py` are **not** covered by the ban above. The ban targets the `bes` CLI itself; using `uv run` to execute other scripts is allowed.

### Stage 3: FTS + LIKE cross-filter (the main tool)

Call the bundled script, which avoids the cross-shell escape pitfalls of inline Python:

```bash
uv run {SKILL_DIR}/scripts/find-cross-filter.py \
  --must "範例客戶" \
  --context "交期,規格,價格,報價,單價,數量" \
  --from-date 2025-04-01 \
  --to-date 2025-06-30
```

(`--context` above = lead time, spec, price, quote, unit price, quantity; these are the Chinese words the mail contains, so they stay Chinese.)

Script behavior: for every mail in the date window, check "MUST appears in body OR subject" and "ANY CONTEXT appears in body"; for each hit, print a 200-character snippet containing the matched keyword. `--snippet-keywords` can change the anchor terms (default: the context list); anchor matching is case-insensitive. A SQL hit with no anchor found is still printed (the snippet takes the start of the body and is tagged `[anchor not found; body start]`, with a count on stderr); it does not disappear.

`--from-date` / `--to-date` have the same semantics as Stage 2: `YYYY-MM-DD`, Asia/Taipei whole day, **both ends inclusive** (the same day may be given at both ends); a malformed value exits 2 and names the parameter and the bad value.

Show the snippets to the user; do not pick the mail yourself, because the user will point straight at it ("that one, #21853"). Narrow candidates to < 20 mails so the user can scan them.

**Thread-context shortcut**: when the user wants "the conversation around that mail", skip the cross-filter and run directly:

```bash
bes thread <db_id> --format text
```

This returns every mail in the thread at once (rebuilt from `in_reply_to` + `references_header`, ordered by `sent_at` ASC). A hard cap of 100 mails guards against the degenerate case.

### Stage 3.5: Customer split + quote extraction

Run it when:
- Stage 3 hits > 5 mails **and** span several customer domains (example: both `@customer-a.example` and `@customer-b.example` appear).
- The user's question involves structured information such as "quote / commitment / price".
- The user asks "have I quoted customer X for Y" (run it even if only one customer matches, because the quote must be extracted).

Method: for each candidate customer domain, rerun the Stage 3 cross-filter script (same `--must` / `--context` / date window) with an added `--participants-domain <domain>`, giving hits grouped by customer; then read each group's full thread text (`bes thread <db_id> --format text`) and extract prices by hand:

```bash
uv run {SKILL_DIR}/scripts/find-cross-filter.py \
  --must "範例客戶" \
  --context "交期,規格,價格,報價,單價,數量" \
  --from-date 2025-04-01 \
  --to-date 2025-06-30 \
  --participants-domain customer-a.example
```

`--participants-domain` means the same as in `bes search`: the address domain of the sender or any recipient (including cc / bcc) **exactly equals** the value (case-insensitive; subdomains and look-alike domains such as `notcustomer-a.example` do not count); repeatable, OR between values; the value is matched literally, and `_` / `%` are not wildcards. Run once per customer domain, swapping the last line.

Cautions for extracting prices by hand:
- An inline "we quote 250" with no unit or currency is easy to miss.
- Amounts inside invoice numbers and bare numbers inside specs are not quotes.
- Always attach the original snippet and its db_id to the report.

Report format:

```
Hits grouped by customer:
- domain A (X mails): quotes listed where present
- domain B (Y mails): no quote
Which customer do you want to pin down? (Which side to exclude?)
```

Enter Stage 4 after the user decides.

### Stage 4: Extract the original mail + Ball-in-court + Negative Evidence

Once the user gives a db_id, **call the global CLI directly; do not read the DB yourself**:

```bash
# Single mail: metadata header + one separator line + clean plain-text body
bes show <db_id> --format text

# Whole thread (recommended):
bes thread <db_id> --format text
```

`--format text` does the HTML cleaning through `query.render_text()`. Multiple mails are separated by `--- (N of M, msg-id=...) ---`.

## Attachment-first search / export (`bes attachments`)

When the user's memory anchor is "that PDF / attachment file name / language version" rather than the mail subject, go straight to the attachment surface; do not search message-first and then page through attachments by hand:

```bash
bes attachments \
  --name-contains TAG \
  --participants-domain customer-a.example \
  --ext pdf \
  --from-date 2025-04-01 \
  --to-date 2025-04-30 \
  --json
```

It returns **one row per attachment** (not one per mail); each row has `attachment_id`, `message_db_id`, `rfc_message_id`, `dedup_key`, `sent_at`, `sender_email`, `subject`, `attachment_name`, `content_hash`, `storage_path`, `folder_path`, `exportable`, `carrying_messages`. To cite the source mail externally, use `rfc_message_id` / `dedup_key`; **never** use the integer `message_db_id` as a stable bookmark.

To copy the matching payloads out, add `--copy-to`:

```bash
bes attachments \
  --name-contains TAG \
  --participants-domain customer-a.example \
  --ext pdf \
  --copy-to C:\Exports\customer-a-tag
```

Semantics and failure modes:

- `exportable=false` only means the DB has no `storage_path` pointer; it does **not** mean the payload is certainly readable. When `storage_path` is non-empty but the CAS file is missing / corrupt / path-escaping, export lists it as `failed`.
- Metadata-only attachments (common from archive-scrape) still appear in the listing; with `--copy-to` each is reported row by row as `skipped-non-exportable`, **never silently dropped**.
- `--dedupe content-hash` merges only **non-null** `content_hash`; null-hash rows are all kept. Winner = latest `sent_at`, then highest `message_db_id`, then lowest `attachment_id`; every source mail carrying the same file stays in `carrying_messages`.
- By default export **does not overwrite** an existing file name; `--overwrite` overwrites, and `--skip-existing` counts into `skipped-existing`.
- `--overwrite` and `--skip-existing` are mutually exclusive; giving both **exits non-zero before any copy**.
- Exit code after export: **only** `failed > 0` exits non-zero; `copied` / `skipped-non-exportable` / `skipped-existing` alone never fail the run.

Known limits:

- `--participants-domain` still inherits the message query's `recipient_search_text LIKE '%@domain%'` semantics, so existing semicolon-packed outbound recipients match; this is deliberately inherited behavior, not a parser already fixed.
- `--ext` goes mainly by the `attachment_name` suffix, falling back to `mime_type` when the name has no suffix; CAS `storage_path` is a hash-only physical location and carries no extension semantics. Old data that lost the extension signal in both name and MIME can still miss.
- The `--copy-to` fallback output file name likewise looks only at `attachment_name` / `mime_type`; when neither yields an extension it uses `.bin`, and never guesses from the CAS path.
- A mail forwarded as an attachment (`.eml`, `message/rfc822`) is **one attachment row**, and the whole archived mail is exportable (named with the attached mail's subject plus `.eml` when there is no file name). Its body does not count as the outer mail's body (`bes search --keyword` does not hit the outer mail through words inside the attached mail), and the attachments it carries are not listed separately. To see the content, export with `--copy-to` and read the `.eml`. For a bounce notice (`multipart/report`), the original mail it carries stays merged into the outer body and search and is not made a separate attachment; this is deliberate so a bounce can be searched by the original mail's content.

**For long threads, extract only new content**: add `--new-only` to `bes show/thread --format text` to keep only each mail's new content (quoted history removed; supports top-posted replies, bottom-posted replies, and replies interleaved between quotes; boundaries detected: `>` quoting, Outlook header blocks including the self-built CJK `寄件者:`/`收件者:`/`主旨:` labels, `-----Original Message-----`, external-mail banners, etc.). When a long thread quotes layer upon layer, size can shrink by an order of magnitude. **Conservative fallback**: when no boundary can be identified it returns the full text without truncating (better to cut too little than to cut real new content). Valid only with `--format text`; combined with `raw`/`json` it exits non-zero.

**Batch retrieval (ingestion-grade, removes the N+1 `bes show`)**:

When the caller is an agent / ingestion pipeline that must fetch the full body and thread linkage of many hits at once (instead of `bes show` one by one), use `bes search`'s opt-in full-body mode:

```bash
bes search --keyword "<MUST term>" --from-date 2025-04-01 --to-date 2025-07-01 \
  --limit 50 --full-body --json
```

- **JSON-only**: `--full-body` without `--json` exits 2 immediately (so full text is not flooded onto a human terminal).
- Each hit additionally returns `rendered_body_text` (the same cleaned body as `bes show --format text`), `message_id`, `dedup_key`, `in_reply_to`, `references`, `to`, `cc`, `bcc`.
- For a stable downstream reference to a message, **prefer `message_id` or `dedup_key`**, not the integer `id` (ids are reshuffled after a rebuild).
- Default `bes search` (without `--full-body`) stays a lightweight summary; behavior unchanged.
- When to use: presenting a single mail / single thread to a **human** -> `bes show` / `bes thread --format text` (with separator-line layout); an agent batch-consuming many mails for post-processing -> `--full-body --json`.

**Mail forensics headers (source IP / Received chain / SPF / DKIM / Authentication-Results)**:

`--format text` / `raw` / `json` give body + metadata only and **do not include the original RFC5322 headers**. To find out "how this mail arrived, which route it took, who the sending source IP is", use the separate header mode:

```bash
bes show <db_id> --headers
```

- Outputs the mail's full RFC5322 header section verbatim; exit 0.
- When the mail has no header data it prints "No raw header data available" and exits 0. **Only backfilled / newly ingested (IMAP incremental) mail has data**; older historical PST mail is NULL until backfilled.
- `--headers` is **mutually exclusive** with `--format` (any value) and with `--json`; combining them exits non-zero with a stderr diagnostic (the two output shapes are incompatible).
- Privacy guard: raw headers can carry internal-infrastructure identifiers (internal IPs, routing hostnames, Authentication-Results). Do not paste the whole section into an external channel unless necessary.

**Ball-in-court detection** (the CLI does not do this; call the bundled script):

```bash
uv run {SKILL_DIR}/scripts/ball-in-court.py \
  --seed-db-id 123

# For a single mail rather than a thread:
uv run {SKILL_DIR}/scripts/ball-in-court.py \
  --seed-db-id 123 --no-thread
```

The script locates the released global BES via `uv tool dir --bin` and finds the last mail through `bes thread <id> --format json`; `--no-thread` uses `bes show <id> --format json` instead. `--database <path>` is optional; when unspecified, BES resolves the environment variable and built-in default, which does not change with cwd. It reads that mail's JSON `direction` field directly (`inbound` = the other side sent it) plus whether the body contains a question or a politeness / chasing word (`?`, `？`, `請`, `kindly`, `please`, `報價`, `quote`, `催`; i.e. please, quote, chase) to decide whether we owe a reply. `direction` comes from the self-address set (see the "Direction" paragraph above) and no longer from domain comparison; the `--our-domains` flag no longer exists.

The "last mail" is chosen by actual time (after cross-timezone offset conversion), not list position. Two **indeterminate** outputs are both different from "no unanswered mail":

- `[indeterminate]` (exit 3), two causes; the message states which:
  - The thread reached the CLI's 100-mail cap (exactly 100 counts too): you got the earliest 100 and cannot see the truly newest mail. BES's truncation warning is forwarded to stderr as is.
  - The thread has more than one mail and any mail's `sent_at` is missing or unparseable: that mail may be the newest, so order cannot be established. The message lists those mails' `db_id`s; `bes show` them one by one and hand them to the user to judge.
- `[unknown-direction]` (exit 0): the last mail has no determinable direction (blank sender); it is not treated as ours.

#### Stage 4 report format (presented to the user) must contain five sections

```
## Facts
db_id / subject / From / To / CC / sent_at / PST source / folder_path
Cleaned body in full (not a summary); multiple mails in time order, marking which is the "final version" (usually the last mail in the thread, sent by us).

## Extracted prices / commitments
List the quotes extracted in Stage 3.5 (original snippet + its db_id).
If the user asked about price but none was extracted: say plainly "the DB has no explicit quote from this customer for this product" (do not gloss over it).

## Ball-in-court
List the last mail where "the other side is waiting for a reply"; give its db_id so the user can follow up.
If none -> write "no unanswered inbound".
If the script returned `[indeterminate]` or `[unknown-direction]` -> write "cannot determine" with the reason; never write it as "no unanswered inbound".

## Excluded candidates (negative evidence)
List every "customer / thread I looked at but excluded" with the reason.
Example: "Excluded 乙商行 (msg 123, 124): same product but a different case, unrelated to this customer".
Without this the user does not know which candidates you already visited, and the same question gets redone next time.

## Caveats
- HTML cleaning is regex-based and may leave a few inline styles
- Quotes are extracted by hand from the original text (easy to miss currency-less wording; do not take invoice numbers for quotes)
- DB cutoff is `SELECT MAX(sent_at) FROM messages`
```

---

## Failure modes

- **Coarse filter, 0 hits**: first check whether the keyword has a hyphenated number without a phrase quote, and whether the time range is too narrow. If it is still 0 after widening the time, **do not assert "no such mail"**; run one sync verification before concluding (see "0-hit sync verification" below). Do not guess.

  #### 0-hit sync verification (turns a false negative into a verified true negative)

  Stage 0's sync trigger is tied to query wording (today / 24h / recent / just now / latest), but the real condition for a stale DB is "the target mail is very new". When the wording looks like a historical lookup but the target is a just-arrived mail, the search hits an unsynced DB and returns a false negative. So when **the five-stage main search (Stage 2 coarse filter / Stage 3 cross-filter) still returns 0 hits after widening**, and this task has not yet run the Stage 0 sync, run one throttle-gated sync first:

  ```bash
  bes sync-imap --pre-task
  ```

  Then branch on the first stdout line plus the exit code:

  | Result | Signal | Action |
  |---|---|---|
  | Actually synced | first stdout line `mbsync: pulled N new messages`, exit 0 | **Search once more**; still 0 -> only then a verified true negative; report "no matching record in the DB" |
  | Skipped, last sync succeeded | first stdout line `sync-imap: skipped` with only a time in parentheses (no `last sync failed` / `did not complete` / `status unknown`; with `--json`, `last_sync_failed: false`), exit 0 (a successful sync within 15 minutes, DB is fresh) | **Do not search again**; 0 hits is a true negative; report "no matching record in the DB" |
  | Skipped, last sync failed or incomplete | stdout contains `last sync failed`, `last sync did not complete`, or `last sync status unknown` (with `--json`, `last_sync_failed: true`), exit 0 | Report "**cannot confirm**: the last sync failed, the DB may be incomplete / stale"; **do not** claim not found |
  | Sync failed | exit != 0, or stdout contains `mbsync: ERROR` | Report "**cannot confirm**: sync failed, the DB may be stale"; **do not** claim not found (avoids the double false negative of a stale DB plus a broken sync) |

  **Scope**: applies to a 0-hit result of the five-stage main search **and to the fast-path's 0 hits above** (the fast-path skips Stage 0, so a 0-hit always falls back to this verification). `contact-history` aggregate mode and Stage 0.5 browse mode do **not** get this fallback (they are not "find one specific mail" semantics; a 0-hit is itself the answer).
- **Coarse filter returns > 200**: usually the MUST term is too generic (for example only "quote"); go back and ask the user for a customer name / product code.
- **Stage 3 cross-filter still misses the key sentence**: the Chinese in the body may differ from the user's wording (for example the user says "交期兩週" but the original says "lead time 2 weeks"); add English synonyms to `--context` and rerun.
- **Ball-in-court false positives**: the trigger words (question marks, 「請」) also match in courtesy phrases, so a ball-in-court flag does not mean certainly unanswered; in the report, let the user judge.

## Wrap-up

- Delete temp files (such as `.tmp_search.json`) when the lookup is done: `rm .tmp_*.json`.
- If the user will keep citing the original mail later, save it to `originals_<topic>_<keyword>.txt` at the **user's current project root** and tell the user the path; **never** write it back into the mailattic repo.
- After finding the mail, ask the user "reply to it?"; yes -> load `capability-c-reply.md` and enter Capability C; no -> wrap up.
