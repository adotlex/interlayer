# Interlayer — Handoff Plan

**Objective.** Identify clusters of LinkedIn users in the operator's own direct (1st-degree)
network who are adjacent to Jane Street and Citadel people.

**Status of this document.** Written at Wave 1 launch. Wave 1 findings may amend the
constraints below; the authoritative build contract is `docs/SETUP_GUIDE.md`, produced by
synthesising Wave 1 output.

---

## 1. The load-bearing constraint

LinkedIn exposes **no** interface — official API, partner API, or export — that returns another
member's connection list. A third party's edge set is private. Therefore:

> The literal request ("users in my network who are in Jane Street's employees' networks")
> requires data that does not exist in any obtainable form.

The project therefore ships an **inference engine, not an observation engine**. It reconstructs
*likely* adjacency from affiliation evidence the operator legitimately holds, attaches a
calibrated confidence to every claim, and labels every edge as inferred rather than observed.
Wave 1 / Agent 1 is charged with confirming or refuting this ceiling with citations; if a
compliant path to true edges exists, the architecture absorbs it through the adapter seam
described in §4.

## 2. Execution model

Three waves of parallel Opus agents, with a synthesis step between waves 1 and 2 and a
repair loop after wave 3. Waves are barriers: every agent in a wave completes before the next
wave launches, because each wave consumes the previous wave's artifacts.

```
Wave 1  research  ──5 agents──▶  docs/research/*.md
                                      │
                              synthesis (orchestrator)
                                      │
                                 docs/SETUP_GUIDE.md
                                      │
Wave 2  build     ──N agents──▶  src/interlayer/**
                                      │
Wave 3  test      ──N agents──▶  tests/**
                                      │
                              ┌───────▼────────┐
                              │  pytest (full) │
                              └───────┬────────┘
                                      │ red
                              repair ─┴─▶ pytest --lf ──▶ repeat until green
                                      │ green
                                   complete
```

### Wave 1 — research (parallel, 5 agents)

| # | Agent | Question it must answer | Artifact |
|---|-------|------------------------|----------|
| 1 | LinkedIn data access | What data is obtainable, by what mechanism, at what ToS/legal risk? Exact export filenames and column headers. Verdict on the true-edge ceiling. | `docs/research/01-linkedin-data-access.md` |
| 2 | Graph & clustering | Bipartite affiliation model, projection weighting, backbone extraction, Leiden vs Louvain, determinism, proximity scoring, evaluation without ground truth. | `docs/research/02-graph-clustering.md` |
| 3 | Project stack | Layout, dependency manager, pinned versions verified to install, shared `models.py` contract, CLI surface, test tooling, zero-overlap module ownership. | `docs/research/03-project-stack.md` |
| 4 | Entity resolution | Citadel LLC vs Citadel Securities vs The Citadel (college) disambiguation; Jane Street variants; normalisation; benchmarked fuzzy thresholds; the firm gazetteer. | `docs/research/04-entity-resolution.md` + `data/gazetteer/firms.yaml` |
| 5 | Privacy & compliance | Controller obligations, engineering guardrails, redaction, retention, scraping posture, false-positive harm framing. | `docs/research/05-privacy-compliance.md` |

Each agent verifies claims by executing commands in the sandbox where verification is
possible, and states uncertainty explicitly rather than guessing.

### Synthesis — orchestrator

Read all five documents, resolve contradictions between them, and emit `docs/SETUP_GUIDE.md`
containing the frozen build contract: directory tree, pinned `pyproject.toml`, the complete
shared `models.py`, per-agent file ownership with **zero overlap**, and the exact public
function signature each module must expose so that modules compose without any agent having
read another agent's code.

### Wave 2 — build (parallel)

One agent per module. Agents share only `models.py` (written by the orchestrator during
synthesis, read-only to all builders) and the setup guide. Overlap is prevented structurally:
no two agents are assigned the same file, and no agent may edit `models.py`.

### Wave 3 — test (parallel)

One agent per test domain. Test agents may read all source but write only into their own
assigned test files. Their brief is adversarial: find real defects, not confirm the happy path.

### Repair loop — orchestrator

1. `pytest -q` full suite.
2. If red: triage each failure to a root cause. Fix the source, not the assertion, unless the
   assertion is genuinely wrong — in which case say so explicitly and justify.
3. `pytest --lf` to re-run **only** the failures.
4. Repeat 2–3 until `--lf` is green, then run the **full** suite again to catch regressions.
5. Plan is complete only on a full-suite green run.

Never skip, `xfail`, or delete a test to reach green.

## 3. Definition of done

- [ ] Every Wave 1 artifact written and internally consistent.
- [ ] `docs/SETUP_GUIDE.md` frozen before Wave 2 launches.
- [ ] Every module implemented against the frozen contract; package imports cleanly.
- [ ] Full end-to-end run on synthetic fixture data produces a ranked cluster report.
- [ ] Full test suite green, including clustering correctness against planted ground truth.
- [ ] Every output row carries a confidence score and provenance.
- [ ] No personal data committed; no network egress in the core pipeline.
- [ ] Pushed to `claude/linkedin-network-clusters-qtjz0s`.

## 4. Architectural seams fixed in advance

These are decided now so that parallel agents cannot diverge on them.

**Pipeline stages are pure and file-to-file.** Each stage reads artifacts and writes artifacts;
no stage holds another stage's internals. This makes stages independently testable and lets
build agents work without coordination.

```
ingest → normalize → build-graph → cluster → score → report
```

**Data sources sit behind an adapter interface.** The export adapter is the only one shipped.
Any other source — a licensed dataset, a permitted enterprise API — plugs into the same
interface without touching the pipeline. No scraper ships in this repository.

**Every derived claim carries provenance.** A person is flagged because of specific evidence;
that evidence is retained and rendered. A result the operator cannot audit is a result they
cannot act on.

**Inferred means inferred.** Edges and scores are labelled as inference throughout the output
surface, never presented as observed fact.
