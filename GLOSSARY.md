# Project Vocabulary

> The single canonical vocabulary for mailattic. It lives at the repo root so the matt-family skills (`/domain-modeling`, `/grilling`, `/grill-with-docs`, `/wait-what`) read it natively (formerly `openspec/LANGUAGE.md`, [D-20260820-3](docs/decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)).
>
> Use the canonical term in contract entries, docs, issues/PRs, and test names. When an `Avoid` synonym shows up in discussion, that is vocabulary drift: name it in the conclusion summary and switch back to the canonical term.
>
> Terms are split into two scopes, one H2 each: `domain` (standard mail protocols and external concepts) and `mechanism` (query/storage semantics this platform invented).
>
> Each heading carries the term's Chinese name as an alias, because other docs and conversations cite terms in Chinese (e.g. 「隔離區」, 「明文授權」). Chinese entries in `Avoid` lists are literals to recognize in Chinese conversation; keep them verbatim.

---

## Standard mail protocols and external concepts (scope: domain)

> Add PST, IMAP, Maildir, thread, UIDVALIDITY and similar external concepts the first time their semantics need clarifying.

### Reply-all (回復)

- **Definition**: Produce a reply that continues the same thread as an existing message. The caller picks the anchor message by its stored RFC822 Message-ID (`messages.message_id`, not the numeric row id) and the reply carries `In-Reply-To` + `References`. Recipients default to the original sender / To / Cc (deduplicated, excluding [[self-address]]); the caller may add recipients, exclude derived addresses, or replace the envelope entirely; the final To must not be empty. **Dead mailboxes are filled in as-is unless explicitly excluded**; bounces are left to SMTP.
- **Avoid**: 回覆 (when it means replying to the sender only), reply, reply-to-sender, 回信
- **Why**: The everyday word 「回復」 maps to two Outlook buttons. This platform defaults to "reply all"; a caller holding a human-approved list may adjust the envelope explicitly, and thread continuity is still decided by the anchor message. Owner: Sean (2026-08-11, confirmed the #84 / #85 and PR #102 review boundary); SoT: `bes draft-reply` recipient assembly; source: `openspec/changes/archive/2026-08-12-control-draft-reply-recipients-and-archive-anchors/ddd-note.md` §2–§4.

---

## Mechanisms this platform invented (scope: mechanism)

### launch-isolation preflight

- **Definition**: The probe supervisor's fail-closed static validation, run before it creates a client process, of the config, argv, child environment, fixture identity, cwd, and record destinations it is about to use. Passing proves only that the supervisor-owned launch inputs are isolated; after launch, a canary provides the second layer, confirming probe availability and fixture provenance.
- **Avoid**: tool-surface attestation, inventory gate, pre-turn inventory check, client compliance proof, 完全無工具
- **Why**: The old contract tied its safety evidence to a session inventory the client had to emit first, but supported clients emit init only after receiving a turn, and that init does not enumerate the available probe tools. The result was an ordering deadlock and an over-claimed proof. This term puts the responsibility on the supervisor, which can decide the actual launch inputs before spawn, and keeps the canary as a dynamic second layer; a request made through flags is never presented as client runtime attestation. Owner: Sean (#97); SoT: the launch-plan boundary in `scripts/probe_supervisor.py`; source: `openspec/changes/archive/2026-08-09-enforce-mcp-probe-launch-isolation/design.md`.

### self-address

- **Definition**: The set of email addresses that represent the mailbox owner. The **operator declares** them; they are stored in the DB `self_addresses` table and maintained with `bes identity add / remove / list`. An empty set (or a table not yet created) means "undeclared": no address is built into the code (`#268`), so with nothing declared no message is judged to be sent by the owner.
- **Avoid**: owner-email, my-address, self-email, own-account
- **Why**: The query layer decides [[direction]] by whether the sender belongs to the self-address set (contact-history first; commitments and open-thread views will reuse it), so every consumer (CLI / MCP / Python API) needs one shared definition. On 2026-08-20 this was upgraded from a single hard-coded entry to user-definable: a user may have several addresses, and an open-source user should not have to edit code to declare themselves. The identity lives in the DB, so the MCP side reads the same DB and inherits it with zero configuration. The three rejected ways of deciding are in Rejected framings. Owner: Sean (settled in #166 grilling); SoT: the `self_addresses` table, resolved through `src/mailattic/identity.py > resolve_self_addresses`; source: #166 Implementation Decisions, #168.

### direction (inbound / outbound)

- **Definition**: A message's direction relative to [[self-address]]: sender ∈ self-address → `outbound` (sent by the owner, including self→self), otherwise → `inbound`; a missing sender gives null rather than a guess (`null` holds only in the output field of sub-ticket B; `contact-history` currently emits only the outbound and inbound counts). Send/receive ratio = outbound count / inbound count. The judgment is **computed on the fly**, stored in no column, and never backfilled: changing the self-address set takes effect across the whole DB.
- **Avoid**: sent/received, in/out, 方向性, 收發向
- **Why**: Tightly bound to [[self-address]]; it is the semantic root of send/receive ratios and contact analysis, shared by contact-history / commitments / open threads. The sender is the comparison key because it is single and stable (why folder-based judgment was rejected: see Rejected framings). **The current implementation is an exact string comparison** (`LOWER(sender_email) IN (...)`), so a dirty value stored as `sean3808<sean3808@mailattic.test>` is missed. "Extract the email part before comparing" is in scope for #44 sub-ticket B and has not landed; flag it as pending when citing. Owner: Sean; SoT: same as [[self-address]], comparison point `src/mailattic/contact_history.py > _run_aggregation`; source: the Decision "direction is judged by whether the sender belongs to self-address" in `openspec/changes/archive/2026-05-30-add-contact-history/design.md`, #166.

### thread-bridge

- **Definition**: An inferred, low-confidence outbound thread-membership edge that reconnects a header-less outbound copy (sender in [[self-address]], with no `in_reply_to` / `references_header`) to its target thread. It is stored in separate columns, leaving the real header fields untouched. It takes part in the `thread_key` connected-component computation but is tagged `source='subject_heuristic'`, which separates it from header-based reference edges.
- **Avoid**: subject-merge, fuzzy-thread-link, soft-edge, inferred-reply
- **Why**: Many header-less outbound replies (MAPI Sent Items) must be reattached to their conversation so ball-in-court judgment sees the whole thread, while the forensic header fields (the `raw_headers` posture) must not be polluted. A separate edge keeps inferred membership auditable, reversible, and rebuildable by backfill. It follows the layering principle of Thunderbird gloda Bugzilla #668491: the index layer trusts headers, and subject inference sits in a separate, marked layer. Owner: Sean; SoT: `scripts/bridge_orphan_outbound.py` + `messages.thread_bridge_target/thread_bridge_source`; source: `openspec/changes/archive/2026-06-30-bridge-orphan-outbound-threads/design.md` Decisions 1–4.

### rebuild

- **Definition**: The disaster-recovery action that **re-ingests** the platform DB from the original PSTs (plus an IMAP backfill of the mail server's recent window). It is the **deep fallback**, and its completeness is capped by source freshness (the PST coverage upper bound plus the server's retention window; whatever falls between the two is unrecoverable). It is **distinct from and not interchangeable with** `restore` (restoring a complete DB backup from an external copy; the primary DR path).
- **Avoid**: restore, recover, 還原, 復原 (these mean "restore an existing DB backup")
- **Why**: Confusing the two in DR discussion is a real risk. On "the day the computer dies" the first choice is restore (complete, recent); rebuild is the way out when restore and every backup are gone, and it has structural gaps. Both are legitimate, but they must be told apart so nobody picks the wrong path during an incident or assumes the recovery is complete. Owner: Sean (advocates backing up PST and DB together to close the gap); SoT: `docs/rebuild-db-from-pst.md` (runbook) + `bes rebuild`; source: Context and D1–D7 of `openspec/changes/archive/2026-06-11-add-disaster-recovery-rebuild/design.md`.

### Rebuild source-count baseline (rebuild 來源筆數基準)

- **Definition**: For a given group of PST sources, the minimum ingested-message count, with an explicit stated shortfall, set on an approved, comparable basis. Meeting the baseline shows only that the source count reached the bar; it does not show that every old message was kept. Without a trustworthy baseline, structural health can still hold, but source-count reconciliation cannot be declared passed.
- **Avoid**: 郵件完整性證明, 逐封保全, PST 原始訊息總數, 抽取帳 (extraction ledger)
- **Why**: The same message duplicated across PSTs, the extraction method, and source attribution all change the ingested count, so the historical first-import count, the old DB distribution, and this run's extraction count cannot be swapped for each other. Owner: Sean (2026-10-03 `#228` grilling, Q1=A, Q2=A); SoT: the question-by-question rulings in the source-baseline discussion (a local working file, not under version control); the overall implementation brief is still to be confirmed.

### Import path (匯入路徑)

- **Definition**: The three ways external mail gets written into the DB; they are not the same dimension. **Two read PSTs**, chosen by the `backend` parameter of `import_pst_sources`: `"outlook"` (Outlook COM; complete recipients; the official path for `bes rebuild` and `bes import`) and `"readpst"` (libpst; **recipients known to be corrupted**; no production caller; used only as a rescue when "a PST is in hand but Outlook is unavailable"). **The third reads a maildir**, the separate function `import_maildir_sources` (`bes import --maildir`); it needs neither Outlook nor readpst and is the path the daily IMAP sync walks every day.
- **Avoid**: 「三條路線」 (counts Windows readpst and WSL readpst as two), backend (insufficient alone: `backend` covers only the two PST paths, not maildir)
- **Why**: Windows readpst and WSL readpst **are the same code** (`resolve_readpst_path` looks for `readpst.exe`, then `readpst`) resolving to different binaries; that is an environment difference, not a branch. Counting them as two yields the false conclusion "there are three backends". The maildir path is also often forgotten, which leads to the false inference "without Outlook only readpst is left", and then to using a path with known-corrupted recipients as the backup (`#175` spent a whole rebuild round fixing exactly that corruption). Owner: Sean (clarified while settling `#184` in the 2026-09-10 grilling); SoT: `import_pst_sources` / `import_maildir_sources` in `src/mailattic/ingest.py`; source: `#184`, `#154`, [D-20260903-1](docs/decisions/D-20260903-1-recipient-rebuild-extraction-on-windows-outlook.md).

### outbound-send

- **Definition**: Submitting one RFC5322 message through the mailattic.test SMTP submission (465/SMTPS), **meaning the delivery itself only**. The follow-up local DB insert (`pst_source='outbound-bes'`), APPEND-to-Sent, and [[BCC-self]] are separate later side effects and are not part of this verb.
- **Avoid**: send (ambiguous unqualified: Outlook's send button and the receiving end of `bes sync-imap` both get called "send" in speech), 寄出, 投遞, deliver
- **Why**: Outlook send and bes send **must coexist** (Outlook is the human primary path; bes is the agent's backup), and calling both "send" would leave incident tracing unable to tell who sent a message and by which path. The side effects are split out because each can fail without undoing the delivery: a failed APPEND only means the message is not visible, while the mail has already gone out. Owner: Sean; SoT: `src/mailattic/outbound.py`; source: §2 of the 2026-05-28 outbound-capability design notes (a local working file, not shipped in the public version).

### Conversational authorization (對話層授權)

- **Definition**: The agent presents the draft (subject, full To/Cc/Bcc, body, attachments, draft number, fingerprint) to the user; once the user says in conversation to send it, the agent sends with the **draft fingerprint** (the SHA-256 of all bytes of the draft `.eml`). Several drafts are presented together and authorized once (one row per draft in the summary table). The tool layer verifies only that "what is sent is what was seen": a fingerprint that differs from the current draft is refused. The CoWork (MCP) form is likewise "send only after seeing, and what is sent is what was seen", but one authorization sends exactly one message (no multi-draft authorization) and only drafts CoWork itself created can be sent: a draft created by the CLI cannot be sent through CoWork.
- **Avoid**: 指紋授權 (the fingerprint is not the authorization; the authorization is the user's sentence), auto-send, `--yes`, confirm token (確認 token)
- **Why**: Claude Code's agent shell tool and `!` are both non-TTY, so accepting only [[Explicit-text authorization]] means no mail can be sent from inside Claude Code at all. Whether the user was asked is something the tool layer cannot test, so it is written as an agent duty (a mandatory skill step) rather than a contract clause. The fingerprint stops a draft being swapped, or a number being mismatched, after presentation; it cannot stop the agent from sending without asking, nor the "old number + old fingerprint taken as a wrong set". Owner: Sean (settled in `#195`, 2026-09-14); SoT: `send_draft(fingerprint=)` / `send_batch(fingerprints=)` and behavior contract #2 in AGENTS.md; source: [D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md).

### Explicit-text authorization (明文授權)

- **Definition**: A human types a **non-empty authorization phrase** into a **real** interactive terminal (stdin and stdout both TTY) before outbound delivery is released; it is the only release form when no fingerprint is supplied. Pressing Enter to accept a default does not count; with no fingerprint and no TTY, sending is always refused. The gate lives inside the core execution function, not in a CLI flag.
- **Avoid**: explicit confirm, 確認, 二次確認, `--yes`, auto-confirm
- **Why**: It is called 「明文」 (explicit text) rather than "confirmation" to exclude the formal confirmation that passes on a bare Enter. It was originally outbound's only authorization form; on 2026-09-14 it narrowed to the TTY form and now coexists with [[Conversational authorization]]. Owner: Sean (named 2026-05-28, replacing the earlier "explicit confirm"; narrowed 2026-09-14); SoT: the TTY gate of `bes send-draft` / `bes send-batch` (behavior contract #2 in AGENTS.md); source: §2 of the 2026-05-28 outbound-capability design notes (a local working file, not shipped in the public version), [D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md).

### BCC-self

- **Definition**: On [[outbound-send]], putting [[self-address]] into the SMTP envelope recipient set (not into a header-visible line) so the owner receives a copy as a **record backstop**. It is currently the secondary path: the primary path for sent-mail visibility is APPENDing the `.eml` into the IMAP `寄件備份` folder after sending.
- **Avoid**: self-copy, 密件副本, 自寄一份, BCC loop
- **Why**: A message sent by bes does not pass through Outlook; without a feed-back, neither the Outlook Sent folder nor this DB has any trace of it. APPEND is best-effort (a failure prints a warning only and leaves the exit code alone), so a backstop independent of IMAP folder state is kept. When the copy flows back it passes the message-id dedup gate and is not ingested twice. Owner: Sean; SoT: `src/mailattic/outbound.py`; source: [D-20260528-2](docs/decisions/D-20260528-2-outbound-visibility-via-bcc-self.md), [D-20260714-2](docs/decisions/D-20260714-2-chinese-sent-folder-of-record.md).

### pre-task sync

- **Definition**: An incremental IMAP sync the caller triggers up front before starting a task (`bes sync-imap --pre-task`), with a 15-minute minimum-interval gate: if the last sync was under 15 minutes ago, it skips.
- **Avoid**: 手動 sync, 強制同步, refresh, force-sync
- **Why**: An agent needs "fresh enough" data before querying mail, but back-to-back tasks would turn that into burst syncs (measured 2m43s–22m56s per run, not proportional to the number of new messages). The gate lets the caller invoke it every time without tracking the last sync time. The meaning is "ensure fresh enough", not "force a pull", so a skip is not a failure; but when the previous sync failed or did not finish, the skip output says so (`last sync failed` and similar), and the data must not then be treated as fresh. Owner: Sean; SoT: `bes sync-imap --pre-task`; source: §2 of the 2026-05-28 outbound-capability design notes (a local working file, not shipped in the public version).

### runtime root（`.project-runtime`）

- **Definition**: The **single** ignored directory under the repo root that holds throwaway tool-generated artifacts, with one named leaf per kind (pytest basetemp, Python temp, uv / XDG / pre-commit caches, marker-ledger collection, cross-process lock). The membership test is "**what happens if the whole thing is deleted**": only what a delete merely forces to be rebuilt, losing no durable state, belongs here. Three kinds are explicitly outside it: business runtime data (`.mailattic-work/`), manual WSL scratch (`work-tmp/`), and `.subagent-input/` and `.subagent-output/` (whose paths the global dispatch protocol hard-codes).
- **Avoid**: 測試暫存區, 系統碟隔離目錄, test artifact root, cache 目錄
- **Why**: **The first intent is a readable repo root**: gathering throwaway folders and caches into one root lets you tell artifacts from the project proper at a glance, and makes "delete the whole thing" a safe action. System-drive isolation was a **second** intent attached on 2026-07-29 and retired on 2026-08-21 with the machine swap ([D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md)); only the second intent was retired. The first intent was in no record before 2026-08-21 (the proposal mentioned only system-drive saturation, and D-20260821-1 even called it "the only recorded reason"), so reading the records yields "this root no longer has a reason to exist", and on 2026-08-21 someone did suggest exactly that. This entry exists to block that misreading. Owner: Sean (settled in the 2026-08-21 grilling); SoT: the managed-leaves list in `scripts/project-tool-environment.ps1`; source: the 2026-08-21 grilling conversation, [D-20260821-1](docs/decisions/D-20260821-1-retire-system-drive-hardware-guard.md).

### Quarantine (隔離區)

- **Definition**: A directory separate from the sync scratch area that holds exactly one kind of thing: the original file of a message that still has any attachment-write failure on record that is neither backfilled nor abandoned by a person (the set `bes db doctor` lists as unresolved). It stays as long as any such record is unresolved. When the message's attachments are backfilled, or the failure is manually abandoned, and the message has no other unresolved attachment failure, the original file is removed; other scratch raw messages are still cleared after seven days.
- **Avoid**: raw archive, 原始信封存, 備份區, 垃圾桶, 待處理匣
- **Why**: The seven-day scratch window is by design (D-20260703-1), but for a message whose attachment write failed, the original file is the only local source from which the attachments can be recovered, and expiring it at seven days would seal the failure shut. Quarantine is the sole exception to that rule, and its admission condition is narrow enough not to breed a "this is a backup" dependency: its only exits are backfill or manual abandonment, the system never abandons on its own and sets no expiry. A message whose failure was resolved by another path is not auto-cleared and still counts in doctor's `quarantined_mail` number. It sits in the business runtime data directory, not in `.project-runtime`, because the latter's test is "deleting the whole thing loses no durable state", and quarantine is the opposite. Owner: Sean (settled in #239, 2026-10-09); SoT: `src/mailattic/quarantine.py`; source: #239, #256.

---

## Maintenance principles

- A new term needs the three fields `Definition` / `Avoid` / `Why`; missing any one makes it non-canonical. `Why` carries Owner / SoT / source.
- A word absent from an `Avoid` list is not thereby approved; before using one, ask whether it belongs in `Avoid`.
- When an existing spec or code still uses an `Avoid` word, switch it back to the canonical term the next time that file is edited (gradual migration).
- Put a term under the H2 of its scope. When the first term belonging to both scopes appears, open a third H2 "Cross-scope concepts" and explain in its Why which two scopes it spans.
- This file is updated actively by `/domain-modeling` and by conclusions from daily discussion; when drift is found, note "Capture to: GLOSSARY.md" in the conclusion section.
- **Rejected naming and design choices are recorded only in Rejected framings below** (to stop old designs from returning); a term's Why holds one pointer there and does not restate the rejection reason.

---

## Rejected framings (historically rejected designs; do not revive)

- **Inferring "who the owner is" automatically from sender frequency** — rejected in `add-contact-history 2026-05-29`, reaffirmed in #166. Reason: it would misjudge a high-frequency external correspondent as the owner; the owner's addresses are declared by the operator. Boundary: [[self-address]], [[direction]].
- **Deciding "our side" by domain** — rejected in #166. Reason: under one domain there are several people and role mailboxes, so domain-based judgment breaks the semantics outright. Boundary: [[self-address]].
- **Declaring the owner's address through a config file / env var** — rejected in #166. Reason: identity must travel with the mailbox DB so the MCP side, reading the same DB, inherits it with zero configuration. Boundary: [[self-address]].
- **Deciding direction by folder_path (Sent/Inbox)** — rejected in `add-contact-history 2026-05-29`. Reason: folder naming differs across PST/IMAP sources and is unreliable. Boundary: [[direction]].
- **Non-interactive send flags that do not bind the draft content** (`--yes` / `--auto-confirm` / `--batch` auto-confirm / pressing Enter for the default) — rejected in `add-outbound-send-capability 2026-05-28`, narrowed 2026-09-14 ([D-20260914-1](docs/decisions/D-20260914-1-send-authorization-is-conversational-with-draft-fingerprint.md)). Reason: such flags cannot even stop the draft being swapped after the user has looked at it. The original "reject any non-interactive bypass" had the actual effect that no mail could be sent from inside Claude Code; sending with the draft fingerprint is now the official entry. Boundary: [[Conversational authorization]], [[Explicit-text authorization]].
- **bes embedding an LLM to draft the body itself** — rejected in `add-outbound-send-capability 2026-05-28` ([D-20260528-1](docs/decisions/D-20260528-1-bes-never-generates-content.md)). Reason: rule-clear work goes to scripts and semantic judgment stays with the caller AI; letting bes generate content would blur its single responsibility as "query backend + outward channel". Boundary: [[outbound-send]].
- **reply-to-sender-only as the default for "reply" (回復)** — rejected in `add-outbound-send-capability 2026-05-28`. Reason: it would quietly fork a 10-person conversation into two. Boundary: [[Reply-all]].
- **Heuristically backfilling `in_reply_to`** — rejected in `bridge-orphan-outbound-threads`. Reason: it disguises inference as a parsed header fact. Boundary: [[thread-bridge]].
- **A second root for "one-off documents produced by agents"** (alongside `.project-runtime`) — rejected in the 2026-08-21 grilling. Reason: on inspection it has **no residents**. The paths of `.subagent-input/` and `.subagent-output/` are hard-coded by the global dispatch protocol (Tier 3 and the `report_write` mode in `orchestrator-discipline.md`), so moving them in this project alone would make subagents silently take the old path; `Issue/` is also not a throwaway artifact but an unclassified junk room mixing PRDs and fixture scripts. Building a root with no residents renames "unclassified" as "has a system"; the real problem with `.subagent-*` is the retention period, not the location. Boundary: [[runtime root（`.project-runtime`）]].
