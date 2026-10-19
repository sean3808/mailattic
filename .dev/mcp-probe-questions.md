# MCP probe question set

Fixed, committed, append-only. Every entry below is a **target turn only**.

The canary turn is not in this set and must not be added to it. `scripts/probe_supervisor.py`
composes it from the canary value it reads at startup and sends it as its own user turn; the
target turn below is sent only after that turn's canary came back. Prefixing a canary preamble to
an entry by hand would send the target before the canary result could stop it, which is the
failure the staged supervisor exists to prevent.

**Identifiers are append-only.** Adding a question appends a new `Q##`; an existing question is
never renumbered, so a given identifier means the same prompt text in every round.

**A question names the behavior it is *aimed at*, not one it is guaranteed to produce.** Whether a
behavior is reachable against the fixture is measured separately (see `Reachability` below);
whether the model chooses the call that reaches it is not knowable in advance at all. A trial is
complete when its record faithfully states what happened — a trial whose model took a narrower
call is a valid observation, recorded as such.

Prompt text is issued verbatim. It is deliberately written the way an operator would ask, without
naming a tool or a parameter, because what the loop measures is whether the tool descriptions are
enough for a low-tier model to choose correctly.

## Questions

### Q01 — CJK keyword retrieval

- **Prompt:** `找一下信件主旨或內文有「探針測試」的郵件，告訴我寄件者和日期。`
- **Expected tool:** `mail_search`
- **Designed for:** ordinary keyword search over CJK content, which is segmented per character
- **Acceptance:** a `mail_search` call whose filters include the CJK term, returning the fixture's
  CJK message

### Q02 — Integer identifier

- **Prompt:** `Show me the full contents of message 1, including who it was sent to.`
- **Expected tool:** `mail_show`
- **Designed for:** the `#54` failure shape — an identifier the description types as an integer,
  which a client may stringify
- **Acceptance:** a `mail_show` call carrying the identifier; the record states the argument type
  actually sent, since a numeric string rather than an integer is the observation this question
  exists to capture

### Q03 — Thread retrieval

- **Prompt:** `There is a conversation in the archive with the subject "Probe oversized thread 1".
  Pull up that whole conversation for me.`
- **Expected tool:** `mail_thread`
- **Designed for:** the per-tool degradation ladder — the fixture's thread carries three 60,000
  character bodies, whose assembled delivery exceeds the response budget
- **Acceptance:** a `mail_thread` call reaching the fixture thread; the record states whether the
  payload came back degraded along the ladder or by some narrower route the model chose

### Q04 — Contact history

- **Prompt:** `What is my correspondence history with probe@example.invalid? Summarise how many
  messages there are and over what period.`
- **Expected tool:** `mail_contact_history`
- **Designed for:** the aggregate-summary tool, which is the one most easily confused with a plain
  sender search
- **Acceptance:** a `mail_contact_history` call for that address. A `mail_search` call filtered by
  sender is a *recorded divergence*, not a pass — it is the description-ambiguity signal this
  question exists to surface

### Q05 — Attachment lookup

- **Prompt:** `Did I ever receive a PDF called probe-report? Tell me which message it came with.`
- **Expected tool:** `mail_attachments`
- **Designed for:** attachment-filename search, which the archive indexes by name only
- **Acceptance:** a `mail_attachments` call naming the attachment; the record notes if the model
  instead reached for `mail_search`'s `attachment_name` filter, which is a legitimate alternative
  and a divergence worth recording

### Q06 — Oversized single record

- **Prompt:** `Open the message whose subject is "Probe oversized record" and show me its body.`
- **Expected tool:** `mail_show`
- **Designed for:** the per-tool degradation ladder on a single record — the fixture's body is
  150,000 characters against a 120,000 character budget
- **Acceptance:** a call reaching that record; the record states which ladder state was delivered,
  or that the model narrowed the request instead. This behavior is **measured reachable** (see
  below), so a trial that does not reach it is an observation about the model, not about the
  fixture

### Q07 — Broad search

- **Prompt:** `Show me every message in the archive from probe@example.invalid, with their full
  message bodies.`
- **Expected tool:** `mail_search`
- **Designed for:** the over-budget **rejection** — `mail_search` refuses rather than degrading,
  because its frozen contract cannot honestly shorten a bare list. This is a different behavior
  from Q03 and Q06 and must never be recorded as a degradation
- **Acceptance:** a `mail_search` call broad enough to be measured against the budget; the record
  states whether the server raised the over-budget rejection, degraded (it must not), or whether
  the model's call stayed within budget

## Reachability against the current fixture

Measured through the server's own assembly and measurement path — the same one
`tests/test_probe_fixture.py` uses — not asserted from the question's intent.

| Question | Designed behavior | Reachable against this fixture |
|---|---|---|
| Q03 | thread degradation ladder | Yes — guaranteed by the harness fixture requirement |
| Q06 | single-record degradation ladder | Yes — guaranteed by the harness fixture requirement |
| Q07 | `mail_search` over-budget rejection | See `docs/mcp-client-probe-loop.md` — measured, not guaranteed |

The harness fixture requirement covers an oversized record and an oversized thread. It says
nothing about aggregate search size, and the generator writes six messages, so Q07's reachability
is a property of this fixture that had to be measured rather than assumed. The measurement and its
outcome are recorded in the protocol document. If it is not reachable, Q07 stays in this set
carrying that mark and the shortfall belongs to the change that owns the fixture generator.

## Coverage

Every read-only tool the server registers is exercised by at least one question:

| Tool | Questions |
|---|---|
| `mail_search` | Q01, Q07 |
| `mail_show` | Q02, Q06 |
| `mail_thread` | Q03 |
| `mail_contact_history` | Q04 |
| `mail_attachments` | Q05 |
