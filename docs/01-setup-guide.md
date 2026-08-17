# Interlayer — Setup Guide (Wave 2 build contract)

**Status:** authoritative. Wave 2 builds against *this* document. Where it conflicts with a
Wave 1 research doc, this document wins — the conflicts were adjudicated deliberately and the
reasoning is recorded in §2.

**Wave 1 inputs:** `docs/research/R1…R5` (5,199 lines). Each work package below names the
sections its owner must read in full. Do not read all five documents; read your own.

---

## 1. What Wave 1 established

Five findings govern the architecture. Everything else is detail.

1. **The bipartite edge set cannot be bought.** (R2) No provider sells LinkedIn
   connection-graph or mutual-connection data, because a member's connection list is never
   rendered on a logged-out surface and mutuals are computed server-side per-viewer. There is
   no artefact to scrape. Vendors sell `connections_count` — one integer, censored at "500+",
   therefore saturated and information-free for senior finance staff. `provides_connection_edges`
   must exist as a capability flag and must be `False` on every shipped adapter.
2. **It cannot be obtained from first-party exports or APIs either.** (R1) The archive, the DMA
   portability API and a GDPR Art. 15 DSAR all return first-person data only. No API tier or
   price unlocks a connection-graph scope. `Connections.csv` gives us `M` and **zero edges**.
3. **The only source is the user's own authenticated session, and quota — not time — is the
   binding constraint.** (R4) The monthly Commercial Use Limit caps *every* acquisition mode at
   the same ~150–250 targets/month. Automation buys speed inside an identical ceiling while
   adding account-loss risk. This is why the tool ships no scraper: not caution, arithmetic.
4. **Two query strategies compose, and both are needed.** (R1 + R4)
   - *Inversion:* `Connections of = m` × `Current company ∈ targets`, once per `m ∈ M`. The
     "must be 1st-degree" constraint on that filter is automatically satisfied by every element
     of `M`. Filters server-side, so non-targets are never retrieved.
   - *Mutuals:* the shared-connections view on a target's profile. Survives the target's
     connection-privacy setting (every mutual is already the viewer's own 1st-degree
     connection), so it costs **zero** coverage — and it backfills the inversion's blind spot,
     which is connections who hid their own lists.
   - Degree is a free lossless pruning oracle: 3rd-degree ⇒ exactly zero shared connections.
5. **Fuzzy matching cannot resolve employer strings, and this was measured, not reasoned.** (R5)
   All eight `rapidfuzz` scorers fail against a 28-positive / 26-negative set.
   `token_set_ratio` scores `Jane Street Capital LLC` **100** and `Jane Street Entertainment`
   **100**. `partial_ratio` scores `The Citadel` **100**. No threshold separates them.

### Research confidence caveat — carry this into the code

This session's egress policy blocks `linkedin.com`, Microsoft Learn and Wikipedia (verified:
all return `000`; only PyPI and server-side search are reachable). **No agent could read
LinkedIn's own docs, ToS text or UI directly.** Every claim about URL parameters, filter
semantics and ToS section numbers is second-hand from sources quoting primary material. R1
tagged facts HIGH/MED/LOW and marked three `[VERIFY IN-PRODUCT]`.

Consequences that are binding on Wave 2:

- **No parser may hard-code a LinkedIn URL shape, query parameter name, or DOM/JSON path as a
  bare literal.** Put them in a single declarative table (`interlayer/collect/schemas.py`) with
  a version stamp and a `verified: bool`, and make unknown shapes a clear diagnostic rather
  than a crash or a silent empty result.
- Any doc we ship states plainly that LinkedIn's UI may have changed since 2026-08-17.

---

## 2. Conflicts between research docs — adjudicated

| # | Conflict | Ruling |
|---|---|---|
| C1 | R3 §pipeline step 1 says `skiprows` the `Connections.csv` preamble; R1 says the preamble length varies and must be sniffed. | **R1 wins.** Sniff for the row beginning `First Name`. A fixed `skiprows=3` is a silent-corruption bug. Fixture set must include 0-, 2-, 3- and 5-line preambles. |
| C2 | R3 step 3 recommends `rapidfuzz` `WRatio ≥ 92` for name matching; R5 rejects fuzzy matching and rejects `unidecode`/`nameparser`. | **Split by object type — they are matching different things.** *Company strings:* R5's deterministic ladder is authoritative and fuzzy is a typo-only backstop. *Person names:* R5's order-free sorted-token key is the identity/blocking key; R3's `WRatio ≥ 92` runs only *within* a block to rank review-queue candidates, never to auto-accept across blocks. NFKD accent-folding is retained (it is not `unidecode`: `é→e`, but `李明` stays `李明`). |
| C3 | R1 makes the Sales Navigator inversion primary; R4 makes HAR capture primary. | **Not a conflict — different layers.** Inversion is the *query strategy* the user runs; HAR is the *capture mechanism* for the results. Both terminate in the same `Collector` interface. Documented together in the operator runbook. |
| C4 | R1 assumes Sales Navigator CSV export exists; R1 itself then reports it was removed 2026-07-01. | **Removed.** Build no Sales Navigator CSV parser. Capture is paste-based or HAR. |
| C5 | R2 recommends Bright Data as the P1 network adapter; PRIV-04 requires only `first_party_export` enabled by default. | **Both hold.** Bright Data ships as *available but disabled*. Default config enables `first_party_export` only. Enabling a network adapter is an explicit, logged user act. |

---

## 3. Architecture

Two hard boundaries, both enforced by tests.

```
        ACQUISITION  (user-driven, optional, may touch network)
        ─────────────────────────────────────────────────────────
        collect/     manual CSV · HAR parser · paste parser
        enrichment/  null · local_file · brightdata · coresignal
                              │
                              │  Person / Target / Edge records only
                              ▼
        ─────────────────────────────────────────────────────────
        ANALYSIS  (pure, offline, deterministic — no network, ever)
        core/     models · config · store · audit · retention
        ingest/   csv parsing · target registry · entity resolution
        graph/    bipartite · projection · clustering · brokerage
        report/   markdown · html · json
        cli.py
```

- **Boundary 1 — network.** Nothing under `core/`, `ingest/`, `graph/`, `report/` may import
  `httpx`, `requests`, `urllib.request` or `socket`. Enforced by PRIV-01/02.
- **Boundary 2 — provenance.** Every record crossing into the analysis layer carries
  `source`, `collected_at`, `observed: bool`. An inferred value may never be presented as an
  observed one (PRIV-19).

### Layout

```
src/interlayer/
  __init__.py
  core/       models.py  ids.py  config.py  store.py  audit.py  retention.py  errors.py
  ingest/     connections_csv.py  targets.py  company_match.py  person_match.py  review.py
  graph/      build.py  project.py  cluster.py  brokerage.py  score.py  label.py
              inference.py  nextsteps.py  fingerprint.py
  collect/    interface.py  schemas.py  manual_csv.py  har.py  registry.py
  enrichment/ interface.py  normalize.py  cache.py  ratelimit.py  budget.py  registry.py
              providers/{null,local_file,brightdata,coresignal}.py
  report/     markdown.py  html.py  json_out.py  redact.py
  cli.py
data/targets.yaml
tests/
```

### The shared contract

`src/interlayer/core/models.py`, `core/ids.py` and `pyproject.toml` are **written by the
orchestrator before Wave 2 launches** and are frozen for the duration of the wave. Every work
package imports them. No Wave 2 agent may edit them; if one is wrong, or you need a dependency
added, report it in `docs/wave2-notes/<your-id>.md` and the orchestrator amends it once,
centrally. This is what makes five agents safe to run in parallel.

A working virtualenv already exists at `.venv/` with the full pinned set installed and
`interlayer` installed editable. Use `.venv/bin/python` and `.venv/bin/pytest` directly — do
not create another venv, and do not run bare `pytest`.

### Determinism rules — mandatory, non-negotiable

R3 proved a fixed Leiden seed is **not** sufficient: with `seed=42` held constant, 12 vertex
permutations of one graph produced **12 different partitions**. Therefore:

1. Nodes are added in sorted ID order. Always.
2. Edges are emitted sorted as `(min_idx, max_idx, weight)`.
3. Projection weights are rounded to **12 dp** before clustering.
4. Clustering is consensus Leiden: 25 seeds → co-association matrix → threshold τ=0.5 →
   final Leiden at `base_seed`.
5. All ties everywhere break by ascending `member_id` / `target_id`.
6. `graph/fingerprint.py` emits a SHA-256 over the canonical result. Identical inputs must give
   an identical fingerprint across processes, venvs and `PYTHONHASHSEED` values.

Never call `networkx.algorithms.bipartite.sets()` — it raises `AmbiguousSolution` on
disconnected graphs, and ours is always disconnected. Pass the explicit member-node set.

### Verified dependency set (R3, empirically installed on Python 3.11.15)

```
networkx==3.6.1  python-igraph==1.0.0  leidenalg==0.12.0  numpy==2.4.6
scipy==1.17.1  pandas==3.0.5  rapidfuzz==3.14.5  scikit-learn==1.9.0
```

Resolves to 15 packages, all prebuilt wheels, no compiler. `graph-tool` is uninstallable via
pip; `cdlib` drags in ~30 packages — both rejected. Use **igraph** for betweenness: networkx is
~40× slower (70.7s vs 1.8s at n=2,000) and its k-sample approximation is both slower *and* less
accurate at n=8,000.

Add for the CLI/report/test layers: `typer`, `pyyaml`, `jinja2`, `pytest`, `pytest-cov`, `ruff`, `mypy`.

---

## 4. Work packages

Five agents, parallel, disjoint file ownership. **Own only your files.** If you need a change in
someone else's, write it to `docs/wave2-notes/<your-id>.md` instead.

### B1 — core: models, config, store, audit, retention, purge

**Read:** R5 §Privacy controls spec (lines 972–1062) in full; R5 §Implications.
**Owns:** `core/{config,store,audit,retention,errors}.py`, `PRIVACY.md`.
(`models.py` and `ids.py` are pre-written and frozen — import, do not edit.)

Deliver: `Config` with `state_root` (default `~/.interlayer/`), `retention_days=90`,
`retain_emails=False`, `enabled_adapters=["first_party_export"]`, `include_inferred=False`,
`allow_contact_fields=False`; a SQLite store under the state root; append-only JSONL audit log
carrying counts and identifiers but **never names**; startup retention sweep; `purge --all`,
`purge --person`, `purge --target` with tombstones that survive re-ingest.

Satisfies PRIV-03, 04, 08, 10, 11, 12, 13, 14, 15, 16, 21.

### B2 — ingest and entity resolution

**Read:** R5 §Target firm profiles, §Proposed target registry (lines 457–821), §Entity
resolution spec (822–971), §Appendix fixture (1197–1314). R1 §Implications (392–565) for the
CSV schema.
**Owns:** `ingest/*`, `data/targets.yaml`, `tests/fixtures/company_strings.yaml`.

Deliver: `Connections.csv` parser that **sniffs** the header row (C1); field whitelist
(PRIV-05); email discarded at parse time unless opted in (PRIV-06); `targets.yaml` shipped
verbatim from R5's registry block, including `jane-street-global` and the `janestreetgroup`
negative pattern; the deterministic company-match ladder returning
`confident | needs_review | no_match`, where `needs_review` **cannot enter the graph**
(PRIV-20); order-free sorted-token person keys, no `unidecode`, no `nameparser` (C2).

Port R5's 100-case fixture verbatim and assert 100/100.

### B3 — graph engine

**Read:** R3 §Recommended pipeline (686–733), §Implications (834–1003), §Test specifications
(1004–1123).
**Owns:** `graph/*`.

Deliver R3 pipeline steps 5–14 exactly: bridge restriction, sparse RA projection
`P = A @ diags(1/max(d_T,1)) @ A.T`, deterministic igraph conversion, consensus Leiden,
brokerage (reach, rarity, igraph betweenness, effective_size, constraint → autonomy),
percentile-ranked composite `0.35·reach + 0.25·rarity + 0.15·betweenness + 0.15·effective_size
+ 0.10·autonomy`, TF-IDF cluster labels with the `" | "` sentinel guard, Wilson lower bound,
next-harvest priorities, fingerprint.

Every golden value in R3 §T1 and §T4 must reproduce exactly. Inferred edges are excluded unless
`include_inferred=True` (PRIV-19, T6).

### B4 — collectors and enrichment adapters

**Read:** R4 §Proposed input schemas (605–814), §Implications (815–1046). R2 §Implications
(494–829).
**Owns:** `collect/*`, `enrichment/*`.

Deliver: `Collector` protocol with `compliance_posture ∈ {first_party_export, manual_capture,
third_party_api, automated}`; **`schemas.py` as the single declarative table of all LinkedIn
URL/JSON shapes**, version-stamped and `verified: bool` (see §1 caveat); hand-fillable CSV
collector; offline HAR parser; `EnrichmentProvider` protocol with
`provides_connection_edges: bool` **`False` everywhere**; `null` and `local_file` providers
(P0, offline); Bright Data and Coresignal present but **disabled by default** (C5); SQLite
cache, token-bucket rate limit, budget guard; contact fields dropped at the adapter boundary.

**Build no adapter that accepts a LinkedIn session cookie.** Ship a test asserting no code path
reads `li_at`.

### B5 — CLI, reporting, packaging, runbook

**Read:** R4 §Recommendation (532–604) and §Coverage economics (1047–1126) for the runbook. R1
§Recommendation (352–391).
**Owns:** `cli.py`, `report/*`, `pyproject.toml`, `README.md`, `docs/02-operator-runbook.md`.

Deliver: `typer` CLI — `init`, `ingest`, `targets`, `review`, `analyse`, `report`, `next`,
`privacy`, `purge`; markdown/HTML/JSON renderers with unconditional email+phone redaction
(PRIV-17), the mandatory report header block (PRIV-18), and visual distinction of inferred
values (PRIV-19); `pyproject.toml` with the §3 pins; a runbook covering the inversion strategy,
the mutuals backfill, degree pruning, quota arithmetic, and the honest risk gradient — R4
established that §8.2(4) technically reaches even manual copying, so **do not claim any path is
cleanly "compliant."** Present a gradient, state the Sales Navigator Core cost ($119.99/mo,
30-day trial, *not* Advanced), and let the user decide.

---

## 5. Acceptance criteria

The plan is complete when all hold:

1. `pytest` green, no skips outside the `slow` marker.
2. Pipeline runs end-to-end on synthetic fixtures **with `socket` blocked**.
3. Every R3 golden value (T1, T3–T9) reproduces exactly.
4. Fingerprint stable across 5 input orderings and `PYTHONHASHSEED ∈ {0,1,999}`, and the T2.4
   negative control **fails without** the sorting rule.
5. All 21 PRIV controls have a passing test.
6. R5's 100-case company fixture: 100/100.
7. `ruff check` and `mypy src/` clean.
8. No adapter reports `provides_connection_edges is True`; no code path reads `li_at`.
9. `README.md`, `PRIVACY.md`, `docs/02-operator-runbook.md` present and non-empty.
