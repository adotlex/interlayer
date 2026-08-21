# Interlayer — Build Setup Guide (Wave 2 contract)

**Status: FROZEN.** Wave 1 is complete; this document is the synthesis of
`docs/research/01`–`05` and supersedes them where they disagree. Wave 2 agents build
against this and nothing else.

---

## 1. What this tool does, precisely

Wave 1 established what is and is not obtainable, and it changed the shape of the product.

**The full edge set of a target firm's employees is unobtainable.** The LinkedIn export is
depth-1 by construction; no API returns member-to-member edges (the Connections API says so
verbatim, and it is partner-gated anyway); no vendor sells edge data; and the remaining route
is prohibited scraping. Proxycurl, the best-known vendor, was sued in January 2025 and shut
down that July. hiQ — routinely cited as having legalised scraping — **lost** on breach of
contract and ended with a $500,000 judgment and an order to destroy the data.

**But the project never needed the full edge set.** It needs

```
{ the operator's connections } ∩ { a target employee's connections }
```

and LinkedIn renders exactly that, as an enumerable list, on any 2nd-degree profile, for free —
and it **survives the target hiding their connections list**, which is the privacy setting
quant-finance professionals most commonly enable. That is ground truth, not a proxy.

So the tool runs on two tiers, and the distinction is structural in the type system, not a
comment:

| Tier | Source | Provenance | What it yields |
|---|---|---|---|
| **1** | `Connections.csv` from the official export | `INFERRED` | Affiliation-based adjacency: who among your connections works, or worked, at a target or alongside its people |
| **2** | Mutual-connection lists a human read and recorded | `OBSERVED` | True bridge edges: this connection of yours *is* connected to that Jane Street employee |

Tier 1 works with zero manual effort and is always wrong sometimes. Tier 2 requires human
collection and is right by construction. The tool ships no scraper; Tier 2 data enters through
a documented adapter.

---

## 2. Frozen decisions

Do not relitigate these. Each is traceable to a Wave 1 finding.

| Decision | Why |
|---|---|
| `src/` layout, package `interlayer`, hatchling backend | Verified building sdist+wheel in 0.73s |
| `uv` + `pyproject.toml` + `uv.lock` | Cold sync 5.9s, no `--system` needed |
| pydantic v2, **all models frozen** | Immutability removes a whole class of cross-stage bug |
| typer CLI, JSONL between stages | Stages stay independently runnable and testable |
| `leidenalg.find_partition(seed=…)` | **Never** `igraph.community_leiden` — it has no seed parameter and was observed diverging across repeated calls |
| Consensus clustering, 50 runs | Single-run Leiden flaps: 20 seeds gave mean pairwise ARI 0.877, cluster counts 66–70. Consensus lifts cross-ensemble ARI to ~1.0 *and* improves NMI |
| Resolution γ = 2.0 (sweep 1/2/4) | γ=1.0 returned clusters of 52–118 people, roughly 4× too coarse to act on. Do **not** pick γ by argmax modularity |
| Hand-rolled canvas renderer | pyvis emits remote Bootstrap tags even in "inline" mode and dies under CSP; plotly inlines cleanly but costs 5.65 MB at 2,800 nodes vs 279 KB |
| `rapidfuzz.WRatio`, accept ≥ 90, review 84–89 | 90 is the top of a flat plateau; recall collapses at 92 for zero precision gain |
| Citadel LLC and Citadel Securities are **separate** `TargetFirm` members | Different companies, different pages, different people. There is deliberately no bare `CITADEL` |
| No `graph-tool` | Confirmed not installable from PyPI. Rules out SBM for v1 |

**Rejected and not to be reintroduced:** Infomap and label propagation (both collapse to a
single community on noisier graphs), greedy modularity (20× slower than Leiden and worse at
every mixing level), TF-IDF/embedding name matching (adds nothing over the guard), splink and
dedupe (disproportionate at this scale).

---

## 3. Layout and ownership

Every module is a package directory exposing exactly one entry point:

```python
def run(cfg: Settings) -> None: ...
```

Stages communicate **only** through `models.py` types and on-disk JSONL. No stage imports
another stage. This is what makes the wave parallelisable.

```
interlayer/
├── pyproject.toml            SCAFFOLD — do not edit
├── uv.lock                   SCAFFOLD — do not edit
├── data/gazetteer/firms.yaml SCAFFOLD — extend only via agent 2's review path
├── src/interlayer/
│   ├── models.py             SCAFFOLD — READ ONLY. Ask; do not edit.
│   ├── config.py             SCAFFOLD — READ ONLY
│   ├── errors.py             SCAFFOLD — READ ONLY
│   ├── io.py                 SCAFFOLD — READ ONLY
│   ├── cli.py                SCAFFOLD — READ ONLY
│   ├── ingest/               AGENT 1
│   ├── normalize/            AGENT 2
│   ├── enrich/               AGENT 3
│   ├── graph/                AGENT 4
│   ├── cluster/              AGENT 5
│   ├── score/                AGENT 5
│   └── report/               AGENT 6
└── tests/conftest.py         SCAFFOLD — READ ONLY
```

| Agent | Owns | Produces |
|---|---|---|
| 1 | `src/interlayer/ingest/**` | `people.jsonl`, `connections.jsonl` |
| 2 | `src/interlayer/normalize/**` | `orgs.jsonl`, `affiliations.jsonl` |
| 3 | `src/interlayer/enrich/**` | `targets.jsonl`, `observations.jsonl` |
| 4 | `src/interlayer/graph/**` | `edges.jsonl` |
| 5 | `src/interlayer/cluster/**`, `src/interlayer/score/**` | `clusters.jsonl`, `scored_people.jsonl`, `scored_clusters.jsonl` |
| 6 | `src/interlayer/report/**` | `report.html`, `manifest.json` |

**Zero file overlap.** If you believe you need to edit a file you do not own, stop and say so
rather than editing it — six agents share these definitions and a unilateral change breaks
everyone silently.

---

## 4. Stage contracts

### Agent 1 — `ingest`

Parse `cfg.input_csv` into `Person` and `Connection` records.

- **Sniff for the header row.** The `Notes:` preamble is 2–4 lines and its length *has changed
  across export versions*, so a fixed `skiprows=3` is a latent bug. Scan for the line
  containing `First Name` and `Last Name`.
- Map columns **by header name, never by index** — column order is not guaranteed.
- Real header: `First Name,Last Name,URL,Email Address,Company,Position,Connected On`.
- `URL` is the only reliable join key. Normalise the slug: strip query strings, trailing
  slashes, locale prefixes. **`/in/ACoA…` URNs are case-sensitive — do not casefold them**, that
  merges distinct humans. Legacy `/pub/` URLs need their full path retained.
- `Connected On` is `DD Mon YYYY`. Emails are usually blank.
- **Emails are dropped unless `cfg.keep_emails`**, in which case HMAC them via `io.hash_email`.
  Normalisation before hashing is `strip()` + `casefold()` only — never strip Gmail dots or
  `+tags`, that is deliberate cross-identity linkage.
- Rows that fail to parse are skipped with a counted warning, never silently.

### Agent 2 — `normalize`

Resolve raw employer and school strings to canonical `Org`s, and emit `Affiliation`s.

The Citadel problem is the whole job. The military college has ~40,000 alumni against the
fund's ~3,150 — it is the dominant false-positive source by an order of magnitude. Three rules,
all mandatory:

1. **Normalise twice.** Keep a `norm_raw` that retains articles and legal suffixes for negative
   lookup, alongside the fuzzy `norm`. Naive normalisation makes `The Citadel` and `Citadel`
   identical and then every scorer returns a perfect match. Index negatives under `norm_raw`
   **only** — indexing them under `norm` vetoes every real Citadel employee.
2. **Field context decides.** `The Citadel` in an education field is the college. In a position
   field it is ambiguous: emit `MatchVerdict.REVIEW`, never a silent match.
3. **Never merge the fund with the market maker.**

No subset-tolerant scorer is safe as a bare accept signal: `partial_ratio`, `token_set_ratio`
and `token_ratio` all score **100** for `Citadel` against `Citadel Broadcasting`. Always run
`WRatio` behind the negative gazetteer plus a token-alignment containment guard — that
combination took precision from ~0.6 to 1.0 with a zero wrong-firm rate.

### Agent 3 — `enrich`

Ingest hand-collected Tier 2 data into `TargetPerson` and `MutualObservation`.

- Define a documented file format (CSV or YAML) a human can fill in by hand, and a template.
- **Ship no scraper and no network code.** This stage must not import `requests`, `httpx`,
  `urllib`, `selenium` or `playwright`.
- Honour `MutualObservation.complete` and `.truncated`: an incomplete reading must never be
  treated as an exhaustive set, because absence from a truncated list is not evidence of
  absence.
- Document the collection workflow in `docs/collecting-observations.md`: company People tab →
  filter to 2nd degree → open each profile → read the mutual-connections list. Human speed
  only, and say why.

### Agent 4 — `graph`

Build the weighted person-person graph.

Weighting matters more than any later algorithm choice: naive projection let a single very
large employer produce **65% of all edges**, and collaboration weighting plus size damping cut
its share to 5.4%. Budget your effort accordingly.

1. Co-tenure filter — no date overlap, no edge. Removes ~62% of candidates. Missing dates get
   `cfg.missing_date_factor`, not exclusion.
2. Weight `= 1/(n_f − 1) × min(overlap_years, 5)/5 × 1/log(size_f + e)`.
3. Disparity filter at `cfg.disparity_alpha`, then rescue each node's top
   `cfg.rescue_top_k` edges so sparse nodes are not orphaned. On a sparse fixture the raw
   filter pruned 500 nodes to 86 — the rescue exists because of that.
4. **Observed bridges from Tier 2 are edges too**, and they carry `Provenance.OBSERVED`. They
   are never pruned by the disparity filter.

`GraphEdge` self-canonicalises orientation; do not sort endpoints yourself.

### Agent 5 — `cluster` + `score`

**Cluster.** Sparse consensus Leiden: `cfg.consensus_runs` seeded runs, co-association matrix,
threshold `cfg.consensus_threshold`, final partition via `leidenalg.find_partition` with
`RBConfigurationVertexPartition` at `cfg.resolution`. Seed every run from `cfg.seed`. Label each
cluster from its dominant orgs and titles. Drop clusters below `cfg.min_cluster_size`.

**Score.** Personalized PageRank at `cfg.pagerank_alpha`, seeded on target-firm people, plus
additive components from `cfg.weights`. Rank clusters by mean non-seed PPR mass.

Every `ScoredPerson` with a non-zero score **must** carry the evidence that moved it — the
model raises otherwise. Observed evidence must outrank every inferred signal; that is why
`weights.observed_mutual` is 10.0 against `direct_employment`'s 6.0.

### Agent 6 — `report`

Render a single self-contained HTML file plus the run manifest.

- **Zero outbound requests when opened.** No CDN, no webfonts, no remote images, and
  specifically **no `licdn.com` avatars** — rendering a page of remote avatars hands the entire
  analysed list back to LinkedIn the moment the operator opens their own report.
- Carry `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src
  'unsafe-inline'; img-src data:; font-src data:">`.
- Every row shows its confidence and its evidence. **Observed and inferred must be visually
  distinct** — an inferred link may never read as fact.
- Honour `cfg.redact`: pseudonymous stable ids, no names, and it must **fail closed** (allowlist
  the fields you serialise; never blocklist).
- Sort every collection before rendering. `PYTHONHASHSEED` produced four distinct set-iteration
  orders across four values, so unsorted output is not reproducible.

---

## 5. Commands

```bash
uv sync --locked                      # NEVER bare `uv sync` — it silently re-locks
uv run pytest -q                      # full suite
uv run pytest --lf --lfnf=none -q     # ONLY previously-failed tests
uv run ruff format . && uv run ruff check --fix .
uv run mypy
uv run interlayer --help
```

`--lfnf=none` is load-bearing: with an empty cache, plain `--lf` silently re-runs the *entire*
suite. Exit code 5 (`NO_TESTS_COLLECTED`) from the `--lf` invocation means nothing is left to
fix — treat it as success.

Six simultaneous `uv sync` runs were verified to exit 0 (the venv is file-locked). The hazard is
that a **bare** `uv sync` re-locks on pyproject drift and mutates the shared venv underneath
every other agent, so `--locked` is mandatory.

---

## 6. Non-negotiables

1. **No network access in the core pipeline.** The autouse fixture in `tests/conftest.py` blocks
   sockets and DNS for every test. Do not add an opt-out marker to a pipeline test.
2. **No scraper**, in any stage, in any form.
3. **Provenance on everything.** A score the operator cannot audit is a score they cannot act on.
4. **Inferred is labelled inferred**, everywhere it surfaces.
5. **Deterministic output.** Same input plus same seed produces byte-identical artifacts. Sort at
   every set boundary.
6. **No personal data in git.** The `.gitignore` uses `/data/*` rather than `/data/` because git
   cannot re-include a file under an excluded directory — do not "simplify" it.
7. **Type-annotate everything.** `mypy` runs with `disallow_untyped_defs`.

## 7. Definition of done, per agent

- `uv run ruff check .` and `uv run mypy` both clean.
- Your stage's `run(cfg)` executes end to end on the fixtures in `tests/conftest.py`.
- Your stage writes its artifacts with `io.write_jsonl` (0600, atomic) and reads inputs with
  `io.read_jsonl`, raising `StageInputMissingError` when run out of order.
- Docstrings explain *why*, not *what*.
- You did not edit a file you do not own.

---

## 8. Wave 3 addendum — testing notes

### LFR benchmark graphs will hang your suite if you copy the usual parameters

`nx.LFR_benchmark_graph` retries internally and can spin **forever**. `max_iters`
bounds the community-assignment loop but **not** the degree-sequence loop, so it does
not save you. The parameters quoted in `docs/research/02-graph-clustering.md`
(`n=250, tau1=3, tau2=1.5, mu=0.1, average_degree=5, min_community=20`) hang past 25
seconds in this environment.

The fix is to specify `min_degree`/`max_degree` explicitly instead of
`average_degree`. These two shapes were probed across mixing levels and converge in
about 0.15 s every time:

```python
# 9 planted communities, works at mu = 0.1 .. 0.5
SMALL = dict(n=300,  tau1=3, tau2=1.5, min_degree=5, max_degree=30,
             min_community=20, max_community=60)
# 17 planted communities, works at mu = 0.1 .. 0.4
LARGE = dict(n=1000, tau1=3, tau2=1.5, min_degree=8, max_degree=50,
             min_community=40, max_community=120)

G = nx.LFR_benchmark_graph(**SMALL, mu=0.3, seed=7, max_iters=500)
G = nx.Graph(G)                                  # it returns a MultiGraph
G.remove_edges_from(nx.selfloop_edges(G))        # and it contains self-loops
truth = {v: min(G.nodes[v]["community"]) for v in G}
```

Wrap generation in a timeout regardless, and mark anything above `SMALL` as `slow`.

### Assertion targets, measured by the build agents

Hold the code to these; they are observed values, not aspirations.

| Property | Target |
|---|---|
| Clustering NMI vs planted truth, mu=0.1 | > 0.90 (measured 0.9986) |
| Clustering NMI vs planted truth, mu=0.3 | > 0.80 (measured 0.9384) |
| Cross-ensemble ARI, consensus, mu=0.3 | > 0.95 (measured 0.9893) |
| Every returned cluster internally connected | 229/229 held |
| Firm matching on the research case table | 100/100, wrong-firm rate 0 |
| Report external references | exactly 0 |
| Redaction leaks | exactly 0 |
| Byte-identical artifacts, same seed | across `PYTHONHASHSEED` 0/1/12345/999 |

### numpy and scikit-learn are test-only

They are in the dev group, not the runtime. Use `sklearn.metrics.adjusted_rand_score`
and `normalized_mutual_info_score` freely in tests, but a test that imports them to
exercise *runtime* behaviour is testing the wrong thing — the pipeline is deliberately
numpy-free.
