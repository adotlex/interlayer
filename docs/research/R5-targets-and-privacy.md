# R5 — Target-firm enumeration, entity resolution, and privacy obligations

Research agent R5, Wave 1. Date: 2026-08-17.

Scope: who the target people are, how to identify them without silently corrupting the
result set, and what obligations attach to holding data about them.

> **Method note.** `linkedin.com`, `wikipedia.org` and most primary-source domains are blocked
> by this session's egress policy, so LinkedIn page facts below come from search-index
> summaries rather than direct page fetches, and are marked with a confidence level.
> Everything in the *Entity resolution spec* was **executed and measured locally** on
> Python 3.11.15 with the real libraries — those numbers are not estimates.
> Numeric LinkedIn company IDs could not be resolved from this environment; the registry
> carries `null` placeholders plus the exact local procedure to fill them.

---

## Summary

- **Four target entities, not two.** Jane Street, Citadel (the hedge fund), and Citadel
  Securities (the market maker) are legally and organisationally distinct. Citadel and
  Citadel Securities have separate LinkedIn pages, separate management, separate technology
  and independent compliance programmes. Merging them corrupts cluster structure, because a
  Citadel Securities quant and a Citadel equities PM sit in different networks.
- **Jane Street has at least three LinkedIn company pages, and one of them is a different
  company entirely.** The live page is `jane-street-global` (~444k followers). A near-dormant
  `jane-street-capital-llc` page also exists, and `janestreetgroup` (~45 followers) is an
  unrelated **corporate-events** business. Slug-based enumeration must whitelist, never
  pattern-match.
- **"Citadel" is one of the worst possible anchor strings.** Confirmed unrelated companies
  include Citadel Federal Credit Union, The Citadel (Military College of South Carolina),
  Citadel Defense, Citadel Information Group, Citadel Servicing, Citadel Broadcasting and
  Citadel Roofing & Solar. Naive substring or fuzzy matching on "Citadel" is guaranteed to
  produce false positives.
- **No fuzzy scorer separates these positives from these negatives.** Measured locally:
  `token_set_ratio` scores `Jane Street Capital LLC` at 100 *and* `Jane Street Entertainment`
  at 100; `partial_ratio` scores `The Citadel` at 100. **Fuzzy matching must not be the
  decision mechanism.** The full scorer sweep is in Findings §2.
- **Ship a deterministic pipeline with fuzzy as a typo-only backstop.** Negative patterns →
  anchor regex → discriminator (`requires`/`excludes`) → exact alias → anchored-prefix →
  `rapidfuzz.fuzz.token_sort_ratio` fallback. Measured **100/100** on a 100-case adversarial
  fixture set. `token_sort_ratio`, not `token_set_ratio` — the reasoning is in §2.
- **`rapidfuzz` 3.14.5** (`requires_python >=3.10`, wheels classified for 3.11/3.13/3.14),
  verified installed and exercised on Python 3.11.15. Throughput measured at 69 µs/row
  uncached, 0.3 µs/row with a distinct-string memo — a **220× speedup**, so memoise.
- **Three-way classification is mandatory, not a nicety.** An unknown "Citadel Something"
  must go to a review queue, never be silently claimed or silently dropped. The review queue
  is bounded by *distinct company strings* containing an anchor (80 distinct strings across a
  30 000-row synthetic corpus), so it is a few dozen human decisions, once.
- **Never `unidecode` a person's name.** Measured: `李明 → "Li Ming "`, `محمد → "mHmd"`. Use
  NFKD combining-mark stripping instead — a no-op for CJK, correct for Latin.
- **Never use `nameparser` to decide given vs family name.** Measured: it parses
  `Nguyễn Văn An` as first=Nguyễn/last=An and `Kim Min-jun` as first=Kim — both invert the
  actual family name. Use an **order-free sorted-token key** instead, which merges
  `Smith, Robert` with `Robert Smith` and `Li Ming` with `Ming Li` without ever needing to
  know which token is the surname.
- **Key on the LinkedIn public identifier, but do not trust it as immutable.** Users may
  change the vanity slug up to 5 times per 180 days and old URLs 404 without redirect. Mint a
  local surrogate `person_id` and treat the slug as a *current alias*, with history.
- **The household exemption almost certainly does not apply here.** GDPR Art. 2(2)(c) is read
  narrowly (*Ryneš*, *Lindqvist*) and falls away as soon as processing has a professional or
  commercial purpose — which warm-intro pathfinding for career or business development is.
  Rely on **legitimate interest (Art. 6(1)(f))** with a documented three-part assessment, and
  build the minimisation controls that make the balancing test pass.
- **This is a legitimate, commercially-sold use case.** LinkedIn itself sells it: Sales
  Navigator **TeamLink** surfaces exactly "who on my side is 1st-degree to this target" and
  offers a warm-intro request flow. The ethical line is not the pathfinding; it is
  aggregation, resale, inference about protected characteristics, and contacting people using
  data they never shared with the user.
- **21 assertable privacy controls** are specified in §Privacy controls spec, each phrased so
  Wave 3 can write a test directly against it.

---

## Target firm profiles

### Jane Street

| Field | Value | Confidence |
|---|---|---|
| Entity id | `jane_street` | — |
| Parent legal entity | **Jane Street Group, LLC** (Delaware) | High |
| Key subsidiaries | Jane Street Capital, LLC (US broker-dealer, SEC/FINRA); Jane Street Execution Services, LLC (US BD); Jane Street Global Trading, LLC; JSCC Limited (Cayman, unregulated); Jane Street Financial Limited (UK, FCA **FRN 486546**, Co. No. 6211806, authorised 2009-05-01); Jane Street Netherlands B.V. (AFM); Jane Street Asia Trading Limited (Hong Kong) | High |
| **Primary LinkedIn slug** | **`jane-street-global`** → `linkedin.com/company/jane-street-global` | High |
| LinkedIn page display name | "Jane Street" | High |
| LinkedIn followers | ~444,139 | Medium |
| LinkedIn members listing this employer | ~3,459–3,746 | Medium |
| LinkedIn size band | 1,001–5,000 | Medium |
| Actual headcount | ~3,317 (Mar 2026, +2.5% YoY from 2,886); other trackers 3,6xx–3,7xx | Medium |
| Geographic split | US 54.6%, UK 21.1%, Hong Kong 11.0% | Medium |
| Offices | New York (HQ, 250 Vesey St), London, Hong Kong, Amsterdam, Singapore, Chicago | High |
| Numeric company ID | **unresolved — see procedure below** | — |

**Employer-string variants seen in the wild:** `Jane Street` · `Jane Street Capital` ·
`Jane Street Capital, LLC` · `Jane Street Group` · `Jane Street Group, LLC` ·
`Jane Street Europe Limited` · `Jane Street Financial Limited` ·
`Jane Street Netherlands B.V.` · `Jane Street Asia` · `Jane Street Global Trading, LLC` ·
`Jane Street Execution Services, LLC` · `JaneStreet` · `Jane St.` · `Jane Street (JS)`

**False-positive traps:**

| Trap | Why it bites |
|---|---|
| `linkedin.com/company/janestreetgroup` | **A different company.** ~45 followers, corporate-events and culture campaigns. A slug-pattern crawler would ingest it as Jane Street Group. |
| `linkedin.com/company/jane-street-capital-llc` | A second, near-dormant Jane Street page. Real but not the live one; enumerating it alone under-collects badly. |
| `Jane Street Entertainment` | Unrelated. |
| `23 Jane Street`, `45 Jane Street` | Jane Street is a real street in Manhattan's West Village and in Toronto. Postal addresses leak into free-text employer fields. |
| Local businesses on Jane Street | Coffee shops, dental practices, physiotherapists, galleries. |
| `Mary Jane …`, `Jane Doe …`, `Jane Iredale` (cosmetics brand) | Contain the token `jane`; caught only by requiring *both* anchor tokens. |
| Bare `JS` | Catastrophically ambiguous — JS Held, JS Bank, JavaScript, personal initials. Must be **review-only, never confident.** |

### Citadel (the hedge fund)

| Field | Value | Confidence |
|---|---|---|
| Entity id | `citadel_llc` | — |
| Legal entity | **Citadel LLC** / Citadel Enterprise Americas LLC; Citadel Advisors LLC (SEC-registered adviser) | High |
| Business | Multi-strategy alternative investment manager. ~$66bn investment capital (Feb 2026). Founded 1990 by Kenneth C. Griffin; originally *Wellington Financial Group*, renamed Citadel 1994. | High |
| **LinkedIn slug** | **`citadel-llc`** → `linkedin.com/company/citadel-llc` | High |
| LinkedIn page display name | **"Citadel"** (not "Citadel LLC") | High |
| LinkedIn members listing this employer | ~4,409 | Medium |
| LinkedIn size band | 1,001–5,000 | Medium |
| Actual headcount | ~3,153 (2024); >3,200 financial professionals | Medium |
| HQ | 830 Brickell Plaza, Miami, FL | High |
| Numeric company ID | **unresolved** | — |

**Business units** (people often put these in the Company field instead of "Citadel"):
Citadel Global Equities · **Surveyor Capital** (launched 2008) · **Ashler Capital** (2018) ·
Citadel International Equities · Strategic Equity Investments · Equity Quantitative Research ·
Fixed Income & Macro · Global Quantitative Strategies (GQS) · Commodities.
Flagship funds: **Wellington**, **Kensington**, Kensington II.
A separate LinkedIn page `citadel-global-equities` also exists.

### Citadel Securities (the market maker)

| Field | Value | Confidence |
|---|---|---|
| Entity id | `citadel_securities` | — |
| Legal entity | **Citadel Securities LLC**; Citadel Securities Europe Limited; Citadel Securities GCS (Ireland) Limited; Citadel Securities Canada ULC | High |
| Business | Global market maker / liquidity provider across equities and fixed income. Split out as a distinct company in 2017. | High |
| **LinkedIn slug** | **`citadel-securities`** → `linkedin.com/company/citadel-securities` | High |
| LinkedIn followers | ~159,115 | Medium |
| LinkedIn members listing this employer | ~1,793 | Medium |
| Actual headcount | ~2,311 (Mar 2026, +1.7% YoY) | Medium |
| Office split | New York ~1,346 · Chicago ~433 · London ~354 · Miami (HQ) · 15 locations total | Medium |
| Numeric company ID | **unresolved** | — |

### Telling the two Citadels apart, and the traps

**How to tell them apart:** the discriminator is the literal token `Securities`. Citadel
Securities employees' page-linked employer string is `Citadel Securities`; hedge fund
employees' is bare `Citadel`. Both firms were founded by and are majority-owned by Ken
Griffin, which is precisely why people conflate them — the confusion spiked after the 2021
GameStop episode put both names in the news simultaneously.

**Residual ambiguity you cannot engineer away:** a bare `Citadel` in a free-text (non
page-linked) position could be written by a Citadel Securities employee being casual. The
registry assigns bare `Citadel` to `citadel_llc` because that is the hedge fund's LinkedIn
page display name, but every entity carries a `group: citadel` field so downstream analysis
can roll both up when the split does not matter, and the report must footnote that bare-
`Citadel` rows carry residual entity ambiguity.

**Unrelated "Citadel" companies confirmed to exist** — the negative-pattern list exists for
these: Citadel Federal Credit Union / Citadel Credit Union (banking) · The Citadel, The
Military College of South Carolina (university) · Citadel Defense Company (counter-drone
defence contractor) · Citadel Information Group (security consultancy) · Citadel Security
Software · Citadel Servicing Corporation (mortgage) · Citadel Broadcasting (radio) · Citadel
Insurance · Citadel Roofing & Solar · Citadel Health · Citadel EHS · Citadel Care Centers ·
Citadel Wealth Management · Citadel Realty Advisors · plus French-language `Citadelle` /
`La Citadelle`.

### Resolving the numeric company IDs locally

Slugs are stable and authoritative; numeric IDs are the more robust join key but are not
publicly indexed. The registry ships `linkedin_company_id: null` for each entity. The user
resolves them once, locally, logged in:

1. Open `https://www.linkedin.com/company/<slug>/`.
2. View source, search `urn:li:organization:` — the trailing digits are the ID.
3. Or: LinkedIn job search filtered to the company; the URL carries `f_C=<id>`. (As of Aug 2026
   LinkedIn's AI job search retains only `f_TPR`, `f_C`, `f_AL`, `f_EA` as URL filters.)
4. Write into `targets.yaml`. A non-null ID enables exact joins on any surface that emits URNs.

---

## Findings

### 1. Enumerating employees from LinkedIn

- The company page **People** tab is the free surface. Filters: location, education/school,
  job function/occupation, seniority, and connection degree. It is explicitly *capped* — it
  does not page through the whole employee base and it biases toward your existing
  connections. It is a starting point, not an enumeration.
  ([cufinder](https://cufinder.io/blog/find-company-employees-on-linkedin/),
  [useembers](https://useembers.com/blog/how-to-search-for-employees-on-linkedin/))
- **People search with the `Current company` filter** returns a fuller current-staff list, but
  free accounts are capped at **1,000 results per query** (100 pages × 10) and hit a
  **Commercial Use Limit at roughly 300 people-searches per month**, resetting at midnight on
  the 1st. Sales Navigator raises this to unlimited searches and 2,500 results per search.
  ([phantombuster](https://phantombuster.com/blog/social-selling/linkedin-search-limits/),
  [dux-soup](https://www.dux-soup.com/blog/linkedin-search-limits))
- **Build consequence:** with ~3,300 Jane Street, ~4,400 Citadel and ~1,800 Citadel Securities
  LinkedIn members, no free surface enumerates any of them completely in one query. The
  1,000-result cap must be worked around by *partitioning* queries (by location, by function,
  by seniority) rather than by paging further — and the tool must record which partitions were
  run so coverage is auditable rather than assumed. Coordinate with R4 on acquisition; R5's
  position is that **enumeration completeness must be reported as a coverage figure, never
  assumed to be 100%.**

### 2. Employer-string matching — the measured case against fuzzy-first

Run locally, `rapidfuzz` 3.14.5, on 28 true-positive and 26 true-negative employer strings.
The table shows, per scorer, the *lowest* score any true positive received versus the
*highest* score any true negative received. **Any row where NEG-max ≥ POS-min is unusable as
a threshold-based decision rule.**

| Scorer | POS min (JS / CS / Citadel) | NEG max | Separable? |
|---|---|---|---|
| `ratio` | 59.6 / 64.3 / 54.9 | 88.0 (`23 Jane Street`) | **No** |
| `partial_ratio` | 93.3 / 100 / 100 | **100** (`The Citadel`, `Jane Street Entertainment`) | **No** |
| `token_sort_ratio` | 56.4 / 64.3 / 51.0 | 88.0 (`23 Jane Street`) | **No** |
| `token_set_ratio` | 73.7 / 100 / 100 | **100** (`Jane Street Entertainment`, `The Citadel Group`) | **No** |
| `WRatio` | 85.5 / 90.0 / 90.0 | 95.0 (`23 Jane Street`) | **No** |
| `partial_token_set_ratio` | 100 / 100 / 100 | **100** (`Street Capital Group`, `La Citadelle`) | **No** |

**Every scorer fails.** This is the single most important finding in section 2: a threshold on
any single rapidfuzz scorer over raw employer strings cannot express this problem.

**Why `token_set_ratio` specifically is the wrong choice here**, despite being the usual
recommendation for company names: it scores on the *intersection* of token sets, so extra
tokens are free. `token_set_ratio("citadel technology", "citadel") == 100`. For an entity
whose canonical name is a single common noun, that makes every "Citadel X" in the world a
perfect match. `partial_ratio` fails for the mirror reason — it finds the best substring
window, so any string *containing* "Citadel" scores 100.

**Why `token_sort_ratio` is the right backstop:** it is order-insensitive (handles
`Securities Citadel`, comma-inverted forms) but **length-sensitive**, so unexplained extra
tokens cost score. Measured: `token_sort_ratio("citadel technology","citadel") = 62.5`,
while the typo `token_sort_ratio("citadel securites","citadel securities") = 97.1`. That is
exactly the discrimination we need: forgive typos, punish foreign tokens.

**Measured result of the shipped algorithm:** 100/100 on a 100-case fixture set spanning
legal-suffix variants, non-US subsidiaries, desk decoration, typos, concatenation,
full-width Unicode, diacritics, the confirmed unrelated-"Citadel" companies, peer trading
firms, and empty input. Two bugs were found and fixed by measurement rather than reasoning:

1. **Shared alias vocabulary forgave foreign tokens.** With one vocabulary per entity,
   `Citadel Capital` was scored *confident* because `capital` appears in the sibling alias
   `Surveyor Capital`. Fixed by scoping the "expected trailing tokens" vocabulary to only
   those aliases that *extend the matched alias as a prefix*.
2. **A typo in the discriminator silently reassigned the firm.** `Citadel Securites` matched
   `citadel_llc` at 100 via `token_set_ratio` — routing a market-maker employee into the
   hedge-fund bucket. Fixed by the `requires`/`excludes` discriminator pair on `securit\w*`
   plus the switch to `token_sort_ratio`.

**Performance:** 30,000 rows classified in 2.07 s (69.2 µs/row). With a memo keyed on the raw
string: 9 ms (0.3 µs/row), 220× faster — because a real `Connections.csv` has only a few
hundred distinct employer strings. **Memoise.**

### 3. Person entity resolution

**Stable key.** The LinkedIn public identifier (`/in/<slug>`) is the only cross-source join
key present in `Connections.csv` (the `URL` column). It is *not* immutable: users may change
the vanity slug up to **5 times per 180 days**, the previous URL keeps working for 180 days
only, and after that it **404s with no redirect**.
([LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a542685/manage-your-public-profile-url),
[linkedviewer](https://linkedviewer.com/blog/linkedin-profile-url-format-guide))
Therefore: mint a local surrogate, treat the slug as a mutable alias with history.

**Name handling — two measured findings that should change the default plan:**

*Do not `unidecode` person names.* Measured on the real library:

| Input | `unidecode` output | Verdict |
|---|---|---|
| `李明` | `"Li Ming "` | Invents a romanisation and a trailing space |
| `张伟` | `"Zhang Wei "` | Same |
| `محمد الأحمد` | `"mHmd l'Hmd"` | Garbage |
| `Владимир Петров` | `"Vladimir Petrov"` | Plausible but lossy |

Use NFKD combining-mark stripping instead. Measured: `José García → jose garcia`,
`Nguyễn Văn An → nguyen van an`, `Ivan Petrović → ivan petrovic`, and **`李明 → 李明`
(unchanged)**. Correct for Latin, a no-op for CJK. (Caveat: it strips Arabic hamza,
`الأحمد → الاحمد` — acceptable for a blocking key, never for the display form.)
`unidecode` remains appropriate for *company* names, which are Latin-script brand strings.

*Do not use `nameparser` to decide given vs family name.* Measured on `nameparser` 2.1.0:

| Input | Parsed first | Parsed last | Correct? |
|---|---|---|---|
| `Robert John Smith` | Robert | Smith | Yes |
| `Nguyễn Văn An` | Nguyễn | An | **No** — Nguyễn is the family name |
| `Kim Min-jun` | Kim | Min-jun | **No** — Kim is the family name |
| `李明` | *(empty)* | 李明 | **No** — whole name as surname |
| `Rajesh Kumar Iyer` | Rajesh | Iyer | Yes |
| `Jan van der Berg` | Jan | van der Berg | Yes |

It encodes a Western given-family assumption and inverts East and Southeast Asian names.
Use it **only** to strip titles and suffixes (`Dr.`, `PhD`, `CFA`), and never to derive a
surname for matching.

*The culturally-neutral alternative — an order-free sorted-token key.* Measured: it merges
`Smith, Robert` with `Robert Smith`, `José García` with `Jose Garcia`, `Nguyễn Văn An` with
`Nguyen Van An`, `Björn Åkesson` with `Bjorn Akesson`, and `Li Ming` with `Ming Li` —
**without ever needing to know which token is the surname.** That last merge is also its
limitation: `Li Ming` and `Ming Li` may be two different people, so the sorted key is a
*blocking* key for candidate generation, never a match decision.

**Nicknames need a lexicon; fuzzy cannot do it.** Measured: `william jones` vs `bill jones`
scores `token_sort_ratio` 43.5 — far below any usable threshold — while the genuinely
different `robert smith` / `roberta smith` scores 96.0. Fuzzy string distance has the
nickname problem exactly backwards. The `nicknames` package (1.0.1, `>=3.8`, installs clean on
3.11) provides a hand-curated lexicon: `robert → {bob, bobby, rob, ...}`, `bill → {william,
robert, will, willis}`. Two cautions: it is Anglo-only (empty for `jose`, `li`, `nguyen`,
`xiaoming`), and it is transitively noisy (`bob → {robert, bobby, bert}`). Use it as an
**additive equivalence expansion that can only promote a pair to *review* or add a bounded
score bonus** — never as a required transformation (so it cannot harm non-Western names) and
never as a sole basis for a confident merge.

**Phonetic keys are Latin-only.** `jellyfish.metaphone("李明")` returns `"L MNK "` — noise.
Gate phonetic blocking behind an `is_latin_script` check.

**Libraries verified installed and exercised on Python 3.11.15:**

| Library | Version | `requires_python` | Role |
|---|---|---|---|
| `rapidfuzz` | 3.14.5 (2026-04-07) | `>=3.10` | Company + name fuzzy scoring |
| `unidecode` | 1.4.0 | `>=3.7` | **Company names only** |
| `jellyfish` | 1.2.1 | `>=3.9` | Metaphone / Jaro-Winkler, Latin-script only |
| `nameparser` | 2.1.0 (2026-08-07) | `>=3.11` | Title/suffix stripping only |
| `nicknames` | 1.0.1 | `>=3.8` | Given-name equivalence expansion |
| `PyYAML` | 6.0.3 | `>=3.8` | Registry loading |

**Rejected:** `dedupe` 3.0.3 (needs labelled training data and an active-learning loop —
disproportionate for a few thousand rows); `splink` 4.0.16 (excellent Fellegi-Sunter engine
but pulls a SQL backend and is aimed at millions of rows); `recordlinkage` 0.16 (last released
2023-07, pandas-coupled); `probablepeople` (0.5.6, last release 2024, and its CRF is trained
on romanised Western names — same cultural failure mode as `nameparser`).

### 4. Privacy, data protection, ethics

**GDPR applies.** Jane Street / Citadel employees are identified natural persons; name,
employer, job title, profile URL and *the fact of a connection to a named third party* are all
personal data. The derived bipartite graph is itself personal data about both endpoints.

**Household exemption — the honest answer is that it probably does not cover this.**
Art. 2(2)(c) exempts processing "by a natural person in the course of a purely personal or
household activity". The CJEU reads it narrowly: *Ryneš* (C-212/13) held it must be limited to
*purely* — exclusively — personal or household activity; *Lindqvist* (C-101/01) held it covers
only activities carried out in the course of private or family life. The ICO's own framing is
that the exemption "falls away as soon as the processing has a professional or commercial
purpose". Warm-intro pathfinding for a job search or business development **is** a
professional purpose. Recital 18 does contemplate social networking within the exemption, and
a strictly private, non-commercial, purely-personal-curiosity use might squeak in — but that
is not a foundation to build on, and it evaporates the moment the output is used for career or
commercial ends.
([gdprhub Art. 2](https://gdprhub.eu/Article_2_GDPR),
[Recital 18](https://gdpr-info.eu/recitals/no-18/),
[ICO exemptions guide](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/exemptions/a-guide-to-the-data-protection-exemptions/),
[DPC Ireland](https://www.dataprotection.ie/en/faqs/general/what-household-exemption))

**So: rely on legitimate interest, Art. 6(1)(f), and document it.** EDPB Guidelines 1/2024
(adopted Oct 2024, superseding WP29 Opinion 06/2014) set three cumulative conditions, all of
which must be recorded:

1. **Purpose** — a lawful, clearly articulated, *real and present* interest, not speculative.
   Here: "identify which of my own existing contacts can introduce me to people at three named
   firms." That is concrete and present-tense.
2. **Necessity** — the processing must be *necessary*, i.e. no less intrusive route achieves
   it. This is what forces data minimisation to be a design constraint rather than a virtue:
   you need name, employer and the edge. You do not need email, phone, photo, education
   history, or anything about people who are not bridges.
3. **Balancing** — against the data subject's interests, rights and freedoms, weighing their
   *reasonable expectations*. This is the favourable factor here: everyone involved published
   their employer on a professional networking site and connected to the user or to a mutual
   contact. Being findable by a mutual contact for an introduction is squarely within the
   reasonable expectations of a LinkedIn user. The unfavourable factor is that the data
   subject has no relationship with the *tool*. Local-only, no-resale, no-sharing,
   short-retention controls are what tip the balance.

([EDPB Guidelines 1/2024 PDF](https://www.edpb.europa.eu/system/files/2024-10/edpb_guidelines_202401_legitimateinterest_en.pdf),
[EDPB announcement](https://www.edpb.europa.eu/news/news/2024/edpb-adopts-opinion-processors-guidelines-legitimate-interest-statement-draft_en),
[Timelex analysis](https://www.timelex.eu/en/blog/three-step-test-practice-edpb-guidelines-legitimate-interest-0))

**Art. 5 principles that convert directly into code:** purpose limitation (5(1)(b)), **data
minimisation** (5(1)(c) — collect only fields the analysis consumes), accuracy (5(1)(d) —
label inferences as inferences), **storage limitation** (5(1)(e) — a retention default and a
purge), integrity and confidentiality (5(1)(f)), and **accountability** (5(2) — an audit log
is the evidence that the rest happened).

**Art. 14 transparency — the real obligation, stated accurately.** Where personal data is not
obtained from the data subject, Art. 14 requires notifying them (identity of controller,
purposes, categories, source, retention, rights) within a reasonable period, at most one
month. Art. 14(5)(b) relieves this where notice is impossible or involves disproportionate
effort — but regulators read it restrictively and expect a *documented, per-project*
assessment, not a blanket claim, together with compensating measures such as a public notice.
For a single individual holding a few hundred records about people who are one or two hops
from them, individual notification is *not* obviously disproportionate — and unlike a
commercial scraper, this user typically *can* reach them. The practical, defensible posture:
(a) hold the data locally, briefly, and minimally; (b) ship a `PRIVACY.md` stating purpose,
categories, retention and rights; (c) treat any actual outreach as the natural moment of
disclosure — say how you found them; (d) honour erasure requests via `purge --person`.
([Art. 14](https://gdpr-info.eu/art-14-gdpr/),
[gdpr-text.com](https://gdpr-text.com/read/article-14/))

**Art. 15/17 practicalities.** Because the user is a controller, a data subject could in
principle make access or erasure requests. `purge --person <id>` plus a tombstone (so
re-ingest does not resurrect them) is the engineering answer to Art. 17.

**CCPA/CPRA, briefly.** The CCPA protects "consumers" — natural persons resident in
California — but only binds a **"business"**, defined by thresholds (broadly: >$25m annual
revenue, or buying/selling/sharing the personal information of 100,000+ consumers or
households, or ≥50% of revenue from selling/sharing personal information). **An individual
doing personal or professional research meets none of these, so CPRA obligations do not
attach.** Two things flip that: operating the tool on behalf of an employer that *is* a
covered business, or **selling or "sharing" the derived data** — "sharing" is defined broadly
enough (cross-context behavioural advertising) that it is worth avoiding entirely. There is no
US federal analogue. The practical upshot: **CPRA is a reason not to commercialise the output,
not a reason not to build the tool.**
([Cal. AG](https://oag.ca.gov/privacy/ccpa),
[IAPP on "business"](https://iapp.org/news/a/cpras-top-operational-impacts-part-2-defining-business))

**Ethics — say the legitimate part plainly.** Warm-intro pathfinding is a normal, mainstream
professional activity that LinkedIn itself sells. Sales Navigator **TeamLink** surfaces
exactly "does anyone on my side have a 1st-degree connection to this person", labels the lead
with a *TeamLink Intro* badge, and provides a request-an-introduction flow; LinkedIn markets
it on the basis that its own reps average ~1,490 connections but reach ~4.7m people through
it, and that TeamLink intros get up to 4× the response rate of cold InMail. This tool computes
the same quantity — `M ∩ connections(t)` — for one person's own network. Nothing about the
computation is exotic or covert.
([LinkedIn TeamLink Help](https://www.linkedin.com/help/sales-navigator/answer/a101027/teamlink-overview),
[LinkedIn Sales Blog](https://www.linkedin.com/business/sales/blog/sales-navigator/extending-the-power-of-teamlink-within-and-across-organizations))

**The specific lines that separate this from surveillance** — these are commitments the code
should enforce, not just aspirations:

1. **No aggregation into a resellable dataset.** Local files, single user, no shared store.
2. **No inference about protected characteristics.** Never derive or store ethnicity,
   nationality, gender, religion, health, sexual orientation, political opinion or trade union
   membership — including *implicitly*, e.g. by clustering on name origin, or by treating
   affinity-group membership as a signal. Deriving special-category data would also engage
   Art. 9, which legitimate interest cannot satisfy.
3. **No sharing of the derived graph.** The bipartite graph is personal data about people who
   never agreed to be in it. Reports are for the user's own decision-making.
4. **No contacting people using data they did not share with the user.** If an email address
   is absent from the export because the person did not opt to share it, do not source it
   elsewhere and do not mail it. Outreach goes through the introduction path, which is the
   whole point.
5. **No covert acquisition.** No credential sharing, no automated scraping in the default
   configuration, no misrepresenting identity to view a profile.
6. **Purpose limitation in practice.** Data collected to find introductions is not reused for
   recruiting pipelines, competitive intelligence or investment research without a fresh
   assessment.

---

## Proposed target registry

Ship verbatim as `src/interlayer/data/targets.yaml`. Wave 2 must not hand-edit the
`normalisation` block — it is the vocabulary the measured 100/100 result depends on.

```yaml
# interlayer target registry
# schema_version is checked at load; bump on any breaking shape change.
schema_version: 1
generated: "2026-08-17"
source: "docs/research/R5-targets-and-privacy.md"

# ---------------------------------------------------------------------------
# Global normalisation vocabulary. Shared by all entities.
# ---------------------------------------------------------------------------
normalisation:
  # Stripped before matching. Never affects the entity decision.
  legal_suffixes:
    - llc
    - l.l.c
    - inc
    - incorporated
    - ltd
    - limited
    - lp
    - l.p
    - llp
    - plc
    - gmbh
    - sa
    - nv
    - bv
    - b.v
    - pte
    - pty
    - co
    - corp
    - corporation
    - holding
    - holdings
    - company
    - companies
    - sarl
    - ag
    - oy
    - oyj
    - ab
    - aps
    - "as"
    - kk
    - kft
    - spa
    - srl
    - sl
    - sas
    - ulc
    - the
    - and
    - "of"
    - group
    - groupe

  # Decoration people add to a Company field that does not change the entity.
  # Stripped to form the "head"; presence never creates or blocks a match.
  modifier_tokens:
    # geography
    - europe
    - europa
    - asia
    - americas
    - america
    - uk
    - us
    - usa
    - emea
    - apac
    - global
    - worldwide
    - international
    - intl
    - ireland
    - netherlands
    - holland
    - singapore
    - hong
    - kong
    - london
    - amsterdam
    - york
    - new
    - ny
    - nyc
    - japan
    - tokyo
    - india
    - australia
    - sydney
    - canada
    - toronto
    - chicago
    - miami
    - shanghai
    - dublin
    - paris
    - zug
    - zurich
    - texas
    - austin
    - boston
    - gcs
    # desk / function decoration
    - markets
    - trading
    - quant
    - quantitative
    - research
    - engineering
    - tech
    - desk
    - team
    - options
    - etf
    - etfs
    - equities
    - equity
    - fixed
    - income
    - macro
    - fx
    - commodities
    - crypto
    - digital
    - assets
    - strategies
    - strategy
    - institutional
    - execution
    - services
    - platform
    - systems
    # employment decoration
    - internship
    - intern
    - contract
    - contractor
    - formerly
    - ex
    - current
    - present
    - remote
    - hybrid
    - trader
    - developer
    - engineer
    - analyst
    - associate
    - vp
    - svp
    - avp
    - director
    - manager
    - head
    - fulltime
    - consultant
    - advisor
    - via
    - at
    - "on"
    - behalf

# ---------------------------------------------------------------------------
# Cross-entity match policy.
# ---------------------------------------------------------------------------
match_policy:
  scorer: "rapidfuzz.fuzz.token_sort_ratio"   # NOT token_set_ratio; see Findings §2
  library: "rapidfuzz>=3.14,<4"
  # Two entities scoring within this margin => ambiguous => review, never confident.
  ambiguity_margin: 5
  # A head token is "unexpected" only if it has no partner in the matched alias's
  # extension vocabulary at or above this ratio. Forgives typos, punishes foreign tokens.
  unexpected_token_forgiveness: 85
  # A typo'd anchor (regex miss, fuzzy hit) can reach review but never confident.
  fuzzy_anchor_min: 88
  # Memoise on the raw company string: measured 220x speedup.
  memoise: true

# ---------------------------------------------------------------------------
# Target entities.
# ---------------------------------------------------------------------------
entities:

  - id: jane_street
    canonical: "Jane Street"
    group: jane_street
    linkedin:
      slug: "jane-street-global"
      company_id: null          # resolve locally: view-source -> urn:li:organization:<id>
      page_display_name: "Jane Street"
      # Additional REAL pages for the same firm; enumerate but do not treat as separate firms.
      secondary_slugs:
        - "jane-street-capital-llc"
      # Slugs that look like the target but are DIFFERENT COMPANIES. Never enumerate.
      confusable_slugs:
        - slug: "janestreetgroup"
          note: "Unrelated corporate-events business, ~45 followers. Not Jane Street."
        - slug: "jane-street-entertainment"
          note: "Unrelated."
    legal_entities:
      - "Jane Street Group, LLC"
      - "Jane Street Capital, LLC"
      - "Jane Street Execution Services, LLC"
      - "Jane Street Global Trading, LLC"
      - "Jane Street Financial Limited"      # UK, FCA FRN 486546
      - "Jane Street Netherlands B.V."       # AFM
      - "Jane Street Asia Trading Limited"   # Hong Kong
      - "JSCC Limited"                       # Cayman, unregulated
    offices: [New York, London, Hong Kong, Amsterdam, Singapore, Chicago]
    headcount_estimate: 3317
    headcount_as_of: "2026-03"

    aliases:
      - "jane street"
      - "jane street capital"
      - "jane street financial"
      - "jane street netherlands"
      - "jane street markets"
      - "jane street execution"
      - "jane street global"
      - "jane st"
    # Matching these yields REVIEW and can never yield CONFIDENT.
    review_only_aliases:
      - "js"
      - "jane"
    # Self-referential abbreviations dropped when forming the head.
    noise_tokens: [js, jsg, jsc]

    anchors:
      - '\bjane\s*st(?:\.|reet|reets)?\b'
      - '\bjanestreet\b'
    # ALL of these must be present (exactly, or fuzzily >= fuzzy_anchor_min) for the
    # typo fallback to fire. Requiring both is what rejects "Jane Iredale".
    anchor_required: [jane, street]

    negative_patterns:
      - '\bjane\s*street\s+(entertainment|coffee|bakery|foods?|theat(?:er|re)|records|media|press|books?|gallery|studios?|dental|clinic|church|deli|salon|realty|apartments|residences|properties|hostel|hotel|pharmacy|florist|tavern|pub|physio\w*|yoga|veterinary|nursery|school|academy|barbers?|laundr\w+)\b'
      - '^\d+[a-z]?\s+jane\s+street\b'     # postal address, e.g. "23 Jane Street"
      - '\bmary\s+jane\b'
      - '\bjane\s+doe\b'
      - '\bcalamity\s+jane\b'
      - '\bjane\s+iredale\b'              # cosmetics brand

    threshold: 88
    review_floor: 70

  - id: citadel_securities
    canonical: "Citadel Securities"
    group: citadel
    linkedin:
      slug: "citadel-securities"
      company_id: null
      page_display_name: "Citadel Securities"
      secondary_slugs: []
      confusable_slugs: []
    legal_entities:
      - "Citadel Securities LLC"
      - "Citadel Securities Europe Limited"
      - "Citadel Securities GCS (Ireland) Limited"
      - "Citadel Securities Canada ULC"
    offices: [Miami, New York, Chicago, London, Hong Kong, Dublin, Sydney, Tokyo]
    headcount_estimate: 2311
    headcount_as_of: "2026-03"

    aliases:
      - "citadel securities"
    review_only_aliases: []
    noise_tokens: [cs, citsec]

    anchors:
      - '\bcitadel\b'
      - '\bcitadelsecurities\b'
    anchor_required: [citadel, securities]
    # DISCRIMINATOR. Must be present, else this is the hedge fund, not the market maker.
    # No leading \b: must also fire on the concatenated form "citadelsecurities".
    requires: 'securit\w*'

    negative_patterns:
      - '\bcitadel\s+security\s+(software|systems|solutions|services|group)\b'
      - '\bcitadel\s+(federal\s+)?credit\s+union\b'
      - '\bmilitary\s+college\b'

    threshold: 88
    review_floor: 70

  - id: citadel_llc
    canonical: "Citadel"
    group: citadel
    linkedin:
      slug: "citadel-llc"
      company_id: null
      page_display_name: "Citadel"     # NB: displays as "Citadel", not "Citadel LLC"
      secondary_slugs:
        - "citadel-global-equities"
      confusable_slugs:
        - slug: "citadel-credit-union"
          note: "Banking. Unrelated."
        - slug: "the-citadel"
          note: "Military College of South Carolina. Unrelated."
        - slug: "citadel-defense-company"
          note: "Counter-drone defence contractor. Unrelated."
    legal_entities:
      - "Citadel LLC"
      - "Citadel Enterprise Americas LLC"
      - "Citadel Advisors LLC"
    business_units:
      - "Citadel Global Equities"
      - "Surveyor Capital"
      - "Ashler Capital"
      - "Citadel International Equities"
      - "Strategic Equity Investments"
      - "Equity Quantitative Research"
      - "Fixed Income and Macro"
      - "Global Quantitative Strategies"
      - "Commodities"
    funds: [Wellington, Kensington, "Kensington II"]
    offices: [Miami, New York, Chicago, London, Hong Kong, Greenwich]
    headcount_estimate: 3153
    headcount_as_of: "2024"

    aliases:
      - "citadel"
      - "citadel enterprise"
      - "citadel enterprise americas"
      - "citadel advisors"
      - "citadel investment"
      - "citadel kensington"
      - "citadel wellington"
      - "citadel gqs"
      - "surveyor capital"
      - "ashler capital"
      - "citadel commodities"
    review_only_aliases: []
    noise_tokens: [cit]

    anchors:
      - '\bcitadel\b'
      - '\bsurveyor\s+capital\b'
      - '\bashler\s+capital\b'
    anchor_required: [citadel]
    # DISCRIMINATOR. Anything securities-flavoured belongs to citadel_securities.
    excludes: 'securit\w*'

    negative_patterns:
      - '\bcitadel\s+(federal\s+)?credit\s+union\b'
      - '\bthe\s+citadel\b'
      - '\bmilitary\s+college\b'
      - '\bcitadelle\b'
      - '\bcitadel\s+(defen[cs]e|information|servicing|broadcasting|insurance|banking|health|ehs|roofing|solar|salisbury|wealth|steel|realty|real\s+estate|mortgage|packaging|construction|exploration|mining|logistics|staffing|academy|school|college|church|ministries|press|studios?|games?|software|plastics|environmental|completions|energy|infrastructure|nursing|rehab|care|cleaning|federal|credit|bank)\b'

    # Higher than the others: the anchor "citadel" is a common noun, so demand more.
    threshold: 92
    review_floor: 72
```

---

## Entity resolution spec

### A. Company normalisation — `normalise_company(s) -> str`

Applied in this order. Each step was exercised by the 100-case fixture.

1. `unicodedata.normalize("NFKC", s)` — folds full-width `Ｊａｎｅ Ｓｔｒｅｅｔ` to ASCII.
2. Replace ideographic space `U+3000` and non-breaking space `U+00A0` with `U+0020`.
3. Map the dash family `U+2010–U+2015`, `U+2212` → `-`; the apostrophe family
   `U+2018 U+2019 U+02BC U+0060 U+00B4` → `'`.
4. `unidecode(s)` — **companies only**, never person names.
5. `.casefold()` — not `.lower()`; correct for `ß`, `İ`.
6. `&` → ` and `.
7. `re.sub(r"(?<=\w)-(?=\w)", " ", s)` — intra-word hyphen becomes a separator, so
   `Citadel-Securities` and `Jane-Street` normalise to the spaced forms.
8. `re.sub(r"[^\w\s.']", " ", s)` — drops `(){}|/,` etc. Keeps `.` and `'` for `B.V.`, `L.P.`.
9. Collapse whitespace, strip.

**Token derivations:**
- `tokens` = split on whitespace, strip leading/trailing `.` and `'`, drop empties.
- `bare_tokens` = `tokens` − `legal_suffixes`.
- `head_tokens` = `bare_tokens` − `modifier_tokens` − entity `noise_tokens`.
- `head` = `" ".join(head_tokens)`.

### B. Company matching — `classify_company(raw) -> MatchResult`

For each entity, in registry order:

1. **Empty guard.** Blank/whitespace-only → `no_match`, reason `empty`.
2. **Negative patterns.** Any `negative_patterns` match against the *normalised* string →
   this entity is eliminated. Negatives beat everything; they are the only unconditional veto.
3. **Review-only aliases.** If `bare_tokens` joined equals a `review_only_aliases` entry →
   candidate at score 100 with `forced_review=True`. Checked against `bare`, not `head`,
   because `noise_tokens` would otherwise delete the very token being tested (`JS`).
4. **Anchor.** Any `anchors` regex matches the normalised string → anchored.
   Otherwise the **fuzzy-anchor fallback**: if for *every* token in `anchor_required` there
   exists a head token with `fuzz.ratio >= fuzzy_anchor_min (88)`, set `forced_review=True`
   and continue. If neither, this entity is eliminated. Requiring *all* anchor tokens is what
   makes `Jane Iredale` (has `jane`, lacks `street`) fall through, while `Citdel Securities`
   survives to review.
5. **Discriminators.** `requires` absent from the normalised string → eliminate.
   `excludes` present → eliminate. This pair is what keeps the two Citadels apart, and it is
   checked on the normalised string (not the head) so it survives concatenation.
6. **Exact alias.** `head` in `aliases`, **or** `head` with spaces removed equals an alias with
   spaces removed (catches `JaneStreet`, `CitadelSecurities`) → score 100, `exact_alias`.
7. **Anchored prefix.** Longest alias first: if `head_tokens` *begins with* the alias's token
   sequence → score 100, `anchored_prefix`, and the trailing tokens go to the
   unexpected-token check. This is what lets `Citadel — Ken Griffin` and `Citadel Technology`
   reach review instead of vanishing.
8. **Fuzzy fallback.** `score = max(fuzz.token_sort_ratio(head, alias) for alias in aliases)`,
   remembering the best alias.

**Unexpected-token check** (applies to steps 7 and 8). Build the *extension vocabulary* of the
matched alias: its own tokens, plus the tokens of every alias that has it as a token-prefix.
**Not the whole entity vocabulary** — that bug scored `Citadel Capital` as confident because
`capital` appears in `surveyor capital`. A token is unexpected iff it is absent from that
vocabulary *and* `max(fuzz.ratio(token, v) for v in vocab) < 85`. The 85 forgiveness threshold
is what admits `Jane Street Captial` (`captial` vs `capital` = 85.7) while rejecting
`Citadel Technology`.

**Decision, over all surviving candidates sorted by score descending:**

| Condition | Result |
|---|---|
| No candidates | `no_match` — `no_anchor_or_negated` |
| Runner-up within `ambiguity_margin` (5) of the leader | `needs_review` — `ambiguous_entity`, both ids recorded |
| `review_only_alias` | `needs_review` |
| `forced_review` (typo'd anchor) | `needs_review` — `fuzzy_anchor` |
| Unexpected tokens present | `needs_review` — tokens listed in the reason |
| `score >= threshold` | **`confident`** |
| `review_floor <= score < threshold` | `needs_review` |
| `score < review_floor` | `no_match` — `below_floor` |

**Thresholds shipped, and why.** `jane_street` 88 / floor 70; `citadel_securities` 88 / 70;
`citadel_llc` **92** / 72. Citadel's is higher because its anchor is a single common noun, so
the prior probability that a "Citadel X" is the hedge fund is much lower than the probability
that a "Jane Street X" is the trading firm. These are the *backstop* thresholds — in the
fixture set the overwhelming majority of true positives resolve at steps 6–7 with score 100
and never reach the fuzzy branch at all. That is deliberate: the threshold governs only the
typo tail, which is why it can be set conservatively without costing recall.

**Precision/recall posture.** The design is tuned for **precision on `confident`, recall on
`confident ∪ needs_review`**. A false positive silently corrupts the cluster structure and the
user cannot detect it; a review item costs one human keystroke. Measured on the fixture set:
zero false positives at `confident`, zero true positives lost to `no_match`. The observed cost
is review volume on unknown "Citadel X" strings — bounded by *distinct* strings (80 across a
30 000-row synthetic corpus), and reduced to near-zero on subsequent runs by the decision
cache below.

**Review decision cache.** Persist adjudications to `~/.interlayer/review_decisions.yaml`,
keyed by the normalised company string → `{entity_id | "none", decided_at, decided_by:"user"}`.
On load, a cached decision short-circuits classification. Each distinct string is adjudicated
once, ever. Cache hits must be recorded in the audit log so a wrong past decision is traceable.

### C. Person entity resolution

**Keying, in priority order:**

1. `linkedin_slug` — extracted from the `URL` column via
   `re.search(r"/in/([^/?#]+)", url)`, then percent-decoded and casefolded. Highest priority,
   but **mutable** (5 changes / 180 days, old URL 404s after 180 days).
2. `linkedin_urn` — numeric member URN if any adapter supplies one. Immutable when present;
   prefer it over the slug whenever available.
3. `(name_key, company_entity_id)` — last-resort composite.

**Mint a surrogate.** `person_id = "p_" + blake2b(first_known_stable_key, digest_size=8).hex()`,
assigned on first sight and **never recomputed**. Store `slug_history: [{slug, first_seen,
last_seen}]`. A profile whose slug changed is re-linked by matching any historical slug, or by
`(name_key, company)` plus human confirmation. Never key application state on the slug
directly — that is the bug that silently duplicates a third of the graph after six months.

**Name normalisation — `normalise_name(s) -> NameKeys`:**

1. `unicodedata.normalize("NFKC", s)`.
2. Strip titles and suffixes with `nameparser.HumanName` — **read `.title` and `.suffix` only**.
   Never read `.first` / `.last` for matching purposes.
3. Diacritic fold: NFD → drop `unicodedata.combining(c)` → NFC → `casefold()`.
   **Do not `unidecode`.** Measured: no-op on CJK, correct on Latin.
4. Normalise separators: `'`-family → `'`, hyphen-family → `-`; collapse whitespace.
5. `display_name` = the original string, unmodified. This is what appears in reports.

**Blocking keys** (candidate generation only — never a match decision):

| Key | Definition | Merges | Notes |
|---|---|---|---|
| `sorted_token_key` | folded name split on `[\s'-]`, tokens sorted, joined | `Smith, Robert`≡`Robert Smith`; `Li Ming`≡`Ming Li`; `Nguyễn Văn An`≡`Nguyen Van An` | Primary. Order-free, so no given/family assumption. |
| `initials_key` | first char of each sorted token | catches `R. Smith` | Low precision; pair with another key. |
| `phonetic_key` | `jellyfish.metaphone` on each token | `Smith`≡`Smyth` | **Gate on `is_latin_script`** — returns noise for CJK. |
| `slug_key` | the LinkedIn slug itself | exact | Highest precision. |

Two records are compared only if they share at least one blocking key. On a few thousand
records this is O(n) in practice and the full pairwise comparison never needs to run.

**Pair scoring, once blocked:**

```
score = 0.60 * fuzz.token_sort_ratio(folded_a, folded_b)
      + 0.25 * (100 * jellyfish.jaro_winkler_similarity(folded_a, folded_b))
      + 0.15 * (100 if same_company_entity else 0)
nickname_bonus = +8 if any given-token pair is nickname-equivalent (nicknames 1.0.1)
```

`>= 92` → auto-merge. `80–92` → review. `< 80` → distinct.
**Hard override:** matching `linkedin_urn`s always merge; *differing* `linkedin_urn`s never
merge regardless of score. The nickname bonus is additive-only and is capped so that it can
promote a pair into review but can never by itself carry a pair over 92 — that constraint is
what keeps the Anglo-only lexicon from distorting non-Western names.

---

## Privacy controls spec

Each item is phrased as an assertable requirement. Wave 3 writes one test per ID.

**Locality and telemetry**

- **PRIV-01** — No module in `interlayer` opens an outbound network connection during
  `load`, `match`, `analyse`, `cluster`, `report` or `purge`. *Test:* monkeypatch
  `socket.socket.connect` / `socket.create_connection` to raise, run each command end-to-end
  on fixtures, assert success.
- **PRIV-02** — The package sends no telemetry, analytics, crash reporting or version check.
  *Test:* assert no import of `requests`, `httpx`, `urllib.request`, `socket` (beyond the
  guard) in any module outside `interlayer.adapters`; assert `interlayer.adapters` is not
  imported by the analysis path.
- **PRIV-03** — All persistent state is written under a single configurable root, default
  `~/.interlayer/`, and nothing is written outside it or the user-specified output path.
  *Test:* run with a temp root under an fs-write audit; assert every written path is a
  descendant of the root or the output path.
- **PRIV-04** — Every acquisition adapter declares `compliance_posture` ∈
  `{first_party_export, manual_capture, third_party_api, automated}` and the default config
  enables only `first_party_export`. *Test:* assert `default_config().enabled_adapters ==
  ["first_party_export"]`; assert every registered adapter class exposes the attribute.

**Minimisation**

- **PRIV-05** — The ingest layer retains only the whitelisted fields
  `{first_name, last_name, linkedin_slug, company_raw, position, connected_on}` from
  `Connections.csv`. *Test:* feed a CSV with extra columns; assert the persisted record has
  exactly the whitelisted keys.
- **PRIV-06** — The `Email Address` column is **discarded at parse time** unless
  `config.retain_emails is True` (default `False`). When retained, it is stored only in a
  field named `email` that is excluded from all report renderers. *Test:* parse a fixture with
  populated emails under default config; assert no email string appears anywhere in the store
  or any rendered report.
- **PRIV-07** — Records for non-bridge people (connections with no edge to any target) are not
  persisted beyond the run that computed them. *Test:* run the pipeline, assert the persisted
  person table contains only bridges plus the targets they bridge to.
- **PRIV-08** — No field, column, derived feature or cluster label encodes or proxies a
  special category under GDPR Art. 9 (racial or ethnic origin, political opinion, religious or
  philosophical belief, trade union membership, genetic, biometric, health, sex life, sexual
  orientation). *Test:* assert the persisted schema's field-name set equals an explicit
  allowlist constant, so adding any new field fails the test until reviewed.
- **PRIV-09** — Name-derived features (script, phonetic key, token order) are used only as
  blocking keys and never appear in report output or cluster labels. *Test:* assert no
  `*_key` field is reachable from any renderer.

**Retention, deletion, and the audit trail**

- **PRIV-10** — Every persisted record carries `collected_at` (ISO-8601 UTC) and `source`
  (adapter id). *Test:* schema assertion on every row after a run.
- **PRIV-11** — A retention default of **90 days** is enforced: on every startup, records with
  `collected_at` older than `config.retention_days` are deleted before any other work.
  *Test:* seed a record dated 91 days ago, start the tool, assert it is gone.
- **PRIV-12** — `interlayer purge --all` removes every file under the state root and exits
  non-zero if anything remains. *Test:* populate state, purge, assert the tree is empty.
- **PRIV-13** — `interlayer purge --person <id|slug>` removes that person, every edge incident
  to them, and every cached derived artefact mentioning them; and writes a tombstone that
  prevents re-ingest of the same key. *Test:* purge, re-run ingest on the original CSV, assert
  the person does not reappear.
- **PRIV-14** — `interlayer purge --target <entity_id>` removes all target-side records for
  that firm and any edges that become orphaned. *Test:* as above.
- **PRIV-15** — An append-only audit log at `<root>/audit.log` records one JSON line per
  collection, match, export and purge event: `{ts, action, adapter, entity_or_person_count,
  config_hash}`. It contains **no personal data** — counts and identifiers only, never names.
  *Test:* run the full pipeline; assert the log is valid JSONL, that every action type is
  present, and that no fixture person's name appears in it.
- **PRIV-16** — The audit log is append-only in practice: the writer opens with mode `"a"` and
  no code path truncates or rewrites it except `purge --all`. *Test:* assert file size is
  monotonically non-decreasing across runs.

**Export and reporting**

- **PRIV-17** — Exported reports redact email addresses and any phone numbers unconditionally,
  regardless of `retain_emails`. *Test:* enable `retain_emails`, export every format, assert
  no `@`-bearing token matching an email regex survives.
- **PRIV-18** — Every exported report carries a header block stating: purpose, the retention
  period in force, that it contains third-party personal data, and that it must not be
  redistributed. *Test:* assert the substring is present in every renderer's output.
- **PRIV-19** — Any value that is inferred rather than observed (affinity edges, inferred
  employer, inferred cluster membership) is emitted with an explicit `inferred: true` flag and
  is visually distinguished in rendered output. *Test:* build a fixture with an inferred edge;
  assert the flag survives to the rendered artefact.
- **PRIV-20** — Ambiguous company matches are never silently resolved: a record classified
  `needs_review` cannot enter the analysis graph until adjudicated, and the report states the
  count of unadjudicated items. *Test:* feed `Citadel Technology`; assert it is absent from the
  graph and counted in the report's review section.
- **PRIV-21** — The repository ships `PRIVACY.md` stating controller identity guidance,
  purposes, data categories, lawful basis (legitimate interest), retention, the Art. 14
  position, and how to exercise access and erasure — and `interlayer privacy` prints it.
  *Test:* assert the file exists, is non-empty, and that the command's output matches it.

---

## Implications for the build

### Modules

```
src/interlayer/
  targets/
    __init__.py
    registry.py        # load + validate targets.yaml
    normalise.py       # company string normalisation
    matcher.py         # the classifier
  people/
    __init__.py
    names.py           # name normalisation + blocking keys
    resolve.py         # pair scoring, merge, surrogate ids
  privacy/
    __init__.py
    retention.py       # startup sweep, purge
    audit.py           # append-only JSONL log
    redact.py          # export-time redaction
  data/
    targets.yaml       # ships verbatim from this document
```

### Signatures

```python
# targets/registry.py
@dataclass(frozen=True)
class TargetEntity:
    id: str; canonical: str; group: str
    linkedin_slug: str; linkedin_company_id: int | None
    aliases: tuple[str, ...]; review_only_aliases: tuple[str, ...]
    noise_tokens: frozenset[str]
    anchors: tuple[re.Pattern, ...]; anchor_required: tuple[str, ...]
    requires: re.Pattern | None; excludes: re.Pattern | None
    negative_patterns: tuple[re.Pattern, ...]
    threshold: int; review_floor: int

class TargetRegistry:
    @classmethod
    def load(cls, path: Path = DEFAULT_TARGETS) -> "TargetRegistry": ...
    def validate(self) -> list[str]:
        """Compile every regex; assert ids unique; assert no alias of one entity is
        matched by another entity's negative_patterns. Returns problems, empty if clean."""
    def by_id(self, entity_id: str) -> TargetEntity: ...
    def by_group(self, group: str) -> list[TargetEntity]: ...

# targets/normalise.py
def normalise_company(s: str) -> str: ...
def company_tokens(s: str) -> list[str]: ...
def company_head(s: str, noise: frozenset[str] = frozenset()) -> list[str]: ...

# targets/matcher.py
class MatchLabel(StrEnum):
    CONFIDENT = "confident"; NEEDS_REVIEW = "needs_review"; NO_MATCH = "no_match"

@dataclass(frozen=True)
class CompanyMatch:
    raw: str; normalised: str
    label: MatchLabel
    entity_id: str | None
    alternate_entity_id: str | None   # populated on ambiguous_entity
    score: float
    method: str                       # exact_alias | anchored_prefix | fuzzy | ...
    reason: str
    unexpected_tokens: tuple[str, ...]

class CompanyMatcher:
    def __init__(self, registry: TargetRegistry,
                 decisions: ReviewDecisionCache | None = None) -> None: ...
    @functools.lru_cache(maxsize=8192)
    def classify(self, raw: str) -> CompanyMatch: ...
    def classify_many(self, raws: Iterable[str]) -> list[CompanyMatch]: ...

class ReviewDecisionCache:
    """~/.interlayer/review_decisions.yaml — adjudicate each distinct string once."""
    def get(self, normalised: str) -> str | None: ...
    def record(self, normalised: str, entity_id: str | None) -> None: ...

# people/names.py
@dataclass(frozen=True)
class NameKeys:
    display: str; folded: str
    sorted_token_key: str; initials_key: str
    phonetic_key: str | None          # None when not Latin script
    is_latin: bool

def normalise_name(raw: str) -> NameKeys: ...
def blocking_keys(k: NameKeys, slug: str | None) -> set[str]: ...

# people/resolve.py
def mint_person_id(stable_key: str) -> str: ...
def score_pair(a: PersonRecord, b: PersonRecord) -> float: ...
def resolve(records: Iterable[PersonRecord]) -> tuple[list[Person], list[ReviewPair]]: ...

# privacy/
def sweep_expired(root: Path, retention_days: int = 90) -> int: ...
def purge_all(root: Path) -> None: ...
def purge_person(root: Path, key: str) -> None: ...
def purge_target(root: Path, entity_id: str) -> None: ...
def audit(root: Path, action: str, **counts: int) -> None: ...
def redact_for_export(rec: Mapping[str, Any]) -> dict[str, Any]: ...
```

### Notes for Wave 2

- `Connections.csv` has **three preamble "Notes:" lines before the header**. Read with
  `skiprows=3`, `encoding="utf-8-sig"`. Detect rather than hardcode: scan for the line
  beginning `First Name,` and skip to it, so a LinkedIn format change degrades to an error
  rather than silent column misalignment.
- `CompanyMatcher.classify` must be memoised (measured 220× speedup; the distinct-string count
  on a 30 000-row corpus was 80).
- Pin `rapidfuzz>=3.14,<4` — the 3.x `fuzz`/`process`/`utils` surface is what this spec targets.
- Ship the 100-case fixture set from this research as `tests/fixtures/company_strings.yaml`;
  it is the regression suite for every future registry edit.
- `TargetRegistry.validate()` should be called at import time in tests: the cheapest way to
  catch a future registry edit that makes an entity's own alias match its own negative pattern.

### Open items for the orchestrator

1. **Numeric LinkedIn company IDs are unresolved** (LinkedIn egress-blocked here). The registry
   ships `null` with the resolution procedure. Not blocking — slugs are sufficient — but a
   non-null ID enables exact joins on URN-emitting surfaces.
2. **Enumeration coverage is a reporting requirement, not an assumption.** No free LinkedIn
   surface enumerates 1,800–4,400 employees in one query (1,000-result cap, ~300 searches/mo).
   Whatever R4 recommends, the tool must emit a coverage figure with the partitions it ran.
3. **Bare `Citadel` carries residual entity ambiguity.** Handled via the `group: citadel`
   rollup plus a report footnote; flag if Wave 2 wants a stricter policy.

---

## Sources

Target firms: [Citadel — Who We Are](https://www.citadel.com/who-we-are/) ·
[Citadel Securities — Who We Are](https://www.citadelsecurities.com/who-we-are/) ·
[Citadel Equities Businesses](https://www.citadel.com/what-we-do/equities/) ·
[Surveyor Capital](https://www.citadel.com/what-we-do/equities/surveyor-capital/) ·
[Crain's — Griffin splits Citadel into two companies](https://www.chicagobusiness.com/article/20170203/NEWS01/170209978/chicago-billionaire-ken-griffin-splits-citadel-into-two-companies) ·
[LegalClarity — Citadel Securities vs Citadel](https://legalclarity.org/citadel-securities-vs-citadel-the-key-differences/) ·
[Wikipedia — Citadel LLC](https://en.wikipedia.org/wiki/Citadel_LLC) ·
[Wikipedia — Citadel Securities](https://en.wikipedia.org/wiki/Citadel_Securities) ·
[Wikipedia — Jane Street Capital](https://en.wikipedia.org/wiki/Jane_Street_Capital) ·
[Jane Street — Join Jane Street](https://www.janestreet.com/join-jane-street/overview/) ·
[SEC — Jane Street Capital, LLC X-17A-5 FY2024](https://www.sec.gov/Archives/edgar/data/1103083/000110308325000002/jscpublic2024.pdf) ·
[FCA Register — Jane Street Financial Limited](https://register.fca.org.uk/s/firm?id=001b000000Mg2UvAAJ) ·
[Revelio Labs — Jane Street headcount](https://www.reveliolabs.com/companies/jane-street-group/employees) ·
[Revelio Labs — Citadel Securities headcount](https://www.reveliolabs.com/companies/citadel-securities/employees) ·
[Unify — Citadel headcount](https://www.unifygtm.com/insights-headcount/citadel-llc)

LinkedIn surfaces: [Jane Street (jane-street-global)](https://www.linkedin.com/company/jane-street-global) ·
[Jane Street Capital, LLC](https://www.linkedin.com/company/jane-street-capital-llc) ·
[Jane Street Group (unrelated events company)](https://www.linkedin.com/company/janestreetgroup) ·
[Citadel (citadel-llc)](https://www.linkedin.com/company/citadel-llc) ·
[Citadel Securities](https://www.linkedin.com/company/citadel-securities) ·
[Citadel Global Equities](https://www.linkedin.com/company/citadel-global-equities) ·
[Export connections — LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a566336/export-connections-from-linkedin) ·
[Manage your public profile URL — LinkedIn Help](https://www.linkedin.com/help/linkedin/answer/a542685/manage-your-public-profile-url) ·
[TeamLink Overview — Sales Navigator Help](https://www.linkedin.com/help/sales-navigator/answer/a101027/teamlink-overview) ·
[Extending the Power of TeamLink](https://www.linkedin.com/business/sales/blog/sales-navigator/extending-the-power-of-teamlink-within-and-across-organizations) ·
[PhantomBuster — LinkedIn search limits](https://phantombuster.com/blog/social-selling/linkedin-search-limits/) ·
[Dux-Soup — LinkedIn search limits](https://www.dux-soup.com/blog/linkedin-search-limits) ·
[cufinder — finding company employees](https://cufinder.io/blog/find-company-employees-on-linkedin/) ·
[Schoeneweis — visualising LinkedIn connections (skiprows=3)](https://bradleyschoeneweis.com/posts/visualizing-your-linkedin-connections)

Libraries: [rapidfuzz on PyPI](https://pypi.org/project/rapidfuzz/) ·
[nicknames on PyPI](https://pypi.org/project/nicknames/) ·
[nameparser on PyPI](https://pypi.org/project/nameparser/) ·
[jellyfish on PyPI](https://pypi.org/project/jellyfish/) ·
[Unidecode on PyPI](https://pypi.org/project/Unidecode/) ·
[splink](https://pypi.org/project/splink/) · [dedupe](https://pypi.org/project/dedupe/)

Privacy: [GDPR Art. 14](https://gdpr-info.eu/art-14-gdpr/) ·
[GDPR Recital 18](https://gdpr-info.eu/recitals/no-18/) ·
[gdprhub — Article 2 GDPR](https://gdprhub.eu/Article_2_GDPR) ·
[EDPB Guidelines 1/2024 on legitimate interest (PDF)](https://www.edpb.europa.eu/system/files/2024-10/edpb_guidelines_202401_legitimateinterest_en.pdf) ·
[EDPB announcement, Oct 2024](https://www.edpb.europa.eu/news/news/2024/edpb-adopts-opinion-processors-guidelines-legitimate-interest-statement-draft_en) ·
[Timelex — the three-step test in practice](https://www.timelex.eu/en/blog/three-step-test-practice-edpb-guidelines-legitimate-interest-0) ·
[ICO — guide to the data protection exemptions](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/exemptions/a-guide-to-the-data-protection-exemptions/) ·
[DPC Ireland — household exemption](https://www.dataprotection.ie/en/faqs/general/what-household-exemption) ·
[California AG — CCPA](https://oag.ca.gov/privacy/ccpa) ·
[IAPP — defining "business" under CPRA](https://iapp.org/news/a/cpras-top-operational-impacts-part-2-defining-business)
