# Interlayer — Handoff Plan

## Objective

Identify **clusters of people in my LinkedIn 1st-degree network who are also in the
1st-degree network of Jane Street and Citadel / Citadel Securities employees.**

Restated as a graph problem:

- `M` = my 1st-degree connections (known, exportable).
- `T` = target-firm people (Jane Street, Citadel, Citadel Securities employees).
- Edge `(m, t)` exists iff `m` is a 1st-degree connection of `t`.
- The set of interest is `B = { m ∈ M : ∃ t ∈ T, (m,t) ∈ E }` — the **bridges**.
- The deliverable is not the flat set `B` but its **cluster structure**: which bridges
  co-occur around the same target people / desks / cohorts, so the user can see
  "these 6 people all sit around the same Jane Street quant-dev pocket" rather than
  a 300-row list.

### The one structural fact that shapes the whole design

LinkedIn does **not** expose another person's connection list. `t`'s connections are
private. But LinkedIn *does* expose, for any profile you can view, the set of
**mutual connections** between you and them — and that set is *exactly*
`M ∩ connections(t)`, which is exactly the edge set we need. So the graph is
obtainable in principle, one target at a time, from the viewer's own account.
Everything downstream (volume, legality, automation posture) follows from that.

## Constraints held throughout

1. **Compliance is a first-class requirement, not a footnote.** LinkedIn's User
   Agreement prohibits automated scraping. `hiQ v. LinkedIn` settled with hiQ
   enjoined; LinkedIn has since pursued data vendors. The system must therefore be
   architected so that the *analysis engine* is fully separable from the
   *acquisition* step, and every acquisition adapter declares its compliance posture
   explicitly. Default configuration must be the fully-compliant one.
2. **The user's own data is unambiguously theirs.** LinkedIn's official data export
   (`Settings → Data privacy → Get a copy of your data`) yields `Connections.csv`.
   That is a first-party, sanctioned, zero-risk source and must be the backbone.
3. **Third parties (JS/Citadel employees) are data subjects.** GDPR/CCPA apply to
   personal data held about them. Design for minimisation, local-only storage, and
   deletion.
4. **Degrade gracefully.** If the true bipartite edge set is unavailable, the system
   must still produce value from *inferred affinity* (shared employer, shared school,
   overlapping tenure, shared group) with the inference clearly labelled as inference.

## Execution model

Three waves of parallel Opus 5 subagents, with a repair loop on the third.

| Wave | Purpose | Agents | Output |
|------|---------|--------|--------|
| 1 | Research all available options | 5 parallel | `docs/research/R*.md` |
| 2 | Build, per the synthesized setup guide | 5 parallel | `src/`, `tests/` |
| 3 | Comprehensive test suite | 4 parallel | test results |
| 3b | Repair loop | as needed | re-run **only** failed tests until green |

Between waves 1 and 2 the orchestrator synthesizes `docs/01-setup-guide.md`, which
is the sole contract Wave 2 builds against.

Agent configuration: `subagent_type: general-purpose`, `model: opus`. Reasoning
effort is inherited from the orchestrating session rather than set per-agent (the
Agent tool exposes `model` but not `effort`; `effort` is a Workflow-only parameter
and workflows were not requested).

## Wave 1 — research briefs

| ID | Brief | Output file |
|----|-------|-------------|
| R1 | First-party LinkedIn surfaces: data export schema, official APIs, Sales Navigator / Recruiter capabilities, ToS + case law | `R1-first-party-linkedin.md` |
| R2 | Third-party data providers and enrichment APIs: who actually sells connection-graph data, operating status, pricing, legal posture | `R2-third-party-providers.md` |
| R3 | Graph methodology: bipartite projection, community detection, bridge/brokerage scoring, library selection for Python 3.11 | `R3-graph-methodology.md` |
| R4 | Acquisition mechanics for mutual connections specifically: URL surfaces, degree visibility, caps, browser-side capture, automation risk | `R4-acquisition-mechanics.md` |
| R5 | Target-firm enumeration and entity resolution: JS/Citadel/Citadel Securities corporate structure, employee identification, name matching, privacy obligations | `R5-targets-and-privacy.md` |

Each research agent writes a decision-grade document: options table, recommendation,
rejected alternatives with reasons, and a "what this means for the build" section
with concrete library/schema/interface proposals.

## Definition of done

- `docs/01-setup-guide.md` exists and is derived from Wave 1 findings.
- The pipeline runs end-to-end on synthetic fixtures with no network access.
- Full test suite green, including compliance guardrail tests.
- Only failed tests are re-run during repair; a repair is not accepted until the
  previously-failing test passes *and* the full suite still passes.
