# Coding Standards — mailattic

Checklist for the **Standards axis of code review** (`/mattpocock-skills:code-review` reads this file). Every rule comes from a defect that actually happened in this repo, with its source cited.

**Mechanical rules live in `ruff.toml` and `.pre-commit-config.yaml` (plus `pyright` per `pyproject.toml`) and are not repeated here.** Behaviour contracts #1–#7 in `AGENTS.md` `## Behaviour contracts` are guarded by their anchor tests and the PR + `/spec-reconcile` gate; this file does not supersede that gate.

**How to apply**: walk all sections, not only the rules that jump out. On a hit, cite the rule ID and the offending line.

The shape shared by most rules below: **the reported result and what actually landed drift apart, and nothing raises.**

---

## A. Exceptions and failure visibility

### A1 — Catch exactly the failure the handler translates; discriminate by error code when one type covers several causes

A handler that relabels an error into an operator instruction ("start Outlook") must catch only that failure. `except Exception` relabelled adapter bugs (`AttributeError` / `TypeError`) as "Outlook not running"; catching every `com_error` still sent the operator to start Outlook when it wasn't even installed. Let everything else propagate unchanged.

_Source: `91d8163`, `4793929` (`outlook_pst.py` — only `com_error` with `hresult == MK_E_UNAVAILABLE` is translated)._

### A2 — One bad record fails that record, not the whole source

When a library raises something outside the existing `except` tuple, map it into the existing per-record failure path (`ingest_failures`) instead of letting it roll back a whole PST or folder. Keep the record findable in `ingest_failures`; silently counting it as unresolved is not a fix.

_Source: `a1a712d` — `HeaderParseError` is not a `ValueError` subclass, escaped both `except` clauses and aborted an entire PST._

### A3 — Counts and status lines must describe what actually committed

Count `inserted` only after the enclosing commit succeeds; carry completed counts through a mid-run exception; print `PARTIAL` when some folders ingested, not `SKIPPED`. Send source-level failures an operator must act on to stderr at the point of failure.

_Source: `1e0ae06` (#194 — committed messages vanished from the count on a mid-folder FETCH failure), `d582af5` (partial success reported as SKIPPED), `326a994` (source-level guard failure invisible to the operator)._

### A4 — When a parser reports defects, check whether it silently dropped data

`email.policy.default` keeps only the first address of a bare-semicolon recipient list and records a defect instead of raising. New header-parsing paths must inspect `defects` (or re-parse) instead of trusting the parsed value.

_Source: `bf35c6e` (issue #15 — domain search missed every recipient but the first)._

---

## B. Time

### B1 — Fix a wrong timezone label by relabelling, not by "attach tz if naive"

Outlook COM returns host wall-clock time already tagged UTC. A "naive → add tzinfo" helper is a no-op on an aware value and the tests stay green. Route COM date reads through `_com_wall_clock_to_aware` (rebuilds from components, tags `Asia/Taipei`).

_Source: `dec0c25`, `docs/decisions/D-20260907-1-com-timestamps-are-relabelled-at-the-read-site.md` (8-hour offset on every matched pair)._

### B2 — Order and display timestamps by absolute instant

`sent_at` keeps the source offset, so string comparison and direct formatting are both wrong across timezones. Sort and compare in SQL with `unixepoch()`; convert to Taipei before rendering to a human (e.g. quoted-reply headers).

_Source: `1e0ae06` (#193 — string sort picked an older message as "latest"), `499fc7e` (#198 — quoted original showed the wrong time)._

---

## C. Persistence and derived data

### C1 — Write files via sibling temp file + atomic replace; "file exists" is not "write complete"

A non-atomic CAS write left a truncated file at the final path; the retry saw it existed, skipped, and returned the hash of the full payload. Treat an existing file of the wrong size or content as unfinished.

_Source: `1e0ae06` (#190, `attachment_store.py`)._

### C2 — The incremental path and the rebuild path of a derived value share one implementation

`thread_key` is a derivable cache; the incremental ingestion path used different reference tokenisation and truncated candidate scans, so thread grouping depended on import order. Route any change to one path through the helper the other uses.

_Source: `1e0ae06` (#191), `30e574a`, `05449c8`._

### C3 — Range fetches keep only items strictly past the watermark

IMAP `UID N:*` may legally return the last already-synced UID when nothing is new; unfiltered, every idle sync refetched it and the watermark could move backwards.

_Source: `1e0ae06` (#192, `imap_native.py`)._

### C4 — Normalise both sides through the ingest path's own normaliser before diffing a recomputed value against the DB

Recomputing every subject and comparing with the stored value showed 361 differing rows, 340 of them only leading/trailing whitespace, and a parser-bug ticket was opened on it. The ingest path (`_coerce_text`) already strips, so the DB held 0 such subjects. The real gap was on the other side: the recompute function returned an un-normalised value and a backfill script wrote it straight in, which would put a shape ingest never produces into the DB. Find out what ingest does to the field (strip, casefold, NFC, ...) and run both sides of the diff through that one function; before filing a "parser bug", run one SQL query asking whether the DB really contains that shape. Not applicable when both sides come out of the same function.

_Source: `[mailattic-dev 2026-09]`._

### C5 — When fixing a read-side parse (unescape, literal decoding), check the write-side is its inverse and trace who uses the parsed value as a key

The parsed result is often sent back to a server (a name), or used as a state key or directory name. Fixing only the parse yields a correctly decoded value that can no longer be selected, and existing data whose key changes after the restore gets rescanned from scratch. Ask the implementer to trace the consumers up front in the dispatch; that catches it earlier than review.

_Source: `[mailattic-dev 2026-10]`._

---

## D. Paths and host independence

### D1 — Default paths are absolute and independent of the caller's cwd

Relative defaults resolved against the calling project's cwd and broke cross-project `bes sync-imap`. The absolute form is deliberate (a globally installed wheel cannot resolve back to the repo). Keep test oracles for these constants literal, not derived from `__file__`, or a regression to relative paths passes.

_Source: `f34c3ee`, `6ecfe2d` (#89)._

### D2 — Bytes put on the wire must not depend on the sending machine

`mimetypes.guess_type` reads the Windows registry, so the same draft got a different attachment `Content-Type` depending on installed software. Use the repo-owned table (`resolve_attachment_mime_type`); add no machine-local override hook.

_Source: `06c5ec9` (#65)._

### D3 — Anything copied out of the repo is bounded by the git-tracked file list

A directory `copytree` shipped `__pycache__`, `*.pyc` and `.env` to the SSOT. Enumerate with `git ls-files` (`tracked_skill_files`); on git failure or an empty list, skip conservatively.

_Source: `0c306b0` (`scripts/deploy-skill-to-ssot.py`)._

---

## E. Tests

### E1 — Scenario tests drive the production entry point, not a replica of its flow

A helper that re-implemented the gate sequence stayed green with the production gate removed. Monkeypatch the seam and call the real `main()`.

_Source: `dd389b3` (#172 review)._

### E2 — Assert against ground truth, not pass-through of the current value

A test asserting `sent_at` passed through verbatim enshrined the 8-hour bug. Regression tests assert the independently known value (the UTC instant) and must go red when the fix is removed.

_Source: `dec0c25`._

### E3 — Budget/threshold tests use a paired comparison

"Still within budget" alone stays green when a removed field comes back. Assert both that restoring the removed fields breaks the budget and that the current shape stays within it.

_Source: `878d897` (#178, `tests/test_mcp_search_attachment_budget.py`)._

### E4 — A paired/ratio guard can compare one payload with itself; print the ratio once and prove the fixture reaches the guarded path

A ratio test written to keep a relaxed snippet within the delivery budget (keyword-path payload <= 1.25x the no-keyword payload) was green; the measured ratio was **0.98**. The fixture put the keyword at the start of the body, so the hit window (133 chars) was shorter than the old 160 cap and both sides were the same payload: pass rate 100%, discrimination 0. After writing a ratio assertion: (1) print the value once; near 1.0 means the two sides do not differ; (2) add an assertion that the fixture really takes the guarded path (at least one row exceeds the old cap); (3) set the bound from the measurement (measured 1.08 -> bound 1.15) — the margin is for fixture drift, not for next round's growth. Not applicable when the two sides come from different code paths that are certain to run.

_Source: `[mailattic-dev 2026-08]`._

### E5 — De-identification is accepted by "an industry reader cannot tell who this is", not by "0 hits on the blocklist"; renaming does not hide business shape

Renamed test data still carried the product type, document kind, shipping lane, unit price and customer country, so a blocklist scan found nothing while a human recognised the industry. (1) Build test data from scratch as a fictional scenario in an unrelated industry; do not "rewrite a real email". (2) When the real source is reachable, do a two-way reverse lookup (real names / domains / attachment names / codes searched in the public tree, and public-tree codes searched in the real data); any hit is a leak. (3) Change only values that identify a person or company; keep operational values (constants, default filenames, number ranges, spot-check keywords) and prove behaviour is unchanged with "AST identical after stripping comments/docstrings". (4) Leave no seam wording after cleaning. Not applicable to pure public-data projects. See also D-20261018-2 for the value-level criteria and accepted residuals.

_Source: `[mailattic-dev 2026-10]`._

---

## Maintenance

- **Add only shapes that have already bitten this repo.** Cite the source (commit / issue / ADR) with every new rule so the next reader can judge whether it still applies.
- Delete a rule when the code it guards is gone.
- When a linter, type check or structural test starts enforcing a rule, move it there and delete it here (the MCP error-message construction points are already enforced by the AST test in `tests/test_mcp_server.py`, so they are not listed).
