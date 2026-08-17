# R4 — Acquisition mechanics for mutual connections

**Agent:** R4 (Wave 1)
**Date:** 2026-08-17
**Scope:** how the bipartite edge set `E = {(m,t) : m ∈ M, t ∈ T}` can actually be obtained
from LinkedIn's mutual-connections surface, what each acquisition mode costs in account
and legal risk, and the literal file formats Wave 2 must implement.

> **Verification note.** `linkedin.com` and most vendor blogs are blocked by this
> session's egress policy, so LinkedIn Help pages and vendor pages could not be fetched
> directly. Claims sourced to those pages come from search-engine extraction of their
> current text and are marked **[indirect]**. Pages I fetched in full are marked
> **[fetched]**. Anything a build decision hangs on is flagged with a confidence level
> and, where it matters, a runtime check the build should perform rather than assume.

---

## Summary

1. **The mutual-connections surface is exactly the edge set we need, and it survives the
   target's privacy settings.** LinkedIn's own help text says that when a member hides
   their connections, the "See connections" link disappears from their profile *but*
   "you can view shared connections from the People tab of the search results page or
   you can click Mutual Connections in the introduction section of their profile."
   Hiding connections costs us **zero coverage**. Every mutual is already your own
   1st-degree connection, so LinkedIn treats the intersection as yours to see.

2. **Degree is a free, lossless pruning oracle.** LinkedIn's degree is shortest-path.
   A 2nd-degree target has ≥1 shared 1st-degree connection *by definition*; a 3rd-degree
   or out-of-network target has **exactly zero**. You never need to visit a 3rd-degree
   target. Degree badges are readable in bulk off a company-filtered people-search
   results list — no per-target cost. On a plausible 2,000-connection finance-adjacent
   network this removes roughly half the work (band: 30–80%).

3. **The binding constraint is LinkedIn's monthly Commercial Use Limit (CUL), not
   wall-clock time.** Free accounts get roughly 300 people-searches/month (LinkedIn does
   not publish the number; it varies by account). Automation does not raise that ceiling.
   **Therefore automation buys essentially zero coverage upside on this specific
   problem** — it converts a 3-hour job into a 20-minute job inside a quota that caps
   both at the same number of targets. That single fact settles the build's posture.

4. **Modes (d) semi-automated and (e) headless are rejected.** They are squarely inside
   LinkedIn User Agreement §8.2 ("software, devices, scripts, robots… to scrape the
   Services"), they risk the user's real professional identity — the exact asset the
   project exists to leverage — and 2026 detection reaches past `navigator.webdriver`
   into input-event physics and CDP GPU signatures. They also buy nothing (see #3).

5. **Mode (c), a browser extension, is rejected as a default and I recommend not
   shipping it at all.** LinkedIn's page-load script now enumerates ~6,200 specific
   Chrome extension IDs plus ~48 device-fingerprint attributes on every page load (up
   from a few hundred IDs in 2024). LinkedIn forced Kleo's 70,000-user extension to shut
   down (June 2025), restricted Taplio (April 2025), and in March 2026 removed
   HeyReach's company page and banned its founder's, CEO's and CMO's personal profiles.
   Extensions are the category LinkedIn is actively hunting.

6. **Mode (b), HAR capture, is the recommended primary collector.** The user browses
   normally with DevTools recording, then exports a HAR and hands the *file* to
   `interlayer`. Nothing in `interlayer` touches LinkedIn. The traffic LinkedIn sees is
   identical to mode (a); HAR export is a client-side browser feature LinkedIn cannot
   observe. It is ~2.5× faster than copy/paste and — the real argument — it yields
   **profile URNs and public identifiers instead of display names**, which join exactly
   against `Connections.csv` rather than fuzzily.

7. **Mode (a), manual transcription, is the guaranteed-available floor.** It must work
   end-to-end with nothing but a text editor, because it is the only mode that survives
   any LinkedIn UI change. Its weakness is identity: display names collide inside a
   2,000-person network, so it must be treated as lower-confidence input.

8. **No mode is literally ToS-blessed except the official data export.** §8.2 also
   prohibits copying "any information obtained from the Services… without the consent of
   LinkedIn", which on its face reaches manual note-taking. The honest framing is a
   *risk gradient*, not a legal/illegal binary. Modes (a) and (b) sit at the far low end:
   ordinary human browsing plus offline analysis of bytes already delivered to the user's
   own machine, with no automated access, no fake accounts, no third-party
   redistribution.

9. **`Meta v. Bright Data` does not help here.** Judge Chen's January 2024 ruling turned
   on *logged-off* scraping of *public* data. Mutual connections are visible only when
   authenticated, so the safe harbour does not apply. Contract terms bind this activity.
   Enforcement precedent is one-directional: hiQ (Dec 2022 consent judgment, $500k,
   permanent injunction, destroy data), Mantheos (2022 settlement, delete data and
   destroy software), Proxycurl (sued Jan 2025 over fake accounts, shut down 4 July 2025,
   permanent injunction plus customer notification).

10. **Sales Navigator is a quota upgrade, not a capability upgrade.** Its "Connections
    of" filter requires the named person to be *your* 1st-degree connection, one at a
    time — useless for a 2nd-degree target. It has no native CSV/XLS export. What it does
    do is remove the CUL and raise the per-search cap to 2,500, letting a user finish 300
    targets in one session on linkedin.com for one month's subscription.

11. **URL parameter names have churned and must not be hard-coded.** Historical
    `facetConnectionOf` / `facetNetwork` became `connectionOf` / `network`; the underlying
    Voyager GraphQL `queryId` hash rotates. The build must parse **whatever the browser
    already produced**, treating ids as opaque and using shape-tolerant traversal with a
    recursive fallback, never a fixed JSON path.

12. **The schema's most important job is distinguishing four states**: list captured
    complete, captured but truncated, observed-and-genuinely-empty, and never-observed.
    Plus a fifth, `skipped_by_degree`, which is an *inference* (sound, but an inference)
    and must be labelled as one so coverage numbers stay honest.

---

## Findings

### A. The mutual-connections surface

#### A.1 Where it appears

Three surfaces expose `M ∩ connections(t)`:

| # | Surface | Description |
|---|---------|-------------|
| 1 | **Profile intro / highlights card** | "N mutual connections", typically naming one or two inline ("Alex Rivera, Dana Wu, and 5 other mutual connections"). Clicking navigates to surface 2. **[indirect]** |
| 2 | **People-search results with the "Connections of" facet** | A normal people-search results page, 10 results/page, filtered to people who are connections of `t` **and** in your 1st-degree network. This is the enumerable surface. |
| 3 | **Sales Navigator lead page → Relationship section** | "Shared Connections". Display-only; no bulk enumeration, no export. **[indirect]** |

Surface 2 is the one that matters. Note that it is *literally a people search* — which
is why it consumes Commercial Use Limit quota (§A.4) and why it is bounded by the
1,000-result search ceiling (irrelevant in practice: mutual counts are tens, not
thousands).

#### A.2 URL patterns — what works today

Two generations of parameter naming are in the wild. LinkedIn dropped the `facet` prefix
around 2019–2020 and switched to URL-encoded JSON arrays.

**Legacy (≈2017–2020), still cited widely in stale documentation:**

```
https://www.linkedin.com/search/results/people/
  ?facetNetwork=["F"]
  &facetConnectionOf=["ACoAABnLzFQB3R58FQw3pHcdg_mUrAwtlu4kVUE"]
  &origin=MEMBER_PROFILE_CANNED_SEARCH
  &page=2
```

Confirmed verbatim in `linkedtales/scrapedin` issue #120 (opened 28 June 2020), which
also documents `facetNetwork` values `F`/`S`/`T` and "up to ~100 pages, 10 results per
page". **[fetched]** — https://github.com/linkedtales/scrapedin/issues/120

**Current form (facet prefix dropped):**

```
https://www.linkedin.com/search/results/people/
  ?connectionOf=%5B%22<opaque-id>%22%5D          # ["<opaque-id>"]
  &network=%5B%22F%22%5D                          # ["F"]
  &origin=MEMBER_PROFILE_CANNED_SEARCH
```

`network` values are `F` (1st), `S` (2nd), `O` (3rd+/out-of-network) — the same
three-value vocabulary the `linkedin-api` Python wrapper documents as
`network_depth ∈ {F, S, O}`. **[indirect]** —
https://github.com/nsandman/linkedin-api/blob/master/DOCS.md

Other `origin` values observed: `SHARED_CONNECTIONS_CANNED_SEARCH`, `FACETED_SEARCH`,
`GLOBAL_SEARCH_HEADER`. `origin` is telemetry, not access control.

The `connectionOf` value is **opaque**: historically a member URN fragment
(`ACoAAB...`), and public-identifier forms have also been reported. **The build must
never construct or interpret it — treat it as an opaque key and carry it through.**

> **Coverage note on `connectionOf` without `network=["F"]`.** A security researcher
> reported (article dated 26 January 2026) that supplying `connectionOf` alone returns
> a target's connections beyond the mutual subset, including for members who have set
> their connections to private, and that there is no UI affordance to do this. LinkedIn
> closed the HackerOne report as **Informative** — i.e. working as intended, not a
> vulnerability. **[indirect]** —
> https://adityatelange.in/blog/linkedin-list-network-without-connecting/
>
> `interlayer` does not need this and **should not use it**. Adding `network=["F"]`
> restricts the result to *your own 1st-degree connections*, which is exactly `M ∩
> connections(t)` and exactly what the product UI itself shows you. Staying inside the
> `network=["F"]` restriction is the difference between reading a surface LinkedIn
> intentionally renders for you and harvesting a member's private connection list.
> Wave 2 should treat an unrestricted `connectionOf` capture as a **validation error**,
> not a bonus (see rule V13).

#### A.3 Visibility by degree

| Target `t` | Profile viewable | Mutuals surface renders | Expected `|M ∩ conn(t)|` | Notes |
|---|---|---|---|---|
| **1st degree** (`t ∈ M`) | Full | Yes | ≥ 0 | `t` is both target and connection; handle as an edge case, don't let `t` appear in its own mutual list. |
| **2nd degree** | Full (public + shared fields) | Yes | **≥ 1, guaranteed** | This is the definition of 2nd degree. |
| **3rd degree** | Limited — typically name, headline, current position | No / count 0 | **0, by definition** | Shortest path = 3 ⇒ no shared 1st-degree neighbour. |
| **Out of network ("3rd+")** | May be name-only or unviewable | No | **0** | Not connected via 1st/2nd/3rd and no shared group. |
| **Target hid connections** | Full | **Yes — unaffected** | Unchanged | See A.5. |

**Does the mutual list still render for 3rd degree? No — and it cannot, because there is
nothing to render.** Some third-party guides muddy this by saying you "might see mutual
connections" on a 3rd-degree profile; that conflates shared *connections* with shared
groups/schools or reflects LinkedIn's degree-recomputation lag. The graph-theoretic
statement is exact and is what the build should rely on, with a small lag allowance
(§F.4).

Sources: LinkedIn Help, "Your Network and Degrees of Connection" **[indirect]** —
https://www.linkedin.com/help/linkedin/answer/a545636/your-network-and-degrees-of-connection

#### A.4 Result caps and the Commercial Use Limit

**Per-search result ceiling**

| Account | Page size | Max pages | Max results/search |
|---|---|---|---|
| Free / Premium | 10 | 100 | **1,000** |
| Sales Navigator | 25 | 100 | **2,500** |

**[indirect]** — https://evaboot.com/blog/hack-to-bypass-linkedin-search-limit ·
https://www.dux-soup.com/blog/linkedin-search-limits

This ceiling is **irrelevant for mutual-connection capture** (a target with >1,000
mutuals inside a 2,000-person network is impossible) but **decisive for target
enumeration**: a company-filtered people search for Jane Street or Citadel will exceed
1,000 hits and must be sliced by title/location/school facets. That is R5's problem, but
R4's schema must carry the resulting `truncated` flag.

**Commercial Use Limit (CUL)**

- **What it is:** a rolling monthly cap on "profile searching" for non-commercial-tier
  accounts. LinkedIn deliberately does **not** publish the number and states it varies.
  **[indirect]** — https://www.linkedin.com/help/linkedin/answer/a564226
- **Observed magnitude:** third-party estimates cluster around **~300 people-searches per
  month** on a free account; individual reports range ~100–1,000 depending on account age
  and activity. Treat as a *budget to be measured*, not a constant. **[indirect]** —
  https://phantombuster.com/blog/social-selling/linkedin-commercial-use-limit/
- **What counts:** people searches; viewing 2nd/3rd-degree profiles; "People Also
  Viewed"; the People tab of a company page; filtered searches.
- **What does not count:** searching a person **by name** in the top search box;
  browsing your own 1st-degree connections.
- **Reset:** midnight PT on the 1st of each calendar month.
- **Behaviour when hit:** search results truncate to ~3 rows and profile browsing beyond
  1st degree is blocked until reset.
- **Paid tiers:** Sales Navigator removes the CUL for people search; Premium Business and
  Recruiter Lite raise or remove it (LinkedIn's help page is the authority on the current
  exempt list — the build should not hard-code entitlements).

**Build consequence.** Budget **1–2 CUL units per target** (1 for the mutuals search;
2 if the user also loads the profile page). A free account therefore covers roughly
**150–250 targets per calendar month**. This is the number that determines project
timeline — not throughput.

#### A.5 Does the target's privacy setting suppress mutuals? **No.**

This is the single highest-value finding for coverage, so it is worth stating precisely.

LinkedIn's "Who can see your connections" setting has two values: **Your connections**
and **Only you**. LinkedIn Help's own text on the effect:

> "Your connection may choose to hide connections they don't want to share. In this case,
> there won't be a 'See connections' option on the member's profile. **However, you can
> view shared connections from the People tab of the search results page or you can click
> Mutual Connections in the introduction section of their profile.**"

**[indirect]** —
https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections

And on the setting itself: choosing "Only you" means "no one else on LinkedIn will be
able to see your full list of connections", but "**mutual connections will always be
visible to people who share them with you**". **[indirect]** —
https://www.linkedin.com/help/linkedin/answer/a540663/who-can-see-your-connections

**Precise statement:** the setting governs the target's *full connection list*. It has
**no effect on the mutual subset**. The mechanism is obvious once stated — every member
of `M ∩ connections(t)` is already your own 1st-degree connection, whose profile you are
independently entitled to see. LinkedIn is not disclosing anything new about `t` beyond
the fact of the edge.

**Coverage impact: none.** Do not model target-side privacy as a coverage loss. The real
coverage losses are: (i) the target blocked you or you blocked them; (ii) a mutual `m`
has restricted profile discovery or blocked you; (iii) CUL exhaustion; (iv) degree-badge
staleness.

*Confidence: high on the direction (LinkedIn's own help text is unambiguous and
consistent across two pages); medium on there being zero edge cases, since LinkedIn ships
regional and account-type variations. Wave 2 should therefore not assert coverage
completeness — it should report `observed / eligible` and let the user see the gap.*

---

### B. The Voyager JSON underneath (relevant only to mode (b))

The rendered mutual-connections page is driven by LinkedIn's internal "Voyager" API.
The current people-search request has this shape **[fetched]** —
https://github.com/dchrastil/ScrapedIn/blob/master/ScrapedIn.py:

```
https://www.linkedin.com/voyager/api/graphql
  ?variables=(start:0,origin:FACETED_SEARCH,
              query:(flagshipSearchIntent:SEARCH_SRP,
                     queryParameters:List((key:resultType,value:List(PEOPLE)),
                                          (key:connectionOf,value:List(<id>)),
                                          (key:network,value:List(F))),
                     includeFiltersInResponse:false))
  &queryId=voyagerSearchDashClusters.994bf4e7d2173b92ccdb5935710c3c5d
```

Headers the browser sends: `csrf-token: ajax:<n>`, `x-restli-protocol-version: 2.0.0`,
cookies `li_at` and `JSESSIONID`.

Response shape:
- Primary path: `data.searchDashClustersByAll.elements[].items[]`
- A sibling normalized `included[]` array of entities keyed by `$type` (Rest.li
  convention; the same document notes LinkedIn "usually responds with paging data in
  every response"). **[fetched]** —
  https://github.com/joshuatz/linkedin-to-jsonresume/blob/main/docs/LinkedIn-Dev-Notes-README.md
- Paging: `count` / `start` query params; response `paging: {count, start, total}`
- Per-entity fields of interest: `entityUrn` / `trackingUrn`
  (`urn:li:fsd_profile:ACoAAA…`), `navigationUrl` (`…/in/<publicIdentifier>`),
  `title.text` (name), `primarySubtitle.text` (headline),
  `entityCustomTrackingInfo.memberDistance` (`DISTANCE_1|DISTANCE_2|DISTANCE_3|OUT_OF_NETWORK`).

**Is it parseable from a HAR? Yes** — it is plain JSON in the response body, and the
`memberDistance` field is a bonus: it lets the parser cross-check degree without a
separate lookup.

**Two stability warnings that must shape the parser:**

1. `queryId` embeds a rotating hash of the registered GraphQL query. Any code that
   *constructs* a request will break on LinkedIn's next deploy. Code that *reads what the
   browser already fetched* is immune. This is an engineering argument for mode (b) over
   modes (c)–(e), independent of compliance.
2. `decorationId` schema names and response field paths change without notice — the
   `linkedin-to-jsonresume` notes call the decoration namespace "extremely
   un-documented". The parser needs a fixed-path fast path **plus** a recursive
   shape-matching fallback (§E.3).

**HAR mechanics.** A HAR is a JSON document with `log.entries[]`; each entry has
`startedDateTime`, `request.url`, `response.status`, and
`response.content.{mimeType, text, encoding}`.

- Chrome ≥ v130 **sanitizes by default**, stripping `Cookie`, `Set-Cookie` and
  `Authorization` headers. An "Export HAR (with sensitive data)" option exists behind
  Settings → Preferences → Network. **[indirect]** —
  https://developer.chrome.com/docs/devtools/network/reference ·
  https://michev.info/blog/post/6693/how-to-export-unsanitized-har-files-with-chrome-and-edge
  **This default is good for us**: `interlayer` wants none of those headers, and the
  documented procedure should tell users to keep sanitization on.
- **Response bodies are the one thing to verify at runtime.** Reports conflict on whether
  Chrome always populates `response.content.text` for XHR. The build must therefore
  *check* rather than assume, and emit an actionable diagnostic ("your HAR contains N
  matching Voyager requests but 0 response bodies — re-export with 'Preserve log'
  enabled, or use the Copy-Response fallback"). A **`.voyager.jsonl` fallback** — user
  right-clicks the request → *Copy response* → pastes into a file, one JSON per line —
  removes the ambiguity entirely and costs nothing to support.

---

### C. Legal and ToS posture

#### C.1 What the User Agreement actually says

§8.2 "Don'ts" prohibits, among other things **[indirect]** —
https://www.linkedin.com/legal/user-agreement:

- "Develop, support or use software, devices, scripts, robots or any other means or
  processes (**including crawlers, browser plugins and add-ons** or any other technology)
  to scrape the Services or otherwise copy profiles and other data from the Services";
- "Use bots or other automated methods to access the Services, add or download contacts,
  send or redirect messages";
- "**Copy, use, disclose or distribute any information obtained from the Services**,
  whether directly or through third parties (such as search engines), without the consent
  of LinkedIn".

LinkedIn's "Prohibited software and extensions" help page restates this for extensions
specifically: third-party software including "crawlers", bots, browser plug-ins or
extensions "that scrape, modify the appearance of, or automate activity on LinkedIn's
website" is not permitted; members using them "risk having their accounts restricted or
shut down". **[indirect]** —
https://www.linkedin.com/help/linkedin/answer/a1341387

**Be honest about the third clause.** It reaches manual copying. There is no reading of
§8.2 under which transcribing a mutual-connections list into a spreadsheet is expressly
permitted. What there *is*: a very large gap between that clause — universally
un-enforced against individual members doing ordinary research on their own network —
and automated collection, which LinkedIn litigates. The build should say this plainly in
its docs rather than claim a compliance blessing it doesn't have.

#### C.2 Case law

| Case | Outcome | Relevance |
|---|---|---|
| **hiQ Labs v. LinkedIn** (N.D. Cal.) | Consent judgment + **permanent injunction**, 8 Dec 2022. $500k against hiQ on breach of contract, CFAA (fake accounts), Cal. §502, trespass to chattels, misappropriation, plus spoliation sanctions. hiQ must cease all scraping and destroy source code, data and derived algorithms. | The "scraping public data is legal" headline was about a *preliminary injunction*, and it ended with hiQ enjoined and paying. Cite the ending, not the middle. |
| **LinkedIn v. Mantheos** (filed 1 Feb 2022) | Settled; consent judgment requires deleting all scraped profile data, destroying the scraping software, and ceasing automated access. Involved hundreds of fake accounts and prepaid cards to obtain Sales Navigator. | Shows LinkedIn pursues mid-size foreign operators to judgment. |
| **LinkedIn v. Proxycurl / Steven Goh** (filed Jan 2025) | Proxycurl shut down **4 July 2025** at ~$10M ARR. Permanent injunction: delete all LinkedIn data, cease unauthorized access, **notify its customers** of the requirements. | The most recent and most direct precedent for a data vendor built on this exact surface. |
| **Meta Platforms v. Bright Data** (N.D. Cal., Judge Chen, 23 Jan 2024) | Summary judgment for Bright Data: Meta's terms govern "your use", and Bright Data "did not 'use' Facebook and Instagram when it engaged in public **logged-off** scraping". No evidence of logged-in scraping in the record. | **Cuts against us.** The safe harbour is explicitly for logged-off, publicly-available data. Mutual connections exist only behind authentication. This project is on the wrong side of the distinction, and should not lean on Bright Data. |

Sources: https://www.proskauer.com/blog/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case ·
https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/ ·
https://news.bloomberglaw.com/privacy-and-data-security/linkedin-settles-data-scraping-lawsuit-against-mantheos ·
https://news.linkedin.com/2022/february/taking-legal-action-to-protect-members-against-scraping ·
https://nubela.co/blog/goodbye-proxycurl/ ·
https://www.socialmediatoday.com/news/linkedin-wins-legal-case-data-scrapers-proxycurl/756101/ ·
https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/ ·
https://blog.ericgoldman.org/archives/2024/01/game-on-bright-data-scores-major-victory-in-web-scraping-dispute-with-meta-guest-blog-post.htm

**Pattern across all four:** LinkedIn's litigation targets *redistribution at commercial
scale*, *fake accounts*, and *automated access*. A single individual analysing their own
network locally, with no resale and no automation, is not in the shape of any of these
cases. That is a real distinction — and it is a distinction about **enforcement risk**,
not about §8.2 compliance.

#### C.3 Data-protection obligations (deferred to R5, flagged here)

Targets are third-party data subjects. R4's schema contributions to that: local-only
storage, a `captured_at` on every record enabling age-based expiry, a per-target delete
path keyed on `target.id`, and minimisation — the schema deliberately carries **no**
email, phone, or free-text profile content.

---

### D. Enforcement ladder and what triggers it

The observed escalation, consistent across vendor incident write-ups **[indirect]**:

```
0. Silent risk scoring          — every action scored; nothing visible
1. Soft friction                — CAPTCHA / "unusual activity detected" / SMS or
                                  phone verification; posts down-ranked; messages
                                  routed to "Other"
2. Feature throttle             — search results truncated, connection requests
                                  blocked, "you've reached the limit" walls
3. Temporary restriction        — account locked, typically 3–7 days first offence,
                                  up to ~2 weeks on repeat; ID verification demanded
4. Permanent restriction / ban  — repeat offenders and aggressive tooling; the most
                                  common path is pushing through step 3 at the same
                                  volume
```

Sources: https://www.linkedhelper.com/blog/linkedin-account-restricted ·
https://blog.closelyhq.com/linkedin-detect-automation-stay-under-radar/ ·
https://www.linkedhelper.com/blog/linkedin-automation-limits

**Triggers, roughly in order of weight:**

1. **Architecture.** The 2026 enforcement wave is described as *architecture-specific*:
   cloud-proxy vendors driving sessions from servers were targeted regardless of whether
   individual users stayed inside daily limits. HeyReach (March 2026) is the exemplar —
   company page removed, founder/CEO/CMO profiles banned; one vendor analysis estimated
   ~40% of accounts on non-compliant tools picked up some restriction in Q1 2026.
   **[indirect]** — https://northlight.ai/blog/northlight-vs-heyreach ·
   https://www.joinvalley.co/blog/linkedin-automation-safety-2026 ·
   https://linkedinsider.blog/linkedin-automation-crackdown-2026
2. **Client-side extension fingerprint.** LinkedIn's page-load script probes for
   **6,222–6,236 specific Chrome extension IDs** plus ~48 hardware/software attributes
   (CPU, memory, screen, battery, timezone). The list grew from a few hundred entries in
   2024 to 6,000+ by early 2026, including 200+ products competing with LinkedIn's own
   sales tools. This is now the subject of a putative class action (N.D. Cal., filed
   6 April 2026, ECPA / CIPA / Cal. §502 claims). **[indirect]** —
   https://www.bleepingcomputer.com/news/security/linkedin-secretly-scans-for-6-000-plus-chrome-extensions-collects-data/ ·
   https://ppc.land/linkedin-hit-with-class-action-over-hidden-browser-scan-of-6-000-extensions/ ·
   https://cyberinsider.com/linkedin-faces-class-action-over-alleged-covert-scanning-of-users-browsers/
3. **Velocity and rhythm.** An aggregate ceiling around ~150 actions/24h is widely cited;
   evenly-spaced timing, 24/7 activity, and zero idle gaps are stronger signals than raw
   volume.
4. **Automation-runtime signals.** In 2026, `navigator.webdriver` and Selenium's `cdc_`
   markers are the trivial layer. What still works: TLS/HTTP-2 transport fingerprints,
   GPU rendering signature under CDP control, and **input-event physics** — mouse
   trajectory entropy, scroll dynamics, keystroke timing — which no automation library
   reproduces reliably at scale. **[indirect]** —
   https://cside.com/blog/headless-browser-detection ·
   https://scrapfly.io/blog/posts/playwright-stealth-bypass-bot-detection
5. **Infrastructure novelty.** Fresh accounts, datacentre IPs, sudden geography changes.

**What is *not* on this list: reading a file on your own laptop.** LinkedIn has no
observability into HAR export or offline parsing. Mode (b)'s account risk equals mode
(a)'s exactly, because the traffic is identical.

---

### E. Reference points — how existing tools position

| Category | Examples | Positioning | Enforcement history |
|---|---|---|---|
| **Official first-party** | Data Export (`Connections.csv`), Marketing/Community APIs, Sales Nav CRM Sync (Advanced Plus) | Sanctioned | None. Zero risk. |
| **Runs in your browser** (extension, your session) | LeadDelta, Evaboot, Dux-Soup (extension by default), Kanbox, Kleo | "Acts as you", "no cloud", "mimics human browsing", lower per-action detectability | Explicitly named by LinkedIn's prohibited-extensions page. **Kleo**: C&D, free extension killed June 2025 at 70k users, relaunched Oct 2025 as a $99/mo web app. **Taplio**: company page restricted April 2025. Individual devs have received C&Ds (HN, Feb 2023). Plus the 6,000-ID extension scan. |
| **Desktop app driving your browser** | Linked Helper, Dux-Soup Cloud | "Local, human-paced" | Same §8.2 clause; risk scales with velocity. |
| **Cloud / proxy session** | PhantomBuster, HeyReach, Expandi, Dripify, Waalaxy, Lobstr | "Always on", "no laptop needed" — user uploads their session cookie | Heaviest. HeyReach March 2026: company page removed, founder/CEO/CMO banned; repositioned to email within weeks. |
| **Cloud scraper / data API** | Apify actors (including a *LinkedIn Shared Connections* actor), Proxycurl (defunct), Mantheos (defunct), hiQ (defunct) | "Data as a service", often with fake-account infrastructure | Litigated to judgment. Three of the named firms no longer exist. |
| **HAR / offline capture** | Stevesie ("legally download to CSV" from a HAR you exported), ad-hoc DevTools workflows | "You captured it in your own browser; we only parse the file" | **No enforcement precedent found in this category** — there is nothing server-side for LinkedIn to observe. |

Sources: https://autoposting.ai/blog/kleo-alternatives · https://magicpost.in/blog/kleo-review ·
https://news.ycombinator.com/item?id=34583932 · https://www.vayne.io/en/blog/best-linkedin-scrapers-2026 ·
https://derrick-app.com/linkedin/scraper/chrome-extension · https://stevesie.com/apps/linkedin-api ·
https://apify.com/data_link_miner/linkedin-shared-connections

**The pattern that should drive the build:** enforcement severity tracks *where the code
runs*, not what data is collected. Code on LinkedIn's pages or against LinkedIn's servers
is the trigger. `interlayer` should therefore place **all** of its code strictly offline,
on the user's disk, on the far side of a file boundary — and make that boundary an
architectural invariant, not a policy.

### E.1 Sales Navigator, specifically

| Feature | Can it enumerate `M ∩ conn(t)` in bulk? | Notes |
|---|---|---|
| **"Connections of" filter** (lead search) | **No** | Accepts **one** person at a time, and that person **must be your 1st-degree connection**. Useless for a 2nd-degree Jane Street employee. **[indirect]** — https://skylead.io/blog/linkedin-sales-navigator-filters/ |
| **Relationship → Shared Connections** (lead page) | Display only | Same information as flagship, no export. |
| **TeamLink / TeamLink Extend** (Advanced, Advanced Plus) | Pools *teammates'* networks | Requires a paid team; irrelevant to a single-user local tool. **[indirect]** — https://derrick-app.com/linkedin/sales-navigator/teamlink-filter |
| **CSV / XLS export** | **Not available** | LinkedIn's help documentation states CSV/XLS export is not available for Sales Navigator data; CRM Sync is Advanced Plus only. **[indirect]** — https://www.cleanlist.ai/blog/2026-04-25-how-to-export-linkedin-sales-navigator-data |
| **Search quota** | **Removes the CUL**, 2,500 results/search | The actual reason to buy it. |

**Verdict:** Sales Navigator adds **quota**, not **capability**. If a user buys one month
(~$99–$159), the recommended play is to **do the capture on flagship linkedin.com**
anyway — the CUL is an account-level entitlement, so the flagship mutual-connections
surface becomes unlimited too, and the flagship Voyager response shape is the one the
parser already handles.

---

## Acquisition mode matrix

Throughput assumes the pruned workload from §F: 141 targets to visit out of 300.

| Mode | Mechanics | Throughput | Coverage | Account risk | Legal risk | Effort | Verdict |
|---|---|---|---|---|---|---|---|
| **(0) Official data export** | `Settings → Data privacy → Get a copy of your data` → `Connections.csv` | One request, 10 min – 24 h | Gives `M` only — **no edges** | **None** | **None** | Trivial (CSV reader) | **Ship. Mandatory backbone.** |
| **(a) Manual, human-paced** | User opens the mutual-connections list, types/pastes names into a CSV | 60–90 s/target → **2.4–3.5 h** | ~100% of pruned set, CUL-bound | **Negligible** — indistinguishable from normal use | Low. §8.2(4) technically reaches manual copying; no enforcement precedent against individuals | Low — CSV reader + validator | **Ship as the guaranteed floor.** |
| **(b) HAR / DevTools capture** | User browses with DevTools recording, exports `.har`, hands the file to `interlayer`; all parsing offline | 20–30 s/target → **47–70 min** + one export | ~100% of pruned set, CUL-bound. **Best identity fidelity: URN + publicId** | **Negligible** — traffic identical to (a); HAR export is client-side and unobservable to LinkedIn | Low — same as (a); no automated access, no third-party redistribution | Medium — streaming HAR reader + shape-tolerant Voyager parser | **Ship as the recommended primary.** |
| **(c) Browser extension (user-driven DOM read)** | MV3 extension reads the DOM of pages the user actually visits; no automated navigation | 10–15 s/target → **24–35 min** | ~100% of pruned set, CUL-bound | **Material.** LinkedIn enumerates ~6,200 extension IDs + ~48 fingerprint attributes on every page load; explicitly prohibited by name | Medium — squarely inside §8.2's "browser plugins and add-ons" clause | High — MV3 build, store review or dev-mode sideloading, ongoing DOM-churn maintenance | **Do not ship.** Saves ~30 min over (b) for a categorically worse risk profile. |
| **(d) Semi-automated, user's own session** | Playwright/Selenium drives a real logged-in profile at human-like pace | With honest pacing (30–60 s + jitter): **1.2–2.4 h** — *no faster than (a)* | Same until the account is restricted, then 0 | **High.** 2026 detection reads input-event physics and CDP GPU signatures; the asset at risk is the user's real professional identity | High — the paradigm §8.2 violation | High — driver, stealth, pacing, session handling, breakage | **Reject.** Not built, not documented. |
| **(e) Fully automated headless at scale** | Headless fleet, proxies, possibly multiple accounts | 2–5 s/target → minutes | Collapses at the CUL wall regardless; fake accounts to evade it move the conduct into CFAA territory | **Severe** — permanent ban | **Severe** — the exact fact pattern in hiQ, Mantheos, Proxycurl | High | **Reject unconditionally.** |

**The decisive column is not throughput — it is the interaction of *Coverage* with the
CUL.** Every mode from (a) to (e) delivers the same ~150–250 targets/month on a free
account, because the ceiling is a server-side monthly quota. Modes (c)–(e) trade a
material escalation in account and legal risk for **30 minutes to 2 hours of the user's
wall-clock time, once**. That is a bad trade at any risk tolerance, and it is why this
build's compliant default is not a compromise — it is the correct engineering answer.

---

## Recommendation

### Ship by default

1. **`ConnectionsCsvCollector`** — posture `FIRST_PARTY`. Consumes LinkedIn's official
   data-export `Connections.csv` (7 columns: `First Name, Last Name, URL, Email Address,
   Company, Position, Connected On`, behind a 2–4 line "Notes:" preamble that the reader
   must skip by detecting the header row rather than a fixed `skiprows`). This is `M`.
   Zero risk, first-party, mandatory.
2. **`HarCollector`** — posture `PASSIVE_CAPTURE`. Parses `.har` files the user exported
   from their own browser. **Recommended primary edge collector.**
3. **`VoyagerJsonlCollector`** — posture `PASSIVE_CAPTURE`. Parses `.voyager.jsonl`
   (one raw Voyager response per line), the *Copy response* fallback for when a HAR
   arrives without response bodies.
4. **`ManualCsvCollector`** — posture `MANUAL`. Parses the hand-fillable `mutuals.csv`.
   The guaranteed floor; must work with nothing but a text editor.
5. **`MutualsJsonlCollector`** — posture read from each record. The canonical interchange
   format; also the output format of every other collector, so the pipeline has one
   internal shape.
6. **`DegreePruneCollector`** — posture `INFERENCE`. Emits `skipped_by_degree` records
   for 3rd-degree/out-of-network targets so coverage accounting is complete. Every record
   it produces is labelled as inference, per the handoff plan's constraint 4.

### Document as a manual procedure the user performs (no code)

`docs/procedures/capture-mutuals.md`, written for a human, containing:

- The degree-pruning step first: run one company-filtered people search per firm slice,
  record each target's degree badge, and **only visit 1st and 2nd degree**.
- The per-target step: **open the target's profile and click "N mutual connections"**.
  This is the default instruction because it is unambiguously an intended UI action.
- The URL-construction alternative, with its caveats stated in full: it saves one CUL
  unit and one page load per target, and it does **not** register a profile view on the
  target (a genuine privacy benefit to the target), **but** the UI's own "Connections of"
  picker only offers your 1st-degree connections, so hand-building the URL for a
  2nd-degree target does something the UI does not offer. `network=["F"]` is
  **mandatory** if the user takes this route.
- The HAR export recipe: DevTools → Network → **Preserve log on**, filter `voyager`,
  browse, then Export HAR. **Keep sanitization on** (Chrome ≥130 default) — `interlayer`
  wants no cookies and will reject files containing them.
- Pacing guidance framed as courtesy and quota management, not evasion: work in sessions,
  stop at the first CAPTCHA or "unusual activity" prompt, do not push through a
  restriction.
- The CUL budget: ~150–250 targets/month free, unlimited on Sales Navigator; the one-month
  Sales Navigator option written up as a legitimate way to finish in a single session.

### Explicitly out of scope

**The default build ships no automated scraper.** There is no Playwright dependency, no
Selenium dependency, no HTTP client that targets `linkedin.com`, no browser extension, and
no stored LinkedIn credential or session cookie anywhere in the codebase or its config.
`interlayer` never opens a network connection to LinkedIn — not by policy, but because no
such code path exists.

This must be enforced, not just asserted:

- **Test `test_no_linkedin_network_egress`** — grep the whole of `src/` for
  `linkedin.com`, `voyager`, `li_at`, `JSESSIONID`, `playwright`, `selenium`,
  `webdriver`, `puppeteer`, and assert zero matches outside `docs/`, test fixtures, and
  string constants used purely for *rejection* (rule V8).
- **Test `test_default_posture_allowlist`** — assert
  `DEFAULT_ALLOWED_POSTURES == {FIRST_PARTY, PASSIVE_CAPTURE, MANUAL, INFERENCE}` and
  that loading a record with posture `ACTIVE_BROWSER` or `AUTOMATED` raises
  `CompliancePostureError`.
- **Test `test_no_network_in_pipeline`** — run the full pipeline on fixtures with
  `socket.socket` monkeypatched to raise.

The `ACTIVE_BROWSER` and `AUTOMATED` enum members exist **only** so the guardrail is
testable and so a third party cannot slip a collector past it by inventing a new posture
string. No collector in the repo declares them.

---

## Proposed input schemas

All paths are relative to the project data root. All timestamps are RFC 3339 UTC with a
`Z` suffix. All files are UTF-8.

### 1. `data/input/connections.csv` — the set `M` (first-party)

LinkedIn's export, **unmodified**. The reader must:
- locate the header row by scanning for a line beginning `First Name,Last Name,URL`
  (do not hard-code `skiprows`; the preamble length has varied between 2 and 4 lines);
- tolerate a UTF-8 BOM;
- tolerate an entirely empty `Email Address` column (typical — the export only includes
  emails for connections who opted in, so 80–90% blank is normal, not an error);
- derive the join key from `URL` (§V2), never from the name.

```csv
First Name,Last Name,URL,Email Address,Company,Position,Connected On
Alex,Rivera,https://www.linkedin.com/in/alexrivera,,Acme Corp,Software Engineer,14 Mar 2021
Dana,Wu,https://www.linkedin.com/in/danawu-7f3a2,dana@example.com,Globex,Quant Researcher,02 Nov 2019
```

### 2. `data/input/targets.csv` — the set `T` with degree (drives pruning)

Hand-fillable. `degree` is the load-bearing column: it decides whether a target is
visited at all.

```csv
target_id,name,profile_url,firm,degree,degree_observed_at,enumeration_source,enumeration_truncated,notes
```

| Column | Required | Type | Notes |
|---|---|---|---|
| `target_id` | yes | string | Stable local key. Use the LinkedIn public identifier when known, else a slug. Must be unique. |
| `name` | yes | string | Display name as shown. |
| `profile_url` | no | url | `https://www.linkedin.com/in/<publicId>/`. Strongly recommended. |
| `firm` | yes | enum | `jane_street` \| `citadel` \| `citadel_securities` \| `other` (R5 owns the vocabulary). |
| `degree` | yes | enum | `1` \| `2` \| `3` \| `out` \| `unknown` |
| `degree_observed_at` | yes | rfc3339 | When the badge was read. Degree decays; §V10 warns on staleness. |
| `enumeration_source` | no | string | e.g. `people_search:jane_street:title=engineer`. Provenance for the roster itself. |
| `enumeration_truncated` | no | bool | `true` if the search that produced this target hit the 1,000/2,500 cap — tells the user the roster is incomplete. |
| `notes` | no | string | Free text. |

```csv
target_id,name,profile_url,firm,degree,degree_observed_at,enumeration_source,enumeration_truncated,notes
jsmith-quant-9a1,Jordan Smith,https://www.linkedin.com/in/jsmith-quant-9a1/,jane_street,2,2026-08-17T09:12:00Z,people_search:jane_street:london,false,
p-okafor-2c8,Priya Okafor,https://www.linkedin.com/in/p-okafor-2c8/,citadel_securities,3,2026-08-17T09:14:00Z,people_search:citadel_securities:nyc,true,pruned - 3rd degree
```

### 3. `data/input/mutuals.csv` — hand-fillable edge list (mode (a))

Long/tidy format: **one row per (target, mutual) edge**. A target with zero mutuals gets
**one** row with `mutual_name` empty and `capture_status=empty` — this is how the schema
distinguishes "observed, none" from "not yet looked at".

```csv
target_id,mutual_name,mutual_profile_url,mutual_headline,capture_status,reported_count,captured_at,source_url,collector,notes
```

| Column | Required | Type | Notes |
|---|---|---|---|
| `target_id` | yes | string | FK into `targets.csv`. |
| `mutual_name` | conditional | string | Required unless `capture_status ∈ {empty, unavailable, skipped_by_degree}`. |
| `mutual_profile_url` | no | url | **Strongly** recommended — without it, matching into `M` is fuzzy (§V6). |
| `mutual_headline` | no | string | Free text, aids disambiguation. |
| `capture_status` | yes | enum | `complete` \| `truncated` \| `empty` \| `unavailable` \| `skipped_by_degree` |
| `reported_count` | no | int | The "N" from "N mutual connections". Drives truncation detection (§V5). |
| `captured_at` | yes | rfc3339 | |
| `source_url` | no | url | The page the user was looking at. |
| `collector` | yes | string | `manual` for this file. |
| `notes` | no | string | |

Target-level columns (`capture_status`, `reported_count`, `captured_at`, `source_url`,
`collector`) repeat on every row of a target. The loader takes the **first non-empty**
value per `target_id` and emits a warning on conflict rather than failing — hand-edited
files will be inconsistent and the tool must not punish that.

```csv
target_id,mutual_name,mutual_profile_url,mutual_headline,capture_status,reported_count,captured_at,source_url,collector,notes
jsmith-quant-9a1,Alex Rivera,https://www.linkedin.com/in/alexrivera/,Software Engineer at Acme,complete,3,2026-08-17T10:04:00Z,https://www.linkedin.com/in/jsmith-quant-9a1/,manual,
jsmith-quant-9a1,Dana Wu,https://www.linkedin.com/in/danawu-7f3a2/,Quant Researcher at Globex,complete,3,2026-08-17T10:04:00Z,,manual,
jsmith-quant-9a1,Sam Okonkwo,,Trader,complete,3,2026-08-17T10:04:00Z,,manual,no profile url captured
kchen-dev-4b2,,,,empty,0,2026-08-17T10:07:00Z,https://www.linkedin.com/in/kchen-dev-4b2/,manual,profile shows no mutuals
r-almeida-8x1,Alex Rivera,https://www.linkedin.com/in/alexrivera/,Software Engineer at Acme,truncated,24,2026-08-17T10:11:00Z,,manual,stopped after page 1 of 3
p-okafor-2c8,,,,skipped_by_degree,,2026-08-17T09:14:00Z,,degree_prune,3rd degree - zero mutuals by definition
w-tanaka-5m9,,,,unavailable,,2026-08-17T10:19:00Z,,manual,hit commercial use limit
```

### 4. `data/interim/mutuals.jsonl` — canonical interchange format

One JSON object per line, one line per **(target, capture event)**. Every collector emits
this; the analysis engine reads only this. Append-only, so re-captures accumulate rather
than overwrite (§V7 resolves duplicates at load).

```json
{
  "schema": "interlayer.mutuals/v1",
  "target": {
    "id": "jsmith-quant-9a1",
    "name": "Jordan Smith",
    "profile_url": "https://www.linkedin.com/in/jsmith-quant-9a1/",
    "urn": "urn:li:fsd_profile:ACoAAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "firm": "jane_street",
    "degree": "2"
  },
  "capture": {
    "status": "complete",
    "reported_count": 3,
    "observed_count": 3,
    "pages_seen": [1],
    "collector": "har",
    "collector_version": "1.0.0",
    "posture": "passive_capture",
    "captured_at": "2026-08-17T10:04:11Z",
    "source_url": "https://www.linkedin.com/search/results/people/?connectionOf=%5B%22ACoAAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx%22%5D&network=%5B%22F%22%5D&origin=MEMBER_PROFILE_CANNED_SEARCH",
    "source_artifact": "raw/2026-08-17-session-01.har",
    "source_artifact_sha256": "3f786850e387550fdab836ed7e6dc881de23001b",
    "parser_strategy": "fastpath:searchDashClustersByAll",
    "network_filter": ["F"],
    "notes": ""
  },
  "mutuals": [
    {
      "name": "Alex Rivera",
      "profile_url": "https://www.linkedin.com/in/alexrivera/",
      "urn": "urn:li:fsd_profile:ACoAAByyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy",
      "headline": "Software Engineer at Acme",
      "member_distance": "DISTANCE_1"
    },
    {
      "name": "Dana Wu",
      "profile_url": "https://www.linkedin.com/in/danawu-7f3a2/",
      "urn": null,
      "headline": "Quant Researcher at Globex",
      "member_distance": "DISTANCE_1"
    }
  ]
}
```

Notes:
- `mutuals` is `[]` when `status` is `empty`, `unavailable` or `skipped_by_degree`. Only
  `status` — never `len(mutuals)` — distinguishes those three.
- `capture.posture` is written by the collector and **re-validated at load** against the
  allowlist. A hand-edited file claiming `posture: "manual"` for automated output is a
  user integrity problem, not a technical one; the field exists to make the compliance
  model auditable, not tamper-proof.
- `parser_strategy` records which extraction path fired (`fastpath:*` or
  `fallback:recursive`), so a future LinkedIn change shows up in the data as a strategy
  shift before it shows up as silent data loss.
- `network_filter` records whether `network=["F"]` was present. Absent or ≠ `["F"]`
  triggers V13.
- The loader adds a `match` block to each mutual during resolution. **Collectors must not
  populate it** — resolution is a separate, testable stage.

### 5. `data/raw/*.har` and `data/raw/*.voyager.jsonl`

Raw inputs, never modified, never committed, referenced from `capture.source_artifact`
and hashed into `capture.source_artifact_sha256`.

**HAR entry-selection predicate** (all must hold):

```
entry.request.method            == "GET"
"/voyager/api/" in entry.request.url
entry.response.status           == 200
entry.response.content.mimeType startswith "application/"
entry.response.content.text     is present and non-empty
```

If `entry.response.content.encoding == "base64"`, base64-decode before parsing.

**HAR field map:**

| HAR path | Maps to |
|---|---|
| `log.entries[].startedDateTime` | `capture.captured_at` |
| `log.entries[].request.url` | `capture.source_url`; also the source of the `connectionOf` id and `network` filter |
| `log.entries[].response.content.text` | JSON body → `mutuals[]` |
| `log.creator.name` / `.version` | recorded in `capture.notes` for diagnostics |

**Voyager body extraction, in order:**

1. `data.searchDashClustersByAll.elements[].items[]` → `.itemUnion.entityResult`
2. `data.data.searchDashClustersByAll…` (some responses double-wrap under `data`)
3. `included[]` entries whose `$type` contains `EntityResultViewModel` or
   `identity.profile.Profile`
4. **Fallback — recursive walk.** Collect every object that has *both* a string matching
   `urn:li:fsd_profile:` *and* a `navigationUrl` matching `/in/`. Set
   `parser_strategy = "fallback:recursive"`.

Per-entity field map: `entityUrn`/`trackingUrn` → `urn`; `navigationUrl` → `profile_url`;
`title.text` → `name`; `primarySubtitle.text` → `headline`;
`entityCustomTrackingInfo.memberDistance` → `member_distance`.

**Truncation from HAR:** compare `paging.total` (when present) against the union of items
collected across all entries sharing the same `connectionOf` id. If
`max(paging.start) + paging.count < paging.total`, set `status = truncated`.

**HAR handling rules (non-negotiable):**
- Stream `log.entries` (e.g. `ijson`); session HARs routinely exceed 100 MB.
- Discard non-matching entries immediately; never hold the whole document in memory.
- **Never** read or persist `request.headers`, `request.cookies`, `response.headers`,
  `response.cookies` — Chrome ≥130 sanitizes by default but other browsers and the
  "with sensitive data" export do not.
- Never copy the raw HAR into the project's tracked data directory; reference it by path
  and hash.
- Enforce rule V8 before any parsing.

---

## Implications for the build

### The `Collector` adapter interface (Wave 2 codes this literally)

`src/interlayer/collect/base.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable


class CompliancePosture(str, Enum):
    """Where the data physically came from. Ordered least-to-most invasive."""
    FIRST_PARTY = "first_party"          # LinkedIn's own sanctioned export
    PASSIVE_CAPTURE = "passive_capture"  # offline parse of traffic the user's
                                         # browser already made, from a file
    MANUAL = "manual"                    # a human read a page and transcribed it
    INFERENCE = "inference"              # derived, not observed (degree pruning)
    ACTIVE_BROWSER = "active_browser"    # code reads/injects into live pages -- NOT SHIPPED
    AUTOMATED = "automated"              # code drives navigation          -- NOT SHIPPED


DEFAULT_ALLOWED_POSTURES: frozenset[CompliancePosture] = frozenset({
    CompliancePosture.FIRST_PARTY,
    CompliancePosture.PASSIVE_CAPTURE,
    CompliancePosture.MANUAL,
    CompliancePosture.INFERENCE,
})


class CaptureStatus(str, Enum):
    COMPLETE = "complete"                    # full list captured
    TRUNCATED = "truncated"                  # captured, known incomplete
    EMPTY = "empty"                          # surface rendered, zero mutuals
    UNAVAILABLE = "unavailable"              # surface did not render
    SKIPPED_BY_DEGREE = "skipped_by_degree"  # pruned as 3rd/out -- INFERRED, not observed


class Degree(str, Enum):
    FIRST = "1"
    SECOND = "2"
    THIRD = "3"
    OUT = "out"
    UNKNOWN = "unknown"

    @property
    def can_have_mutuals(self) -> bool:
        """3rd degree and out-of-network have zero shared 1st-degree connections
        by the definition of shortest-path degree. This is the pruning oracle."""
        return self in (Degree.FIRST, Degree.SECOND, Degree.UNKNOWN)


@dataclass(frozen=True, slots=True)
class PersonRef:
    """Identity of a person. At least one of urn / profile_url / name must be set."""
    name: str | None = None
    profile_url: str | None = None   # normalised, see V2
    urn: str | None = None           # urn:li:fsd_profile:ACoAA...
    headline: str | None = None
    member_distance: str | None = None

    @property
    def key(self) -> str:
        """Join key, strongest available identifier first. The prefix records WHICH
        identifier matched, so V6 can report resolution strength per edge.
        (`self.urn` already starts with 'urn:li:...', so it is not re-prefixed.)"""
        if self.urn:
            return self.urn
        if self.profile_url:
            return f"url:{self.profile_url}"
        return f"name:{(self.name or '').casefold()}"

    @property
    def is_strongly_identified(self) -> bool:
        return bool(self.urn or self.profile_url)


@dataclass(frozen=True, slots=True)
class TargetRef(PersonRef):
    id: str = ""
    firm: str = "other"
    degree: Degree = Degree.UNKNOWN


@dataclass(frozen=True, slots=True)
class Provenance:
    collector: str
    collector_version: str
    posture: CompliancePosture
    captured_at: datetime
    source_url: str | None = None
    source_artifact: str | None = None
    source_artifact_sha256: str | None = None
    parser_strategy: str | None = None
    network_filter: tuple[str, ...] | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class MutualSet:
    """One capture event for one target. The unit of everything downstream."""
    target: TargetRef
    status: CaptureStatus
    mutuals: tuple[PersonRef, ...] = ()
    reported_count: int | None = None
    pages_seen: tuple[int, ...] = ()
    provenance: Provenance = field(kw_only=True)

    @property
    def observed_count(self) -> int:
        return len(self.mutuals)

    @property
    def is_truncated(self) -> bool:
        if self.status is CaptureStatus.TRUNCATED:
            return True
        return (self.reported_count is not None
                and self.reported_count > self.observed_count)

    @property
    def is_inferred(self) -> bool:
        return self.status is CaptureStatus.SKIPPED_BY_DEGREE

    @property
    def yields_edges(self) -> bool:
        return self.status in (CaptureStatus.COMPLETE, CaptureStatus.TRUNCATED)


@runtime_checkable
class Collector(Protocol):
    """Every acquisition adapter. Adapters read FILES. No adapter opens a socket."""
    name: str
    version: str
    posture: CompliancePosture

    def accepts(self, path: Path) -> bool:
        """Cheap check -- extension plus a small header sniff. No full parse."""
        ...

    def collect(self, path: Path) -> Iterable[MutualSet]:
        """Yield one MutualSet per (target, capture event). Must be a generator:
        inputs can be hundreds of MB. Must not raise on a single malformed record --
        yield what parses and accumulate the rest into CollectionReport.errors."""
        ...
```

`src/interlayer/collect/registry.py`:

```python
class CompliancePostureError(RuntimeError):
    """A collector or record declared a posture outside the allowlist."""


def load_all(
    paths: Sequence[Path],
    *,
    allowed_postures: frozenset[CompliancePosture] = DEFAULT_ALLOWED_POSTURES,
) -> CollectionReport:
    """The ONLY entry point into acquisition. Enforces the posture allowlist twice:
    once on the collector, once on every record it produces."""
```

Widening the allowlist requires **both** an explicit config key
(`compliance.allow_postures`) **and** the CLI flag `--i-accept-tos-risk`. Neither is
documented in the quickstart. No shipped collector declares `ACTIVE_BROWSER` or
`AUTOMATED`, so both paths are dead ends by construction — they exist to be tested
against, not used.

### Provenance model

Every edge in the final graph traces to a `Provenance`. Three consequences Wave 2 must
implement:

1. **Edges carry their posture.** A bridge discovered only via `skipped_by_degree`
   inference (i.e. absence of an edge, not presence) is never presented as an observation.
   The report must be able to say "these 47 bridges are observed; these 12 targets were
   pruned by degree and therefore contribute no edges — that pruning is sound but
   inferred."
2. **Every edge carries `captured_at`.** Graph freshness is reportable, and age-based
   deletion (constraint 3 of the handoff plan) is a filter on this field.
3. **Provenance merges, never overwrites.** When the same edge is captured twice
   (§V7), the merged record keeps a `provenance: [...]` list. Downstream confidence is a
   function of the strongest posture present and the most recent `captured_at`.

### Validation rules (each is one test)

| # | Rule | Severity |
|---|---|---|
| **V1** | Every `PersonRef` has ≥1 of `urn` / `profile_url` / `name`. | error |
| **V2** | Normalise `profile_url` → `https://www.linkedin.com/in/<publicId>/`: lowercase host and publicId, strip query, fragment, trailing slash variance, and locale subdomain prefixes (`uk.linkedin.com`, `?originalSubdomain=de`). Both `Connections.csv` `URL` values and captured URLs go through this. | error on unparseable |
| **V3** | Drop the viewer's own profile from any `mutuals` list. | silent |
| **V4** | A target must not appear in its own `mutuals` list. | warn + drop |
| **V5** | If `reported_count > observed_count`, force `status = TRUNCATED` regardless of what the collector claimed. | silent correction |
| **V6** | Every mutual SHOULD resolve into `M`. Unresolved → keep, mark `resolved=false`, warn. **Never silently drop** — `Connections.csv` may predate a recent connection. Resolution order: `urn` → normalised `profile_url` → exact casefolded name → **no fuzzy matching by default** (a 2,000-person network has real name collisions; fuzzy matching behind `--fuzzy-names` with the match recorded in `match.method`). | warn |
| **V7** | Dedupe on `(target.key, mutual.key)`. On conflict: keep the strongest identity, keep the latest `captured_at`, **merge provenance into a list**. | silent |
| **V8** | Reject any input file containing `li_at=`, `JSESSIONID`, `csrf-token`, or an `Authorization` header value. Hard error, with remediation text ("re-export your HAR with sanitization enabled"). Runs **before** parsing. | fatal |
| **V9** | Every record's `posture` ∈ allowlist. Checked on the collector and again per record. | fatal |
| **V10** | Warn when `captured_at` is older than `freshness_days` (default 90) or when `degree_observed_at` is older than 30 days — degree badges go stale and a stale `3` may now be a `2`. | warn |
| **V11** | A target with `degree ∈ {3, out}` and `status = COMPLETE` and `observed_count > 0` is contradictory. Trust the **observation**, correct the degree, warn. | warn + correct |
| **V12** | Coverage accounting must distinguish five states per target: `observed_complete`, `observed_truncated`, `observed_empty`, `pruned_by_degree`, `unobserved`. A target absent from all inputs is `unobserved`, never `empty`. | required output |
| **V13** | A `PASSIVE_CAPTURE` record whose `network_filter` is absent or ≠ `("F",)` means the capture was not restricted to mutuals and may contain the target's non-mutual connections. **Reject the record** with an explanatory error. | fatal |
| **V14** | `mutuals.csv` target-level columns that conflict across rows of the same `target_id` → take first non-empty, warn. Never fail a hand-edited file over this. | warn |

### Coverage metrics the pipeline must emit

```
targets_total
targets_by_degree            {1: n, 2: n, 3: n, out: n, unknown: n}
targets_observed_complete
targets_observed_truncated
targets_observed_empty
targets_pruned_by_degree     # sound inference, reported separately
targets_unobserved           # the honest gap
edges_total
edges_strongly_identified    # both endpoints matched by urn or url
bridges_total                # |B| = distinct m with >=1 edge
observation_rate             = observed_* / (observed_* + unobserved)
resolution_rate              = mutuals resolved into M / mutuals total
cul_units_estimated          = 1 or 2 per visited target + enumeration searches
```

`observation_rate` and `resolution_rate` belong at the top of every report. A clustering
result computed over 40% coverage is a different claim from one computed over 95%, and
the tool must never let the user forget which one they are reading.

---

## Coverage economics

**Scenario:** `|M| = 2,000` connections; `|T| = 300` Jane Street / Citadel employees.

### Step 1 — degree pruning (the cheap, decisive step)

Degree badges are read in bulk off company-filtered people-search result pages —
10 per page, ~100 results per page-scroll session, a handful of searches total, no
per-target cost. Then:

| Degree | Share (central) | Count | Must visit? | Mutuals |
|---|---|---|---|---|
| 1st | 2% | 6 | yes | ≥ 0 |
| 2nd | **45%** | **135** | **yes** | **≥ 1 guaranteed** |
| 3rd / out | 53% | 159 | **no** | **0, provably** |

**141 of 300 targets need visiting — 53% of the work eliminated for a handful of
searches, with no information loss.**

The 2nd-degree share is the dominant uncertainty. Modelling the user's 2,000 connections
and a target's ~1,000 connections as draws from a relevant professional pool of size `N`,
`P(share ≥ 1 connection) ≈ 1 − exp(−2×10⁶ / N)`:

| Pool `N` | Interpretation | P(2nd degree) | Targets to visit |
|---|---|---|---|
| 10M | user's network unrelated to finance | ~18% | ~60 |
| 2M | finance/tech professionals, relevant geographies | ~63% | ~190 |
| 500k | NYC/London quant-adjacent | ~98% | ~295 |

Homophily pushes a genuinely finance-adjacent user toward the high end. **The build should
not guess: it measures this directly from `targets.csv` and reports it**, because the
pruning ratio *is* the project's cost estimate.

### Step 2 — per-mode cost, 141 targets

| Mode | s/target | Wall-clock | CUL units | Months on free (~300/mo) | Coverage achieved | Identity fidelity |
|---|---|---|---|---|---|---|
| **(a) Manual** | 60–90 | **2.4–3.5 h** | 141–282 | **1** | ~100% of pruned set | Display names → fuzzy match into `M` |
| **(b) HAR** | 20–30 | **47–70 min** | 141–282 | **1** | ~100% of pruned set | **URN + publicId → exact match** |
| **(c) Extension** | 10–15 | 24–35 min | 141–282 | 1 | ~100% | URN + publicId |
| **(d) Semi-auto, paced** | 30–60 (enforced) | 1.2–2.4 h | 141–282 | 1 | ~100%, → 0 on restriction | URN + publicId |
| **(e) Headless at scale** | 2–5 | 5–12 min | blocked | — | collapses | — |

**Sales Navigator variant (~$99–159 for one month):** CUL removed, so all 141 targets fit
in **one session**. Mode (b) becomes a ~1-hour job, end to end, in a single evening. Do
the capture on flagship `linkedin.com` — the entitlement is account-level, and the
flagship Voyager response shape is the one the parser handles.

### Step 3 — the conclusion the table forces

Every mode lands in the **same month** and delivers the **same coverage**, because
coverage is gated by a monthly server-side quota that no client-side technique raises.

- (c) buys ~35 minutes over (b), at the cost of shipping code into a category LinkedIn
  scans 6,200 signatures for and has killed three named products over in 18 months.
- (d) buys **nothing at all** — properly paced, it is slower than (b).
- (e) buys minutes, and the litigation record.

**Automation has no coverage upside on this problem.** The compliant build is not the
cautious build; it is the correct one. The only lever that actually moves coverage is one
month of Sales Navigator — a payment to LinkedIn for a higher quota, which is the
sanctioned way to want more.

### What actually limits coverage (ranked)

1. **CUL exhaustion** — 150–250 targets/month free. *Mitigation:* budget and report
   `cul_units_estimated`; recommend Sales Navigator for one month.
2. **Enumeration truncation** — the 1,000/2,500-result cap on company searches means `T`
   itself may be incomplete. *Mitigation:* `enumeration_truncated` flag; R5 slices by
   title/location/school.
3. **Degree-badge staleness** — a `3` that has since become a `2` is silently dropped.
   *Mitigation:* V10 warns after 30 days; document a "re-badge and re-prune" pass.
4. **Weak identity in manual mode** — display-name collisions inside 2,000 connections.
   *Mitigation:* prefer mode (b); report `resolution_rate`; keep fuzzy matching opt-in.
5. **Stale `Connections.csv`** — mutuals that resolve to nothing. *Mitigation:* V6 keeps
   them and warns; re-export `Connections.csv` at the start of a capture campaign.
6. **Target-side connection privacy** — **not a limit.** See §A.5.

---

## Rejected alternatives

| Rejected | Why |
|---|---|
| Building a browser extension | 30-minute time saving over HAR capture; LinkedIn enumerates ~6,200 extension IDs per page load; Kleo/Taplio/HeyReach precedent; high build and maintenance cost against DOM churn. |
| Playwright/Selenium with the user's session | No throughput advantage once honestly paced; paradigm §8.2 violation; 2026 detection reaches input-event physics and CDP GPU signatures; puts the user's real professional identity at risk. |
| Any headless/fleet approach | hiQ, Mantheos, Proxycurl. |
| Calling Voyager directly from Python (`linkedin-api` and similar) | Same as (e) legally; and technically brittle — the `queryId` hash rotates on every LinkedIn deploy. The library's own docs state it violates §8.2. |
| Buying connection-graph data from a vendor | R2's finding; Proxycurl's injunction required customer notification, which is a live downstream risk for buyers. |
| Using `connectionOf` **without** `network=["F"]` | Returns the target's non-mutual connections, including for members who set connections to private. Not needed for `E`; rejected by rule V13. |
| Sales Navigator as the acquisition mechanism | "Connections of" needs the person to be *your* 1st-degree connection; no CSV export. It is a quota purchase, nothing more. |
| Inferring edges from "People Also Viewed" / browsemap | Not the edge set — it is a behavioural co-view signal, not `M ∩ conn(t)`. Belongs in the graceful-degradation path (constraint 4) as clearly-labelled inference, not in `E`. |

---

## Open questions for the orchestrator

1. **CUL magnitude is unverified for this specific account.** LinkedIn does not publish
   it. The build should *measure* it: log `cul_units_estimated` and let the user record
   when they hit the wall. Do not hard-code 300.
2. **Whether the profile card view alone consumes a CUL unit** (as distinct from the
   click-through search) is undocumented. Budget 2/target conservatively; the direct-URL
   procedure costs 1.
3. **Whether Chrome always populates `response.content.text` for Voyager XHRs** — reports
   conflict. Mitigated by runtime detection plus the `.voyager.jsonl` fallback; Wave 3
   should test both fixture shapes.
4. **Sales Navigator's own `/sales-api/` response shape** is not covered by the v1 parser.
   Recommendation stands that users capture on flagship `linkedin.com` even when
   subscribed; revisit only if that proves inconvenient.
5. **R5 owns** the `firm` vocabulary and target enumeration; R4's schema takes both as
   given via `targets.csv`.

---

## Sources

**Fetched in full**
- https://github.com/linkedtales/scrapedin/issues/120
- https://github.com/dchrastil/ScrapedIn/blob/master/ScrapedIn.py
- https://github.com/joshuatz/linkedin-to-jsonresume/blob/main/docs/LinkedIn-Dev-Notes-README.md

**LinkedIn first-party (indirect — egress-blocked in this session)**
- https://www.linkedin.com/legal/user-agreement
- https://www.linkedin.com/help/linkedin/answer/a1341387 — Prohibited software and extensions
- https://www.linkedin.com/help/linkedin/answer/a564226 — Commercial use limit
- https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections
- https://www.linkedin.com/help/linkedin/answer/a540663/who-can-see-your-connections
- https://www.linkedin.com/help/linkedin/answer/a545636/your-network-and-degrees-of-connection
- https://www.linkedin.com/help/linkedin/answer/a566336/export-connections-from-linkedin
- https://news.linkedin.com/2022/february/taking-legal-action-to-protect-members-against-scraping

**Surface mechanics, limits, Sales Navigator (indirect)**
- https://adityatelange.in/blog/linkedin-list-network-without-connecting/
- https://phantombuster.com/blog/social-selling/mutual-connections-linkedin-guide/
- https://phantombuster.com/blog/social-selling/linkedin-commercial-use-limit/
- https://evaboot.com/blog/hack-to-bypass-linkedin-search-limit
- https://www.dux-soup.com/blog/linkedin-search-limits
- https://linkedapi.io/guides/understanding-linkedin-limits
- https://skylead.io/blog/linkedin-sales-navigator-filters/
- https://derrick-app.com/linkedin/sales-navigator/teamlink-filter
- https://www.cleanlist.ai/blog/2026-04-25-how-to-export-linkedin-sales-navigator-data
- https://github.com/nsandman/linkedin-api/blob/master/DOCS.md

**HAR mechanics**
- https://developer.chrome.com/docs/devtools/network/reference
- https://issues.chromium.org/issues/378076279
- https://michev.info/blog/post/6693/how-to-export-unsanitized-har-files-with-chrome-and-edge
- https://stevesie.com/apps/linkedin-api

**Legal**
- https://www.proskauer.com/blog/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case
- https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/
- https://news.bloomberglaw.com/privacy-and-data-security/linkedin-settles-data-scraping-lawsuit-against-mantheos
- https://nubela.co/blog/goodbye-proxycurl/
- https://www.socialmediatoday.com/news/linkedin-wins-legal-case-data-scrapers-proxycurl/756101/
- https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/
- https://blog.ericgoldman.org/archives/2024/01/game-on-bright-data-scores-major-victory-in-web-scraping-dispute-with-meta-guest-blog-post.htm

**Enforcement and detection**
- https://www.bleepingcomputer.com/news/security/linkedin-secretly-scans-for-6-000-plus-chrome-extensions-collects-data/
- https://ppc.land/linkedin-hit-with-class-action-over-hidden-browser-scan-of-6-000-extensions/
- https://cyberinsider.com/linkedin-faces-class-action-over-alleged-covert-scanning-of-users-browsers/
- https://northlight.ai/blog/northlight-vs-heyreach
- https://www.joinvalley.co/blog/linkedin-automation-safety-2026
- https://linkedinsider.blog/linkedin-automation-crackdown-2026
- https://autoposting.ai/blog/kleo-alternatives
- https://magicpost.in/blog/kleo-review
- https://news.ycombinator.com/item?id=34583932
- https://www.linkedhelper.com/blog/linkedin-account-restricted
- https://www.linkedhelper.com/blog/linkedin-automation-limits
- https://blog.closelyhq.com/linkedin-detect-automation-stay-under-radar/
- https://pettauer.net/en/linkedin-tos-breaches-risk-enforcement-comparison/
- https://cside.com/blog/headless-browser-detection
- https://scrapfly.io/blog/posts/playwright-stealth-bypass-bot-detection
- https://www.vayne.io/en/blog/best-linkedin-scrapers-2026
- https://derrick-app.com/linkedin/scraper/chrome-extension
- https://apify.com/data_link_miner/linkedin-shared-connections
