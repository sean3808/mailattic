# Capability D - New Thread (start a fresh mail thread)

> `SKILL.md`'s capability routing table loads this file (MANDATORY).
> Precondition: no anchor message needed.
> A reply to an existing mail (`capability-c-reply.md`) remains the default whenever earlier correspondence on the topic exists. Use this capability only when contacting a person for the first time, or when starting a genuinely separate topic that should not be grouped into an existing thread.

## When to use

- The user says "幫我寫一封新信給 X", "起個新 thread 給 Y", or "draft 一封全新郵件"
- A contact is brand new (no earlier mail exists in the DB to reply to)
- The user explicitly wants a fresh thread rather than continuing an existing conversation
- **Default choice**: if an earlier mail on the same topic exists, use **Capability C (Reply)** instead. Do not break thread history by starting new threads unnecessarily.

## How to use

Four steps. **The caller AI writes the body into a file; bes does mechanical steps only; the user decides in conversation whether to send**:

**bes resolves the sending settings file itself; `--env-file` is normally not needed**: `draft-new` / `send-draft` / `send-batch` take `--env-file <path>` -> env var `MAILATTIC_SMTP_ENV_FILE` -> built-in default (`.env.sean3808` at the repo root), in that order. Pass the flag only to send from a different account. When the file exists but no sending account can be read from it, `draft-new` uses the only entry in `bes identity list` as From; zero or several entries exit 2. The sending account shows as `From:` in Step 2's stdout.

### Step 1: the caller AI writes the body into a file

```
<user's current project root>/draft_<topic>.txt
```

A plain-text UTF-8 file with no headers (To/From/Subject are passed as flags to bes). A Markdown pipe table in the body (header row, `---` separator row, body rows; every row the same cell count) renders as a table in the HTML part; other Markdown is sent as plain text, and the plain-text part is the file unchanged. After the user edits it as needed, go to Step 2.

### Step 2: produce the draft

Run `bes draft-new` with at least one `--to`, a non-blank `--subject`, and `--body-from`:

```bash
bes draft-new --to recipient@example.com --subject "Project Kickoff" --body-from <user's current project root>/draft_<topic>.txt

# Multiple recipients and CC/BCC (each flag is repeatable):
bes draft-new --to primary@example.com --to partner@example.com \
    --cc team@example.com --bcc audit@example.com \
    --subject "Quarterly Update" --body-from <...>/draft.txt

# Attach files (--attach is repeatable):
bes draft-new --to client@example.com --subject "Proposal" --body-from <...>/draft.txt \
    --attach "proposal.pdf" --attach "specs.xlsx"
```

bes behaviour:
- **Required fields**: `--to` requires at least one valid address; `--subject` must be non-empty, single line, without control characters; `--body-from` must point to an existing UTF-8 text file. Missing or invalid required fields exit 2 with no draft written.
- **Recipient address boundary**: every `--to` / `--cc` / `--bcc` value must be a single, syntactically complete address with an ASCII addr-spec. Unicode display names such as `"王小明" <user@example.com>` are supported.
- **From**: the sending address named by `SMTP_USER` in the resolved sending settings file.
- **BCC-self**: adds ourselves automatically to Bcc, preserving sent-mail tracking backstop.
- **No threading headers**: the message has a freshly generated bracketed Message-ID and no `In-Reply-To` or `References`.
- **Attachments**: attached files are verified and packaged into the draft `.eml`.
- **Recipient familiarity check**: every caller-given `--to`, `--cc`, and `--bcc` address is checked against the DB (BCC-self is excluded). The output marks each address as:
  - `seen`: address appeared before in the DB (as sender or recipient).
  - `never seen (domain seen)`: address was never seen before, but its non-public domain is known in the DB.
  - `never seen`: address and domain have never been encountered in the DB.
  Marks are informational warnings, not refusals; first contact remains possible.
- The `.eml` is written to `outbound\drafts\<draft_id>.eml` under the database's directory and is **not sent**.
- Stdout prints `draft_id:` / `From:` / `Subject:` / `To:` / `Cc:` / `Bcc:` / `Message-ID:` / `Attachments:` / `fingerprint:` / `Recipient check:`.

### Step 3: present for review (mandatory, never skipped)

**Whether to send is decided by the user in conversation; this is the agent's duty.** Present all of the following at once, then stop and wait for the reply:

- Draft id (`draft_id`) - always label it; sending uses exactly this one
- Sending account (`From:` in Step 2's stdout)
- Subject
- Full `To` / `Cc` / `Bcc` (as in Step 2's stdout)
- **Agent Duty**: explicitly name every recipient marked `never seen` or `never seen (domain seen)`, drawing Sean's attention to any typos or first-contact addresses before sending
- The body you wrote (the Step 1 file content)
- Attachment names and sizes (as in Step 2's stdout; say none if there are none)
- Fingerprint (`fingerprint:` in Step 2's stdout)

### Step 4: send with fingerprint

Once the user approves the draft in conversation, send with `--fingerprint`:

```bash
bes send-draft <draft_id> --fingerprint <fingerprint>
```

For sending multiple drafts together, use `bes send-batch <draft_id1>=<fp1> <draft_id2>=<fp2>`.
After sending, the message is stored as an outbound-send message with its own thread in the DB and appended to IMAP `寄件備份`.
