# 04 — Entity Resolution: Company Matching & Person Dedupe

**Wave 1 / Agent 4.** Scope: how to decide (a) "this employer string means Jane Street /
Citadel" and (b) "person A and person B are the same human". Every number in this
document was produced by code executed in the sandbox; the benchmark harness is
described in §9 and the reproduction commands are in §10.

**Deliverables**
- `data/gazetteer/firms.yaml` — the curated gazetteer: 21 entities (3 target, 10 adjacent, 8 negative), 159 target/adjacent aliases, 56 negative-entity aliases, 92 `negative_aliases`, 17 veto regexes, 9 quarantined `weak_aliases`.
- This document — algorithm, thresholds, and the Wave 3 test matrix (§8, 96 cases).

---

## 0. The one-paragraph answer

Do **not** fuzzy-match company strings against target names. Match them against a
hand-curated gazetteer that contains *both* what the firm is called *and* what it is
explicitly not, then run every fuzzy candidate through a token-alignment guard before
accepting it. In the sandbox this took precision from **0.821 → 1.000** on the curated
set and drove the wrong-firm rate to **0.000** on a leave-one-alias-out generalisation
test. The scorer choice is almost irrelevant compared to the gazetteer and the guard.

---

## 1. The Citadel disambiguation problem

### 1.1 "Citadel" is at least eight different things

| Entity | Type | LinkedIn slug | Approx. size | Why it collides |
|---|---|---|---|---|
| **Citadel** (Citadel Enterprise Americas LLC) | Multi-strategy hedge fund, Ken Griffin, Miami (ex-Chicago) | `company/citadel-llc` | **~3,150** employees (2024); ~354k followers | The target |
| **Citadel Securities** | Market maker, *separate company*, same founder | `company/citadel-securities` | **~2,350** employees (2,325 Mar-2026 / 2,393 Jun-2026); ~159k followers | Different company, different page, different people |
| **The Citadel** (The Military College of South Carolina) | **University**, Charleston SC, founded 1842 | `school/the-citadel` *(unverified)* | **~35,000+** living alumni | By far the biggest volume source of false positives |
| Citadel Broadcasting / Citadel Communications | Radio, defunct (absorbed by Cumulus, 2011) | `company/citadel-broadcasting-co` | ~0 (defunct), long tail of ex-employees | `partial_ratio("Citadel","Citadel Broadcasting") == 100` |
| Citadel Credit Union | Retail bank, Exton PA | `company/citadel-credit-union` | ~500 | **Financial-services context makes it look adjacent** |
| Citadel Defense Company | Counter-UAS defence tech, San Diego | `company/citadel-defense` | ~100 | Quant-adjacent-sounding engineers |
| Citadel Servicing Corp / Acra Lending | Mortgage lender, Irvine CA | `company/acralending` | ~500 | Financial-services context again |
| The Citadel Group Ltd | Australian government IT | `au.linkedin.com/company/thecitadelgroup` | ~400 | "Citadel Group" normalises to "citadel" |
| "Citadel Security/Protection/Risk" (many firms) | Physical & cyber security, worldwide | various | ~thousands aggregate | **One token from "Citadel Securities"** |

> **Slug caveat.** `linkedin.com` is egress-blocked from the research sandbox, so slugs
> were recovered from public search-result metadata. Every slug in the gazetteer carries
> `linkedin_slug_verified: false` except `susquehanna-international-group`. Verify before
> using a slug as a hard key. The *matching logic does not depend on slugs* — they are
> provenance, not the decision procedure.

### 1.2 The three rules that actually solve it

**Rule C1 — The definite article is load-bearing, so normalise twice.**
The obvious normalisation ("drop stopwords") destroys the single most useful signal in
the whole problem: `normalize("The Citadel") == normalize("Citadel") == "citadel"`.
Measured: *every* scorer returns **100.0** for that pair after naive normalisation.

Therefore compute **two** normalisations of every input and of every gazetteer alias:

- `norm_raw(s)` — casefold, NFKC, de-accent, de-punctuate, collapse whitespace. **Keeps
  `the`. Keeps legal suffixes.** Used *only* for negative-alias lookup and exact hits.
- `norm(s)` — everything above **plus** stopword drop and trailing legal-suffix strip.
  Used for the fuzzy index.

Negative aliases are indexed under `norm_raw` **only**. This is not cosmetic — indexing
them under `norm` too is an outright bug that I hit and fixed: `norm("The Citadel Group")
== "citadel"`, which would have inserted `"citadel"` into the veto table and rejected
*every legitimate Citadel employee*. `matcher.audit_index()` exists to assert this class
of collision never reappears; it currently reports `conflicts: []`.

**Rule C2 — The field the string came from changes its meaning.**

| String | EDUCATION field | POSITION / COMPANY field |
|---|---|---|
| `The Citadel` | `the_citadel_college` (confident) | `citadel_ambiguous` — **not** a match |
| `Citadel` | `the_citadel_college` (confident) | `citadel_llc` (confident) |
| `The Citadel, The Military College of South Carolina` | college | college (veto fires on the position side too) |
| `Citadel Securities` | reject (nobody is educated at a market maker) | `citadel_securities` |

Encoded in the gazetteer as `field_scope: [education]` on `the_citadel_college`, and as
the `disambiguation_rule` string on the same record. `entity_type: university` implies
education-only; `fund` / `market_maker` imply position-only.

`citadel_ambiguous` is a real output state, not an error. A bare `The Citadel` in an
employer field is genuinely under-determined and should be routed to review with the
corroborating signals attached (location = Charleston SC → college; location =
Miami/Chicago/NYC/London → fund; a degree or graduation year present anywhere on the
profile → college).

**Rule C3 — Citadel LLC and Citadel Securities never merge.**
They are separate companies with separate LinkedIn pages and non-overlapping staff. Both
are `tier: target` with `sibling_entity` pointing at each other, and the trailing token
`securities` is *never* in the guard's ALLOW list, so `citadel` cannot drift to
`citadel securities` or back. Aggregating them into a "Citadel" cluster would be the
single most misleading thing this product could do — a Citadel Securities C++ engineer
is not a warm intro to a Citadel LLC portfolio manager.

Two more collisions worth calling out explicitly:
- `Citadel Security` vs `Citadel Securities` — one character apart. `partial_ratio`
  scores **90.9**, `jaro_winkler` **91.3**; only `ratio`/`token_sort_ratio` (74.4) and the
  guard separate them. Handled by a `negative_pattern` on `citadel_securities`.
- Citadel's internal pod brands appear verbatim as employers: **Surveyor Capital (A
  Citadel Company)**, **Ashler Capital**, **Aptigon Capital**, **Pioneer Path**,
  **Ravelin Technology**, **Global Quantitative Strategies**. These are true positives for
  `citadel_llc` and are in the alias list; without them you silently lose a real slice of
  the target population (Surveyor alone is ~177 people).

### 1.3 Size weighting

Rarity should scale roughly `1/approx_headcount`, so the magnitudes matter more than the
precision of the figures. The operative point: **The Citadel college's alumni population
(~35k) is an order of magnitude larger than Citadel LLC's headcount (~3.15k)**. If the
college leaks into the fund bucket, the fund bucket is mostly college alumni. This is why
the college is a first-class `tier: negative` entity rather than a regex afterthought.

---

## 2. Jane Street

**Canonical:** `Jane Street`, LinkedIn `company/jane-street-global`, **~3,700** employees
listed on LinkedIn (~444k followers). Jane Street Group, LLC is the parent; the firm
self-describes as ~3,000+ people across NY, London, Hong Kong, Singapore, Amsterdam.

**Legal entities that appear verbatim in employer fields** (all in the alias list):
Jane Street Group LLC · Jane Street Capital LLC · Jane Street Execution Services LLC ·
Jane Street Financial Limited (UK, FCA) · Jane Street Netherlands B.V. (AFM) ·
Jane Street Hong Kong Limited (SFC) · Jane Street Singapore Pte. Ltd. · Jane Street Asia
Trading Limited · Jane Street Europe.

**Duplicate LinkedIn pages** people still list themselves under:
`company/jane-street-capital-llc`, `company/janestreetgroup`. Treat all three slugs as
`jane_street`.

**False positives — "Jane Street" is a real road** (Toronto arterial, NYC West Village,
several UK/AU towns). Confirmed live LinkedIn page: **Jane Street Entertainment** — a
genuinely unrelated company that a naive matcher scores **100** against "Jane Street" on
`partial_ratio`, `token_set_ratio` and `token_ratio`. Handled by a `negative_pattern`
listing consumer/toponym nouns plus a `\b\d+\s+jane\s+street\b` street-address rule.

**`JS` is a trap and is quarantined.** It is filed under `weak_aliases`, which require an
**exact** hit *and* corroboration (a `janestreet.com` domain, a matching title, or a
second Jane Street signal on the profile). `JS` alone matches JavaScript, Johnson &
Johnson-adjacent abbreviations, and thousands of initialisms. Same treatment for
`SIG`, `IMC`, `CS`, `TS`, `Virtu`, `Millennium`.

**Ancillary adjacency signals** (recorded under `culture_signals`; corroborating evidence
only, never sufficient alone):
- **OCaml** in a skills/headline field is a remarkably high-precision Jane Street signal —
  the firm is the dominant industrial OCaml shop.
- Competitive-programming pipeline: IMO, IOI, Putnam, ICPC, USACO, Codeforces.
- Jane Street's own recruiting surface area: Estimathon, Figgie, "JS Puzzles", the Jane
  Street Symposium, AMP (Academy of Math and Programming).
- Citadel's equivalent: the Citadel Datathon / Correlation One Invitational, "Discover
  Citadel". These generate a large population of people who *attended a Citadel event*
  and are not employees — worth a distinct `event_attendee` edge type, not an employment edge.

---

## 3. Normalization algorithm

`cleanco` **is not the answer, but its data is.** Measured over 28 tricky inputs
(Experiment J), `cleanco.basename()` agreed exactly with the required output **12/28**.
It strips legal suffixes competently and ships a curated database of **194 legal-form
terms across 16 entity types and 67 countries** — genuinely useful, and the right source
to seed `normalization.legal_suffixes`. But it does **not** casefold, NFKC-normalise,
strip diacritics, remove zero-width characters, collapse whitespace, split on `|`, unify
`&`, or drop a leading `The`. Observed failures on our exact inputs:

```
basename("Citadel Securities (Europe) Limited") -> "Citadel Securities (Europe"   # unbalanced paren
basename("Jane Street (JS)")                    -> "Jane Street (JS"              # same bug
basename("D. E. Shaw & Co., L.P.")              -> "D. E. Shaw & Co."             # '& Co.' survives
basename("ＪＡＮＥ　ＳＴＲＥＥＴ")                        -> "ＪＡＮＥ ＳＴＲＥＥＴ"                 # no NFKC
basename("Société Générale")                    -> "Société Générale"             # no de-accent
basename("Citadel | Chicago")                   -> "Citadel | Chicago"            # no segmentation
```

`company-name-matcher` is embedding-based (a language model over company names) and is
aimed at large-scale vector search; at our scale (a few thousand strings against ~240
aliases, which the full pipeline processes at **20,594 strings/sec**) it adds a model
dependency and a non-auditable score for no measurable gain. `name_matching` (DNB) is a
TF-IDF/n-gram matcher — same conclusion as §4's TF-IDF result. **Recommendation: write
the ~40 lines, seed the suffix list from cleanco's `termdata`.**

### 3.1 The algorithm (numbered pseudocode)

```
NORMALIZE(s, drop_noise, strip_suffix, segment) -> string

  1. if s is null or blank: return ""
  2. if segment:  s <- PRIMARY_SEGMENT(s)
         split on  /\s*[|•·/›»]\s*|\s+[-–—]\s+/   and take the FIRST non-empty part
         # LinkedIn employer strings are routinely "Company | Location",
         # "Company - Team", "Citadel Servicing Corp / Acra Lending".
  3. s <- unicodedata.normalize("NFKC", s)        # fullwidth -> ASCII, ligatures, ﬁ -> fi
  4. s <- delete U+200B U+200C U+200D U+2060 U+FEFF        # zero-width characters
  5. s <- replace U+00A0 (nbsp) with U+0020
  6. s <- s.casefold()                            # casefold, NOT lower() (ß -> ss, İ)
  7. s <- unicodedata.normalize("NFKD", s); drop all combining marks   # é -> e
  8. s <- replace "&" with " and "
  9. s <- replace U+2010..U+2015, U+2212 with "-"  # unicode dashes
 10. s <- replace every non-[\w\s] character with " "
 11. s <- collapse runs of whitespace; strip
 12. toks <- s.split()
 13. if strip_suffix: toks <- STRIP_SUFFIXES(toks)
 14. if drop_noise:   toks <- [t for t in toks if t not in {"the"}]   (unless it empties)
 15. toks <- DROP_ACRONYM_ECHO(toks)
 16. return " ".join(toks)

STRIP_SUFFIXES(toks)                              # END of the string only, repeatedly
  a. while len(toks) > 1:
  b.     if toks[-1] in LEGAL_SUFFIXES: pop; continue
  c.     for n in (4,3,2):                        # punctuation-exploded acronyms
  d.         if len(toks) > n and every one of toks[-n:] is a single character
  e.            and "".join(toks[-n:]) in LEGAL_SUFFIXES: delete toks[-n:]; continue outer
  f.     if toks[-1] == "and": pop; continue      # the residue of "& Co."
  g.     break
  # "d e shaw and co l p" -> "d e shaw"     ("D. E. Shaw & Co., L.P.")

DROP_ACRONYM_ECHO(toks)                           # only when len(toks) >= 3
  a. if toks[-1] == initials(toks[:-1]): drop it  # "jane street js"   -> "jane street"
  b. elif toks[0] == initials(toks[1:]):  drop it # "hrt hudson river trading" -> ...
```

Two normalisations are materialised per string, per Rule C1:

```
norm_raw(s) = NORMALIZE(s, drop_noise=False, strip_suffix=False, segment=True)
norm(s)     = NORMALIZE(s, drop_noise=True,  strip_suffix=True,  segment=True)
```

Verified outputs:

| input | `norm_raw` | `norm` |
|---|---|---|
| `Jane Street Group, LLC` | `jane street group llc` | `jane street` |
| `The Citadel` | `the citadel` | `citadel` |
| `Citadel \| Chicago` | `citadel` | `citadel` |
| `D. E. Shaw & Co., L.P.` | `d e shaw` | `d e shaw` |
| `ＪＡＮＥ　ＳＴＲＥＥＴ` | `jane street` | `jane street` |
| `Jane Street<U+200B> Capital` | `jane street capital` | `jane street capital` |
| `Société Générale` | `societe generale` | `societe generale` |
| `Jane Street (JS)` | `jane street` | `jane street` |

### 3.2 Do **not** strip parentheticals

`strip_parentheticals: false`. `Surveyor Capital (A Citadel Company)` is the single most
informative employer string a Citadel employee can write; deleting the parenthetical
destroys it. The acronym-echo rule (step 15) removes the only parenthetical worth
removing — a bare restatement of the initials.

---

## 4. Fuzzy matching: what was measured

### 4.1 The `partial_ratio` trap — and it is wider than `partial_ratio`

Raw pairwise scores on normalised strings (Experiment D). **Bold = an unusable 100.**

| A | B | ratio | partial_ratio | token_sort | token_set | WRatio | token_ratio | jaro_winkler |
|---|---|---|---|---|---|---|---|---|
| `citadel` | `citadel broadcasting` | 51.9 | **100.0** | 51.9 | **100.0** | 90.0 | **100.0** | 87.0 |
| `citadel` | `citadel credit union` | 51.9 | **100.0** | 51.9 | **100.0** | 90.0 | **100.0** | 87.0 |
| `citadel` | `citadel securities` | 56.0 | **100.0** | 56.0 | **100.0** | 90.0 | **100.0** | 87.8 |
| `citadel securities` | `citadel security software` | 74.4 | 90.9 | 74.4 | 74.4 | 74.4 | 74.4 | 91.3 |
| `citadel` | `the citadel` → both `citadel` | **100.0** | **100.0** | **100.0** | **100.0** | **100.0** | **100.0** | **100.0** |
| `jane street` | `jane street coffee` | 75.9 | **100.0** | 75.9 | **100.0** | 90.0 | **100.0** | 92.2 |
| `jane street` | `jane street entertainment` | 61.1 | **100.0** | 61.1 | **100.0** | 90.0 | **100.0** | 88.8 |
| `two sigma` | `two sigma ventures` | 66.7 | **100.0** | 66.7 | **100.0** | 90.0 | **100.0** | 90.0 |
| `sig` | `sig sauer` | 50.0 | **100.0** | 50.0 | **100.0** | 90.0 | **100.0** | 84.4 |
| `optiver` | `optiver australia` | 58.3 | **100.0** | 58.3 | **100.0** | 90.0 | **100.0** | 88.2 |

**The warning generalises.** `partial_ratio` is the notorious one, but `token_set_ratio`
and `token_ratio` are *equally* broken for this problem, and for the same structural
reason: whenever one string's tokens are a **subset** of the other's, the score saturates
at 100 regardless of how discriminating the extra tokens are. Any subset-tolerant scorer
used as a bare accept signal will merge Citadel with Citadel Broadcasting. The row for
`citadel`/`the citadel` shows that *no* scorer can help once normalisation has eaten the
article — that must be solved upstream (Rule C1), not by tuning.

### 4.2 Naive matching (targets only, no negatives, no guard) — 94 cases

| scorer | P | R | F1 | FP |
|---|---|---|---|---|
| `ratio` | 0.948 | 1.000 | 0.973 | 3 |
| `token_sort_ratio` | 0.948 | 1.000 | 0.973 | 3 |
| `QRatio` | 0.948 | 1.000 | 0.973 | 3 |
| `jaro_winkler` @95 | 0.948 | 1.000 | 0.973 | 3 |
| `jaro_winkler` @85 | 0.640 | 1.000 | 0.780 | 31 |
| `token_set_ratio` | **0.647** | 1.000 | 0.786 | **30** |
| `token_ratio` | **0.647** | 1.000 | 0.786 | **30** |
| `partial_ratio` @85 | **0.618** | 1.000 | 0.764 | **34** |
| `WRatio` @85 | **0.591** | 1.000 | 0.743 | **38** |

A third to a half of everything a naive matcher returns is wrong. `WRatio` at a low
threshold is the *worst* option available.

### 4.3 Full pipeline — negatives + containment guard

**Ablation** (`token_set_ratio`, threshold 92, 94 cases):

| configuration | P | R | F1 | FP |
|---|---|---|---|---|
| no negatives, no guard | 0.821 | 1.000 | 0.902 | 12 |
| negative gazetteer only | 0.965 | 1.000 | 0.982 | 2 |
| containment guard only | 0.982 | 1.000 | 0.991 | 1 |
| **negatives + guard** | **1.000** | **1.000** | **1.000** | **0** |

With both defences in place **every scorer scores 1.000/1.000 at every threshold from 84
to 100** — including `partial_ratio`. That is the finding: *the scorer stops mattering.*
Precision comes from the gazetteer and the guard; the scorer only supplies recall. It
also means the clean-set result is not evidence for a threshold, because the curated cases
mostly resolve by exact hit. Thresholds were chosen from the two generalisation tests below.

### 4.4 The containment guard

```
CONTAINMENT_GUARD(q_norm, alias_norm, tok_thr=80) -> bool
  1. greedily align each query token to its best unused alias token by fuzz.ratio
  2. a token pair counts as aligned iff ratio >= tok_thr (80)
  3. extras <- unaligned query tokens  ∪  unaligned alias tokens
  4. extras <- extras minus {initials(q_tokens), initials(alias_tokens)}
  5. return extras ⊆ ALLOW
```

`ALLOW` is a closed list of non-discriminating filler: geographies (`london`, `europe`,
`hong`, `kong`, `apac`, `chicago`, `miami`, …), generic finance nouns (`trading`,
`capital`, `management`, `investments`, `markets`, `financial`), and structural words
(`group`, `holdings`, `international`, `company`, `a`, `the`, `and`, `of`).
`broadcasting`, `securities`, `credit`, `union`, `defense`, `servicing`, `coffee`,
`entertainment`, `sauer`, `ventures` are deliberately **absent**, which is exactly why
`citadel → citadel broadcasting` and `two sigma → two sigma ventures` are rejected.

Step 2 is what makes the guard typo-tolerant: an exact token-set comparison collapsed
recall to **8%** under 1-character corruptions; fuzzy token alignment restores it to
**68–74%**. (That regression was caught by the benchmark, not by inspection.)

### 4.5 Threshold selection — the two generalisation tests

**Experiment E — typo robustness.** Every case corrupted with 1 and 2 random
character edits (delete / substitute / transpose), 5 samples each: 275 positives, 195
negatives.

| scorer | thr | recall | FP-rate | precision |
|---|---|---|---|---|
| `ratio` | 84 | 0.731 | 0.056 | 0.948 |
| `ratio` | 88 | 0.680 | 0.031 | 0.969 |
| `ratio` | 92 | 0.633 | 0.021 | 0.978 |
| `ratio` | 95 | 0.509 | 0.000 | 1.000 |
| `jaro_winkler` | 88 | 0.738 | 0.056 | 0.949 |
| `WRatio` | 88 | 0.680 | 0.031 | 0.969 |
| `token_set_ratio` | 88 | 0.480 | 0.031 | 0.957 |

At 2 typos, recall falls to 0.28–0.46 while precision stays ≥ 0.977 — the failure mode
degrades safely (misses, not merges). Note `token_set_ratio` is the *worst* scorer under
typos (a corrupted token breaks set intersection outright) even though it was the most
dangerous scorer without the guard.

**Experiment F — leave-one-alias-out.** Each of the 159 target aliases is deleted from
the index, then queried. This simulates the variant the curator never thought to write.

| scorer | thr | recall | wrong-firm |
|---|---|---|---|
| `WRatio` | 84–90 | **0.692** | **0.000** |
| `token_set_ratio` | 84–96 | 0.679 | 0.000 |
| `jaro_winkler` | 84 | 0.673 | 0.000 |
| `token_sort_ratio` | 84 | 0.610 | 0.000 |
| `ratio` | 84 | 0.585 | 0.000 |
| `WRatio` | 92 | 0.572 | 0.000 |

**Wrong-firm rate is 0.000 for every scorer at every threshold.** Behind the guard, an
unrecognised string is *dropped*, never *misrouted*. That is the property that matters:
a missed connection costs recall, a misrouted one poisons the product.

### 4.6 Recommendation

> **`rapidfuzz.fuzz.WRatio`, always behind the containment guard and the negative
> gazetteer. Accept ≥ 90. Review 84–89. Reject < 84.**

Justification, straight from the numbers:
- `WRatio` has the best hold-out recall (0.692) at zero wrong-firm rate, and matches the
  best typo precision (0.969 at 88–90).
- **90 is the top of the plateau.** Hold-out recall is flat at 0.692 across 84/86/88/90
  and drops to **0.572 at 92** — twelve points of recall for *no* precision gain. Pick the
  high end of a flat region.
- Below 84 typo FP-rate starts climbing (0.056 at 84) with no recall benefit.
- The 84–89 review band is where an LLM adjudicator or a human earns its keep; at our
  volume it will contain tens of rows, not thousands.
- `WRatio` alone, without the guard, is the *worst* scorer measured (P = 0.591). The
  recommendation is the pair, never the scorer alone.

**Banned as bare accept signals:** `partial_ratio`, `partial_token_sort_ratio`,
`partial_token_set_ratio`, and unguarded `token_set_ratio` / `token_ratio`.

### 4.7 TF-IDF char n-grams and embeddings

TF-IDF `char_wb` (2,4) + cosine against the alias index, on the 94 clean cases, without
the negative gazetteer:

| cosine threshold | P | R | F1 |
|---|---|---|---|
| 0.55 | 0.873 | 1.000 | 0.932 |
| 0.75 | 0.932 | 1.000 | 0.965 |
| 0.90 | 0.948 | 1.000 | 0.973 |
| 0.95 | 0.948 | 1.000 | 0.973 |

It plateaus at exactly the same precision as plain `ratio` (0.948) and, like every
scorer, is fixed by the gazetteer rather than by tuning. `sparse_dot_topn` addresses a
scaling problem we do not have: **the full pipeline runs at 20,594 strings/sec**, so
3,000 connections resolve in ~0.15 s. Embeddings add an opaque score and a model
dependency to a problem whose entire difficulty is *semantic distinctions invisible to
string similarity* (Citadel-the-fund vs Citadel-the-college are nearly identical strings
and completely different entities — no embedding of the name alone can fix that).

**Verdict: no TF-IDF, no embeddings, no `sparse_dot_topn`. rapidfuzz + the gazetteer.**

---

## 5. Architecture: curated gazetteer, not pure fuzz

`data/gazetteer/firms.yaml`, `schema_version: 1`. 21 entities (3 `target`, 10 `adjacent`,
8 `negative`) / 159 target+adjacent aliases / 92 `negative_aliases` / 17 veto regexes /
9 `weak_aliases` under exact-match quarantine.

```yaml
- key: citadel_securities
  canonical_name: Citadel Securities
  entity_type: market_maker          # fund | market_maker | university | unrelated
  tier: target                       # target | adjacent | negative
  approx_headcount: 2350
  linkedin_slug: citadel-securities
  linkedin_slug_verified: false
  domains: [citadelsecurities.com]
  sibling_entity: citadel_llc        # separate company — never merge
  aliases: [...]                     # ~20 surface forms
  weak_aliases: [CS]                 # exact hit + corroboration required
  negative_aliases: [Citadel Security Software, ...]
  negative_patterns: ['citadel securit(y|ies) (software|agency|services|guards|patrol)']
  culture_signals: {...}
  role_families: [trader, quant_researcher, swe, quant_dev, ops, recruiter, compliance]
```

**Resolution order** (each step short-circuits):

```
RESOLVE(text, field_kind) -> (entity_key | None | AMBIGUOUS, score, reason)
 1. raw <- norm_raw(text);  raw_full <- norm_raw(text, segment=False);  n <- norm(text)
 2. if raw or n is in NEGATIVE_EXACT      -> reject, reason="negative_exact:<key>"
 3. if any NEGATIVE_PATTERN matches raw, n, or raw_full -> reject, reason="negative_pattern:<key>"
 4. if raw or n is in EXACT_ALIAS_INDEX   -> candidate, score 100
 5. else best <- argmax over alias index of WRatio(n, alias)
 6.      if best.score < 84                    -> reject
 7.      if not CONTAINMENT_GUARD(n, best.alias) -> reject, reason="guard_reject"
 8.      if best.score < 90                    -> REVIEW
 9. apply FIELD_SCOPE: university entities are education-only; fund/market_maker are
    position-only. A position-field hit on a university entity -> AMBIGUOUS.
10. if candidate.tier == negative -> reject (an *explicit, auditable* no)
11. if alias came from weak_aliases and score < 100 -> reject; else require corroboration
12. return candidate.key
```

Steps 2–3 run **before** step 4 on purpose: the veto must beat an exact positive hit,
because `The Citadel` normalises onto `Citadel`'s exact key.

Why a gazetteer rather than tuned fuzz:
1. **The hard distinctions are semantic, not lexical.** No threshold separates a hedge
   fund from a military college whose names differ by one article.
2. **Negatives are first-class.** A rejection carries a reason string naming the entity it
   was rejected as — auditable, testable, explainable in the UI.
3. **It is the natural place for headcount, type, domains and culture signals**, which
   downstream scoring needs anyway.
4. **Curation is cheap and bounded.** Three target firms, ten adjacent, eight negatives.
   Each new alias discovered in production is a one-line YAML edit with a regression test,
   not a threshold change that silently perturbs every other match.
5. **It degrades safely.** Experiment F: unknown strings are dropped, never misrouted.

**Operating procedure:** log every `reject` and every `REVIEW` with its reason and score.
The unmatched-string log *is* the curation backlog; hold-out recall of 0.692 means roughly
30% of genuinely novel surface forms need a human to add them once.

---

## 6. Person-level entity resolution

### 6.1 The LinkedIn slug is the primary key — nothing else comes close

LinkedIn's `Connections.csv` export has a 3-line "Notes:" preamble that **must be
skipped** before the header row. Current column set:
`First Name, Last Name, URL, Email Address, Company, Position, Connected On`.
`Email Address` is populated only when the connection opted in (~30–40% in practice, and
not reliable enough to key on). Older exports omit `URL` entirely — **detect the header,
do not assume it**, and fail loudly if `URL` is absent rather than silently falling back
to name matching.

The public-profile slug is the only stable identifier in the file. Normalisation:

```
NORMALIZE_SLUG(u) -> key | None
 1. trim; percent-decode (%C3%A9 -> é); NFKC
 2. if no scheme: prefix "https://" when it contains "linkedin.com",
    else if it has no "/" treat the whole string as a bare slug
 3. urlsplit; discard scheme, netloc (kills the uk./de./www. locale subdomains),
    query (?trk=..., ?originalSubdomain=...), and fragment
 4. capture the first path segment after /in/ or /pub/
 5. strip trailing "/"  (this also discards /en, /overlay/contact-info, /details/...)
 6. if the path was /pub/  -> return "pub:" + the FULL remaining path, casefolded
 7. if the slug matches ^ACoA[A-Za-z0-9_-]+$  -> return "urn:" + slug, CASE PRESERVED
 8. otherwise return slug.casefold()
```

Two edge cases that are silent data-corruption bugs if missed, both verified in the sandbox:

- **Member-URN slugs (`/in/ACoAAAB...`) are case-sensitive base64.** Casefolding them
  merges distinct humans. `ACoAAABcDeFgHiJkLmNoP` and `ACoAAABcDEFGHIJKLMNOP` must stay
  separate keys — they do, under the `urn:` namespace.
- **Legacy `/pub/name/12/345/678` URLs** carry their discriminating digits in *later* path
  segments. Taking only the first segment maps every `pub/jane-doe/*` to `jane-doe`.
  Namespaced as `pub:` with the full tail retained.

Verified: **12 distinct URL spellings** of one profile — `http`/`https`, `www`/bare/`uk.`,
trailing slash, `?trk=`, `?originalSubdomain=`, `/en`, `/overlay/contact-info/`, `/in/`
prefix, bare slug, uppercase — collapse to the single key `jane-doe-12345678`, while all
distinct profiles stay distinct. Company URLs (`/company/...`) correctly return `None`.

**Caveat:** the slug is stable but not immutable — users can change their vanity URL.
Store `(slug, first_seen, last_seen)` and keep a slug-history table so a rename shows up
as a merge candidate rather than a new person.

### 6.2 Recommended design

**Deterministic key on the normalised slug, fuzzy only as a fallback.** At a few thousand
records with a near-universal strong identifier, probabilistic linkage is unjustified
machinery.

- **`splink`** (Fellegi–Sunter, DuckDB/Spark, actively maintained, the strongest of the
  three) is built for millions of records without identifiers. Adopting it here means
  taking on an EM-trained model and its diagnostics to solve a `dict` lookup.
- **`dedupe`** requires interactive active learning — a human labelling session — for a
  problem where >95% of records resolve by exact key.
- **`recordlinkage`** (installed and inspected) is the closest fit and its `Index.block` /
  `Index.sortedneighbourhood` API is a reasonable source of blocking patterns, but its
  comparison primitives are the same jellyfish/rapidfuzz functions we call directly.

**Verdict: no linkage library. ~60 lines of deterministic code plus rapidfuzz for the
fallback.** Revisit `splink` only if the product later ingests 2nd-degree data (10⁵–10⁶
records) where no slug exists.

### 6.3 The fuzzy fallback, when the slug is missing

Blocking first, to avoid O(n²): block key = `(soundex(last_token), first_letter(first_token))`.
Measured on the 39-name test set: **70 candidate pairs vs 741 exhaustive — 9.4%.**
Sorted-neighbourhood on the normalised full name is a reasonable second blocking pass.

Name normalisation matters far more than the scorer (Experiment L, 26 hand-built pairs):

| scorer | thr | P | R | F1 |
|---|---|---|---|---|
| raw `token_sort_ratio` | 85 | 0.700 | 0.389 | 0.500 |
| normalised `token_sort_ratio` | 90 | 0.875 | 0.778 | 0.824 |
| normalised `token_set_ratio` | 90 | 0.895 | 0.944 | 0.919 |
| **normalised `token_set_ratio`** | **97** | **1.000** | **0.833** | **0.909** |
| normalised `jaro_winkler` | 90 | 0.800 | 0.889 | 0.842 |
| normalised + `metaphone` | 95 | 0.833 | 0.833 | 0.833 |

Adding a nickname map, de-accenting and middle-initial dropping moves precision from
**0.700 → 1.000** and recall from **0.389 → 0.833**; changing the scorer moves neither
much. `metaphone`/`soundex` are useful for *blocking*, not for scoring — they over-merge
(`Michael`/`Michelle` collide) and are Anglocentric, which is a poor fit for a
CJK/Slavic/Indic-heavy quant population.

`PERSON_NAME_NORM`: casefold → NFKD de-accent → strip punctuation (`O'Brien`→`obrien`,
`Miller-Weiss`→`miller weiss`) → collapse whitespace → map nicknames via a `NICK` dict
(bob→robert, bill→william, liz→elizabeth, …) → drop single-character tokens (middle
initials) when ≥3 tokens remain.

**Recommendation: `token_set_ratio` ≥ 97 on `PERSON_NAME_NORM`, within a block, and only
when both records agree on a corroborating field (resolved employer, or school, or
location).** `token_set_ratio` is chosen here — where it is safe — because the failure
mode is the *opposite* of the company case: it correctly handles `Sarah Miller` /
`Sarah Miller-Weiss` (married-name change) as a token-subset relation, which is a true
positive for people and a false positive for companies. Everything below 97 goes to review.
Never auto-merge on name alone.

Known residual failures, for Wave 3's awareness: `Xiaoming Li` / `Li Xiaoming` (CJK name
order) is recovered by `token_set_ratio` only; `Alexander Petrov` / `Aleksandr Petrov`
(transliteration) needs the fuzzy path, not the nickname map; `Sarah Miller` / `Sarah
Weiss` (married-name change with *no* shared surname) is unrecoverable from names alone
and requires the slug.

---

## 7. Title normalization

Two orthogonal axes plus flags. Parse the *position* string with ordered regexes, first
match wins, longest pattern first.

### 7.1 Seniority

| rank | code | cues (case-insensitive, word-bounded) |
|---|---|---|
| 0 | `INTERN` | `intern`, `internship`, `summer analyst`, `co-op`, `trainee`, `apprentice` |
| 1 | `NEW_GRAD` | `new grad`, `graduate (analyst\|program\|scheme)`, `campus hire`, `rotational` |
| 2 | `ANALYST` | `analyst` (not `summer analyst`), `associate analyst`, `junior` |
| 3 | `ASSOCIATE` | `associate` (not `associate analyst`), `senior analyst` |
| 4 | `SENIOR` | `senior`, `sr\.`, `lead`, `staff`, `II`/`III` levels |
| 5 | `PRINCIPAL` | `principal`, `senior staff`, `distinguished`, `architect` |
| 6 | `VP` | `vice president`, `\bVP\b`, `AVP`, `SVP`, `EVP`, `director` |
| 7 | `MD` | `managing director`, `\bMD\b`, `executive director`, `head of` |
| 8 | `PARTNER` | `partner`, `member`, `principal (at a fund)` |
| 9 | `EXEC` | `C[A-Z]O`, `chief .* officer`, `founder`, `co-founder`, `president` |

Ambiguity notes Wave 3 must encode: `MD` is `Doctor of Medicine` outside finance — require
a resolved finance employer. `Director` is `VP`-tier at a bank and `EXEC`-tier at a
startup — bind seniority to `entity_type`. `Associate` at Jane Street is a *junior* rung;
at a law firm it is not. `Principal` at a hedge fund is senior; `Principal Engineer` is
IC-track `PRINCIPAL`, not management.

### 7.2 Function

| code | cues |
|---|---|
| `QUANT_TRADER` | `quant(itative)? trader`, `trader` + a quant employer |
| `TRADER` | `trader`, `trading`, `market maker`, `execution trader` |
| `QUANT_RESEARCHER` | `quant(itative)? research(er)?`, `\bQR\b`, `researcher`, `strategist` |
| `PORTFOLIO_MANAGER` | `portfolio manager`, `\bPM\b`, `analyst` + a pod name (Surveyor / Ashler) |
| `SWE` | `software engineer`, `developer`, `\bSWE\b`, `programmer`, `full[- ]stack` |
| `QUANT_DEV` | `quant(itative)? developer`, `\bQD\b`, `trading systems`, `low latency`, `\bFPGA\b` |
| `INFRA` | `infrastructure`, `\bSRE\b`, `platform`, `devops`, `network`, `systems engineer` |
| `DATA` | `data (scientist\|engineer)`, `machine learning`, `\bML\b`, `\bAI\b` |
| `OPS` | `operations`, `middle office`, `back office`, `settlement`, `treasury`, `clearing` |
| `RISK` | `risk`, `compliance`, `legal`, `counsel`, `audit`, `regulatory` |
| `RECRUITER` | `recruit(er\|ing)`, `talent`, `\bHR\b`, `people (ops\|team)`, `campus`, `sourcer` |
| `SALES` | `sales`, `coverage`, `relationship manager`, `business development`, `\bIR\b` |
| `BIZOPS` | `strategy`, `business ops`, `chief of staff`, `product manager`, `\bPM\b` (non-fund) |
| `OTHER` | fallback |

### 7.3 Flags and why the axes are kept separate

`is_former` (past-tense dates / "ex-"), `is_contractor`, `is_founder`, `is_campus_recruiter`.

This is the axis the product's scoring depends on. **A Jane Street recruiter and a Jane
Street trader are opposite kinds of connection**: the recruiter is a high-reach, low-signal
node whose job is knowing everyone (their presence in the network says almost nothing
about the user's closeness to the firm), while the trader is a low-reach, high-signal node
(a genuine intro path). Recommended treatment: weight `RECRUITER` and `SALES` down hard
for *adjacency inference* but surface them separately as *reachable contacts*; weight
`QUANT_TRADER` / `QUANT_RESEARCHER` / `PORTFOLIO_MANAGER` up. Combine multiplicatively
with the size weight (`1/approx_headcount`) so a connection at 100-person Citadel Defense
never outranks one at Citadel proper for the wrong reason.

---

## 8. Test matrix for Wave 3

**94 executable company cases** (§8.1–8.7) plus 6 field-context cases (§8.8), 19 slug
cases (§8.9) and 22 name pairs (§8.10). `expect` is the entity key or `NONE` (must not
match any target). Field context is `position` unless stated.

The **Result** column is *observed*, not predicted: all 94 company cases were executed at
the recommended settings (`WRatio`, accept ≥ 90, guard ON, negatives ON) — **94/94 pass,
0 fail**, with `audit_index()` reporting no negative/positive key collisions. §8.9 was
executed against `normalize_slug`; all 19 pass. Parameterise directly from this table.

### 8.1 Jane Street — true positives

| # | input | expect | result |
|---|---|---|---|
| 1 | `Jane Street` | `jane_street` | pass |
| 2 | `Jane Street Capital` | `jane_street` | pass |
| 3 | `Jane Street Capital, LLC` | `jane_street` | pass |
| 4 | `Jane Street Group` | `jane_street` | pass |
| 5 | `Jane Street Group, LLC` | `jane_street` | pass |
| 6 | `JANE STREET GROUP LLC` | `jane_street` | pass |
| 7 | `jane street` | `jane_street` | pass |
| 8 | `Jane  Street   Capital` (multiple spaces) | `jane_street` | pass |
| 9 | `Jane Street Europe` | `jane_street` | pass |
| 10 | `Jane Street Financial Limited` | `jane_street` | pass |
| 11 | `Jane Street Financial Ltd.` | `jane_street` | pass |
| 12 | `Jane Street Netherlands B.V.` | `jane_street` | pass |
| 13 | `Jane Street Asia Trading Limited` | `jane_street` | pass |
| 14 | `Jane Street Hong Kong Limited` | `jane_street` | pass |
| 15 | `Jane Street Singapore Pte. Ltd.` | `jane_street` | pass |
| 16 | `Jane Street Execution Services, LLC` | `jane_street` | pass |
| 17 | `Jane St.` | `jane_street` | pass |
| 18 | `JaneStreet` | `jane_street` | pass |
| 19 | `Jane-Street` | `jane_street` | pass |
| 20 | `Jane Street (JS)` | `jane_street` | pass |
| 21 | `Jane Street<U+200B> Capital` (zero-width space) | `jane_street` | pass |
| 22 | `ＪＡＮＥ　ＳＴＲＥＥＴ` (fullwidth + ideographic space) | `jane_street` | pass |

### 8.2 Jane Street — near-miss true negatives

| # | input | expect | result |
|---|---|---|---|
| 23 | `Jane Street Coffee` | NONE | pass |
| 24 | `Jane Street Entertainment` (real LinkedIn page) | NONE | pass |
| 25 | `Jane Street Dental Practice` | NONE | pass |
| 26 | `123 Jane Street Bakery` | NONE | pass |
| 27 | `Mary Jane Street Foods` | NONE | pass |
| 28 | `Jane Street Studios Toronto` | NONE | pass |
| 29 | `Janestreet Realty of Toronto` | NONE | pass |

### 8.3 Citadel LLC (the fund) — true positives

| # | input | expect | result |
|---|---|---|---|
| 30 | `Citadel` | `citadel_llc` | pass |
| 31 | `Citadel LLC` | `citadel_llc` | pass |
| 32 | `Citadel \| Chicago` | `citadel_llc` | pass |
| 33 | `Citadel Advisors LLC` | `citadel_llc` | pass |
| 34 | `Citadel Enterprise Americas LLC` | `citadel_llc` | pass |
| 35 | `Citadel Investment Group` | `citadel_llc` | pass |
| 36 | `Surveyor Capital (A Citadel Company)` | `citadel_llc` | pass |
| 37 | `Ashler Capital (A Citadel Company)` | `citadel_llc` | pass |
| 38 | `Citadel Global Equities` | `citadel_llc` | pass |
| 39 | `CITADEL  llc` | `citadel_llc` | pass |

### 8.4 Citadel Securities — true positives (must NOT resolve to `citadel_llc`)

| # | input | expect | result |
|---|---|---|---|
| 40 | `Citadel Securities` | `citadel_securities` | pass |
| 41 | `Citadel Securities LLC` | `citadel_securities` | pass |
| 42 | `Citadel Securities Europe Limited` | `citadel_securities` | pass |
| 43 | `CITADEL SECURITIES (EUROPE) LIMITED` | `citadel_securities` | pass |
| 44 | `Citadel Securities LP` | `citadel_securities` | pass |
| 45 | `Citadel Securities GCS (Ireland) Limited` | `citadel_securities` | pass |

### 8.5 Citadel — the money negatives

| # | input | expect | result |
|---|---|---|---|
| 46 | `The Citadel` | NONE (position field) | pass |
| 47 | `The Citadel, The Military College of South Carolina` | NONE | pass |
| 48 | `Citadel Military College of South Carolina` | NONE | pass |
| 49 | `The Citadel Alumni Association` | NONE | pass |
| 50 | `The Citadel School of Engineering` | NONE | pass |
| 51 | `Citadel Broadcasting` | NONE | pass |
| 52 | `Citadel Broadcasting Corporation` | NONE | pass |
| 53 | `Citadel Communications` | NONE | pass |
| 54 | `Citadel Credit Union` | NONE | pass |
| 55 | `Citadel Federal Credit Union` | NONE | pass |
| 56 | `Citadel Defense Company` | NONE | pass |
| 57 | `Citadel Servicing Corporation` | NONE | pass |
| 58 | `Citadel Servicing Corp / Acra Lending` | NONE | pass |
| 59 | `The Citadel Group` | NONE | pass |
| 60 | `Citadel Group Australia` | NONE | pass |
| 61 | `Citadel Security Software` | NONE | pass |
| 62 | `Citadel Insurance Services` | NONE | pass |
| 63 | `Citadel Salisbury` | NONE | pass |
| 64 | `Citadel Bank` | NONE | pass |
| 65 | `Citadel Mall` | NONE | pass |
| 66 | `Citadelle Distillery` | NONE | pass |
| 67 | `La Citadelle` | NONE | pass |

### 8.6 Adjacent quant firms

| # | input | expect | result |
|---|---|---|---|
| 68 | `Two Sigma Investments` | `two_sigma` | pass |
| 69 | `Hudson River Trading` | `hrt` | pass |
| 70 | `HRT` | `hrt` | pass |
| 71 | `Jump Trading` | `jump` | pass |
| 72 | `D. E. Shaw & Co.` | `de_shaw` | pass |
| 73 | `The D. E. Shaw Group` | `de_shaw` | pass |
| 74 | `Optiver` | `optiver` | pass |
| 75 | `IMC Trading` | `imc` | pass |
| 76 | `Susquehanna International Group` | `sig` | pass |
| 77 | `SIG Susquehanna` | `sig` | pass |
| 78 | `Virtu Financial` | `virtu` | pass |
| 79 | `Millennium Management` | `millennium` | pass |
| 80 | `Point72 Asset Management` | `point72` | pass |
| 81 | `Point 72` | `point72` | pass |
| 82 | `Two Sigma Ventures` | `two_sigma` | pass |
| 83 | `IMC Financial Markets` | `imc` | pass |
| 84 | `Optiver Australia` | `optiver` | pass |

> #82 is a deliberate policy choice, not an accident: Two Sigma Ventures is folded into
> `two_sigma` as a same-family alias, whereas **Jump Capital is a `negative_alias` of
> `jump`** (#89) because a Jump Capital VC connection is not a Jump Trading connection.
> If Wave 2 wants the opposite policy for either, change the YAML, not the matcher.

### 8.7 Cross-firm confusable negatives

| # | input | expect | result |
|---|---|---|---|
| 85 | `Sigma Software` | NONE | pass |
| 86 | `SIG Sauer` | NONE | pass |
| 87 | `SIG Group AG` | NONE | pass |
| 88 | `Virtu Health` | NONE | pass |
| 89 | `Jump Capital` | NONE | pass |
| 90 | `Millennium Physician Group` | NONE | pass |
| 91 | `Millennium Trust Company` | NONE | pass |
| 92 | `Hudson River Community Credit Union` | NONE | pass |
| 93 | `Hudson Bay Capital` | NONE | pass |
| 94 | `Shaw Communications` | NONE | pass |

### 8.8 Field-context cases (education field) — Wave 3 must add the `field_kind` parameter

| # | input | field | expect |
|---|---|---|---|
| 95 | `The Citadel` | education | `the_citadel_college` |
| 96 | `Citadel` | education | `the_citadel_college` |
| 97 | `The Citadel, The Military College of South Carolina` | education | `the_citadel_college` |
| 98 | `Citadel Securities` | education | NONE |
| 99 | `Jane Street` | education | NONE (or `jane_street` as an *event/program* edge, not employment) |
| 100 | `The Citadel` | position | `citadel_ambiguous` → review queue |

### 8.9 Slug normalization cases (all verified)

| # | input | expected key |
|---|---|---|
| 101 | `https://www.linkedin.com/in/jane-doe-12345678/` | `jane-doe-12345678` |
| 102 | `http://linkedin.com/in/jane-doe-12345678` | `jane-doe-12345678` |
| 103 | `https://uk.linkedin.com/in/jane-doe-12345678` | `jane-doe-12345678` |
| 104 | `https://www.linkedin.com/in/jane-doe-12345678?trk=people-guest_people_search-card` | `jane-doe-12345678` |
| 105 | `https://www.linkedin.com/in/jane-doe-12345678/en` | `jane-doe-12345678` |
| 106 | `https://www.linkedin.com/in/jane-doe-12345678/?originalSubdomain=uk` | `jane-doe-12345678` |
| 107 | `www.linkedin.com/in/jane-doe-12345678` | `jane-doe-12345678` |
| 108 | `/in/jane-doe-12345678` | `jane-doe-12345678` |
| 109 | `jane-doe-12345678` (bare) | `jane-doe-12345678` |
| 110 | `https://www.linkedin.com/in/JANE-DOE-12345678/` | `jane-doe-12345678` |
| 111 | `https://www.linkedin.com/in/jane-doe-12345678/overlay/contact-info/` | `jane-doe-12345678` |
| 112 | `https://www.linkedin.com/in/%C3%A9lodie-martin-4b2a1c` | `élodie-martin-4b2a1c` |
| 113 | `https://de.linkedin.com/in/j%C3%B6rg-m%C3%BCller-9a8b7c` | `jörg-müller-9a8b7c` |
| 114 | `https://www.linkedin.com/in/ACoAAABcDeFgHiJkLmNoP` | `urn:ACoAAABcDeFgHiJkLmNoP` (**case preserved**) |
| 115 | `https://www.linkedin.com/in/ACoAAABcDEFGHIJKLMNOP` | `urn:ACoAAABcDEFGHIJKLMNOP` (**≠ #114**) |
| 116 | `https://www.linkedin.com/pub/jane-doe/12/345/678` | `pub:jane-doe/12/345/678` |
| 117 | `https://www.linkedin.com/pub/jane-doe/12/345/679` | `pub:jane-doe/12/345/679` (**≠ #116**) |
| 118 | `https://www.linkedin.com/company/citadel-llc` | `None` (not a person) |
| 119 | `` (empty) | `None` |

### 8.10 Person-name cases

Positive: `Robert Chen`/`Bob Chen` · `Robert J. Chen`/`Robert Chen` · `William Zhang`/`Bill
Zhang` · `Elizabeth Novak`/`Liz Novak` · `Katherine O'Brien`/`Kate OBrien` · `Jose
Garcia`/`José García` · `Jorg Muller`/`Jörg Müller` · `Anna Kowalska`/`Anna Kowalski` ·
`Xiaoming Li`/`Li Xiaoming` · `Sarah Miller`/`Sarah Miller-Weiss` · `Andrew Ng`/`Andy Ng` ·
`Chris Park`/`Christopher Park` · `Alexander Petrov`/`Aleksandr Petrov` · `Nguyen Van
An`/`Van An Nguyen`.

Negative: `Robert Chen`/`Roberta Chen` · `Sarah Miller`/`Sarah Weiss` · `Michael
Chang`/`Michelle Chang` · `David Kim`/`Daniel Kim` · `Chris Park`/`Christine Park` ·
`Alexander Petrov`/`Alexandra Petrova` · `Wei Zhang`/`Wei Zhao` · `J. Smith`/`John Smith`.

---

## 9. Benchmark harness

Reference implementation preserved at **`docs/research/04-entity-resolution/`**:
- `matcher.py` — normalisation + index build + containment guard + `match()`. Loads
  `data/gazetteer/firms.yaml` directly, so the gazetteer is executable, not just prose.
  Includes `audit_index()`, which asserts no negative alias collides with a positive key.
- `cases.py` — the 94 executable company cases of §8.1–8.7 as `(input, expected)` tuples.
- `slugs.py` — `norm_slug()` plus the §8.9 cases.

Wave 3 should import these directly or port them under `src/`. The benchmark drivers
(`bench.py` A–D, `bench3.py` E–F, `bench4.py` G–I, `bench5.py` J, `bench6.py` L) were
run in a scratch directory and are not preserved; every number they produced is recorded
in §4 and §6 above, and each experiment is described precisely enough to rebuild.

Versions: `rapidfuzz` 3.14.5 · `jellyfish` · `cleanco` 2.x · `scikit-learn` 1.x ·
`recordlinkage` · Python 3.11.15. `thefuzz` was installed; it is a thin wrapper over
`python-Levenshtein` with the same `partial_ratio` failure mode and is strictly dominated
by `rapidfuzz` on both speed and API — no separate benchmark warranted.

**Production dependencies: `rapidfuzz`, `pyyaml`, `jellyfish` (blocking only).** Not
`cleanco` (copy its term list), not `sklearn`, not `sparse_dot_topn`, not `recordlinkage`,
not `splink`, not `dedupe`, not an embedding model.

## 10. Open items for Wave 2/3

1. **Verify the LinkedIn slugs in a real browser** and flip `linkedin_slug_verified`.
   `school/the-citadel` is the least certain and the most important.
2. **Populate `field_kind`** end-to-end; §8.8 cannot be tested until the resolver accepts it.
3. **Decide the `citadel_ambiguous` policy** — review queue, LLM adjudication, or drop.
4. Confirm the export header at runtime; **fail loudly if `URL` is missing** rather than
   silently degrading to name matching.
5. Log every reject/review with its reason; that log is the alias-curation backlog.
6. Headcounts are third-party 2024–2026 estimates for order-of-magnitude weighting only.
