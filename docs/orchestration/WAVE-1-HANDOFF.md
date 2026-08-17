# Wave 1 Handoff Plan — Research

**Status:** dispatched
**Executors:** 6 × Opus 5 (high effort) subagents, parallel, read-only research
**Objective:** map the full option space for `interlayer` so Wave 2 can build without re-litigating decisions.

## Context given to every Wave 1 agent

- Repo `adotlex/interlayer` is greenfield: `README.md` (one line), one commit, no code, no config.
- Environment: Node v22.22.2, npm 10.9.7, pnpm 10.33.0, Python 3.11.15, uv 0.8.17, Go 1.24.7,
  Cargo 1.94.1, Docker 29.3.1. 4 CPUs, 15 GiB RAM. Outbound HTTPS via agent proxy.
- Product hypothesis to validate or refute: **interlayer is a typed interoperability layer** —
  one stable application-facing interface over many interchangeable backend providers,
  with resilience (retry / timeout / circuit breaker / rate limit) and routing/fallback built in.

## Hard constraints Wave 1 must design within

| # | Constraint | Why |
|---|---|---|
| C1 | Wave 2 agents build **in parallel** | Module boundaries must be file-disjoint — one owner per file, no shared-file edits |
| C2 | Wave 3 must re-run **only failed tests** | Test runner must support precise per-file and per-test-name selection from the CLI |
| C3 | Deterministic tests | No wall-clock sleeps, no network, no randomness without injected seed/clock |
| C4 | Zero or near-zero runtime dependencies | Supply chain surface, install time, and offline reproducibility |
| C5 | Everything runs offline after install | The green/repair loop cannot depend on network flakiness |

## Research streams

| ID | Stream | Must answer |
|---|---|---|
| R1 | Runtime, language, build & packaging | TS/Node vs Go vs Rust vs Python; module format (ESM/CJS/dual); build tool; tsconfig strictness; Node version floor |
| R2 | Test framework & **selective re-run** | Exact CLI syntax to re-run one file / one test by name; machine-readable result output (JSON) to drive the repair loop; coverage; parallelism on 4 CPUs; fake timers |
| R3 | Architecture patterns | Registry/adapter/provider abstraction shapes; capability negotiation; typed error taxonomy; middleware composition; config validation |
| R4 | Resilience algorithm semantics | Exponential backoff + jitter variants; circuit breaker state machine + thresholds; token vs leaky bucket; timeout/cancellation via AbortSignal; precise edge-case behaviour to test |
| R5 | Quality gates & parallel-safe layout | Lint/format/typecheck choice; CI on GitHub Actions; **file-ownership map** enabling ≥6 disjoint parallel build units |
| R6 | Prior art & public API ergonomics | Comparable libraries; what their APIs get right/wrong; naming of the public surface; DX pitfalls to avoid |

## Deliverable contract (identical for all six)

Each agent writes `docs/orchestration/findings/R<N>-<slug>.md` containing:

1. **Options considered** — table with trade-offs, not prose.
2. **Recommendation** — one choice, stated plainly.
3. **Rationale** — tied to constraints C1–C5 above.
4. **Rejected and why** — so Wave 2 does not revisit.
5. **Concrete specifics** — exact versions, exact CLI flags, exact config snippets. No hand-waving.
6. **Risks** — what could bite Wave 2 or Wave 3.

Agents return a ≤400-word summary as their final message. The findings file is the durable artifact.

## Exit criteria

All six findings files present, recommendations mutually consistent (or conflicts explicitly
surfaced for the orchestrator to resolve), and enough specificity that the Wave 2 setup guide
can be written without further research.
