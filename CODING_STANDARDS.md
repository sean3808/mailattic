# Coding Standards — mailattic

Checklist for the **Standards axis of code review** (`/mattpocock-skills:code-review` reads this file). Every rule comes from a defect that actually happened in this repo, with its source cited.

**Mechanical rules live in `ruff.toml` and `.pre-commit-config.yaml` (plus `pyright` per `pyproject.toml`) and are not repeated here.** Behaviour contracts #1–#6 in `AGENTS.md` `## 行為契約` are guarded by their anchor tests and the PR + `/spec-reconcile` gate; this file does not supersede that gate.

**How to apply**: walk all sections, not only the rules that jump out. On a hit, cite the rule ID and the offending line.

The shape shared by most rules below: **the reported result and what actually landed drift apart, and nothing raises.**

---

## A. Exceptions and failure visibility

### A1 — Catch exactly the failure the handler translates; discriminate by error code when one type covers several causes

A handler that relabels an error into an operator instruction ("start Outlook") must catch only that failure. `except Exception` relabelled adapter bugs (`AttributeError` / `TypeError`) as "Outlook not running"; catching every `com_error` still sent the operator to start Outlook when it wasn't even installed. Anything else must propagate unchanged.

_Source: `91d8163`, `4793929` (`outlook_pst.py` — only `com_error` with `hresult == MK_E_UNAVAILABLE` is translated)._

### A2 — One bad record fails that record, not the whole source

When a library raises something outside the existing `except` tuple, map it into the existing per-record failure path (`ingest_failures`), rather than letting it roll back a whole PST or folder. Do not "fix" it by silently counting the record as unresolved either — it must stay findable in `ingest_failures`.

_Source: `a1a712d` — `HeaderParseError` is not a `ValueError` subclass, escaped both `except` clauses and aborted an entire PST._

### A3 — Counts and status lines must describe what actually committed

Count `inserted` only after the enclosing commit succeeds; carry completed counts through a mid-run exception; print `PARTIAL` when some folders ingested, not `SKIPPED`. Source-level failures an operator must act on go to stderr at the point of failure.

_Source: `1e0ae06` (#194 — committed messages vanished from the count on a mid-folder FETCH failure), `d582af5` (partial success reported as SKIPPED), `326a994` (source-level guard failure invisible to the operator)._

### A4 — When a parser reports defects, check whether it silently dropped data

`email.policy.default` keeps only the first address of a bare-semicolon recipient list and records a defect instead of raising. New header-parsing paths must inspect `defects` (or re-parse) rather than trust the parsed value.

_Source: `bf35c6e` (issue #15 — domain search missed every recipient but the first)._

---

## B. Time

### B1 — Fix a wrong timezone label by relabelling, not by "attach tz if naive"

Outlook COM returns host wall-clock time already tagged UTC. A "naive → add tzinfo" helper is a no-op on an aware value and the tests stay green. COM date reads go through `_com_wall_clock_to_aware` (rebuilds from components, tags `Asia/Taipei`).

_Source: `dec0c25`, `docs/decisions/D-20260907-1-com-timestamps-are-relabelled-at-the-read-site.md` (8-hour offset on every matched pair)._

### B2 — Order and display timestamps by absolute instant

`sent_at` keeps the source offset, so string comparison and direct formatting are both wrong across timezones. Sort/compare in SQL with `unixepoch()`; convert to Taipei before rendering to a human (e.g. quoted-reply headers).

_Source: `1e0ae06` (#193 — string sort picked an older message as "latest"), `499fc7e` (#198 — quoted original showed the wrong time)._

---

## C. Persistence and derived data

### C1 — Write files via sibling temp file + atomic replace; "file exists" is not "write complete"

A non-atomic CAS write left a truncated file at the final path; the retry saw it existed, skipped, and returned the hash of the full payload. Treat a wrong-size or wrong-content existing file as unfinished.

_Source: `1e0ae06` (#190, `attachment_store.py`)._

### C2 — The incremental path and the rebuild path of a derived value share one implementation

`thread_key` is a derivable cache; the incremental ingestion path used different reference tokenisation and truncated candidate scans, so thread grouping depended on import order. Any change to one path must go through the helper the other uses.

_Source: `1e0ae06` (#191), `30e574a`, `05449c8`._

### C3 — Range fetches keep only items strictly past the watermark

IMAP `UID N:*` may legally return the last already-synced UID when nothing is new; unfiltered, every idle sync refetched it and the watermark could move backwards.

_Source: `1e0ae06` (#192, `imap_native.py`)._

---

## D. Paths and host independence

### D1 — Default paths are absolute and independent of the caller's cwd

Relative defaults resolved against the calling project's cwd and broke cross-project `bes sync-imap`. The absolute form is deliberate (a globally installed wheel cannot resolve back to the repo); test oracles for these constants stay literal, not derived from `__file__`, or a regression to relative paths passes.

_Source: `f34c3ee`, `6ecfe2d` (#89)._

### D2 — Bytes put on the wire must not depend on the sending machine

`mimetypes.guess_type` reads the Windows registry, so the same draft got a different attachment `Content-Type` depending on installed software. Use the repo-owned table (`resolve_attachment_mime_type`); no machine-local override hook.

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

"Still within budget" alone stays green when a removed field comes back. Assert that restoring the removed fields breaks the budget, and that the current shape does not.

_Source: `878d897` (#178, `tests/test_mcp_search_attachment_budget.py`)._

---

## Maintenance

- **Only shapes that have already bitten this repo.** Cite the source (commit / issue / ADR) with every new rule so the next reader can judge whether it still applies.
- Delete a rule when the code it guards is gone.
- When a linter, type check or structural test starts enforcing a rule, move it there and delete it here (the MCP error-message construction points are already enforced by the AST test in `tests/test_mcp_server.py`, so they are not listed).
