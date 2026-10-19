# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This is a **single-context** repo: one glossary, decisions under `docs/decisions/`. There is no `GLOSSARY-MAP.md` and no per-package context.

**The glossary lives at the repo root: `GLOSSARY.md`.** That is exactly the file the engineering skills (`/domain-modeling`, `/grilling`, `/grill-with-docs`, `/wait-what`) read and maintain natively. It was `openspec/LANGUAGE.md` until 2026-08-20, when the Spectra chain was retired and the file moved ([D-20260820-3](../decisions/D-20260820-3-migrate-to-contract-anchored-flowback.md)). There is exactly one glossary — never create a second one.

## Before exploring, read these

- **`AGENTS.md`** at the repo root (`CLAUDE.md` only imports it) — the project's own orientation doc (vision, architecture map, DB schema, conventions, the `## Behaviour contracts` behavioral canon). Read it first; it is the highest-signal file in the repo.
- **`GLOSSARY.md`** — the glossary / ubiquitous language. Terms carry `Definition` / `Avoid` / `Why` (with Owner + SoT).
- **`docs/decisions/`** — this repo's ADRs. Read the ones that touch the area you're about to work in. **Note the path: `docs/decisions/`, not `docs/adr/`.**
- **`docs/INDEX.md`** — generated index of everything under `docs/`. Use it to find the relevant topic doc instead of globbing.

## ADR conventions in this repo

ADRs are one decision per file, named `docs/decisions/D-YYYYMMDD-N-<slug>.md` (`N` = same-day sequence number).

`docs/decisions.md` is a **generated index — never edit it by hand**. After adding or renaming an ADR:

```bash
uv run python scripts/gen_doc_index.py --target all
```

Index freshness is maintained by a harness hook (session start + after any write under `docs/`), so it is no longer a commit gate — running the command above just applies the refresh immediately.

## Where behavior questions go

The current behavioral canon is **AGENTS.md `## Behaviour contracts`** (each entry bound to an anchor test) plus the code itself. The pre-2026-08-20 Spectra capability contracts are not carried in this repo ([D-20261010-1](../decisions/D-20261010-1-showcase-ships-ahead-of-remoteization-in-a-fresh-repo.md)). `docs/decisions/` holds *why a path was chosen over alternatives*.

- A behavior question → read `## Behaviour contracts`, then the code.
- A "why is it built this way / can I change it" question → read the ADR.

## File structure

```
/
├── AGENTS.md                       ← project orientation + behaviour contracts (read first)
├── CLAUDE.md                       ← one line: @AGENTS.md
├── GLOSSARY.md                      ← glossary / ubiquitous language
├── docs/
│   ├── INDEX.md                    ← generated
│   ├── decisions.md                ← generated ADR index, do not hand-edit
│   └── decisions/
│       ├── D-20260525-1-no-wsl-write-to-mnt-drive.md
│       └── D-20260820-3-migrate-to-contract-anchored-flowback.md
└── src/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Each entry lists `Avoid` synonyms explicitly — using one of those is drift, not a stylistic choice.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (surface it; `/domain-modeling` maintains the glossary as terms are resolved in discussion).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts D-20260602-1 (native Windows daily sync) — but worth reopening because…_
