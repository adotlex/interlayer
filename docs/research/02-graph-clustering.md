# Graph Model, Clustering Methodology & Library Selection

**Wave 1 / Agent 2 research report**
**Date:** 2026-08-21
**Scope:** How to build a graph from LinkedIn affiliation data, cluster it stably, and rank clusters by proximity to Jane Street / Citadel.

> **All benchmark numbers in this document were produced by actually running the code**
> in this sandbox (Python 3.11.15, uv 0.8.17). Install statuses are verified, not assumed.
> Where I am uncertain or extrapolating, I say so explicitly.

---

## 0. Executive summary

**Recommended stack (all verified pip-installable, zero compilation):**
`networkx` + `python-igraph` + `leidenalg` + `scipy` + `numpy` (+ `scikit-learn` for metrics).

**Recommended pipeline:** bipartite affiliation graph → Newman-weighted projection with
co-tenure and firm-size corrections → disparity-filter backbone → **consensus Leiden**
(RBConfiguration, γ swept, 50 runs, co-association threshold 0.5) → personalized PageRank
seeded on known JS/Citadel people, paired with an explainable evidence-count fallback.

**The single most important finding:** run-to-run flapping is real at the target scale.
At n=3,000 twenty differently-seeded Leiden runs gave mean pairwise ARI of only **0.877**
(min 0.794) and cluster counts ranging 66–70. Consensus clustering fixes this: cross-ensemble
ARI rose to **1.0000 / 0.9970 / 0.9210** at LFR mu = 0.2 / 0.3 / 0.4 — and *also* improved
accuracy (NMI 0.719 → 0.756 at mu=0.4). Consensus is not optional polish; it is the
difference between a tool a human can trust and one that reshuffles its answer every run.

---

## 1. Graph construction from affiliation data

### 1.1 The data reality dictates the model

We cannot observe true 2nd-degree edges. LinkedIn exposes no API for another member's
connection list, so the "who knows whom" graph among the user's connections is unobservable.
Everything must be **inferred from shared affiliations**.

This makes the natural primitive a **bipartite graph**:

```
People (1st-degree connections + known JS/Citadel roster)
   ↕  incidence edges
Affiliations (company-stints, schools+years, locations, groups)
```

An affiliation node should be a **company-stint bucket**, not just a company. `("Google",
2015–2019)` is a far better affiliation than `("Google")`, because Google-2015 and
Google-2023 populations barely overlap socially. Same for schools: `("MIT", "BS", 2012–2016)`.

**Critical framing:** these are *evidence* edges, not social edges. Every downstream number
is a likelihood-of-acquaintance estimate. The UI must never claim "these people know each
other" — only "these people have strong shared context."

### 1.2 Bipartite projection and why naive projection is a disaster

Projection collapses the bipartite graph onto People: `P = B Bᵀ`, where `P[i][j]` counts
shared affiliations. The problem is that **every affiliation of size n becomes a clique of
n(n−1)/2 edges**. This is quadratic, so a handful of large employers dominate everything.

Measured on a simulated 1,200-person export (realistic firm-size distribution, one
200,000-person "MegaCorp"):

| Projection variant | Edges | Density | Mean degree | Max degree |
|---|---:|---:|---:|---:|
| Naive (unweighted, no co-tenure) | 150,303 | 0.2089 | 250.5 | 610 |
| + co-tenure requirement | 56,964 | 0.0792 | 94.9 | 357 |

Single-firm clique contributions in that same export:

| Firm | Members in export | Real firm size | Naive clique edges |
|---|---:|---:|---:|
| MegaCorp | 443 | 200,000 | **97,903** |
| JaneStreet | 93 | 2,500 | 4,278 |
| Citadel | 93 | 6,000 | 4,278 |
| TinyShop | 48 | 40 | 1,128 |

**MegaCorp alone produced 65% of all edges in the graph.** A density of 0.21 on a social
graph is absurd — real personal networks are ~0.01–0.05. Community detection on this returns
"the people who worked at big companies," which is worthless.

### 1.3 Correction 1 — Newman collaboration weighting

Newman's co-authorship weighting (Newman 2001, *Scientific collaboration networks II*)
divides each pairwise contribution by group size minus one:

```
w_ij = Σ_over shared affiliations f   1 / (n_f − 1)
```

Two people who were the only two employees contribute 1.0. Two people in a 1,000-person
cohort contribute 1/999 each. This is available directly in NetworkX and **I verified it
computes exactly Newman's formula**:

```python
import networkx as nx
from networkx.algorithms import bipartite

B = nx.Graph()
B.add_nodes_from(["p1", "p2", "p3"], bipartite=0)   # people
B.add_nodes_from(["f1", "f2"], bipartite=1)         # affiliations
B.add_edges_from([("p1","f1"), ("p2","f1"), ("p3","f1"),
                  ("p2","f2"), ("p3","f2")])

P = bipartite.collaboration_weighted_projected_graph(B, ["p1", "p2", "p3"])
# p2 & p3 share f1 (3 members → 1/2) and f2 (2 members → 1/1) ⇒ weight 1.5  ✓ verified
# plain weighted_projected_graph would give 2 for that pair — no size correction
```

NetworkX also ships `weighted_projected_graph`, `overlap_weighted_projected_graph`
(Jaccard-style), and `generic_weighted_projected_graph` (arbitrary callable — the escape
hatch we'll actually use).

### 1.4 Correction 2 — firm-size (inverse-size / hyperbolic) weighting

Newman weighting uses `n_f` = members *in our export*. That is the wrong denominator. If 20
of the user's connections worked at a 40-person startup, `n_f = 20` under-penalizes nothing —
but they genuinely all know each other. If 20 worked at a 200,000-person firm, `n_f = 20` gives
the same weight, which is badly wrong.

So use the **true firm headcount** as a second, independent damping term:

```
w_ij(f) = [1 / (n_f − 1)]  ×  cotenure_factor(i, j, f)  ×  1 / log(size_f + e)
```

`log` rather than `1/size` because `1/size` is too brutal (a 200,000-person firm would get
weight 5e-6, effectively deleting an edge that might still be real if they were on the same
10-person team). `1/log` compresses a 5,000× size range into about a 2× weight range, which
matches intuition better. **I am uncertain this is the optimal functional form** — it is a
defensible prior, not a derived result, and should be a tunable.

Measured effect on **share of total edge weight** captured by each firm:

| Weighting | MegaCorp (200k people) | TinyShop (40 people) | TinyShop:MegaCorp ratio |
|---|---:|---:|---:|
| Naive | **60.8%** | 0.7% | 0.011 |
| Newman 1/(n−1) | 15.9% | 1.8% | 0.113 |
| Newman + size | **5.4%** | 2.0% | **0.370** |

A **33× reweighting** toward the informative small firm. This is the whole ballgame: co-working
at a 40-person shop is strong evidence of acquaintance; co-working at MegaCorp is nearly none.

### 1.5 Correction 3 — co-tenure (time overlap)

Two people who both worked at Firm F but five years apart have essentially **zero** chance of
knowing each other. Require interval intersection:

```python
COTENURE_CAP = 5          # years; overlap beyond this adds nothing
MISSING_DATE_FACTOR = 0.3

def cotenure_factor(start_i, end_i, start_j, end_j):
    """Return 0.0 (no edge) .. 1.0 (fully co-tenured). None dates ⇒ reduced credit."""
    if None in (start_i, end_i, start_j, end_j):
        return MISSING_DATE_FACTOR
    overlap = min(end_i, end_j) - max(start_i, start_j)
    if overlap <= 0:
        return 0.0                                  # no edge from this affiliation
    return min(overlap, COTENURE_CAP) / COTENURE_CAP
```

Measured: requiring positive co-tenure cut the projection from **150,303 → 56,964 edges
(−62%)** with no loss of real signal. This is the cheapest, highest-value filter available.
Apply it **before** anything else.

Notes on the design:
- **Saturate, don't scale linearly.** 8 years together isn't 8× stronger than 1 year. Cap at
  ~5 years.
- **Resolution.** LinkedIn gives month precision when enriched, year-only otherwise. Year
  granularity is fine; use half-open intervals `[start, end)` and treat a still-current role
  as ending "today."
- **Missing dates.** A stint with unknown dates should get a *reduced* co-tenure factor
  (~0.3), not be dropped and not be treated as full overlap. Dropping loses real people;
  full-credit re-creates the clique blowup.
- **Schools** need the same treatment: same university, degrees 12 years apart = no edge.
- **Locations** are weak affiliations. "San Francisco Bay Area" has millions of members. Give
  them a very small weight or use them only as a tie-breaker; never as a primary edge source.

### 1.6 Correction 4 — statistical backbone extraction

Even after weighting, the graph is too dense. Weighting fixes *relative* importance but
doesn't remove edges. Backboning does — and unlike a global weight threshold, it is
**scale-aware**: it prunes per-node, so a weakly-connected person isn't erased entirely.

#### Disparity filter (Serrano, Boguñá & Vespignani, PNAS 2009)

For each edge, normalize by the node's strength and test against a null hypothesis that a
node's strength is distributed uniformly at random among its edges. Keep the edge if it is
significant **from at least one endpoint** (this is what protects low-strength nodes):

```python
def disparity_filter(G, alpha=0.05):
    """Serrano et al. 2009 multiscale backbone."""
    K = nx.Graph(); K.add_nodes_from(G.nodes(data=True))
    for u, v, d in G.edges(data=True):
        w = d["weight"]
        for a in (u, v):                       # significant at EITHER endpoint
            k = G.degree(a)
            if k <= 1:
                continue
            s = sum(G[a][x]["weight"] for x in G[a])
            if (1 - w / s) ** (k - 1) < alpha:  # closed-form p-value
                K.add_edge(u, v, **d)
                break
    return K
```

Verified on the 1,200-person / 55,065-edge weighted projection:

| α | Nodes kept | Edges kept | % edges | % weight | Density |
|---:|---:|---:|---:|---:|---:|
| 0.50 | 1200 | 23,386 | 42.5% | 90.9% | 0.0325 |
| 0.30 | 1196 | 16,345 | 29.7% | 79.2% | 0.0229 |
| 0.15 | 1190 | 11,244 | 20.4% | 59.6% | 0.0159 |
| **0.05** | **1163** | **7,619** | **13.8%** | **42.4%** | **0.0113** |
| 0.01 | 1114 | 4,888 | 8.9% | 31.0% | 0.0079 |

α=0.05 keeps 97% of nodes while cutting 86% of edges and landing density at 0.011 — squarely
in the plausible range for a social graph.

#### Noise-corrected backbone (Coscia & Neffke, ICDE 2017)

Uses a Bayesian binomial null on *node pairs* rather than per-node uniformity, and is
reported to be more robust under noisy data. My first implementation was wrong (returned zero
edges); the corrected version follows the paper's lift/variance formulation:

```python
def noise_corrected(G, delta=1.64):
    """Coscia & Neffke 2017. delta ≈ 1.64 ~ one-sided 95%."""
    K = nx.Graph(); K.add_nodes_from(G.nodes(data=True))
    n = sum(d["weight"] for _, _, d in G.edges(data=True)) * 2.0
    stre = {v: sum(G[v][x]["weight"] for x in G[v]) for v in G}
    for u, v, d in G.edges(data=True):
        nij, ni, nj = d["weight"], stre[u], stre[v]
        if min(nij, ni, nj) <= 0:
            continue
        mean_prior = (ni * nj / n) * (1.0 / n)
        kappa = n / (ni * nj)
        score = (kappa * nij - 1) / (kappa * nij + 1)
        var_prior = (1.0/(n**2)) * (ni*nj*(n-ni)*(n-nj)) / ((n**2) * (n-1))
        if var_prior <= 0:
            continue
        a_pri = ((mean_prior**2 / var_prior) * (1 - mean_prior)) - mean_prior
        b_pri = (mean_prior / var_prior) * (1 - mean_prior**2) - (1 - mean_prior)
        a_post, b_post = a_pri + nij, n - nij + b_pri
        exp_pij = a_post / (a_post + b_post)
        var_nij = exp_pij * (1 - exp_pij) * n
        dd = (1.0/(ni*nj)) - (n * ((ni+nj) / ((ni*nj)**2)))
        sd = math.sqrt(max(var_nij * (((2*(kappa + nij*dd)) / ((kappa*nij + 1)**2))**2), 0.0))
        if score - delta * sd > 0:
            K.add_edge(u, v, **d)
    return K
```

Verified on the same graph:

| δ | Nodes | Edges | % edges | Density |
|---:|---:|---:|---:|---:|
| 0.0 | 1200 | 41,557 | 75.5% | 0.0578 |
| 0.5 | 1198 | 13,400 | 24.3% | 0.0187 |
| 1.0 | 1193 | 8,096 | 14.7% | 0.0114 |
| **1.64** | 1176 | 4,611 | 8.4% | 0.0067 |
| 2.5 | 1017 | 2,431 | 4.4% | 0.0047 |

δ=1.0 gives almost exactly the same sparsity as disparity α=0.05 (14.7% vs 13.8% of edges).

**Recommendation:** default to **disparity filter at α=0.05**. It is simpler, has one
interpretable parameter, is the field standard, and matched NC's output closely here. Expose
NC as an alternative — it is theoretically better-motivated for bipartite projections, and if
Wave 2 finds the disparity filter is stranding people, switch.

#### Verified behavioural difference between the two filters

Running both filters on a graph with **uniformly random edge weights** (i.e. pure noise, no
real structure) is a revealing sanity check:

| Filter | Edges kept out of 3,163 |
|---|---:|
| Disparity filter (α=0.05) | **0** |
| Noise-corrected (δ=1.0) | **3,163 (all)** |

The disparity filter correctly keeps **nothing** — with uniform weights no edge is locally
anomalous, so there is no backbone to find. The NC filter keeps **everything**, because its
null model is about node *strengths* rather than local weight disparity, and uniform weights
don't violate it.

Neither is "wrong" — they test different nulls — but it means **the disparity filter degrades
safely (toward empty) while NC degrades unsafely (toward the full dense graph)**. Since a dense
result would silently produce garbage clusters, this is a further argument for disparity as the
default, and a good property to assert in tests.

**Important caveat I verified:** on a *sparser* affiliation graph (N=500 people), disparity at
α=0.05 pruned 5,116 edges down to 63, leaving only 86 of 500 people connected. That is far too
aggressive when much of the graph is genuinely noise. **Mitigation:** always union the backbone
with each node's top-k highest-weight edges (k≈3) so nobody is stranded, and treat α as a
tunable that Wave 2 should calibrate against a target density (~0.01–0.03).

---

## 2. Community detection algorithms

### 2.1 Benchmark results (measured, not cited)

LFR benchmark graphs, n=1,000, τ₁=3, τ₂=1.5, avg degree 10, min community 20, 25 planted
communities. mu is the mixing parameter — the fraction of each node's edges that go *outside*
its community. Higher mu = harder. Scored by NMI / ARI against planted ground truth.

| Algorithm | mu=0.1 | mu=0.2 | mu=0.3 | mu=0.4 | mu=0.5 | mu=0.6 | time @ mu=0.3 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Leiden (modularity)** | .999 | .982 | **.935** | **.719** | **.327** | .124 | 0.079s |
| **igraph Leiden** | .999 | .982 | .937 | .712 | .320 | **.145** | **0.023s** |
| Leiden CPM (r=0.01) | .999 | **.987** | .937 | .646 | .213 | .128 | — |
| Leiden CPM (r=0.05) | .993 | .980 | .914 | **.749** | **.518** | **.377** | — |
| igraph Louvain | .997 | .972 | .910 | .652 | .235 | .142 | 0.010s |
| nx Louvain | .999 | .983 | .911 | .680 | .312 | .123 | 0.076s |
| **Infomap** | .999 | **.993** | **.963** | **.829** | **.000** | **.000** | 0.166s |
| Label propagation | .996 | .992 | .911 | **.000** | .000 | .000 | 0.008s |
| Greedy modularity (CNM) | .828 | .761 | .601 | .338 | .187 | .115 | **1.535s** |

*(values are NMI; full ARI figures were collected and track NMI closely)*

### 2.2 Honest trade-offs

**Leiden — RECOMMENDED.** Traag, Waltman & van Eck 2019, *From Louvain to Leiden:
guaranteeing well-connected communities* (Sci Rep 9:5233). Fixes a genuine, severe Louvain
defect: **Louvain can produce internally disconnected communities.** In the authors'
experiments up to 25% of Louvain communities were badly connected and up to 16% were literally
disconnected. Leiden adds a refinement phase that guarantees every community is internally
connected, and it runs *faster* than Louvain. In my benchmark Leiden beat Louvain at every
single mu value. **There is no scenario in this project where Louvain is the right choice over
Leiden.** A disconnected "cluster" shown to a user as "this group of people" would be a
correctness bug, not a quality nit.

**Infomap.** Best accuracy in the mid-range (mu=0.3–0.4, NMI .963/.829 — clearly ahead of
Leiden's .935/.719). But it **catastrophically collapses at mu≥0.5, returning a single
community (NMI 0.000)**. Its map-equation objective decides no partition beats the trivial
one. Our real graph is noisy and inferred, likely in the mu≈0.4–0.6 regime, so this failure
mode is a live risk, not theoretical. Excellent as a cross-check, dangerous as the only method.

**Label propagation.** Fastest (0.008s) and fine at low mu, but **collapses to one community
at mu≥0.4** — same failure, earlier onset. Also the least stable by construction. Reject.

**Greedy modularity (CNM).** Worst accuracy at *every* mu **and** the slowest (1.5s vs
Leiden's 0.08s — 20× slower and much worse). It also systematically under-splits (found 9
communities where truth was 25). No reason to use it. Reject.

**SBM / degree-corrected SBM.** Theoretically the most principled option: it's a generative
model, does model selection via minimum description length (so it picks the number of
communities for you rather than needing a resolution parameter), and the degree-corrected
variant handles the heavy-tailed degree distributions our projection produces. It is also
**resolution-limit-free** in a way modularity is not. **The blocker is purely practical:
the good implementation is in `graph-tool`, which is not pip-installable** (see §5).
`cdlib.algorithms.sbm_dl` exists but is a thin wrapper that *requires graph-tool anyway* —
importing cdlib prints a warning that `graph_tool` is missing. **Recommendation: skip SBM for
v1.** Revisit only if Wave 2 finds modularity-based clustering unsatisfying and the team
accepts a conda/Docker dependency.

**Hierarchical / agglomerative.** Useful for a drill-down UI (expand a cluster into
sub-clusters). But you get this more cheaply from Leiden by **sweeping γ** and nesting the
results, which keeps one algorithm and one code path. Recommend γ-sweep over a separate
hierarchical method.

### 2.3 The resolution limit — and whether it bites here

Fortunato & Barthélemy (2007) showed modularity optimization **cannot resolve communities
smaller than roughly √(2m) internal edges**, where m is the edge count. Communities below that
scale get merged into larger ones no matter how well-separated they are.

**Measured on the realistic backbone (1,163 nodes, 8,207 edges):**

```
m = 8207  →  √(2m) = 128.1
default modularity partition: k=19, sizes = [118, 93, 88, 78, 77, 71, 69, 65, 64, 64, 56, 52]
```

**Yes, it bites — badly.** Every cluster the default found is in the 52–118 range, i.e.
pinned right at the resolution floor. But a *useful* cluster for this product is
"the ~15 people from my Jane-Street-adjacent trading circle," not "these 118 people."
**Default γ=1.0 produces clusters roughly 4× too coarse to act on.**

Resolution sweep on that same graph (RBConfiguration / modularity with resolution parameter):

| γ | k | k with ≥5 members | Largest | Median (big) | Q |
|---:|---:|---:|---:|---:|---:|
| 0.25 | 2 | 2 | 1125 | 581 | 0.038 |
| 0.5 | 8 | 8 | 304 | 108 | 0.484 |
| 1.0 | 19 | 19 | 118 | 64 | **0.523** |
| **2.0** | 28 | 28 | 74 | **41** | 0.516 |
| **4.0** | 46 | 46 | 46 | **24** | 0.494 |
| 8.0 | 69 | 69 | 27 | 17 | 0.454 |

Note that **Q peaks at γ=1.0 but the actionable range is γ=2–4.** This is the key lesson:
*maximizing modularity does not maximize usefulness.* Do not pick γ by argmax Q.

**CPM vs Modularity in Leiden.** Traag, Van Dooren & Nesterov (2011), *Narrow scope for
resolution-limit-free community detection*, proved the **Constant Potts Model is
resolution-limit-free** — it compares against a constant rather than a configuration-model
null, so the objective is local and cluster sizes don't depend on total graph size. That is a
real theoretical advantage.

In practice my benchmark shows CPM's resolution parameter is **much** harder to tune: it maps
directly to an internal-density threshold, so a small change swings cluster count wildly. At
mu=0.6, CPM r=0.05 gave 101 communities against a true 25 — massive over-fragmentation — while
scoring *better* NMI (.377 vs .124) largely because NMI rewards fine partitions. That NMI win
is partly an artifact, not pure signal.

**Recommendation:** use **RBConfigurationVertexPartition (modularity with a γ knob)** as the
default, γ=1.0 as the reference and **γ ∈ {1.0, 2.0, 4.0}** as the shipped sweep. Expose CPM
behind a flag for users who want size-independent clusters. Rationale: γ on modularity is
intuitive ("bigger γ = smaller clusters"), robust across graph sizes, and the resolution limit
is neutralized in practice by sweeping γ upward — we are not relying on γ=1.

---

## 3. Determinism & stability

### 3.1 Which libraries are actually deterministic (verified)

| Call | Repeated 10× → identical? |
|---|---|
| `leidenalg.find_partition(g, ..., seed=42)` | **YES — byte-identical, every time, at every mu tested** |
| `igraph.Graph.community_leiden(...)` | **NO** — identical at mu=0.2 but *diverged* at mu=0.4 |
| `igraph.Graph.community_multilevel()` w/ global RNG seeded once | **NO** |
| `sknetwork.clustering.Louvain(random_state=42)` | YES |
| `infomap.Infomap(seed=42)` | YES |

**This is a decisive finding.** `python-igraph`'s `community_leiden` has **no `seed`
parameter** and depends on igraph's global RNG; seeding that global once does *not* make
repeated calls identical, because state advances between calls. `leidenalg.find_partition`
takes an explicit `seed=` and is genuinely reproducible.

**⇒ Use `leidenalg.find_partition(seed=...)` for anything user-facing.** Use igraph's native
`community_leiden` only for throwaway speed work where reproducibility doesn't matter.

### 3.2 Run-to-run flapping is real at our scale

Seeding alone gives you *reproducibility* (same seed → same answer) but not *stability* (the
answer isn't robust — it's just one arbitrary local optimum, frozen). If the input data
changes slightly (one new connection), a seeded run can jump to a completely different
partition. Measured across 30 different seeds, n=1,000 LFR:

| mu | Leiden mean ARI | Leiden min ARI | k range | Louvain mean ARI | Louvain min ARI |
|---:|---:|---:|---:|---:|---:|
| 0.2 | 0.996 | 0.946 | 22–23 | 0.969 | 0.906 |
| 0.3 | 0.931 | 0.796 | 19–22 | 0.860 | 0.738 |
| 0.4 | **0.602** | **0.486** | 14–17 | 0.531 | 0.357 |
| 0.5 | 0.169 | 0.087 | 8–12 | 0.160 | 0.082 |

And on the realistic affiliation-graph backbone (20 seeds):

| Network size | Backbone | Mean ARI | Min ARI | k range |
|---:|---|---:|---:|---:|
| N=500 | 86n / 63e | **1.000** | 1.000 | 28–28 |
| N=1500 | 362n / 454e | 0.990 | 0.963 | 53–54 |
| N=3000 | 1173n / 1696e | **0.877** | **0.794** | 66–70 |

At N=500 Leiden is perfectly stable. At N=3,000 it is not — a min ARI of 0.794 means two runs
disagree substantially about who belongs with whom. **A user re-running the tool would see
different groups.** Unacceptable for a tool whose output drives outreach decisions.

### 3.3 Consensus clustering — the fix

Lancichinetti & Fortunato (2012), *Consensus clustering in complex networks* (Sci Rep 2:336).
Run the algorithm many times, build a **co-association matrix** `D[i][j]` = fraction of runs
placing i and j together, threshold it, and re-cluster the consensus graph. Iterate until the
matrix is a block structure.

Verified results — this is the money table:

| mu | Consensus NMI | Consensus ARI | k | **Cross-ensemble ARI** | Single-run ARI (for comparison) |
|---:|---:|---:|---:|---:|---:|
| 0.2 | 0.982 | 0.963 | 23 | **1.0000** | 0.996 |
| 0.3 | 0.938 | 0.881 | 22 | **0.9970** | 0.931 |
| 0.4 | **0.756** | 0.601 | 23 | **0.9210** | 0.602 |
| 0.5 | 0.430 | 0.269 | 29 | 0.5647 | 0.169 |

"Cross-ensemble ARI" = agreement between three *entirely independent* ensembles (different
base seeds, 50 runs each). At mu=0.4 stability jumps **0.602 → 0.921**, and accuracy also
improves (NMI 0.719 → 0.756). Consensus both stabilizes *and* denoises. It also recovered the
right cluster count at mu=0.4 (k=23 vs true 25, where single-run Leiden collapsed to 14–17).

**Use the sparse implementation** — I verified it gives identical accuracy at lower cost and
actually scales:

```python
import numpy as np, scipy.sparse as sp, igraph as ig, leidenalg as la

def sparse_consensus(g, n_runs=50, threshold=0.5, max_iter=10, base_seed=0, gamma=1.0):
    """Lancichinetti–Fortunato consensus, sparse co-association."""
    n, cur = g.vcount(), g
    for it in range(1, max_iter + 1):
        rows, cols = [], []
        for r in range(n_runs):
            w = cur.es["weight"] if "weight" in cur.es.attributes() else None
            m = np.asarray(la.find_partition(
                cur, la.RBConfigurationVertexPartition, weights=w,
                resolution_parameter=gamma, seed=base_seed + r).membership)
            order = np.argsort(m, kind="stable")
            for grp in np.split(order, np.flatnonzero(np.diff(m[order])) + 1):
                if len(grp) < 2:
                    continue
                a, b = np.repeat(grp, len(grp)), np.tile(grp, len(grp))
                keep = a < b
                rows.append(a[keep]); cols.append(b[keep])
        if not rows:
            break
        r_, c_ = np.concatenate(rows), np.concatenate(cols)
        D = sp.coo_matrix((np.ones(len(r_), np.float32), (r_, c_)), shape=(n, n)).tocsr()
        D.data /= n_runs
        D.data[D.data < threshold] = 0.0
        D.eliminate_zeros()
        Dc = D.tocoo()
        if len(Dc.data) == 0 or np.all(Dc.data >= 1.0):
            break                      # converged: matrix is block-structured
        cur = ig.Graph(n=n, edges=list(zip(Dc.row.tolist(), Dc.col.tolist())))
        cur.es["weight"] = Dc.data.tolist()
    Dc = D.tocoo()
    return ig.Graph(n=n, edges=list(zip(Dc.row.tolist(), Dc.col.tolist()))
                    ).connected_components().membership, it
```

**Cost (verified):** n=1,000 / 50 runs ≈ **7–13s** per ensemble. n=5,000 / 30 runs ≈ 71s.
Dense equivalent at n=3,000 was 20s and 36 MB; dense at n=50,000 would need ~10 GB, so the
sparse version is mandatory above ~5,000 nodes. For our 500–3,000 target, **sparse consensus
with 50 runs costs under 15 seconds** — completely acceptable for a tool run occasionally.

### 3.4 Stability metrics — which to use

- **ARI (Adjusted Rand Index)** — chance-corrected, in [−1, 1], 0 = random. **Primary metric.**
  Punishes splitting/merging appropriately. Use `sklearn.metrics.adjusted_rand_score`.
- **NMI (Normalized Mutual Information)** — in [0, 1], intuitive, but **biased upward for
  partitions with many small clusters**. I observed exactly this: CPM r=0.05 at mu=0.6 scored
  NMI .377 while producing 101 clusters against a true 25. Prefer **AMI** (adjusted MI) when
  cluster counts differ between the two partitions being compared.
- **VI (Variation of Information)** — a true metric (satisfies the triangle inequality), so
  it's the right choice if you ever need to *average* or *embed* partitions. Lower is better,
  unbounded above. `cdlib.evaluation.variation_of_information`.

**Recommendation:** report ARI as the headline, AMI as the cluster-count-robust check, and VI
if partition-space geometry is ever needed. Assert on ARI in tests.

---

## 4. Scoring proximity to Jane Street / Citadel

### 4.1 Method comparison

**Personalized PageRank / Random Walk with Restart — PRIMARY.** Seed the restart vector
uniformly over all known JS + Citadel people; the stationary distribution ranks everyone by
how often a walker starting from the target firms visits them. Advantages: uses the whole
graph (multi-hop), naturally down-weights hub-mediated paths, respects edge weights, one
parameter, fast, and available in all three candidate libraries (verified working in
`networkx`, `igraph`, and `sknetwork` — all three agree closely on karate club).

**The α convention is a genuine footgun.** In `networkx.pagerank(alpha=...)`, α is the
**damping / continuation** probability; **restart probability = 1 − α**. Many papers define
`c` as the restart probability, i.e. the opposite. Measured effect:

| α (damping) | Restart prob | Probability mass retained on seed nodes |
|---:|---:|---:|
| 0.50 | 0.50 | 0.562 |
| 0.70 | 0.30 | 0.379 |
| **0.85** | **0.15** | **0.230** |
| 0.95 | 0.05 | 0.121 |

At α=0.5, **56% of all score mass sits on the seeds themselves** — the walk barely leaves
home, so you learn little about non-seeds. At α=0.95 the walk wanders so far it approaches
global PageRank and the personalization washes out. **Recommend α = 0.85** (restart 0.15): it
leaves 77% of mass on non-seeds while staying local enough to be meaningfully personalized.
This matches the standard PageRank default and my measurements support it here. Sweep
α ∈ {0.70, 0.85} if Wave 2 wants to tune.

**Adamic-Adar and Resource Allocation — RECOMMENDED as the explainable layer.** Both score a
pair by their common neighbors, weighted **inversely by neighbor degree** — a shared
connection who knows 500 people is much weaker evidence than one who knows 12. AA uses
`1/log(deg)`, RA uses `1/deg` (more aggressive). Both verified working in networkx
(`nx.adamic_adar_index`, `nx.resource_allocation_index`). Their killer feature is that the
score **decomposes into named contributors** — you can literally list which people drove it.

**Katz index.** Sums all paths, exponentially damped by length (`β^len`). Effectively a
weighted multi-hop count; correlates strongly with PPR but needs a matrix inverse or
truncation, and β must satisfy β < 1/λ_max for convergence. `nx.katz_centrality_numpy` works
(verified) but is dense-matrix-based. **No advantage over PPR here.** Skip.

**SimRank.** "Two nodes are similar if their neighbors are similar" — recursive and elegant,
but O(n²) memory for the full matrix and slow. Verified working via
`nx.simrank_similarity(G, source, target)` for single pairs. It answers a different question
(structural equivalence, not proximity) — two people at *different* firms with similar-shaped
networks score high, which is not what we want. **Skip.**

**Hitting time / commute time.** Expected random-walk steps to reach a target. Theoretically
appealing but **notoriously dominated by node degree** (hitting time to a high-degree node is
short from anywhere), which is exactly the pathology our whole weighting scheme fights. Also
expensive. **Skip.**

**Weighted-count baseline.** "Number of backbone edges to a known JS/Citadel person, weighted
by edge weight." Trivially explainable, instant, and a necessary sanity check. Verified
Spearman correlation with mean-PPR across clusters: **0.584** — correlated enough to validate
PPR, different enough that PPR is adding real multi-hop information. Keep both.

### 4.2 Recommended scoring design

**Primary: personalized PageRank, α=0.85, seeded on all known JS+Citadel people, weighted.**

**Cluster-level score:** rank clusters by the **mean PPR of their non-seed members**, not the
sum (sum just ranks big clusters first). Report alongside: cluster size, count of seeds inside
the cluster, and the fraction of members with a direct backbone edge to a seed.

Verified output on the simulated network:

```
  cluster size seeds seed%  meanPPR  adj adj%
      15   39     5  12.8  0.001679   31  79.5
      18   19     1   5.3  0.001530   18  94.7
      17   31     2   6.5  0.001184    9  29.0
       6   69     6   8.7  0.001152   48  69.6
```

**Explainable fallback (must ship alongside, not instead):** for every ranked person, emit the
**provenance trail** that produced their score. Because every edge was built from explicit
affiliation evidence, this is free — just retain the provenance dict during projection:

```
person 1158 (ppr=0.00433) has 4 backbone edges to seeds:
   via 598: firm9  co-tenure 2011-2013 (w=0.00351)
   via 559: firm9  co-tenure 2007-2013 (w=0.00878)
   via 822: firm31 co-tenure 2021-2023 (w=0.00169)
   via 822: MegaCorp co-tenure 2015-2017 (w=0.00007)
```

Note how this display **also exposes the weighting working correctly**: the MegaCorp link
contributes 0.00007 while the firm9 link contributes 0.00878 — a 125× difference the user can
see and sanity-check. **Store `(firm, overlap_start, overlap_end, weight)` tuples per edge
during projection.** Retrofitting provenance later is painful; it costs almost nothing now.
This is the single highest-leverage implementation note in this document.

---

## 5. Library selection — VERIFIED install status

All installs run with `uv pip install --system <pkg>` on Python 3.11.15, 2026-08-21.

| Library | Version | Install | Leiden? | Speed @50k | Verdict |
|---|---|---|---|---|---|
| **networkx** | **3.6.1** | ✅ pure Python | Louvain only | 117.6s (slow) | ✅ **ADOPT** — I/O, LFR, PPR, link prediction, bipartite helpers |
| **python-igraph** | **1.0.0** | ✅ wheel, no compile | ✅ native C | **1.78s** | ✅ **ADOPT** — fast core, algorithms |
| **leidenalg** | **0.12.0** | ✅ wheel, no compile | ✅ + `seed=` | 8.45s | ✅ **ADOPT** — the only reproducible Leiden |
| **scipy** | **1.17.1** | ✅ | — | — | ✅ **ADOPT** — sparse matrices |
| **numpy** | **2.4.6** | ✅ | — | — | ✅ **ADOPT** |
| **scikit-network** | **0.33.5** | ✅ | ✅ `Leiden` + `Louvain` | not benchmarked | 🟡 OPTIONAL — clean sparse API, deterministic `random_state` |
| **cdlib** | **0.4.1** | ⚠️ installs, **warns** | ✅ (wraps leidenalg) | — | 🟡 DEV-ONLY — heavy deps (matplotlib, seaborn, plotly, pulp, pyvis); `sbm_dl` needs graph-tool |
| **infomap** | **2.15.1** | ✅ wheel | n/a | — | 🟡 OPTIONAL — cross-check only; collapses at mu≥0.5 |
| **python-louvain** | 0.16 | ✅ | ❌ | — | ❌ REJECT — superseded by `nx.community.louvain_communities`; use Leiden |
| **graph-tool** | — | ❌ **NOT ON PyPI** | ✅ (+SBM) | — | ❌ REJECT for v1 |

**graph-tool — verified not pip-installable.** Actual error:

```
$ uv pip install --system graph-tool
  × No solution found when resolving dependencies:
  ╰─▶ Because there are no versions of graph-tool and you require graph-tool,
      we can conclude that your requirements are unsatisfiable.
```

It is a C++ library over Boost, CGAL and expat; those cannot be managed by a Python-only
package manager. Official install paths are conda-forge, apt/pacman, Homebrew/Macports, or
source build. **Do not put it in `pyproject.toml`.** It's the only library with a
best-in-class degree-corrected SBM, so note it as a future option gated on accepting a
conda or Docker dependency.

**cdlib caveat.** Installs fine but pulls a large dependency tree (matplotlib, seaborn,
plotly, pulp, pyvis, scikit-learn, pandas, tqdm...) and prints on import:
`Note: to be able to use all crisp methods, you need to install some additional packages:
{'bayanpy', 'wurlitzer', 'graph_tool'}`. Its `evaluation` module is genuinely convenient
(NMI, ARI, AMI, VI, omega, F1, modularity, conductance, internal density — all verified
working). **Recommend: dev/test dependency only, not a runtime dependency.** Use
`sklearn.metrics` in production code — it's already needed and far lighter.

### Verified clean-room install

```bash
$ uv venv cleanroom --python 3.11
$ uv pip install networkx python-igraph leidenalg scikit-network scipy numpy pandas
Resolved 11 packages in 61ms
Installed 11 packages in 26ms
 + igraph==1.0.0          + networkx==3.6.1        + numpy==2.4.6
 + pandas==3.0.5          + python-igraph==1.0.0   + scikit-network==0.33.5
 + scipy==1.17.1          + leidenalg==0.12.0      + texttable==1.7.0
 + python-dateutil==2.9.0.post0                    + six==1.17.0
```

**Zero compilation, 26 ms, 11 packages.** (Minor note: `scikit-network` reports
`sknetwork.__version__ == '0.33.0'` while pip metadata says `0.33.5` — a packaging
inconsistency upstream, harmless.)

### Speed reference (measured, avg degree 10)

| n | leidenalg | igraph Leiden | igraph Louvain | nx Louvain |
|---:|---:|---:|---:|---:|
| 500 | 0.048s | 0.011s | 0.005s | 0.059s |
| 3,000 | 0.419s | 0.068s | 0.064s | 0.578s |
| 10,000 | 1.735s | 0.257s | 0.613s | 4.189s |
| 50,000 | 8.453s | **1.780s** | 29.980s | **117.609s** |

`leidenalg` is ~4.7× slower than igraph's native C Leiden at 50k, but **at our 500–3,000 target
it costs under 0.5 seconds** — utterly irrelevant next to reproducibility. NetworkX Louvain is
unusable above ~10k nodes.

**Final recommended stack:**

```toml
dependencies = [
  "networkx>=3.6",       # I/O, bipartite projection, LFR, PPR, link prediction
  "python-igraph>=1.0",  # fast graph core
  "leidenalg>=0.12",     # reproducible seeded Leiden — the workhorse
  "scipy>=1.17",         # sparse co-association matrices
  "numpy>=2.0",
  "scikit-learn>=1.9",   # ARI / NMI / AMI
]
[dependency-groups]
dev = ["cdlib>=0.4", "infomap>=2.15"]   # cross-checks and extra metrics in tests
```

---

## 6. Evaluation without ground truth

### 6.1 Internal quality metrics (all verified working)

| Metric | Call | Interpretation |
|---|---|---|
| Modularity | `nx.community.modularity(G, comms)` | >0.3 = meaningful structure. **Don't maximize it — see §2.3** |
| Coverage & performance | `nx.community.partition_quality(G, comms)` | fraction of intra-community edges; fraction of correct pairs |
| Conductance | `nx.conductance(G, S)` / `cdlib.evaluation.conductance` | **lower = better**; boundary edges ÷ volume. Best single per-cluster metric |
| Internal edge density | `cdlib.evaluation.internal_edge_density` | cohesion within a cluster |
| Cluster size distribution | — | guard: flag if any cluster > 25% of graph, or if >50% of nodes are singletons |

Verified on karate club: modularity 0.4266, partition_quality (0.6923, 0.7736), conductance
mean 0.2875, internal density mean 0.4508.

**Silhouette on embeddings** is possible (embed with node2vec/spectral, then
`sklearn.metrics.silhouette_score`) but I'd **advise against it for v1**: it adds an embedding
dependency and hyperparameters, and it measures cluster quality *in embedding space*, which
can differ from graph space. Conductance answers the same question directly. Noted as a
possible later addition; I have not benchmarked it.

### 6.2 Synthetic benchmarks with planted ground truth — the key to Wave 3

Since real ground truth doesn't exist, **generate graphs where it does**. Both generators are
in NetworkX (verified: `hasattr(nx, "LFR_benchmark_graph")` and
`hasattr(nx, "stochastic_block_model")` both `True`).

**LFR benchmark** (Lancichinetti–Fortunato–Radicchi) — the field standard. Power-law degrees
*and* power-law community sizes, so it's realistically heterogeneous, and mu directly tunes
difficulty. Verified fast: **n=1,000 generates in 0.03–0.19s**, so it's completely viable
inside a unit test suite.

```python
import networkx as nx

def lfr(n=1000, mu=0.1, seed=42):
    G = nx.LFR_benchmark_graph(n=n, tau1=3, tau2=1.5, mu=mu, average_degree=10,
                               min_community=20, max_degree=int(0.1 * n), seed=seed)
    G.remove_edges_from(nx.selfloop_edges(G))
    G = nx.Graph(G)
    truth = {}
    for i, c in enumerate({frozenset(G.nodes[v]["community"]) for v in G}):
        for v in c:
            truth[v] = i
    return G, truth
```

⚠️ **Two gotchas Wave 3 must handle.** (1) LFR returns a multigraph with self-loops — you must
strip both, as above, or edge counts and modularity are wrong. (2) `LFR_benchmark_graph` can
raise `ExceededMaxIterations` for some parameter combinations; pin the parameters above (they
worked reliably at every mu from 0.1 to 0.6) and wrap in a retry if you vary them.

**Stochastic block model** — simpler, exact control over the block structure, good for
targeted edge cases (unequal block sizes, one giant block + many small ones — which is exactly
our MegaCorp pathology):

```python
sizes = [50, 40, 30, 200]                 # deliberately imbalanced
p = [[0.30, 0.02, 0.02, 0.01],
     [0.02, 0.30, 0.02, 0.01],
     [0.02, 0.02, 0.30, 0.01],
     [0.01, 0.01, 0.01, 0.05]]
G = nx.stochastic_block_model(sizes, p, seed=42)
```

**Also test on a synthetic *bipartite affiliation* generator**, not just LFR. LFR tests the
clustering step; it does not test projection, co-tenure weighting, or backboning. Wave 3
should build a fixture that plants known groups as shared company-stints (with known firm
sizes and date ranges), runs the *entire* pipeline, and asserts recovery. That's the only test
that exercises §1 end-to-end.

---

## 7. Small-graph reality check (500–3,000 nodes)

**Speed is a non-issue.** Leiden on a 3,000-node backbone: **40 ms**. Full 50-run consensus:
under 15 s. Nothing in this pipeline needs optimizing for our scale. Optimize for correctness,
determinism and explainability instead.

**Which algorithms behave badly here:**
- **Greedy modularity (CNM)** — 1.5–1.8 s at n=1,000 (20× slower than Leiden) *and* the worst
  accuracy at every mu. Strictly dominated.
- **Label propagation** — near-instant, but degenerates to one giant community once mixing
  rises. Its variance is also highest. Reject.
- **Infomap** — fine at n≈1,000 but the mu≥0.5 collapse is a hard failure, and inferred social
  graphs are noisy. Cross-check only.
- **networkx Louvain** — fine at this scale (0.08 s at n=1,000), only unusable at 50k. But
  Leiden beats it on quality anyway.

**Does the resolution limit bite? YES — measured.** At m=8,207 the modularity blind spot is
√(2m) ≈ 128 internal edges, and the default partition returned clusters of 52–118 members.
For a personal network that's far too coarse. **This is the main reason γ must be swept**, and
it's why "just use default Louvain" would produce a bad product. At smaller N the effect
shrinks (N=500 → backbone m=63 → √(2m)≈11, and clusters of 3–5 people are resolvable), so the
limit bites hardest exactly in the 1,500–3,000 range that's most common.

**Do simpler methods suffice?** Partly — but not entirely, and the gaps matter:
- At **N=500**, single-seeded Leiden was **perfectly stable (ARI = 1.000 across 20 seeds)**.
  Consensus is genuinely unnecessary there and could be skipped as an optimization.
- At **N=3,000**, mean ARI fell to 0.877 with min 0.794. **Consensus is required.**
- **Recommendation: always run consensus.** It costs <15 s, it's a no-op when the graph is
  already stable (it converged in 2 iterations at low mu), and making the behaviour
  size-dependent adds a branch and a surprising failure mode for larger networks. Uniformity
  beats micro-optimization here.

**The genuinely hard part is not the clustering.** At our scale every decent algorithm agrees
when the signal is clean. The difficulty is upstream: **projection weighting and backboning
determine the answer far more than the choice of community detection algorithm.** Budget
Wave 2's effort accordingly — a bad firm-size weight will ruin results that no clustering
algorithm can rescue.

---

## 8. RECOMMENDED DEFAULT PIPELINE

```
INPUT: Connections.csv + optional enrichment + JS/Citadel roster
```

**Step 1 — Build bipartite affiliation graph.**
Nodes: people ∪ affiliations. Affiliation = `(org, kind, start_year, end_year)` bucket.
Kinds: `company_stint`, `school_stint`, `group`, `location` (location weight ×0.1).
*Retain provenance from this point forward.*

**Step 2 — Co-tenure filter.** Drop any co-affiliation pair with
`min(end_i, end_j) − max(start_i, start_j) ≤ 0`. Missing dates → factor 0.3, not 1.0 and not
dropped. **Measured: removes ~62% of candidate edges.**

**Step 3 — Weighted projection onto People.**
```
w_ij = Σ_f  [1/(n_f − 1)] · [min(overlap_f, 5)/5] · [1/log(size_f + e)]
```
Defaults: `COTENURE_CAP = 5` years, `size_f` from a firm-headcount table with fallback
`size_f = n_f` when unknown. **Measured: cuts MegaCorp's weight share 60.8% → 5.4%.**

**Step 4 — Backbone extraction.** Disparity filter, **α = 0.05**. Then **union with each
node's top-3 edges by weight** so no one is stranded. Target density 0.01–0.03; if outside,
adjust α and log it. *(Alternative: noise-corrected, δ = 1.0.)*
**Measured: 55,065 → 7,619 edges, 97% of nodes retained.**

**Step 5 — Consensus Leiden.**
`sparse_consensus(g, n_runs=50, threshold=0.5, max_iter=10, base_seed=0, gamma=γ)`
using `leidenalg.RBConfigurationVertexPartition`, weighted, explicit `seed=`.
**Sweep γ ∈ {1.0, 2.0, 4.0}; default γ = 2.0** (median cluster ≈ 41 — actionable, vs 64 at
γ=1.0). Surface the sweep as a UI granularity slider.
**Measured: cross-ensemble ARI 0.92–1.00.**

**Step 6 — Score proximity.** Personalized PageRank, **α = 0.85** (restart 0.15), weighted,
personalization = uniform over JS+Citadel seeds present in the backbone.

**Step 7 — Rank clusters.** By **mean PPR of non-seed members**. Report per cluster: size,
seed count, seed %, mean PPR, and % of members with a direct backbone edge to a seed.

**Step 8 — Explain.** For each surfaced person emit the provenance trail:
`via <seed>: <firm>, co-tenure YYYY–YYYY (w=…)`. Always show the weighted-count baseline
next to the PPR score. **Measured Spearman(PPR, baseline) = 0.584** — if this correlation
ever drops near zero, the PPR is being driven by something the user can't see: alert, don't ship.

**Step 9 — Quality gate.** Compute modularity, conductance, size distribution. Warn if
modularity < 0.3 (no real structure), if any cluster > 25% of nodes, or if > 50% of nodes are
singletons.

### Default parameters, collected

| Parameter | Default | Source |
|---|---|---|
| `COTENURE_CAP` | 5 years | judgement; saturating |
| `MISSING_DATE_FACTOR` | 0.3 | judgement — **uncertain, tune in Wave 2** |
| firm-size damping | `1/log(size + e)` | judgement — **uncertain, tune in Wave 2** |
| `LOCATION_WEIGHT` | 0.1 | judgement |
| disparity `alpha` | **0.05** | Serrano 2009; verified 13.8% edges / 42% weight |
| top-k rescue edges | 3 | prevents stranding (verified failure at N=500) |
| consensus `n_runs` | **50** | Lancichinetti–Fortunato; verified sufficient |
| consensus `threshold` | **0.5** | Lancichinetti–Fortunato |
| consensus `max_iter` | 10 | converged in 2–3 in practice |
| Leiden partition type | `RBConfigurationVertexPartition` | γ-tunable modularity |
| resolution `gamma` | **2.0** (sweep 1.0 / 2.0 / 4.0) | measured cluster-size table §2.3 |
| PPR `alpha` (damping) | **0.85** (restart 0.15) | measured seed-mass table §4.1 |
| `random_seed` | 0 | must be explicit everywhere |

---

## 9. TESTABLE PROPERTIES FOR WAVE 3

Thresholds below are set **conservatively relative to measured values** so tests aren't flaky.
Measured value shown in parentheses.

### Correctness on planted communities (LFR)
1. LFR n=1000, mu=0.1 → **NMI vs truth > 0.90** *(measured 0.999)*
2. LFR n=1000, mu=0.1 → **ARI vs truth > 0.90** *(measured 0.999)*
3. LFR n=1000, mu=0.2 → **NMI > 0.85** *(measured 0.982)*
4. LFR n=1000, mu=0.3 → **NMI > 0.80** *(measured 0.935–0.938)*
5. LFR n=1000, mu=0.1 → recovered cluster count within ±20% of 25 *(measured exactly 25)*
6. LFR n=1000, mu=0.4 → **NMI > 0.60**; consensus must beat single-run
   *(measured 0.756 vs 0.719)*
7. SBM with 4 imbalanced blocks (50/40/30/200) → **ARI > 0.85**

### Determinism (must be exact, not approximate)
8. Same seed, 10 repeats → **membership vectors byte-identical** *(verified true for
   `leidenalg`)*
9. Full pipeline twice on identical input → **ARI == 1.0 exactly**
10. Consensus with base_seed 0 vs 1000 vs 2000, mu=0.2 → **pairwise ARI > 0.95**
    *(measured 1.0000)*
11. Same, mu=0.3 → **ARI > 0.95** *(measured 0.9970)*
12. Same, mu=0.4 → **ARI > 0.85** *(measured 0.9210)*
13. Consensus stability must **exceed** single-run stability at mu=0.4
    *(measured 0.921 vs 0.602)*
14. **Regression guard:** never call `igraph.community_leiden` in production code — it is
    non-deterministic (verified). Assert via a source grep test.

### Graph construction
15. Naive vs Newman-weighted projection: naive gives ≥2× the edge count *(measured 150,303 vs
    56,964 after co-tenure)*
16. Co-tenure filter removes >40% of candidate edges on a fixture with realistic date spread
    *(measured 62%)*
17. Two people at a 200,000-person firm get **strictly lower** edge weight than two at a
    40-person firm, all else equal
18. Two people at the same firm with **zero** date overlap ⇒ **no edge** (weight exactly 0)
19. Firm-size weight share: largest firm's share of total weight must be **< 15%** after
    Newman+size weighting *(measured 5.4%; naive was 60.8%)*
20. Disparity filter at α=0.05 retains **>90% of nodes** *(measured 96.9%)* and **<25% of
    edges** *(measured 13.8%)*
21. Disparity filter output density ∈ [0.005, 0.05] on the standard fixture *(measured 0.0113)*
22. **No node is stranded**: after backbone + top-k rescue, isolate count == 0
23. Backbone is a subgraph: every backbone edge exists in the projection with an equal weight
23a. **Noise floor:** on a graph with uniformly random weights, disparity filter at α=0.05
    retains **< 5% of edges** *(measured: exactly 0 of 3,163)* — proves the filter isn't
    passing noise through
23b. Backbone edge count is **monotonically non-decreasing in α** (α=0.01 ⊆ α=0.05 ⊆ α=0.5)
24. Newman weighting matches `nx.bipartite.collaboration_weighted_projected_graph` exactly on a
    small hand-checked fixture *(verified: 3-member + 2-member groups → weight 1.5)*

### Scoring
25. PPR with seeds = JS+Citadel: **every seed outranks the median non-seed**
26. A person with 3 backbone edges to seeds outranks an otherwise-identical person with 0
27. **Spearman(mean cluster PPR, direct-adjacency %) > 0.4** *(measured 0.584)* — catches PPR
    being driven by invisible structure
28. PPR α=0.85 leaves **>70% of mass off the seed set** *(measured 77.0%)*
29. Every surfaced person has a **non-empty provenance trail** — no unexplainable results
30. Provenance weights sum to the stored edge weight (within float tolerance)

### Robustness / degenerate inputs
31. Empty connections file → clean error, no crash
32. Single connection → returns empty clusters, no crash
33. All connections at one firm → must not return one giant cluster covering >90% of nodes
    *(this is the MegaCorp pathology — the guard in Step 9)*
34. Disconnected graph → clusters never span components
35. **Every returned cluster is internally connected** *(the Leiden guarantee — assert it
    directly; this is what Louvain violates in up to 16% of cases)*
36. Person with no employment history → appears in output, not silently dropped

### Performance
37. n=3,000 backbone: single Leiden < 1 s *(measured 40 ms)*
38. n=3,000: full consensus (50 runs) < 60 s *(measured ~20 s dense / faster sparse)*
39. n=1,000: LFR fixture generation < 2 s *(measured 0.03–0.19 s)*

---

## 10. Open questions and uncertainties

Stated plainly, so Wave 2/3 don't treat guesses as findings:

1. **Firm-size damping form.** `1/log(size + e)` is a defensible prior, not a derived result.
   `1/sqrt(size)` or a bucketed step function may work better. Needs empirical tuning against
   real data — **this is the highest-uncertainty parameter in the pipeline.**
2. **`MISSING_DATE_FACTOR = 0.3`** is a guess. Real LinkedIn exports have a lot of missing
   dates; the right value depends on how many, which I couldn't measure without real data.
3. **Disparity α interacts with graph sparsity.** I verified it over-prunes on sparse
   affiliation graphs (N=500 → only 86 of 500 nodes retained). The top-k rescue mitigates this,
   but α likely needs to be data-adaptive (target a density band rather than a fixed α).
4. **Noise-corrected vs disparity on *this* data.** They gave similar sparsity on my simulated
   graph, but Coscia & Neffke argue NC is better specifically for bipartite projections. I
   could not distinguish them without ground truth. Worth an A/B in Wave 2.
5. **Whether γ=2.0 is right** depends on what cluster size is actually useful in the UI. My
   recommendation assumes ~20–40 people is actionable. If the product wants ~10, use γ=4–8.
6. **SBM was not benchmarked** because graph-tool isn't installable here. Its degree-corrected
   variant may genuinely outperform modularity on our heavy-tailed projection. Unresolved.
7. **My simulated data is not real data.** Firm-size distributions, co-tenure patterns and
   roster overlap were all synthesized. The *relative* conclusions (naive projection is
   catastrophic; consensus fixes flapping; disparity works) are robust, but absolute numbers
   will shift.

---

## References

- Traag, Waltman & van Eck (2019). *From Louvain to Leiden: guaranteeing well-connected communities.* Scientific Reports 9:5233. https://www.nature.com/articles/s41598-019-41695-z
- Traag, Van Dooren & Nesterov (2011). *Narrow scope for resolution-limit-free community detection.* Phys. Rev. E 84:016114. https://arxiv.org/abs/1104.3083
- Lancichinetti & Fortunato (2012). *Consensus clustering in complex networks.* Scientific Reports 2:336. https://www.nature.com/articles/srep00336
- Serrano, Boguñá & Vespignani (2009). *Extracting the multiscale backbone of complex weighted networks.* PNAS. https://www.pnas.org/doi/10.1073/pnas.0808904106
- Coscia & Neffke (2017). *Network Backboning with Noisy Data.* IEEE ICDE. https://arxiv.org/pdf/1701.07336
- Coscia (2019). *The Impact of Projection and Backboning on Network Topologies.* https://arxiv.org/pdf/1906.09081
- Newman (2001). *Scientific collaboration networks II: Shortest paths, weighted networks, and centrality.* Phys. Rev. E 64:016132.
- Neal (2014). *The backbone of bipartite projections.* Social Networks. https://www.sciencedirect.com/science/article/abs/pii/S0378873314000343
- Fortunato & Barthélemy (2007). *Resolution limit in community detection.* PNAS.
- graph-tool installation docs. https://graph-tool.skewed.de/installation.html
