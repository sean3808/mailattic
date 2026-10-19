# Capability C - Reply (reply to an existing mail)

> `SKILL.md`'s capability routing table loads this file (MANDATORY).
> Prerequisite: the original mail must already be found through Capability B, with its stored RFC822 `message_id` in hand (not an integer DB / archive row id). If the caller has not run Find, **load** `capability-b-find.md` first.
> `capability-a-sync.md` is not needed (reply does not depend on a fresh sync).

## When to use

- The user says "幫我回覆 X 那封", "draft 一個 reply 給 Y", or "我要對某封信回信"
- Capability B found the original and the user follows with "OK 對這封寫個回覆"
- **Precondition**: the mail being replied to must be an **existing DB mail** (the thread exists). To start a new thread to a new recipient, see "Unsupported capabilities" in SKILL.md

## How to use

Four steps. **The caller AI writes the body into a file; bes does mechanical steps only; the user decides in conversation whether to send**:

**bes resolves the sending settings file itself; `--env-file` is normally not needed**: `draft-reply` / `send-draft` / `send-batch` take `--env-file <path>` -> env var `MAILATTIC_SMTP_ENV_FILE` -> built-in default (`.env.sean3808` at the repo root), in that order. Pass the flag only to send from a different account. The file must hold `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD`; archive-scrape's `MAIL_SERVER_ENV_PATH` is not an SMTP settings source. When the file exists but no sending account can be read from it, `draft-reply` uses the only entry in `bes identity list` as From; zero or several entries exit 2 and say which setting is needed (bes has no built-in sender address). The sending account shows as `From:` in Step 2's stdout, so show it to the user at review ([D-20260915-1](../../../docs/decisions/D-20260915-1-smtp-settings-file-resolved-by-bes-with-from-shown-at-review.md)).

### Step 1: the caller AI writes the reply body into a file

```
<user's current project root>/reply_<topic>.txt
```

A plain-text UTF-8 file with no headers (To/From/Subject are all assembled by bes; to change the subject use `--subject`, see Step 2). After the user edits it as needed, go to Step 2.

### Step 2: produce the draft

**Pick a safe anchor first**: an archive-scrape sent row is only evidence for finding the mail, and does not guarantee complete reply-all evidence. The server sometimes splits one sent mail into rows by delivery batch, so that row's To/Cc may miss other domains. Anchor on an **inbound** mail of the same thread first; when an archive-scrape row is passed, bes refuses by default and writes no `.eml`.

**Get the anchor value**: in the Find stage, retrieve the hit with `bes search ... --full-body --json`, take the full RFC822 Message-ID from that hit's `message_id` field (for example `<abc123@example.com>`), and pass it verbatim as `bes draft-reply`'s first argument. Do not pass the integer `id` / `db_id` shown by search/show.

With no inbound anchor, a caller that has verified the correct envelope by hand may pass `--allow-archive-anchor`. This flag is a safety declaration that "I verified by hand"; it does not mean the archive is complete. bes still warns on stderr and prints the final To/Cc/Bcc in stdout. If the caller holds an approved list, also use `--replace-recipients`, so the archive message_id only anchors the thread:

```bash
bes draft-reply <rfc822_message_id> --body-from <user's current project root>/reply_<topic>.txt

# Attach files (--attach is repeatable):
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --attach "supplier-quote.pdf" --attach price-list.xlsx

# Add people on top of the reply-all derivation (each flag is repeatable):
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --to buyer@client.example --cc sales@example.com --bcc audit@example.com

# Explicit placement wins: even if buyer was derived into Cc, they move to To, no --exclude needed:
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --to buyer@example.com

# Remove people from the reply-all derivation (--exclude acts on derived addresses only):
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --exclude former-contact@example.com

# Use only the hand-approved list, skip the reply-all derivation (at least one --to):
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --replace-recipients --to approved@example.com --cc approved-cc@example.com

# Borrow an existing thread for a new topic: replace the subject, In-Reply-To / References still follow the anchor mail:
bes draft-reply <rfc822_message_id> --body-from <...>/reply.txt \
    --subject "索取文件（範例）" --replace-recipients --to info@example.com

# Envelope verified by hand, allow an archive message_id to serve only as the thread anchor:
bes draft-reply <archive_rfc822_message_id> --body-from <...>/reply.txt \
    --allow-archive-anchor --replace-recipients \
    --to approved@example.com --cc approved-cc@example.com
```

bes behaviour (none of it needs caller intervention):
- **Default To / Cc**: reply-all on the original, de-duplicated, excluding ourselves (so nothing is sent to ourselves)
- **Additions and explicit placement**: repeatable `--to ADDR` / `--cc ADDR` / `--bcc ADDR` add to the derived result, de-duplicated across buckets by case-insensitive email. Explicit To/Cc is the authoritative placement: the same address is first removed from every derived bucket, then put in the explicit bucket; when the same address is listed explicitly in both To and Cc, To wins and only one copy is kept
- **Excluding derived addresses**: repeatable `--exclude ADDR` removes addresses only from the reply-all-derived To/Cc. It does not filter the explicit lists; if the same address also appears in any explicit `--to` / `--cc` / `--bcc` (even with different case), that is contradictory arguments and the command exits 2 before env / DB I/O with no draft
- **Full replacement**: `--replace-recipients` ignores the reply-all derivation; To/Cc and any extra Bcc come only from this call's explicit values (the existing automatic BCC-self stays)
- **Final To invariant**: in every mode the resolved To needs at least one address; if To is empty (including replace without `--to`, or `--exclude` removing the only derived To), exit 2 and no draft
- **Contradictory flags rejected early**: `--exclude` only filters the reply-all derivation while `--replace-recipients` skips the derivation entirely, so the two must not be combined. The handler exits 2 before reading the DB with no draft, and stderr is exactly `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.`; the caller removes `--exclude`, or drops `--replace-recipients`
- **Exclusion overlapping explicit recipients is also rejected early**: `--exclude` is compared case-insensitively with every explicit To/Cc/Bcc address; any intersection makes the handler exit 2 before env / DB I/O with no draft, listing all conflicts in a fixed order of normalised addresses. stderr reads `--exclude conflicts with explicit recipient address(es): <address-1>, <address-2>. Remove each address from --exclude or from --to/--cc/--bcc.`. The caller removes each address from `--exclude` or from the explicit `--to` / `--cc` / `--bcc`, as intended; never expect the exclusion to filter the explicit list silently
- **Explicit-address parser boundary**: every `--to` / `--cc` / `--bcc` / `--exclude` value must be a single, syntactically complete address whose addr-spec is ASCII addr-spec. malformed raw syntax such as an unclosed angle bracket, or a non-ASCII addr-spec such as `audit@例え.テスト`, gets exit 2 before env / DB I/O with no draft; a Unicode display name with an ASCII addr-spec, such as `"王小明" <audit@example.com>`, is supported
- **archive anchor override**: an archive-scrape anchor still gets exit 2 and no draft by default; only an explicit `--allow-archive-anchor` lets it through, with a stderr warning. This flag lifts only the anchor-provenance gate and does not fix recipients
- **Subject**: by default the original subject with `Re: ` prepended (not duplicated if present). With `--subject <text>` that subject is used verbatim (**no** automatic `Re: `); only the subject changes, and the thread is still decided by the anchor mail; a Chinese subject is encoded correctly. An empty or whitespace-only string, or one with a newline / control characters, always exits 2 with no draft. When the anchor mail's subject does not match what this mail is about (for example borrowing an old thread to ask the same contact for other documents), use `--subject`, so the recipient sees the purpose from the subject and later searches match
- **In-Reply-To + References**: always continue the thread chain from the original that `<rfc822_message_id>` points at, fully separate from the recipient choices above
- **From**: the sending address named by `SMTP_USER` in the resolved sending settings file
- **BCC**: adds ourselves automatically, then any explicit `--bcc` (a record backstop; sent-folder visibility mainly comes from the post-send APPEND into `寄件備份`, see "Outlook sent-folder visibility" below)
- **`--attach <path>` (repeatable)**: attaches a local file to the draft. The original file name is kept (including Chinese) and the MIME type is inferred. A path that is missing / a directory / unreadable gives a clear error + non-zero exit + no `.eml` (no half-finished product). Attachments sent through bes are written into the attachment store and indexed when the sent mail is ingested, so `bes attachments` and `bes search --attachment-name` can search them later
- The `.eml` is written to `outbound\drafts\<draft_id>.eml` under the database's directory and is **not sent**
- On success, stdout prints `draft_id:` / `From:` / `Subject:` / the final `To:` / `Cc:` / `Bcc:` / `Message-ID:` / `Attachments:` (each attachment's name and byte size, `none` if there is none) / `fingerprint:` (the SHA-256 of the full `.eml` bytes). The caller must compare the To/Cc/Bcc lines with the hand-approved list and must not infer recipients from the anchor mail alone

### Step 3: present for review (mandatory, never skipped)

**Whether to send is decided by the user in conversation; this is the agent's duty.** bes only verifies that "what is sent equals what the user saw" (the fingerprint) and cannot verify "you asked" - so skipping this step is blocked by no tool, and is still a violation.

**Single mail**: present all of the following at once, then stop and wait for the reply:

- Draft id (`draft_id`) - always label it; sending uses exactly this one
- Sending account (`From:` in Step 2's stdout)
- Subject
- Full `To` / `Cc` / `Bcc` (as in Step 2's stdout; do not summarise as "the customer and 3 others")
- The body you wrote (the Step 1 file content)
- Attachment names and sizes (as in Step 2's stdout; say none if there are none)
- Fingerprint (`fingerprint:` in Step 2's stdout)

Go to Step 4 only when the user clearly says send ("寄", "送出", "幫我寄"). If the user wants changes: delete the old `.eml`, edit the body file, rerun Step 2, and **present the new draft id and fingerprint for review again**.

**Several mails: one review, one authorisation.** Do not ask per mail. Present one summary table, one row per mail: draft id, From, subject, full To/Cc/Bcc, attachments. When the body is one shared template, present the template once and then list each mail's varying fields; when the bodies differ, attach each mail's body. If the user says send to the whole table, send the whole batch.

**Known gap (covered by discipline)**: bes does not retire old drafts automatically - an old `.eml` not deleted when revising stays in the pending list. The fingerprint blocks "id and fingerprint mismatched" and "draft changed after review", but not "the whole old id + old fingerprint pair used by mistake". When sending, use only the `draft_id` + `fingerprint` pair from the latest review.

### Step 4: send with the fingerprint

```bash
bes send-draft <draft_id> --fingerprint <sha256>
bes send-batch <id1>=<sha1> <id2>=<sha2> ...   # all with fingerprints, or all without
bes list-drafts [--json]                                     # shows draft_id / recipients / subject / created time; a draft stuck in sending also shows status / marker path
```

**Claude Code's `!` is not a TTY.** `! bes send-draft ...` looks like it runs in a terminal but has no TTY, and a send without a fingerprint is refused all the same. The agent's Bash / PowerShell tools, background tasks, and pipes are not TTYs either. **In Claude Code always send with `--fingerprint`**, and do not tell the user to run it with `!`. Interactive authorisation without a fingerprint works only in a real terminal the user opened themselves (for example Windows Terminal).

`send-draft` behaviour:
- **Authorisation, either one**: with `--fingerprint` -> recompute the current draft's fingerprint, send only on a match, no prompt; without -> a real TTY is required, a draft summary is printed, and a non-empty confirmation word is requested
- **Fingerprint mismatch**: refuse, write no send-once marker, exit 1. It means the draft changed after review, or the id and fingerprint are mismatched. **The error message deliberately does not print the new fingerprint** - do not compute the new fingerprint and resend; go back to Step 3 and present for review again
- **No fingerprint and not a TTY**: refuse; the message lists the two ways out (give a fingerprint / run in a real terminal)
- With a fingerprint it still prints the draft summary to stdout, leaving it in the conversation record as send evidence
- SMTP uses the resolved sending settings file (SMTPS 465 / submission); the summary includes `From:`
- After sending, the BCC copy is not shown on the wire, but the envelope includes ourselves
- The DB gains one row recorded with `pst_source='outbound-bes'`
- After SMTP succeeds, the `.eml` is APPENDed into the IMAP `寄件備份` (with `\Seen`), so Outlook's sent folder shows the mail

`send-batch` behaviour (for one approval and a whole-batch send, such as a batch of identical notices):
- Takes an **explicitly enumerated draft_id list** (not "send all", to avoid sending leftover drafts); a list with a duplicate id is rejected outright
- **Fingerprints all given or all omitted**: mixing `<draft_id>=<sha>` with a bare `<draft_id>` -> exit 2, nothing is sent
- With fingerprints: verified per mail, **a mismatch refuses only that mail (`failed`) and the rest are sent**, no prompt; without: prints a whole-batch summary (each mail's draft_id / full To·Cc·Bcc / subject / attachment names) and authorises the whole batch **once** in a real TTY
- Each mail goes through the same gates as `send-draft`; **a partial failure does not abort**: a failed mail is recorded and the sending continues, and the end reports per mail `sent`/`partial`/`skipped`/`failed` (`partial` = sent but some recipients were refused by SMTP, see failure modes)
- The first mail is attempted at once; after that a fixed wait of **90 seconds** sits between sendable drafts, so a short burst of about 20+ cumulative recipients does not draw `450 4.7.1 too many recipients` / `421 too many errors`; this pacing is core behaviour, so do not bypass it
- An already-sent draft (marker `sent`/`sending`) is `skipped` automatically and is not a failure; only any `failed` makes the exit non-zero. A `sending` skip reason carries the marker path and the steps (see "Draft stuck in `sending`" below)
- When nothing is sendable (all skipped / prep failed / fingerprint mismatch) no token is asked and the report prints directly; without fingerprints and not a TTY the whole batch is refused
- After each success, the DB's `outbound-bes` row and the `sent` marker are each persisted before the next mail. If the wait is interrupted, first run `bes list-drafts` to list the unsent drafts again; on a `sending` marker, check the SMTP / DB state first and **never resend blindly** (steps under "Draft stuck in `sending`" below). Items already `sent` are safe to skip, and drafts not yet started are enumerated explicitly and resumed

## Failure modes + precondition checks

- **Draft stuck in `sending`** (`list-drafts` shows `status: sending`; `--json` has `status` / `marker_path` / `next_step`): after a send failure the send-once marker deliberately stays at `sending` (the mail may have gone out, so it is not released automatically); `send-draft` refuses and `send-batch` reports `skipped` and exits 0. Steps: (1) the message and `list-drafts` both print the marker file's actual path; (2) the facts: this mail may have reached some or all recipients (To, Cc, Bcc), and a send may still be running; (3) **only when it is certain that no send is still running and no recipient received it may it be resent** -> delete that marker file and resend (before resending, still go through Step 3 review with the fingerprint); if anyone already received it, it is uncertain, or the user wants to give up -> keep the marker and do not resend the whole mail. The user judges how to be certain (asking the recipient and checking the sent folder / server records are references only; "the recipient has not got it yet" does not mean it was not sent); when unsure, do not resend. The one exception: the message says explicitly `SMTP login was rejected ... the mail was not sent` (login rejected, SMTP never accepted anything), and after fixing the credentials the marker may be deleted and the mail resent without checking recipients. There is no "unmark" command; deleting the file is the unmark

- **`SMTP settings file not found`**: the file resolved through the three layers does not exist (common after switching machines, or a wrong env var); all three outbound commands exit 2 before DB or network I/O, and the message lists three ways out. Report to the user to choose one; do not substitute `MAIL_SERVER_ENV_PATH`
- **`From:` is not the expected sending account**: stop at Step 3 and do not send; report to the user, point `--env-file` at the correct account's settings file, and regenerate the draft
- **SMTP authentication failure / `Could not load SMTP config`**: the settings file exists but its content is wrong; remind the user to check that it holds `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD`; never try to fill in content yourself
- **`interactive authorization required`** (no fingerprint, send run in a non-TTY environment): if the user already saw the draft in conversation and said send -> add the `--fingerprint` that `draft-reply` printed and rerun; if it was not yet presented -> do Step 3 first. Never tell the user to run it with `!` (`!` is not a TTY), and never import private helpers such as `_prepare_draft_for_send` / `_submit_prepared_draft` to bypass this
- **`draft fingerprint mismatch`**: the draft changed after review, or the id and fingerprint are mismatched. `send-draft`: this mail was not sent and no marker was written. `send-batch`: only the mismatched mail is refused (`failed`, no marker), and the other mails with matching fingerprints **are sent as normal** - before resending, read the per-mail report and handle only the `failed` one; do not rerun the whole batch. Go back to Step 3 and present the **current** draft for review again with the fresh `draft-reply` output; never compute a new fingerprint yourself
- **Outlook sent-folder visibility / an append warning is not a send failure**: Outlook for sean3808@mailattic.test now runs over IMAP, and its sent folder is IMAP's `寄件備份`. After a successful send, bes APPENDs a copy there on a best-effort basis. **If stderr prints an append warning, the mail has already gone out** - the warning only means "Outlook's sent folder may not show this mail" and does not mean it was not sent. **Never resend because of an append warning** (it would really send two). To confirm delivery, check the DB row `pst_source='outbound-bes'` or the BCC-self copy, not whether the mail shows in the sent folder.
- **append is skipped on purpose when the IMAP account is not the sending account**: when `IMAP_USER` and `SMTP_USER` differ, bes refuses the APPEND and warns (to stop archiving a whole outbound copy into someone else's mailbox via a wrong account). The mail has still been sent.
- **Some recipients refused by SMTP (`Partially sent` / `partial`, #224)**: the server refused some addresses at the RCPT stage (for example `450 4.7.1 too many recipients`) and accepted the rest; the mail **has been sent**. `send-draft` prints `Partially sent: message_id=...`, stderr lists each refused address with its code/reason, **exit 3**; `send-batch` reports that mail's outcome as `partial` (stderr lists them too), overall exit 1; MCP `mail_send` returns `status="partial"` with `accepted` / `refused[{address,code,reason}]` / `warning`; the Python API returns same-named fields including `delivery="partial"`. The marker is `sent` and records `refused`, so **never resend the whole mail** (it would duplicate to the accepted recipients, and the marker blocks a resend anyway) - open a new draft for the `refused` addresses only. The refused one may be the customer, or only BCC-self (then the DB / inbox backstop does not exist; report to the user). The recipients in the DB are the "intended list", not a delivery guarantee. **DATA accepted and only the closing QUIT failed (#230)**: the mail is still recorded as `sent`, the result carries an extra `close_warning` (CLI / batch also print `SMTP close failed after accept` on stderr, exit code unchanged); the mail has been sent, **never resend**. If SMTP refused **all** recipients, treat it as `SMTP submission failed` (the marker stays at `sending` and needs human intervention; the message carries the marker path and steps, see "Draft stuck in `sending`" above).
- **SMTP accepted is not delivered**: `sent` only means the submission server took the mail; it can still bounce later.
- **`draft-reply` does not wire in an LLM for content**: there is no way to have bes generate content; the caller AI must write the body into a file and feed it through `--body-from`
- **Contradictory recipient flags (replace)**: seeing `--exclude only filters reply-all-derived recipients; --replace-recipients skips derivation entirely. Remove --exclude, or drop --replace-recipients.` means the command was rejected before reading the DB. By intent, remove `--exclude`, or drop `--replace-recipients` to return to reply-all derivation mode
- **Contradictory recipient flags (address overlap)**: seeing `--exclude conflicts with explicit recipient address(es): <addresses>. Remove each address from --exclude or from --to/--cc/--bcc.` means the listed addresses exist in both the exclusion and some explicit To/Cc/Bcc list (compared case-insensitively); the command already exited 2 before env / DB I/O with no draft. Remove each conflicting address from either `--exclude` or the explicit placement, then regenerate; never expect the exclusion to filter the explicit list
- **Recipient address violating the contract**: malformed raw syntax such as an unclosed angle bracket, or any non-ASCII addr-spec, gets exit 2 before env / DB I/O and leaves no draft. Unicode is allowed only in the display name; the actual addr-spec must still be ASCII
- **Final To empty**: no recipient mode may leave the final To empty; on seeing `at least one To recipient is required`, add an explicit `--to` or adjust the exclusion and regenerate. That run exits 2 and leaves no draft
- **Changing a draft**: **editing the `.eml` directly is unsafe** (bes does not recompute headers); the way to change is to delete the draft and regenerate: `rm outbound/drafts/<draft_id>.eml` -> rerun `bes draft-reply` with the new body file
- **Idempotency of an already-sent draft**: `send-draft` carries a send-once marker; calling it again on an already-sent draft refuses and prints a message (one stuck in `sending` also carries the marker path and steps)

## Wrap-up

- Put the reply body file (`reply_<topic>.txt`) in the user's current project root; never write it back into the mailattic repo
- `outbound/drafts/<draft_id>.eml` is managed by the platform itself; the caller does not clean it
- After sending, the DB holds a `pst_source='outbound-bes'` record that Capability B can find
