# R3 — Graph Methodology

Research agent R3, Wave 1. Date: 2026-08-17.
All empirical claims in this document were verified on **Python 3.11.15 (GCC 13.3.0, Linux x86_64, glibc 2.39)**
in a scratch venv. Timings and numbers are measured, not estimated.

---

## Summary

1. **Project onto `M`, don't cluster the bipartite graph directly.** Measured head-to-head across
   0–80% target-column loss, projection+Leiden and native bipartite CPM are statistically
   indistinguishable (NMI 0.885 vs 0.887 at full data; 0.759 vs 0.746 at 80% loss). Projection wins on
   engineering grounds: one resolution knob instead of three, all the brokerage machinery works on it,
   and it is far more robust to resolution misspecification (γ ∈ [0.5, 2.0] all give k=8 correct;
   bipartite `resolution_parameter_01` collapses to k=1 at 0.005 and explodes to k=30 at 0.2).
2. **Use resource-allocation (RA) weighting, `w_uv = Σ_{t ∈ N(u)∩N(v)} 1/deg(t)`.** This is the fix for the
   hub pathology. Measured: a target with degree 100 (vs median 7) hands a raw co-count weight of
   **1.0000** to a pair sharing nothing else; RA gives **0.0100**, Adamic-Adar only **0.2171**. AA under-corrects
   because `1/log(d)` decays too slowly. RA also beat raw count on accuracy (NMI 0.885 vs 0.864).
3. **Leiden over Louvain**, via `leidenalg.RBConfigurationVertexPartition`. Louvain can return
   internally disconnected communities (up to 25% badly connected, 16% disconnected in Traag et al.);
   Leiden guarantees connectivity. igraph's native `community_multilevel` is the Louvain fallback.
4. **Determinism requires sorted node order, not just a seed. This is the single most important build
   constraint in this document.** Measured: with `seed=42` held fixed, 12 random vertex permutations of the
   same graph produced **12 distinct partitions**. With node order canonicalised (sorted IDs, sorted edge
   list) and `seed=42`, 12 runs produced **1 partition**, and the pipeline fingerprint was byte-identical
   across two different venvs and three `PYTHONHASHSEED` values.
5. **Consensus over 25 seeds**, Lancichinetti–Fortunato co-association with τ=0.5. Single-seed Leiden gave
   12 distinct partitions across 12 seeds on an ambiguous graph. Consensus also yields a free per-node
   **stability score** (mean in-cluster co-association) to display as confidence.
6. **Use `igraph.betweenness()`, never `networkx.betweenness_centrality()`.** Measured at 2,000 nodes /
   71,758 edges: igraph exact **1.8s**, networkx exact **70.7s** (~40x). At 8,000 nodes igraph exact is 47s
   while networkx's k=256 *approximation* takes 57s and only correlates 0.90 with truth.
7. **Composite bridge score = weighted mean of five percentile ranks** (reach .35, rarity .25, betweenness
   .15, effective size .15, autonomy .10). Percentile ranks make it explainable and unit-free — every
   sub-score is "top X% of your network", which is what the user has to act on.
8. **Never let missing data masquerade as a zero.** Store `E` as an explicit tri-state
   (observed-present / observed-absent / not-collected) with a per-target `harvested` flag. Every reach
   figure is a **lower bound** and must be rendered as "≥ n".
9. **Keep inferred edges in a separate layer with `observed=False` and a calibrated `confidence`.**
   Default pipeline runs observed-only; inferred edges enter only behind an explicit flag and must never
   silently change a headline number.
10. **Skip `graph-tool`.** Empirically confirmed unavailable on PyPI (`uv pip install graph-tool` →
    "no versions of graph-tool"); it needs Boost/CGAL/expat via conda. Its DC-SBM is theoretically
    superior but is not worth a conda dependency for a local tool at this scale.
11. **Skip `cdlib`.** It installs fine but drags in matplotlib, plotly, seaborn, pyvis, pulp and
    ~30 more packages. The recommended set is **15 packages total**, all prebuilt wheels, no compiler needed.
12. **Test against planted-partition/SBM, not LFR.** Measured: SBM planted partitions give
    NMI=ARI=**1.0000** exactly and reproducibly (assertable in CI). networkx's `LFR_benchmark_graph`
    **raises `ExceededMaxIterations` at μ=0.1 and μ=0.2** with reasonable parameters, and at μ≥0.4 Leiden
    scores NMI 0.24 — LFR is unusable as a CI gate here.

---

## Findings

### 1. Modelling the problem

#### 1.1 Formalisation

Bipartite graph `G = (M ∪ T, E)`, `E ⊆ M × T`, edge `(m,t)` iff m is a 1st-degree connection of t.
Let `A ∈ {0,1}^{|M|×|T|}` be the biadjacency matrix. Degrees: `d_m = Σ_t A_mt` (member's *reach* into T),
`d_t = Σ_m A_mt` (target's *observed mutual count*).

The deliverable is cluster structure over `B = { m ∈ M : d_m > 0 }`. Note `|B| ≪ |M|` typically — most
of the user's 500–10,000 connections touch no target at all, and the pipeline should drop them early.

#### 1.2 Projection weighting schemes

All defined over the shared-target set `S_uv = N(u) ∩ N(v)`:

| Scheme | Formula | Hub-corrected? | Notes |
|---|---|---|---|
| Raw co-count | `|S_uv|` | **No** | A single mega-target makes hundreds of unrelated people mutually adjacent at full weight |
| Jaccard | `|S_uv| / |N(u) ∪ N(v)|` | Partly | Corrects for *member* degree, not *target* degree. Wrong axis for our pathology |
| Cosine (Salton) | `|S_uv| / √(d_u d_v)` | Partly | Same wrong axis as Jaccard |
| Adamic-Adar | `Σ_{t∈S_uv} 1/log d_t` | Weakly | Correct axis but decays too slowly; measured hub weight 0.2171 |
| **Resource allocation** | **`Σ_{t∈S_uv} 1/d_t`** | **Yes** | Zhou et al. 2007 ProbS. Measured hub weight 0.0100 |
| Newman collaboration ("hyperbolic") | `Σ_{t∈S_uv} 1/(d_t − 1)` | Yes | Newman 2001. Near-identical to RA; singular at `d_t=1` |

**The hub pathology.** Bipartite projection is lossy, and naive co-occurrence projection turns every
target of degree `d` into a `d`-clique at full weight. One popular target — a recruiter, a well-known
partner, a conference organiser — therefore manufactures a large artificially cohesive blob that any
modularity-based method will happily report as a community. This is not a hypothetical:
in the measured test, a single degree-100 target inside a 6-pocket planted instance gave two otherwise
unrelated members a raw co-count weight of 1.0, equal to a genuine same-pocket tie.

**Measured hub-suppression (degree-100 hub vs median target degree 7, weight assigned to a hub-only pair):**

| Weighting | NMI | ARI | k | hub-pair weight |
|---|---|---|---|---|
| count | 0.891 | 0.889 | 6 | **1.0000** |
| ra | 0.887 | 0.875 | 6 | **0.0100** |
| aa | 0.902 | 0.889 | 6 | 0.2171 |
| newman | 0.857 | 0.846 | 6 | 0.0101 |

RA and Newman both crush the hub by two orders of magnitude. RA is preferred over Newman purely because
`1/(d_t − 1)` is undefined at `d_t = 1` and needs a guard. This is exactly the "rare shared neighbour is
more informative" intuition of Adamic-Adar, taken to full inverse-degree strength.

Note that on *clean* planted instances all weightings score similarly (NMI 0.86–0.92) — the weighting
choice does not matter much when the data is well-behaved. It matters enormously when there is a hub,
which real LinkedIn data will always have.

Sources:
- Zhou, Ren, Medo, Zhang, "Bipartite network projection and personal recommendation", Phys. Rev. E 76, 046115 (2007) — https://link.aps.org/doi/10.1103/PhysRevE.76.046115
- Newman, "Scientific collaboration networks II", Phys. Rev. E 64, 016132 (2001) — cited in the networkx `collaboration_weighted_projected_graph` docstring
- Adamic & Adar, "Friends and neighbors on the Web", Social Networks 25:211–230 (2003) — https://en.wikipedia.org/wiki/Adamic%E2%80%93Adar_index
- Liben-Nowell & Kleinberg, "The Link Prediction Problem for Social Networks" — https://www.cs.cornell.edu/home/kleinber/link-pred.pdf
- https://en.wikipedia.org/wiki/Bipartite_network_projection

#### 1.3 Direct bipartite community detection vs projecting first

Barber's bipartite modularity uses a null model restricted to bipartite-admissible edges, optimised by
BRIM; biLouvain uses Murata+ modularity and is deterministic. The standing critique of projection is
information loss: after projecting you keep one node class and discard the other.

`leidenalg` ships native bipartite support: `CPMVertexPartition.Bipartite(graph,
resolution_parameter_01, resolution_parameter_0=0, resolution_parameter_1=0,
degree_as_node_size=False, types='type')`, returning three partitions optimised jointly via
`Optimiser.optimise_partition_multiplex([p01, p0, p1], layer_weights=[1,-1,-1])`.

**Measured head-to-head** (8 planted pockets, 320 members, 80 targets, 15% cross-pocket noise;
`drop` = fraction of target columns deleted to simulate uncollected targets; evaluated only on members
retaining ≥1 observed edge):

| drop | method | k | NMI | ARI | sec |
|---|---|---|---|---|---|
| 0.0 | proj-RA | 8 | 0.8851 | 0.8813 | 0.04 |
| 0.0 | proj-AA | 8 | 0.8850 | 0.8808 | 0.05 |
| 0.0 | proj-count | 8 | 0.8641 | 0.8605 | 0.04 |
| 0.0 | **bipartite-CPM** | 8 | 0.8872 | 0.8815 | 0.05 |
| 0.3 | proj-RA | 8 | **0.8613** | **0.8471** | 0.03 |
| 0.3 | bipartite-CPM | 8 | 0.8511 | 0.8384 | 0.05 |
| 0.6 | proj-RA | 7 | **0.7971** | **0.7777** | 0.05 |
| 0.6 | bipartite-CPM | 9 | 0.7959 | 0.7686 | 0.06 |
| 0.8 | proj-RA | 7 | **0.7594** | **0.7570** | 0.02 |
| 0.8 | bipartite-CPM | 7 | 0.7463 | 0.7480 | 0.07 |

The theoretical advantage of direct bipartite clustering does not materialise. RA projection matches or
beats it at every missing-data level. And RA degrades gracefully: even with 80% of targets uncollected
it still recovers NMI 0.76.

**Resolution robustness is the decisive difference:**

| bipartite `res_01` | k | NMI | | projection `γ` | k | NMI |
|---|---|---|---|---|---|---|
| 0.005 | **1** | 0.0000 | | 0.5 | 8 | 0.8780 |
| 0.01 | 5 | 0.7554 | | 0.75 | 8 | 0.8851 |
| 0.02 | 8 | 0.8990 | | 1.0 | 8 | 0.8851 |
| 0.05 | 8 | 0.8872 | | 1.25 | 8 | 0.8851 |
| 0.1 | 9 | 0.8889 | | 1.5 | 8 | 0.8991 |
| 0.2 | **30** | 0.7017 | | 2.0 | 8 | 0.9061 |

The bipartite route needs its resolution tuned within a narrow band around 0.02–0.1 or it fails
catastrophically. The projection route is correct across a 4x range of γ. For a tool that must work
unattended on data whose density we cannot predict, that robustness is worth more than 0.002 NMI.

Sources:
- Barber, "Modularity and community detection in bipartite networks", Phys. Rev. E 76, 066102 (2007)
- Pesantez-Cabrera & Kalyanaraman, biLouvain — https://eecs.wsu.edu/~ananth/papers/bilouvainMain.pdf
- "Community Detection in Large-Scale Bipartite Biological Networks" — https://www.frontiersin.org/journals/genetics/articles/10.3389/fgene.2021.649440/full
- Arthur, "Modularity and Projection of Bipartite Networks" — https://arxiv.org/pdf/1908.02520

#### 1.4 Degree-corrected SBM, and the modularity critiques

Two well-founded critiques of modularity maximisation:

**Resolution limit** (Fortunato & Barthélemy, PNAS 2007): modularity maximisation cannot resolve
communities smaller than ~`√(2L)` edges, where `L` is the total edge count, and will merge small
well-defined clusters into larger sparser ones. Directly relevant here: a genuine 4-person pocket around
one Jane Street desk is exactly the object we care about, and exactly what modularity tends to swallow.

**Degeneracy** (Good, de Montjoye & Clauset 2010): the modularity landscape typically has an exponential
number of near-optimal partitions with very different structure, so the argmax is not meaningfully
"the" answer.

Both are real. The mitigations adopted here are (a) CPM as an available alternative quality function,
whose γ is interpretable as a direct edge-density threshold and which is not subject to the resolution
limit in the same way, (b) consensus clustering across seeds to average over the degenerate landscape,
and (c) reporting stability per node so the user can see which assignments are shaky.

The principled alternative is Bayesian inference of a **degree-corrected SBM** (Karrer & Newman 2011),
ideally Peixoto's nested/hierarchical DC-SBM, which fits a generative model, corrects for broad degree
distributions, selects the number of groups by minimum description length rather than a resolution
knob, and resolves the resolution limit in the hierarchical formulation. Newman also showed modularity
maximisation is equivalent to a special-case DC-SBM likelihood, which frames modularity as a restricted
version of the same idea.

**Rejected on practical grounds only.** The only mature implementation is `graph-tool`, which is
conda-only (see §7). For a local tool whose graphs are ≤10,000 nodes and whose output must be explainable
to a non-specialist, an inference framework the user cannot `pip install` is the wrong trade. Recorded
in §Rejected as a future upgrade if the tool ever grows a conda path.

Sources:
- Fortunato & Barthélemy, "Resolution limit in community detection", PNAS 2007 — https://www.pnas.org/doi/10.1073/pnas.0605965104
- Peixoto, "Bayesian stochastic blockmodeling" — https://arxiv.org/abs/1705.10225
- Lee & Wilkinson, "A review of stochastic block models and extensions for graph clustering" — https://appliednetsci.springeropen.com/articles/10.1007/s41109-019-0232-2
- graph-tool inference docs — https://graph-tool.skewed.de/static/doc/demos/inference/inference.html

---

### 2. Community detection algorithm selection

#### 2.1 Louvain vs Leiden

Louvain's local-moving phase can strand a community's nodes such that the community becomes internally
disconnected — the algorithm never re-examines connectivity within a community once aggregated. Traag,
Waltman & van Eck measured **up to 25% badly connected and up to 16% disconnected communities** on real
networks, worsening with iteration. Leiden adds a refinement phase that guarantees γ-connectivity, and
under iteration converges to a partition where all subsets of all communities are locally optimally
assigned. Leiden is also faster in practice.

For this project the disconnection defect is disqualifying for Louvain: a "cluster of bridges" that is
internally disconnected is not a pocket, and the user would act on a fiction.

Source: Traag, Waltman & van Eck, *Scientific Reports* 9:5233 (2019) — https://www.nature.com/articles/s41598-019-41695-z / https://arxiv.org/abs/1810.08473

#### 2.2 Algorithm comparison

Measured on the 8-node smoke fixture (all found the correct 3 pockets or a coarsening thereof):

| Algorithm | igraph call | k | Verdict |
|---|---|---|---|
| **Leiden (leidenalg)** | `la.find_partition(...)` | 3 | **Primary.** Seeded, resolution-parameterised, connectivity guarantee |
| Leiden (igraph native) | `g.community_leiden(...)` | 3 | Viable but **no `seed` argument** — must seed the global RNG |
| Louvain | `g.community_multilevel(weights=...)` | 3 | **Fallback.** No extra dependency; accept disconnection risk |
| Walktrap | `g.community_walktrap(...).as_clustering()` | 3 | Deterministic, O(n²log n); fine at our scale, no resolution control |
| Infomap | `g.community_infomap(edge_weights=...)` | 2 | Flow-based; under-splits weighted projections. Not recommended |
| Label propagation | `g.community_label_propagation(...)` | 2 | Fast but unstable, no resolution control. Not recommended |
| Fast-greedy (CNM) | `g.community_fastgreedy(...).as_clustering()` | 3 | Superseded by Leiden |

**Recommendation: primary = `leidenalg` Leiden with `RBConfigurationVertexPartition`; fallback = igraph
`community_multilevel` (Louvain).** The fallback exists so the pipeline still runs if `leidenalg` (the
only package in the set needing a C++ build toolchain upstream) is unavailable on some future platform.

`RBConfigurationVertexPartition` is chosen over `ModularityVertexPartition` because it exposes
`resolution_parameter` (at γ=1.0 it is exactly modularity), and over `CPMVertexPartition` because CPM's
γ is an absolute density threshold that must be re-tuned per dataset, whereas RBConfiguration's γ=1.0
default is meaningful on any graph. Keep CPM available as a config option for users chasing small dense
pockets, since it sidesteps the resolution limit.

#### 2.3 Resolution parameter selection

γ=1.0 is the default and is exactly modularity. Measured on the golden fixture, k=3 and q=0.260000 are
identical across γ ∈ {0.5, 1.0, 1.5, 2.0} — well-separated data is insensitive to γ. On the noisier
320-member instance, γ ∈ [0.5, 2.0] all recovered k=8 with NMI 0.878–0.906.

Recommended procedure: **sweep γ ∈ {0.5, 0.75, 1.0, 1.25, 1.5, 2.0}, and select the γ whose consensus
partition is most stable** (highest mean co-association), tie-broken toward γ=1.0. Expose γ as a CLI
override. Do not auto-select on modularity — that just re-runs the degeneracy problem.

#### 2.4 Determinism — the critical build constraint

`leidenalg.find_partition` accepts `seed`; the RNG is not reseeded between replicates by design.
**A fixed seed alone is not sufficient for reproducibility.**

Measured on an LFR graph (n=300, μ=0.45), holding `seed=42` fixed:

| Condition | Distinct partitions |
|---|---|
| 12 random vertex permutations, same seed | **12** |
| 12 runs, same node order, same seed | **1** |
| 12 different seeds, same node order | **12** |

The algorithm's tie-breaking depends on vertex index order, so node insertion order is part of the
determinism contract. The full recipe:

1. Sort member IDs lexicographically; build the index map from the sorted list.
2. Emit the edge list as sorted `(min_idx, max_idx, weight)` tuples.
3. Round weights to 12 decimal places before they enter igraph, so float accumulation order cannot
   perturb tie-breaks.
4. Pass an explicit integer `seed` to every `find_partition` call.
5. For the bipartite route, use `Optimiser(); opt.set_rng_seed(seed)` — verified deterministic over 4 runs.
6. For igraph-native calls with no `seed` argument, `igraph.set_random_number_generator(random.Random(s))`
   before each call — verified reproducible.

**Verified end to end:** the full pipeline prototype produced fingerprint
`3b2d29e8f047567aceb3531b34ec833113f3e612ca015e2037db827a8ff39304` identically under
`PYTHONHASHSEED=1` and `PYTHONHASHSEED=999`, in two different virtualenvs. `PYTHONHASHSEED` does not
need to be pinned *provided* node order is sorted — but sorting is mandatory.

#### 2.5 Consensus clustering

Lancichinetti & Fortunato's consensus procedure: run the algorithm `n_runs` times, build the
co-association matrix `C_ij` = fraction of runs placing i and j together, threshold at τ to drop
non-significant co-occurrence, re-cluster the consensus graph, repeat to convergence.

Measured: `n_runs=25`, τ=0.5, one re-clustering pass gave a stable k=3 on the test instance, identical
across two disjoint seed blocks (`base_seed=0` and `base_seed=1000`). One pass is sufficient at our
scale; iterate-to-convergence is unnecessary complexity.

The co-association matrix is also the **confidence surface**: per-node stability = mean `C_ij` over
`j` in the same final cluster. Surface this in the output — it tells the user which cluster assignments
to trust.

Source: Lancichinetti & Fortunato, "Consensus clustering in complex networks", *Sci. Rep.* 2:336 (2012) — https://www.nature.com/articles/srep00336 / https://arxiv.org/pdf/1203.6093

---

### 3. Bridge / brokerage scoring

#### 3.1 The metrics

| Metric | Graph | Definition | Meaning for the user |
|---|---|---|---|
| **Reach** `d_m` | bipartite `B` | count of targets adjacent to m | "knows N people at the firms" |
| **Rarity** `r_m` | bipartite `B` | `Σ_{t∈N(m)} 1/d_t` | "knows people few others know" |
| **Betweenness** | projection `P` | normalised shortest-path betweenness | "sits between otherwise separate pockets" |
| **Effective size** | projection `P` | `|N(u)| − avg redundancy` (Burt) | "non-redundant contacts" |
| **Constraint** | projection `P` | Burt's `c_i = Σ_j (p_ij + Σ_q p_iq p_qj)²` | "how boxed-in they are" (low = broker) |
| **Autonomy** | projection `P` | `1 − min(constraint, 1)` | constraint inverted so high = good |
| **Embeddedness** | projection `P` | mean co-association within cluster | "is this person really in this pocket" |

`rarity` is the single most decision-relevant metric after reach and deserves emphasis: a contact who
knows one obscure Citadel Securities developer is worth more as an introduction path than one who knows
the same well-connected recruiter as 200 other people. It is the node-level analogue of RA weighting.

**Gould-Fernandez brokerage roles** classify each brokered triple `a→v→b` by group membership into
coordinator (all same group), itinerant/consultant (a,b same group, v outside), gatekeeper (a outside,
v,b inside), representative (a,v inside, b outside), and liaison (all three different). Two problems
here: the framework is defined for **directed** networks and our projection is undirected, which
collapses gatekeeper/representative into one indistinguishable role; and the groups would be our own
inferred clusters, making the roles circular. **Recommendation: compute a simplified undirected
two-role version only** — `coordinator_count` (brokered pairs within own cluster) and `liaison_count`
(brokered pairs spanning two other clusters) — and treat it as a descriptive tag, not a score
component. Full G-F adds no actionable information for this use case.

Sources:
- Burt, *Structural Holes* (1992); networkx implementation — https://networkx.org/documentation/stable//_modules/networkx/algorithms/structuralholes.html
- Everett & Borgatti, "Unpacking Burt's constraint measure", *Social Networks* (2020) — https://www.sciencedirect.com/science/article/abs/pii/S0378873320300101
- Gould & Fernandez brokerage, `sna::brokerage` — https://search.r-project.org/CRAN/refmans/sna/html/brokerage.html
- Weighted/normalised G-F — https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0274475

#### 3.2 Recommended composite score

Explainability was the stated priority, so the score is a **weighted mean of percentile ranks**, not a
z-score blend or a learned model. Every component answers "what percentile of your network is this
person on this axis", which composes into a plain-English explanation.

```
components (all computed then percentile-ranked to [0,1] within the bridge set B):
  reach          d_m                       from bipartite B
  rarity         Σ_{t∈N(m)} 1/d_t          from bipartite B
  betweenness    igraph betweenness        from projection P
  effective_size Burt effective size       from projection P  (NaN → 0.0)
  autonomy       1 − min(constraint, 1)    from projection P  (NaN → 0.0, i.e. constraint 1.0)

bridge_score(m) = 0.35·pct(reach)
                + 0.25·pct(rarity)
                + 0.15·pct(betweenness)
                + 0.15·pct(effective_size)
                + 0.10·pct(autonomy)
```

Weights are a defensible prior, not a fitted result, and must be config-overridable. The rationale:
reach dominates because it is the thing the user can most directly act on; rarity is second because it
distinguishes a valuable path from a crowded one; the three structural measures split the remaining 40%
and are correlated with each other, so no single one dominates.

`pct` is a deterministic average-rank percentile with ties sharing the mean rank, sorted by
`(value, member_id)` so ordering is stable. Verified on the golden fixture.

**Verified output on the golden fixture** (m0 is the planted cross-pocket bridge; every metric ranks it
first, which is the sanity property to assert):

| node | reach | rarity | betweenness | eff_size | constraint |
|---|---|---|---|---|---|
| **m0** | **4** | **1.166667** | **0.750000** | **6.250000** | **0.330729** |
| m1 | 2 | 0.666667 | 0.0 | 1.0 | 0.878906 |
| m2 | 2 | 0.666667 | 0.0 | 1.0 | 0.878906 |
| m3–m8 | 2 | 0.583333 | 0.0 | 1.0 | 0.781250 |

**NaN handling is mandatory.** Verified: `nx.effective_size` and `nx.constraint` both return `nan` for
degree-0 nodes in the projection (members whose targets are all private to them). Coalesce
`effective_size → 0.0` and `constraint → 1.0` (maximum redundancy) before ranking, or the whole score
column becomes NaN.

#### 3.3 Performance

Measured on the projection (member-count × projection edges):

| Operation | 2,000 × 71,758 | 8,000 × 338,748 |
|---|---|---|
| `igraph.betweenness()` exact | **1.8s** | **47.4s** |
| `nx.betweenness_centrality()` exact | 70.7s | (not run) |
| `nx.betweenness_centrality(k=64)` | 2.5s, corr 0.851 | 15.4s, corr 0.714 |
| `nx.betweenness_centrality(k=256)` | 10.0s, corr 0.960 | 56.7s, corr 0.903 |
| `igraph.eigenvector_centrality()` | 0.0s | 0.2s |
| `nx.effective_size()` | 1.2s | 11.1s |
| `nx.constraint()` | 0.5s | 5.4s |

**Use `igraph.betweenness()`.** networkx's approximation is both slower and worse than igraph's exact
computation at 8,000 nodes. Keep `nx.effective_size` / `nx.constraint` — they have no igraph equivalent
and their cost is acceptable. If a graph ever exceeds ~15,000 bridge nodes, gate betweenness behind a
flag rather than approximating it.

---

### 4. The sparse / missing-data problem

#### 4.1 Do not confuse "not collected" with "no edge"

The failure mode: a target whose mutual-connections page was never opened has `d_t = 0`, which is
indistinguishable in the matrix from a target genuinely connected to nobody in `M`. Any statistic over
targets (rarity denominators, coverage, "who is most connected") is then silently wrong.

**Required data model — tri-state, not boolean:**

- `Target.harvested: bool` — was this target's mutual list actually retrieved?
- `Edge` rows exist **only** for observed-present pairs.
- Absence of an edge means "no edge" **only if** `target.harvested is True`; otherwise it means unknown.

Consequences that must be enforced in code:

- `rarity` denominators `d_t` must be computed over harvested targets only.
- `reach` is a **lower bound**. Render as `≥ n`, never as `n`.
- Cluster structure is conditioned on the harvested subset. If coverage is low, say so in the output
  header rather than in a footnote.
- Never impute a zero. A target with `harvested=False` is excluded from the matrix entirely.

Measured on the head-to-head: at 60% of targets uncollected, RA projection still recovers NMI 0.797, and
at 80% NMI 0.759. Degradation is graceful, which supports shipping partial results — but only with the
coverage figure attached.

#### 4.2 Which target to collect next (active learning)

Standard active-learning taxonomy — uncertainty, diversity, expected-performance, representativeness —
maps cleanly here. A pure uncertainty criterion is unavailable (we cannot estimate `P(edge)` before
looking), so use a **representativeness + novelty** proxy that needs no model:

```
priority(t) = α·n_known_bridges_touching(t)      # yield: how many known bridges we expect to confirm
            + β·n_distinct_clusters_touched(t)   # spanning: connects pockets we already know
            + γ·Σ_{c ∈ clusters(t)} 1/(1+seen_c) # novelty: favour under-sampled clusters
   defaults α=1.0, β=1.0, γ=0.5
```

`n_known_bridges_touching(t)` uses whatever partial signal exists (an inferred edge, a shared employer,
a target already partially observed); when nothing is known it falls back to the novelty term, which
keeps the ordering total and deterministic. Break ties by `target_id` for reproducibility.

Cold start (no `E` at all): fall back to firm/team stratification — sample targets to cover distinct
`(firm, team)` cells uniformly, so the first harvest round maps the breadth of `T` rather than
over-sampling one desk.

#### 4.3 Expressing confidence

Report three distinct numbers, never collapsed into one:

1. **Coverage** = `|harvested targets| / |T|`, printed in the report header.
2. **Per-node stability** = mean in-cluster co-association from the consensus matrix, in `[0,1]`.
3. **Reach as a lower bound** with a Wilson score interval on the *share* of harvested targets a member
   reaches. Verified: `wilson_lb(3, 5) = 0.231` at z=1.96 — with only 5 targets harvested, observing 3
   hits supports a true reach share of only ≥23%, which correctly communicates how little 5 samples say.

Sources:
- Active sampling / uncertainty on graphs — https://arxiv.org/html/2501.08450v1, https://arxiv.org/pdf/1705.05085
- SMARTQUERY hybrid uncertainty reduction — https://arxiv.org/pdf/2212.01440

---

### 5. The inference fallback

When `E` is unavailable for a target, infer likely adjacency from shared attributes. **Inferred edges
must never be silently mixed with observed ones.**

#### 5.1 Hard separation

- `Edge.observed: bool` and `Edge.confidence: float` on every edge.
- Observed edges always have `confidence = 1.0`.
- The default pipeline runs `include_inferred=False`. Inferred edges enter only via an explicit flag.
- Any output row derived from an inferred edge carries a visible marker.
- Run the pipeline **both ways** and report the delta, so the user sees exactly what the inference bought.

#### 5.2 Scoring model

Additive log-odds over inverse-frequency-damped cues. Log-odds because cues are conditionally
near-independent and it keeps the output a calibrated probability; inverse-frequency damping because
"we both worked at a 50,000-person company" is nearly worthless while "we both worked at an 80-person
startup" is strong evidence — the same rare-feature logic as RA weighting and Adamic-Adar.

```
z = -3.0                                                     # base rate, P≈0.047 with no cues
if same_employer and tenure_overlap_years > 0:
    z += 2.2 · min(overlap, 4)/4 · √(log(100)/log(max(employer_size, e)))
if same_school and year_overlap > 0:
    z += 1.4 · min(overlap, 4)/4 · √(log(500)/log(max(cohort_size, e)))
if same_city:      z += 0.5 · city_pop_bucket
if shared_groups:  z += 0.3 · min(shared_groups, 3)
p_inferred = sigmoid(z)
```

**Verified outputs:**

| cue set | p_inferred |
|---|---|
| no cues | 0.047 |
| same city only | 0.076 |
| same employer 3y, 200-person company | 0.188 |
| same employer 3y, 50,000-person company | 0.127 |
| employer 4y (80-person) + school 2y + city | 0.619 |

The coefficients are a documented prior, not a fit — we have no labelled data. They are chosen so that a
single weak cue never crosses the default `min_confidence=0.5` gate, and only a genuine
multi-cue coincidence does. Tenure overlap saturates at 4 years because marginal returns beyond
"we overlapped for a few years" are negligible.

Tenure overlap is the strongest available cue: the homophily literature consistently finds shared
organisational membership among the strongest predictors of tie formation, operating through focal
closure (ties forming between people sharing a context, without common acquaintances). Same-employer
overlap therefore deserves roughly double the weight of same-school overlap, which is itself far
stronger than co-location.

Sources:
- McPherson, Smith-Lovin & Cook, "Birds of a Feather: Homophily in Social Networks", *Annu. Rev. Sociol.* 27 (2001)
- Kossinets & Watts on focal vs triadic closure; https://arxiv.org/pdf/1808.05035
- Occupational/organisational homophily — https://arxiv.org/pdf/2004.09293

---

### 6. Cluster labelling

**Recommendation: TF-IDF over per-cluster attribute documents, top-3 terms.** Simple, deterministic, no
model, no network call.

Build one pseudo-document per cluster by concatenating member attributes (company, position, school,
city), fit `TfidfVectorizer` across the cluster documents, and take the highest-weighted terms per
cluster. Because IDF is computed *across clusters*, terms shared by every cluster (e.g. "engineer" if
everyone is an engineer) are automatically suppressed, which is exactly the desired behaviour.

Verified configuration:

```python
TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=1,
                token_pattern=r"(?u)\b\w[\w\-\.]+\b", lowercase=True)
```

Verified output on a synthetic three-cluster corpus:

```
cluster 0: infra, google, sre
cluster 1: wharton, 2016, mba
cluster 2: nyc, quant, jane
```

`sublinear_tf` made no difference to the top-3 on the test corpus but is kept because it damps a single
member repeating the same employer string many times.

**Implementation gotcha found in the prototype.** Concatenating attribute fields with a plain space lets
bigrams straddle field boundaries — the prototype emitted `"engineer wharton"` as a top term, which is a
nonsense position/school hybrid. **Join fields with a sentinel token** (`" | "`) and strip any candidate
n-gram containing it, or build separate per-field vectorizers and merge. Required for readable labels.

Two refinements worth including:

- **Stopword list** for LinkedIn boilerplate: `senior, junior, lead, staff, principal, the, at, of, inc,
  llc, ltd, corp`.
- **Fallback**: if the top TF-IDF weight is below a floor (e.g. 0.15) the cluster has no coherent theme —
  label it `"mixed (n=…)"` rather than emitting a misleading term.

Also emit, alongside the text label, the cluster's **top shared targets** (targets adjacent to the most
cluster members). "These 6 people all reach t_A, t_B, t_C" is often more actionable than any text label,
and it is free.

---

### 7. Library selection for Python 3.11 — empirical results

Platform: Python 3.11.15, GCC 13.3.0, Linux x86_64, glibc 2.39. Installer: `uv 0.8.17`.

| Package | Verdict | Evidence |
|---|---|---|
| **networkx 3.6.1** | **Adopt** | Bipartite utilities, structural holes, planted-partition/SBM generators. Pure Python, no build |
| **python-igraph 1.0.0** | **Adopt** | C core; 40x faster betweenness; full community-detection suite. Prebuilt wheel |
| **leidenalg 0.12.0** | **Adopt** | Leiden with `seed`, resolution, and native bipartite. Prebuilt wheel, no compiler needed |
| **scipy 1.17.1** | **Adopt** | Sparse matmul projection — 0.02s for 8,000×1,600 |
| **numpy 2.4.6** | **Adopt** | Transitive requirement |
| **scikit-learn 1.9.0** | **Adopt** | `normalized_mutual_info_score`, `adjusted_rand_score`, `TfidfVectorizer` |
| **rapidfuzz 3.14.5** | **Adopt** | Name matching; `WRatio("Jon Smith") → "John Smith"` @ 94.7 |
| **pandas 3.0.5** | **Adopt** | CSV ingest of LinkedIn `Connections.csv` |
| polars 1.43.2 | **Reject** | Installs cleanly, but at ≤10,000 rows pandas is not a bottleneck. Adds `polars-runtime-32` |
| scikit-network 0.33.5 | **Reject** | Redundant with igraph+leidenalg. Also ships a version bug: `sknetwork.__version__` reports `0.33.0` while dist metadata says `0.33.5` |
| cdlib | **Reject** | Installs, but pulls matplotlib, plotly, seaborn, pyvis, pulp, thresholdclustering, python-louvain, +~30 more |
| **graph-tool** | **Reject — cannot install** | `uv pip install graph-tool` → `no versions of graph-tool`. Needs Boost/CGAL/expat via conda |

**graph-tool verbatim failure:**
```
× No solution found when resolving dependencies:
╰─▶ Because there are no versions of graph-tool and you require graph-tool,
    we can conclude that your requirements are unsatisfiable.
```
Confirmed by upstream: graph-tool is "not installable via Python-only package management systems such
as pip" — https://graph-tool.skewed.de/installation.html

**Compiler requirement: none.** A compiler is present on this box (`gcc`, `g++`), but the full 15-package
set installed in **50ms**, which is only possible from prebuilt wheels. Native-extension audit:
`leidenalg`, `numpy`, `scipy`, `rapidfuzz`, `pandas`, `scikit-learn` ship `.so` files (all prebuilt);
`networkx` and the `python-igraph` Python shim are pure Python.

**Version compatibility note.** `python-igraph 1.0.0` is a major release with breaking API changes
against the 0.10.x line. `leidenalg 0.12.0` is compatible (verified working). If Wave 2 hits an
igraph API surprise, the documented escape hatch is pinning `python-igraph==0.11.*`, but the pinned
1.0.0 combination is verified working here and should be used.

Sources:
- https://github.com/vtraag/leidenalg and https://github.com/vtraag/leidenalg/blob/main/CHANGELOG
- https://igraph.org/2025/09/20/igraph-1.0.0-c.html
- https://github.com/igraph/python-igraph/releases
- https://graph-tool.skewed.de/installation.html

---

### 8. Validation strategy

#### 8.1 Generator choice — planted partition, not LFR

**networkx `LFR_benchmark_graph` is unsuitable as a CI gate.** Two measured problems:

```
LFR n=500 mu=0.1: ExceededMaxIterations: Could not assign communities; try increasing min_community
LFR n=500 mu=0.2: ExceededMaxIterations: Could not assign communities; try increasing min_community
LFR n=500 mu=0.3: k= 6/6  NMI=0.6984  ARI=0.7150
LFR n=500 mu=0.4: k= 7/6  NMI=0.2419  ARI=0.1612
LFR n=500 mu=0.5: k= 8/6  NMI=0.1237  ARI=0.0644
```
(`tau1=2.5, tau2=1.5, average_degree=10, min_community=30, seed=42, max_iters=5000`)

It raises on the easy cases and scores badly on the hard ones — a flaky test that also fails to
discriminate. `stochastic_block_model` and `planted_partition_graph` are reliable and give exact,
assertable values.

#### 8.2 Verified planted-partition results (Leiden, RBConfiguration γ=1.0, seed=42)

| Case | sizes | p_in | p_out | k found | NMI | ARI | modularity |
|---|---|---|---|---|---|---|---|
| `pp_4x25_p.85_q.02` | 4×25 | 0.85 | 0.02 | 4/4 | **1.0000** | **1.0000** | 0.6752 |
| `pp_3x30_p.60_q.03` | 3×30 | 0.60 | 0.03 | 3/3 | **1.0000** | **1.0000** | 0.5564 |
| `pp_5x40_p.30_q.01` | 5×40 | 0.30 | 0.01 | 5/5 | **1.0000** | **1.0000** | 0.6618 |
| `pp_6x20_p.40_q.05` | 6×20 | 0.40 | 0.05 | 6/6 | 0.9814 | 0.9797 | 0.4316 |
| `pp_8x15_p.50_q.04` | 8×15 | 0.50 | 0.04 | 8/8 | 0.9851 | 0.9804 | 0.4838 |

The first three recover ground truth *exactly* and are the right CI gates: assert `NMI == 1.0`, not
`NMI > 0.9`. The last two are the "hard but should still work" tier at ≥0.95.

#### 8.3 Scale

Measured on a structured instance (8,000 members, 1,600 targets, 40 pockets, |E|=34,675):

| Stage | Time |
|---|---|
| RA sparse projection (`A @ diag(1/d_T) @ Aᵀ`) | **0.02s** → 339,091 projection edges |
| Leiden (`n_iterations=2`) | **2.4s** → k=40, NMI 0.9434, ARI 0.9316 |

**No pruning is needed at the project's stated scale.** Full-fidelity projection + Leiden on the
worst-case 10,000-member graph completes in seconds and gives the best accuracy.

Pruning results, recorded so nobody re-derives them:

| Strategy | edges | isolates | Leiden | k | NMI | ARI |
|---|---|---|---|---|---|---|
| none (`min_shared≥1`) | 339,091 | 0 | 2.4s | 40 | **0.9434** | **0.9316** |
| `min_shared≥2` | 32,383 | 831 | 0.5s | 880 | 0.9136 | 0.8802 |
| `min_shared≥3` | 2,515 | 5,248 | 0.7s | 5,724 | 0.6232 | 0.0665 |
| top-k=10 union | 58,809 | — | 0.7s | 40 | 0.8921 | 0.8698 |
| top-k=30 union | 161,179 | — | 1.5s | 40 | 0.9069 | 0.8876 |

If a future graph does need sparsification, **top-k union is strictly better than `min_shared`
thresholding** — it preserves k=40 exactly while `min_shared≥2` shatters it into 880 clusters by
isolating 831 nodes. Caveat for whoever tunes this: pruning was also tested on a *uniform-random*
bipartite graph where `min_shared≥2` produced 3,825 clusters — random graphs have no structure to
preserve and are a misleading testbed. Tune on structured fixtures only.

#### 8.4 Metrics

- **NMI** (`sklearn.metrics.normalized_mutual_info_score`) — main gate.
- **ARI** (`sklearn.metrics.adjusted_rand_score`) — secondary; more sensitive, chance-corrected.
- **Modularity** — report only; never assert a floor, because of degeneracy.
- **Cluster count k** — assert exactly on clean fixtures.
- **Determinism fingerprint** — SHA-256 of the sorted `{member_id: cluster_id}` JSON. The strongest
  and cheapest regression test available.

---

## Recommended pipeline

Numbered, implementable without further research.

1. **Load `M`.** Read LinkedIn `Connections.csv` with pandas (`skiprows` the export preamble; the real
   header row starts at `First Name`). Build `Member` records. `member_id = sha1(normalized_profile_url)`
   if a URL exists, else `sha1(lower(first + "|" + last + "|" + company))`.
2. **Load `T`.** Build `Target` records with `firm ∈ {jane_street, citadel, citadel_securities}`, an
   optional `team`, and a mandatory `harvested: bool`.
3. **Normalise names** for matching: NFKD-casefold, strip accents/punctuation, **collapse internal
   whitespace** (verified: rapidfuzz's `utils.default_process` does *not* collapse runs of spaces —
   `WRatio("JOHN  SMITH", "john smith")` scores 95.2, not 100), drop suffixes (`jr`, `iii`, `phd`, `cfa`).
   Match with `rapidfuzz.process.extractOne(..., scorer=fuzz.WRatio)`; accept ≥92, queue 85–92 for
   review, reject <85.
4. **Load `E`.** One row per observed mutual connection, `observed=True, confidence=1.0`. Discard any
   edge whose target has `harvested=False`.
5. **Restrict to bridges.** `B_set = {m : d_m > 0}`. Drop all other members before any graph work.
6. **Build the bipartite graph.** `networkx.Graph`, nodes added in **sorted ID order**, members
   `bipartite=0`, targets `bipartite=1`, edges added sorted. Never call `bipartite.sets()` — verified to
   raise `AmbiguousSolution` on disconnected graphs, which ours always will be. Always pass the explicit
   member-node set.
7. **Project onto `M` with RA weighting.** Use the sparse route:
   `A` (CSR, |B_set|×|T_harvested|) → `P = A @ diags(1/max(d_T,1)) @ A.T` → `setdiag(0)`,
   `eliminate_zeros()`. Verified 0.02s at 8,000×1,600. Round weights to 12 dp.
8. **Convert to igraph deterministically.** Sorted node names; edges emitted as sorted
   `(min_idx, max_idx, weight)`; `g.vs["name"]` set from the sorted list.
9. **Cluster with Leiden + consensus.**
   - For `s in range(25)`: `la.find_partition(g, la.RBConfigurationVertexPartition, weights="weight",
     resolution_parameter=γ, n_iterations=-1, seed=base_seed + s)`.
   - Accumulate co-association `C_ij`; divide by 25.
   - Threshold at τ=0.5, build the consensus graph, run Leiden once more with `seed=base_seed`.
   - Default `γ=1.0`, `base_seed=42`. Optionally sweep γ ∈ {0.5, 0.75, 1.0, 1.25, 1.5, 2.0} and keep the
     most stable.
   - Fallback if `leidenalg` is missing: `g.community_multilevel(weights="weight")` after
     `igraph.set_random_number_generator(random.Random(seed))`.
10. **Compute brokerage metrics.** `reach` and `rarity` from the bipartite graph; `betweenness` from
    **`g.betweenness()` (igraph)**; `effective_size` and `constraint` from `networkx` on the projection.
    Coalesce NaN → `effective_size=0.0`, `constraint=1.0`. Derive `autonomy = 1 − min(constraint, 1)`.
11. **Score.** Percentile-rank each of the five components over `B_set` (ties share the average rank,
    sorted by `(value, member_id)`), then
    `score = 0.35·reach + 0.25·rarity + 0.15·betweenness + 0.15·effective_size + 0.10·autonomy`.
12. **Label clusters.** TF-IDF over per-cluster attribute documents, fields joined with `" | "` sentinel,
    top-3 terms; `"mixed (n=…)"` if the top weight < 0.15. Attach the cluster's top-5 shared targets.
13. **Rank and emit.** Sort clusters by `sum(score)` descending, members within a cluster by `score`
    descending, ties by `member_id`. Emit coverage, per-node stability, and `reach` as `≥ n`.
14. **Emit next-harvest priorities.** Top-20 unharvested targets by
    `1.0·n_known_bridges + 1.0·n_clusters_touched + 0.5·novelty`, ties broken by `target_id`.

---

## Verified dependency set

```bash
uv venv --python 3.11 .venv
uv pip install \
  "networkx==3.6.1" \
  "python-igraph==1.0.0" \
  "leidenalg==0.12.0" \
  "numpy==2.4.6" \
  "scipy==1.17.1" \
  "pandas==3.0.5" \
  "rapidfuzz==3.14.5" \
  "scikit-learn==1.9.0"
```

Full resolved set (`uv pip freeze`, 15 packages):

```
igraph==1.0.0
joblib==1.5.3
leidenalg==0.12.0
narwhals==2.24.0
networkx==3.6.1
numpy==2.4.6
pandas==3.0.5
python-dateutil==2.9.0.post0
python-igraph==1.0.0
rapidfuzz==3.14.5
scikit-learn==1.9.0
scipy==1.17.1
six==1.17.0
texttable==1.7.0
threadpoolctl==3.6.0
```

Environment: `Python 3.11.15 (main, Mar 3 2026, 09:26:23) [GCC 13.3.0]`,
`Linux-6.18.5-fc-v20-x86_64-with-glibc2.39`, `uv 0.8.17`.
Install: `Resolved 15 packages in 34ms / Installed 15 packages in 50ms` — all prebuilt wheels,
**no compiler invoked**.

Verified working in this exact environment:

- `networkx.algorithms.bipartite.generic_weighted_projected_graph` with a custom `weight_function`
  (the RA/AA extension point) — confirmed `weight_function(B, u, v)` signature.
- `leidenalg.find_partition(..., seed=, n_iterations=-1, resolution_parameter=)` for
  `RBConfigurationVertexPartition`, `ModularityVertexPartition`, `CPMVertexPartition`.
- `leidenalg.CPMVertexPartition.Bipartite(...)` + `Optimiser.set_rng_seed(...)` +
  `optimise_partition_multiplex([p01,p0,p1], layer_weights=[1,-1,-1])`.
- igraph `community_leiden`, `community_multilevel`, `community_walktrap`, `community_infomap`,
  `community_label_propagation`, `community_fastgreedy`, `betweenness`, `eigenvector_centrality`.
- `networkx.effective_size`, `networkx.constraint`, `networkx.local_constraint`.
- `networkx.planted_partition_graph`, `networkx.stochastic_block_model`.
- `sklearn.metrics.normalized_mutual_info_score`, `adjusted_rand_score`,
  `sklearn.feature_extraction.text.TfidfVectorizer`.
- `rapidfuzz.process.extractOne`, `fuzz.WRatio`, `fuzz.token_sort_ratio`, `utils.default_process`.

**End-to-end determinism proof.** The full pipeline prototype (project → consensus-Leiden → score →
label) produced identical output across two independent virtualenvs and different hash seeds:

```
### run A  (PYTHONHASHSEED=1, .venv-graph)
clusters: 3  sizes: [13, 12, 11]
PIPELINE FINGERPRINT: 3b2d29e8f047567aceb3531b34ec833113f3e612ca015e2037db827a8ff39304

### run B  (PYTHONHASHSEED=999, .venv-lean)
clusters: 3  sizes: [13, 12, 11]
PIPELINE FINGERPRINT: 3b2d29e8f047567aceb3531b34ec833113f3e612ca015e2037db827a8ff39304
```

**Known packaging defect (informational):** `scikit-network` reports `__version__ == "0.33.0"` while its
distribution metadata says `0.33.5`. Not in the recommended set; noted in case it is reconsidered.

---

## Rejected

| Approach | Reason |
|---|---|
| **Raw co-count projection** | Hub pathology. A degree-100 target gives unrelated pairs weight 1.0 (vs 0.0100 under RA). Manufactures fake clusters |
| **Jaccard / cosine projection** | Normalise by *member* degree; our pathology is *target* degree. Wrong axis. Also measured slightly worse (NMI 0.871 vs 0.885) |
| **Adamic-Adar projection** | Right axis but under-corrects — hub-pair weight 0.2171 vs RA's 0.0100. Keep as a config option, not the default |
| **Newman `1/(d_t−1)` weighting** | Equivalent to RA in practice but singular at `d_t=1`, needing a guard for no benefit |
| **Direct bipartite CPM clustering** | Measured equal-or-worse accuracy at every missing-data level, and catastrophically resolution-sensitive (k=1 at 0.005, k=30 at 0.2 vs projection stable over γ ∈ [0.5,2.0]). Keep the code path available behind a flag |
| **Louvain as primary** | Can emit internally disconnected communities (25% badly connected, 16% disconnected). Retained as fallback only |
| **Infomap / label propagation** | Both under-split weighted projections (k=2 where truth is 3) and offer no resolution control |
| **graph-tool DC-SBM** | **Cannot be installed.** Not on PyPI; requires conda + Boost/CGAL/expat. Theoretically the best method — revisit only if the project ever adopts conda |
| **cdlib** | Installs but pulls matplotlib, plotly, seaborn, pyvis, pulp, python-louvain and ~30 more packages for algorithms igraph already has |
| **scikit-network** | Redundant with igraph+leidenalg; plus a `__version__`/metadata mismatch |
| **polars** | Fine, but pandas is not a bottleneck at ≤10,000 rows. Fewer dependencies wins |
| **`networkx.betweenness_centrality`** | 40x slower than igraph exact (70.7s vs 1.8s at n=2,000). Its k-sample approximation is *both* slower and less accurate than igraph exact at n=8,000 |
| **LFR benchmark for CI** | Raises `ExceededMaxIterations` at μ=0.1 and 0.2; scores NMI 0.24 at μ=0.4. Flaky and non-discriminating. Use planted partition/SBM |
| **`min_shared≥2` pruning** | Shatters a correct k=40 into 880 clusters by isolating 831 nodes. Unnecessary anyway — full projection runs in 2.4s at 8,000 members |
| **Full Gould-Fernandez brokerage** | Defined for directed graphs; our projection is undirected, collapsing gatekeeper/representative. Groups would be our own clusters, making roles circular. Simplified 2-role version only |
| **Single-seed Leiden** | 12 seeds → 12 distinct partitions on ambiguous graphs. Consensus is mandatory |
| **`bipartite.sets()`** | Verified to raise `AmbiguousSolution` on disconnected graphs. Always pass the explicit node set |

---

## Implications for the build

### Module boundaries

```
src/interlayer/
  model.py        # dataclasses: Member, Target, Edge, Cluster, BridgeScore, AnalysisResult
  normalize.py    # name/company normalisation, rapidfuzz matching
  graph.py        # bipartite construction + RA projection + igraph conversion
  cluster.py      # Leiden, consensus, stability
  score.py        # brokerage metrics + percentile composite
  label.py        # TF-IDF cluster labelling
  infer.py        # inferred-edge scoring (separate layer)
  collect.py      # active-learning next-target priority
  report.py       # ranking + rendering
```

`graph.py`, `cluster.py`, and `score.py` must have **no I/O** — pure functions over the dataclasses.
That is what makes the deterministic tests in §Test specifications possible.

### Data structures

```python
from dataclasses import dataclass, field
from typing import Literal, Optional

Firm = Literal["jane_street", "citadel", "citadel_securities"]

@dataclass(frozen=True, slots=True)
class Member:
    member_id: str                  # sha1 of normalized profile URL, or name|company
    full_name: str
    first_name: str = ""
    last_name: str = ""
    profile_url: str = ""
    email: str = ""
    company: str = ""
    position: str = ""
    school: str = ""
    city: str = ""
    connected_on: Optional[str] = None      # ISO date from Connections.csv

@dataclass(frozen=True, slots=True)
class Target:
    target_id: str
    full_name: str
    firm: Firm
    team: str = ""
    profile_url: str = ""
    harvested: bool = False         # MANDATORY: False => absence of edge means UNKNOWN

@dataclass(frozen=True, slots=True)
class Edge:
    member_id: str
    target_id: str
    observed: bool = True           # True = harvested mutual; False = inferred
    confidence: float = 1.0         # always 1.0 when observed
    provenance: str = "mutual_connections"

@dataclass(frozen=True, slots=True)
class BridgeScore:
    member_id: str
    reach: int                      # LOWER BOUND — render as ">= reach"
    rarity: float
    betweenness: float
    effective_size: float
    constraint: float
    autonomy: float
    composite: float                # 0..1
    cluster_id: int
    stability: float                # 0..1, mean in-cluster co-association

@dataclass(frozen=True, slots=True)
class Cluster:
    cluster_id: int
    member_ids: tuple[str, ...]     # sorted
    label: str
    top_targets: tuple[str, ...]    # sorted by shared-member count desc, then id
    mean_stability: float

@dataclass(frozen=True, slots=True)
class AnalysisResult:
    clusters: tuple[Cluster, ...]
    scores: tuple[BridgeScore, ...]
    coverage: float                 # harvested / |T|
    n_targets_harvested: int
    n_targets_total: int
    next_targets: tuple[tuple[str, float], ...]
    used_inferred_edges: bool
    fingerprint: str                # sha256 of sorted {member_id: cluster_id}
```

### Function signatures

```python
# graph.py
def build_bipartite(members: Sequence[Member], targets: Sequence[Target],
                    edges: Sequence[Edge], *, include_inferred: bool = False,
                    min_confidence: float = 0.5) -> nx.Graph: ...

def bridge_members(B: nx.Graph, member_ids: Sequence[str]) -> list[str]: ...

def project_ra(B: nx.Graph, member_ids: Sequence[str], target_ids: Sequence[str], *,
               use_confidence: bool = True,
               weighting: Literal["ra","aa","count","jaccard","cosine"] = "ra"
               ) -> tuple[sp.csr_matrix, sp.csr_matrix, list[str], list[str]]:
    """Returns (biadjacency A, projection P, sorted_member_ids, sorted_target_ids)."""

def to_igraph(P: sp.csr_matrix, names: Sequence[str]) -> ig.Graph:
    """MUST sort names and emit sorted (min,max,weight) edges. Round weights to 12dp."""

# cluster.py
def leiden_once(g: ig.Graph, *, gamma: float = 1.0, seed: int = 42) -> list[int]: ...

def leiden_consensus(g: ig.Graph, *, gamma: float = 1.0, n_runs: int = 25,
                     base_seed: int = 42, tau: float = 0.5
                     ) -> tuple[list[int], np.ndarray]:
    """Returns (membership, co_association_matrix)."""

def stability_scores(membership: Sequence[int], C: np.ndarray) -> list[float]: ...

def select_gamma(g: ig.Graph, gammas: Sequence[float] = (0.5,0.75,1.0,1.25,1.5,2.0),
                 *, base_seed: int = 42) -> float: ...

# score.py
def brokerage_metrics(B: nx.Graph, g: ig.Graph, member_ids: Sequence[str],
                      target_ids: Sequence[str]) -> dict[str, dict[str, float]]: ...

def percentile_rank(values: Mapping[str, float]) -> dict[str, float]:
    """Average-rank percentiles in [0,1]; sort key (value, key) for determinism."""

DEFAULT_WEIGHTS = {"reach":0.35,"rarity":0.25,"betweenness":0.15,
                   "effective_size":0.15,"autonomy":0.10}

def composite_score(metrics: Mapping[str, Mapping[str, float]],
                    weights: Mapping[str, float] = DEFAULT_WEIGHTS) -> dict[str, float]: ...

# label.py
def label_clusters(members_by_id: Mapping[str, Member],
                   clusters: Mapping[int, Sequence[str]], *,
                   top_n: int = 3, min_weight: float = 0.15,
                   sentinel: str = " | ") -> dict[int, str]: ...

# infer.py
def infer_edge_confidence(m: Member, t: Target, *,
                          employer_size: int | None = None,
                          cohort_size: int | None = None,
                          shared_groups: int = 0) -> float: ...

# collect.py
def next_targets(B: nx.Graph, all_target_ids: Sequence[str], harvested: AbstractSet[str],
                 membership: Mapping[str,int], *, alpha=1.0, beta=1.0, gamma=0.5,
                 top: int = 20) -> list[tuple[str, float]]: ...
```

### Non-negotiable implementation rules

1. **Sort everything.** Node insertion order, edge lists, iteration over dicts. Verified: unsorted order
   yields 12 different answers from the same seed.
2. **Round projection weights to 12 dp** before igraph ingestion.
3. **Never call `bipartite.sets()`** — pass the explicit member set.
4. **Coalesce NaN** from `effective_size` (→0.0) and `constraint` (→1.0) before ranking.
5. **`reach` is a lower bound.** The renderer must prefix `≥`.
6. **Join label fields with `" | "`** and drop n-grams containing the sentinel.
7. **`igraph.betweenness()`, never networkx's.**
8. **Compute the fingerprint** (sha256 of sorted `{member_id: cluster_id}`) and put it in
   `AnalysisResult` — it is the cheapest regression test available.

---

## Test specifications

All values below were measured in the verified environment and are exact unless a tolerance is given.

### T1 — Golden fixture (canonical, use everywhere)

```
M = m0..m8 (9), T = t0..t5 (6)
E = [(m0,t0),(m0,t1),(m1,t0),(m1,t1),(m2,t0),(m2,t1),
     (m3,t2),(m3,t3),(m4,t2),(m4,t3),(m5,t2),(m5,t3),
     (m6,t4),(m6,t5),(m7,t4),(m7,t5),(m8,t4),(m8,t5),
     (m0,t2),(m0,t4)]          # m0 is the planted cross-pocket bridge
all targets harvested=True
```

Assertions:

- **T1.1** target degrees == `{t0:3, t1:3, t2:4, t3:3, t4:4, t5:3}`; member reach == `{m0:4, others:2}`.
- **T1.2** RA projection has exactly **15 edges**, with weights (12dp, exact):
  `m0-m1 = m0-m2 = m1-m2 = 0.666667`; `m0-m3 = m0-m4 = m0-m5 = m0-m6 = m0-m7 = m0-m8 = 0.25`;
  `m3-m4 = m3-m5 = m4-m5 = m6-m7 = m6-m8 = m7-m8 = 0.583333`.
- **T1.3** count projection weights: `m0-m1 = 2`, `m0-m3 = 1`, `m3-m4 = 2`. (Contrast test: raw count
  gives m0-m3 weight 1 out of max 2, RA gives 0.25 out of max 0.667 — RA suppresses the hub-mediated tie
  relatively more.)
- **T1.4** AA projection: `m0-m1 = 1.820478`, `m0-m3 = 0.721348`, `m3-m4 = 1.631587` (±1e-6).
- **T1.5** Leiden γ=1.0 seed=42 → **k == 3**, membership
  `{m0:1, m1:1, m2:1, m3:2, m4:2, m5:2, m6:0, m7:0, m8:0}`, modularity == **0.260000** (±1e-6).
- **T1.6** Same result for γ ∈ {0.5, 1.5, 2.0} — resolution stability.
- **T1.7** Brokerage (exact, ±1e-6):
  `m0`: reach 4, rarity **1.166667**, betweenness **0.750000**, effective_size **6.250000**,
  constraint **0.330729**. `m1`,`m2`: rarity 0.666667, betweenness 0.0, eff 1.0, constraint 0.878906.
  `m3`–`m8`: rarity 0.583333, betweenness 0.0, eff 1.0, constraint 0.781250.
  igraph unnormalised betweenness: `m0 == 21.0`, all others `== 0.0`.
- **T1.8** `composite_score` ranks **m0 first**, strictly greater than every other member.

### T2 — Determinism

- **T2.1** 10 runs of the full pipeline on T1 → identical fingerprint.
- **T2.2** Shuffle input `members`/`edges` lists with 5 different orderings → identical fingerprint.
  *(This is the regression test for the sorting rule. It must fail if sorting is removed.)*
- **T2.3** Run under `PYTHONHASHSEED` ∈ {0, 1, 999} → identical fingerprint (subprocess test).
- **T2.4** Negative control: build igraph **without** sorting on an ambiguous graph
  (`nx.LFR_benchmark_graph(300, 2.8, 1.6, 0.45, average_degree=8, min_community=25, seed=11)`),
  apply 12 vertex permutations with `seed=42` → **> 1 distinct partition**. Documents *why* the rule exists.
- **T2.5** `leiden_consensus(base_seed=42)` and `leiden_consensus(base_seed=1000)` on T1 → same partition.

### T3 — Planted partition / SBM correctness

Leiden, `RBConfigurationVertexPartition`, γ=1.0, seed=42, `n_iterations=-1`, unweighted:

| id | generator | assert |
|---|---|---|
| T3.1 | `nx.stochastic_block_model([25]*4, p_in=.85, p_out=.02, seed=42)` | `k == 4`, `NMI == 1.0`, `ARI == 1.0` |
| T3.2 | `nx.stochastic_block_model([30]*3, p_in=.60, p_out=.03, seed=42)` | `k == 3`, `NMI == 1.0`, `ARI == 1.0` |
| T3.3 | `nx.stochastic_block_model([40]*5, p_in=.30, p_out=.01, seed=42)` | `k == 5`, `NMI == 1.0`, `ARI == 1.0` |
| T3.4 | `nx.stochastic_block_model([20]*6, p_in=.40, p_out=.05, seed=42)` | `k == 6`, `NMI >= 0.95` (obs 0.9814) |
| T3.5 | `nx.stochastic_block_model([15]*8, p_in=.50, p_out=.04, seed=42)` | `k == 8`, `NMI >= 0.95` (obs 0.9851) |
| T3.6 | `nx.planted_partition_graph(4, 25, 0.85, 0.02, seed=42)` | `k == 4`, `NMI == 1.0`, `ARI == 1.0` |

Ground truth for `stochastic_block_model` is block order: `sum([[i]*s for i,s in enumerate(sizes)], [])`.
**Do not use `LFR_benchmark_graph` in CI** — it raises `ExceededMaxIterations` at μ ≤ 0.2.

### T4 — Hub pathology (the weighting regression test)

Fixture: 6 planted pockets (12 targets, 25 members each, 12% cross-pocket noise, `default_rng(3)`),
plus one extra "hub" target joined to 100 randomly chosen members spanning all pockets.

- **T4.1** raw-count projection gives a hub-only member pair weight `== 1.0`.
- **T4.2** RA projection gives that same pair weight `== 0.01` (±1e-6) — i.e. `<= 0.05`.
- **T4.3** AA gives `≈ 0.2171` — assert `> 0.15`, documenting that AA under-corrects.
- **T4.4** RA still recovers `k == 6` with `NMI >= 0.85` in the hub's presence (observed 0.887).

### T5 — Missing data

- **T5.1** A target with `harvested=False` contributes **no** edges and is excluded from `d_t`
  denominators, so `rarity` is unchanged by adding unharvested targets.
- **T5.2** `coverage == n_harvested / n_total` exactly.
- **T5.3** Dropping 30% / 60% / 80% of target columns from the T4 base fixture keeps
  `NMI >= 0.80 / 0.70 / 0.70` on members retaining ≥1 edge (observed 0.861 / 0.797 / 0.759).
- **T5.4** `wilson_lb(3, 5) == 0.231` (±0.001); `wilson_lb(0, 0) == 0.0`.
- **T5.5** `next_targets` is deterministic and ties break by `target_id` ascending.

### T6 — Inferred edges

- **T6.1** `include_inferred=False` (default) → an inferred-only edge does not appear in `B`.
- **T6.2** `include_inferred=True, min_confidence=0.5` → an edge with `confidence=0.62` appears;
  one with `confidence=0.40` does not.
- **T6.3** `infer_edge_confidence` values (±0.005): no cues **0.047**; same city only **0.076**;
  same employer 3y @200 **0.188**; same employer 3y @50,000 **0.127**;
  employer 4y @80 + school 2y @300 + city **0.619**.
- **T6.4** Monotonicity: smaller employer ⇒ strictly higher confidence, all else equal.
- **T6.5** `AnalysisResult.used_inferred_edges` is `True` iff ≥1 inferred edge entered the graph.

### T7 — Labelling

- **T7.1** Three clusters with member companies/schools
  `{Google/SRE/infra}`, `{Wharton/MBA/2016}`, `{Jane Street/Citadel/quant/NYC}` →
  labels contain `google`, `wharton`, `nyc` respectively (top-3 term membership).
- **T7.2** A cluster with no repeated attribute term → label starts with `"mixed"`.
- **T7.3** No emitted label contains the `" | "` sentinel, and no bigram straddles two attribute fields
  (regression test for the `"engineer wharton"` bug).
- **T7.4** Labelling is deterministic across 5 runs.

### T8 — Edge cases

- **T8.1** Empty `E` → empty clusters, `coverage == 0.0`, no exception.
- **T8.2** Single member, single target → 1 cluster of size 1, no NaN in any score.
- **T8.3** A member whose targets are all degree-1 → projection isolate; `effective_size == 0.0`,
  `constraint == 1.0`, `autonomy == 0.0`, no NaN reaches the composite.
- **T8.4** Disconnected bipartite input does **not** raise (regression test for `bipartite.sets()`).
- **T8.5** Duplicate edges in the input are deduplicated before projection.
- **T8.6** A target with `harvested=True` but zero mutuals is legal and contributes `d_t = 0`
  without a divide-by-zero (guard `max(d_t, 1)`).

### T9 — Performance (mark `slow`, keep out of the default CI run)

- **T9.1** 8,000 members × 1,600 targets × 40 pockets: RA projection `< 1s` (observed 0.02s);
  Leiden `n_iterations=2` `< 15s` (observed 2.4s); `NMI >= 0.90` (observed 0.9434).
- **T9.2** 10,000 members: full pipeline excluding betweenness `< 60s`.
- **T9.3** `igraph.betweenness()` at 2,000 nodes `< 10s` (observed 1.8s).
