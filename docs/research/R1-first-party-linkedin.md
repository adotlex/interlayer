# R1 — First-Party LinkedIn Surfaces

**Research agent:** R1 (Wave 1)
**Date:** 2026-08-17
**Scope:** LinkedIn official data export; official LinkedIn APIs as of 2026; Sales Navigator; Recruiter / Recruiter Lite; Terms of Service and legal posture.

**Research-conditions note.** `linkedin.com`, `learn.microsoft.com`, `en.wikipedia.org`, `courtlistener.com` and all other direct page fetches were blocked by this session's egress policy. Everything below is sourced through web search over secondary and primary-quoting sources, with URLs inline. Where a fact is load-bearing for Wave 2 code and could not be confirmed against LinkedIn's own page, it is tagged **[VERIFY IN-PRODUCT]**. Wave 2 must treat those as *defaults with fallbacks*, not as guarantees. Confidence tags used throughout: **[HIGH]** = multiple independent sources agree; **[MED]** = single credible source or practitioner consensus; **[LOW]** = inference.

---

## Summary

1. **The official export gives you `M` and nothing else.** `Connections.csv` is a clean, zero-risk, complete list of your 1st-degree connections. No first-party export — self-serve archive, DMA portability API, or GDPR Article 15 DSAR — contains second-degree, connection-of-connection, or mutual-connection data. This is definitive. LinkedIn's own Connections API documentation states 2nd-degree connections "are not available from LinkedIn," and Article 15(4) GDPR gives LinkedIn a standing basis to refuse third-party graph data. The bipartite edge set `E` is **not** obtainable from any export.

2. **`Connections.csv` schema is stable and quirky.** Header row is `First Name,Last Name,URL,Email Address,Company,Position,Connected On`, preceded by 2–3 "Notes:" preamble lines that must be skipped. `Connected On` is `DD MMM YYYY` (e.g. `18 Aug 2024`). `Email Address` is blank for the large majority of rows (typically 50–90% blank) because the source setting is opt-in and off by default. Parse defensively: locate the header row by content, do not hardcode `skiprows=3`.

3. **No official API returns a connection list or mutual connections to an individual developer.** `r_1st_connections_size` returns a **count only** and requires Partner Program approval. The Connections API (list form) is Partner-gated, returns only the *authenticated member's own* 1st-degree connections, and explicitly excludes 2nd-degree. Sign In with LinkedIn / OIDC yields `openid`, `profile`, `email` only. Sales Navigator's SNAP API is **closed to new partners**. Realistic approval odds for an individual with no ATS/CRM product: effectively zero (Talent partner approval is reported at <10% and 3–6 months, for companies).

4. **The one first-party surface that actually yields the edges is LinkedIn people search's `connectionOf` facet** — the same facet behind the "N mutual connections" link on every profile. It is available in the ordinary web UI, is a normal human browsing action, and is the *only* sanctioned surface that exposes `M ∩ connections(t)`.

5. **Critical architectural finding — invert the enumeration.** The naive plan is one query per target (`connectionOf=t`, ~10,000 queries across Jane Street + Citadel + Citadel Securities). The far better plan is one query per *own connection*: `connectionOf=m` **AND** `currentCompany ∈ {Jane Street, Citadel, Citadel Securities}`, for each `m ∈ M`. This returns exactly `{t ∈ T : (m,t) ∈ E}` — the same bipartite edge set, indexed the other way — in **|M| queries (~500–3,000) instead of |T| (~10,000)**, with the company filter applied server-side. It is roughly an order of magnitude cheaper *and* strictly better for data minimisation, because you never retrieve non-target people. Wave 2 should model both directions as adapters over one edge schema.

6. **Direction B's one blind spot** is connections who set "Who can see your connections" to *Only you*; for them `connectionOf=m` degrades to shared-connections-only. Direction A (`connectionOf=t`, mutual-connections surface) is unaffected by that setting and is the correct backfill. Design for a hybrid: B over all of `M`, then A over a prioritised slice of `T`.

7. **Free accounts cannot do this at scale.** The Commercial Use Limit caps people searches at roughly 250–350/month, resets midnight PST on the 1st, cannot be lifted, and is not visible as a counter. For |M| in the thousands this is a hard blocker. **Sales Navigator Core at US$119.99/mo (30-day free trial) removes it** and carries the same search filters as Advanced. Core is the right tier; Advanced ($159.99/mo) buys TeamLink and Smart Links, which are useless to a solo user; Advanced Plus (~$1,300–1,600/seat/yr) buys CRM sync.

8. **Sales Navigator CSV export is gone.** Per LinkedIn's own help documentation, CSV/XLS export is not available for Sales Navigator data as of 1 July 2026; CRM Sync on Advanced Plus is the only sanctioned bulk egress. Any tool promising Sales Navigator CSV export in 2026 is a scraper. The build must not depend on a Sales Navigator export file.

9. **Recruiter and Recruiter Lite add nothing.** Their network facet is degree-only (`1st / 2nd / 3rd`) — there is no "connections of person X" equivalent. Recruiter Lite (~US$170/mo) is *more* restrictive than Sales Navigator on network reach. Reject both.

10. **Legal posture is settled and unfavourable to automation, favourable to manual use.** User Agreement §8.2 bans "software, devices, scripts, robots or any other means or processes (including crawlers, browser plugins and add-ons or any other technology) to scrape the Services or otherwise copy profiles and other data." `robots.txt` is `User-agent: * / Disallow: /` with a whitelist-application notice. hiQ won on the CFAA (scraping *public* pages is not "without authorization" — 9th Cir., 18 Apr 2022) but **lost on breach of contract** and exited via a Dec 2022 consent judgment: $500,000, permanent injunction, deletion of all scraped data and code. LinkedIn has since won the same way against Mantheos (2022) and Proxycurl/Nubela (settled 2025; Proxycurl shut down 4 July 2025 under a permanent deletion injunction).

11. **Browser extensions are now a first-order account risk, not a grey area.** The April 2026 "BrowserGate" investigation documented LinkedIn silently probing for 6,000+ Chrome extensions and building a 48-signal device fingerprint attached to every API request. Whatever one thinks of the practice, its operational meaning is unambiguous: extension-assisted capture is detectable by design. Rate the extension path **high account risk**, and never make it the default.

12. **Recommended first-party stack, in order:** (1) official data export for `M` — mandatory backbone; (2) manual/human-paced `connectionOf` browsing on Sales Navigator Core for `E`, with a paste-in/clipboard capture path rather than an injected extension; (3) an explicitly-labelled inference-only degraded mode when `E` is unavailable. Everything else is rejected.

---

## Findings

### 1. LinkedIn official data export

#### 1.1 Where it lives and the two speeds

Path: **Me → Settings & Privacy → Data privacy → How LinkedIn uses your data → Get a copy of your data.** Two options are offered:

| Option | Contents | Turnaround (reported) |
|---|---|---|
| "Want something in particular?" (fast file) | Checkbox list of specific data types, including a **Connections** checkbox | ~10 minutes, commonly cited as 10–24 min |
| "Download larger data archive" | Everything LinkedIn stores: messages, posts, comments, search history, profile, ad-targeting inferences, invitation history, logins | ~24 h, reported up to 24–72 h, delivered in **two batches** |

The download link in the notification email is valid **72 hours**. Archive is a ZIP of CSVs plus some JSON and a `Rich_Media` media folder. Size is dominated by `Rich_Media` and `messages.csv`; a text-only archive for a typical account is single-digit MB.

Key operational point for the build: **the fast file with only the Connections checkbox is sufficient for `M`, and returns in ~10 minutes.** The full archive is only needed if we want the auxiliary inference signals in §1.3.

Sources: [connectsafely.ai — export guide 2026](https://connectsafely.ai/articles/export-linkedin-contacts-connections-guide-2026) · [espirian.co.uk — LinkedIn data archive](https://espirian.co.uk/linkedin-data-archive/) · [lagrowthmachine.com](https://lagrowthmachine.com/export-linkedin-contacts/) · [thewindowsclub.com](https://www.thewindowsclub.com/download-linkedin-data)

#### 1.2 `Connections.csv` — exact schema **[HIGH]**

The real header row, verbatim:

```
First Name,Last Name,URL,Email Address,Company,Position,Connected On
```

Preceded by a preamble. The preamble is **notes text, not CSV**, and is commonly 2–4 lines including a blank line, beginning with `Notes:`. Multiple independent sources describe it as "2–3 line preamble" and "delete the first 3–4 rows of notes so the column headers sit on row 1". The canonical Python idiom circulated in tutorials is `pd.read_csv('Connections.csv', skiprows=3)`.

> **Do not hardcode `skiprows=3`.** The line count has varied over time and by locale, and at least one source says 2–3 while another says 3–4. Wave 2 must sniff for the header. See §Implications.

Field notes:

| Column | Type / format | Notes |
|---|---|---|
| `First Name` | str | May be empty for members who restrict name visibility, or for out-of-network/withdrawn accounts. |
| `Last Name` | str | Same. Often an initial only. |
| `URL` | str | Public profile URL, form `https://www.linkedin.com/in/<publicIdentifier>`. **This is the join key.** Sometimes empty. |
| `Email Address` | str | **Usually blank.** Populated only where the connection enabled the opt-in email-visibility/export setting, which is **off by default**. Reported fill rates range from "30–50% present" to "80–90% blank"; treat as *mostly blank*. |
| `Company` | str | Current company as free text at export time. Not an entity ID. Not normalised. |
| `Position` | str | Current title as free text. |
| `Connected On` | `DD MMM YYYY` | e.g. `18 Aug 2024`, `15 Mar 2024`. English month abbreviations. **[MED]** — locale variation is plausible; parse permissively. |

Not present: notes/tags from LinkedIn's relationship-management features, phone numbers, connection-degree annotations, or any second-degree data.

Sources: [networkcleaner.com](https://www.networkcleaner.com/blog/export-linkedin-connections-csv) · [magicpost.in](https://magicpost.in/blog/export-linkedin-contacts) · [getboothiq.com](https://getboothiq.com/blog/how-to-export-linkedin-contacts) · [bradley-schoeneweis (pandas idiom)](https://bradley-schoeneweis.medium.com/visualizing-your-linkedin-connections-with-python-pandas-networkx-pyvis-40bf846a532) · [krischase.com](https://www.krischase.com/blog/build-your-own-networkexplorer-linkedin-data-visualization) · [connectsafely.ai](https://connectsafely.ai/articles/export-linkedin-contacts-connections-guide-2026) · [salesrobot.co](https://www.salesrobot.co/blogs/export-linkedin-contacts)

#### 1.3 Other archive files — does each help infer network structure?

Verified-present filenames across sources: `Connections.csv`, `Contacts.csv`, `Invitations.csv`, `messages.csv`, `Profile.csv`, `Positions.csv`, `Registration.csv`, `Shares.csv`, `Ad_Targeting.csv`, `Company Follows.csv`, `Member_Follows.csv`, `Hashtag_Follows.csv`, `Articles/`, `Rich_Media/`, plus a Search Queries file, Endorsements (given/received), Recommendations (given/received), Skills, Education, Languages, Projects, Publications, Events, Reactions, Comments, and login history. **[MED]** on exact casing/spelling of the less-common files — several are reported by description rather than literal filename, and LinkedIn has renamed files historically (note the inconsistent casing: `Connections.csv` vs `messages.csv`). Wave 2 must match case-insensitively and by fuzzy name.

| File | Contents | Value for this problem |
|---|---|---|
| **`Connections.csv`** | 1st-degree list, 7 cols above | **Essential.** This *is* `M`. Backbone of everything. |
| `Contacts.csv` | Address-book contacts you imported/synced (phone, email), including people not on LinkedIn | **Low.** Off-platform contacts, no graph edges. Privacy-hot: contains third parties who never consented to LinkedIn. Recommend we do not ingest it at all. |
| `Invitations.csv` | Incoming and outgoing invitations with timestamps and messages **[MED]** — sources agree on "all incoming invitations, with dates & times and messages"; whether outgoing is included is inconsistent. Column names unconfirmed; commonly reported as `From`, `To`, `Sent At`, `Message`, `Direction`. **[VERIFY IN-PRODUCT]** | **Low–medium.** Direction and timing of the invite is a weak signal about who sought whom, useful only for tie-breaking cluster narratives. Does not yield edges. |
| `messages.csv` | Full DM history. Columns **[HIGH]**: `CONVERSATION ID`, `CONVERSATION TITLE`, `FROM`, `SENDER PROFILE URL`, `TO`, `RECIPIENT PROFILE URLS`, `DATE`, `SUBJECT`, `CONTENT`, `FOLDER`, `ATTACHMENTS`, `IS MESSAGE DRAFT` | **Medium, but only for tie strength — and it is the single most privacy-sensitive file in the archive.** Group threads in `RECIPIENT PROFILE URLS` are genuine co-occurrence evidence (people the user put in a room together). Message *content* has no place in this tool. Recommend: optionally ingest **headers only**, never `CONTENT`, and default the whole file off. |
| `Company Follows.csv` | Companies you follow, with follow dates | **Low for `M`, but directly useful for `T`:** it is a free, first-party confirmation of the canonical LinkedIn company entities for Jane Street / Citadel / Citadel Securities if the user follows them. Hand to R5 for entity resolution. |
| `Member_Follows.csv` | Members you follow who are not connections | **Low.** Following is not an edge in our sense. Can seed extra target candidates. |
| `Positions.csv` | Your own job history: company, title, description, location, dates | **Low for `M`; useful as *context*.** Establishes the user's own tenure overlaps, which is what makes a "shared employer" inference meaningful in degraded mode. |
| `Profile.csv` | Your own profile: headline, summary, industry, location, current position | **Very low.** Self-description only. |
| `Skills.csv` | Your own skills list | **None.** No graph content. |
| Endorsements (given/received) | Who endorsed you and whom you endorsed, with skill | **Medium — the best genuine graph signal in the archive after `Connections.csv`.** An endorsement is a directed, deliberate act between two members. It reweights `M`: strongly-endorsed people are stronger bridges. Still only touches `M`, never `T`. |
| Recommendations (given/received) | Full-text recommendations with author | **Medium, same shape as endorsements, lower volume, higher signal.** A written recommendation is much stronger evidence of a real relationship than a connection. Excellent for edge weighting within `M`. |
| `Ad_Targeting.csv` | LinkedIn's **inferred** attributes about you used for ad targeting: age range, gender, company size, degrees, schools, fields of study, **company connections**, job functions, groups, industries, interests, seniorities, skills, titles, years of experience | **Low and dangerous.** The "company connections" and "groups" inference categories look tantalising but they are LinkedIn's *inferences about the ad-targetable you*, not an edge list, and are unversioned and unexplained. Do not use as evidence. Interesting only as a sanity check that you are ad-targetable as "connected to people at X". |
| Search Queries | Your past LinkedIn searches | **None for structure; useful for provenance.** If the user has already run `connectionOf` searches manually, this file is a free audit trail of what was looked at. Genuinely useful for the compliance log. |
| Group memberships | Groups you belong to **[MED]** — reported as present, exact filename unconfirmed | **Medium for degraded mode.** Shared group membership is one of the four inference signals the handoff plan calls for. Note it only tells you *your* groups, not who else is in them — so as an inference feature it can only be combined with per-person group data acquired elsewhere. |
| `Registration.csv`, login history, `Reactions.csv`, `Comments.csv`, `Shares.csv`, `Articles/`, `Rich_Media/` | Account metadata and your own content | **None.** |

#### 1.4 Does any first-party export contain connection-of-connection or mutual-connection data?

**No. Definitively no.** Three independent confirmations:

- The self-serve archive: "LinkedIn's native export never includes 2nd or 3rd-degree connections – only your direct 1st-degree contacts." ([linkedhelper.com](https://www.linkedhelper.com/blog/how-to-download-1st-2nd-3rd-degree-linkedin-connections-to-excel-2/))
- LinkedIn's own Connections API documentation, quoted in search results: "you cannot get connections of that user's connections (2nd-degree connections)" and "2nd-degree connections, or connections of your member's connections, are not available from LinkedIn." ([learn.microsoft.com — Connections API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api))
- The DMA Member Data Portability API's documented domains are the member's own profile, posts, invitations, messages, social actions and synced contacts — the same first-person surface, not the graph. ([learn.microsoft.com — Member Data Portability (3rd Party)](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-3rd-party/?view=li-dma-data-portability-2025-11))

A GDPR Article 15 DSAR does not change this. Article 15(4) expressly limits the right of access where it "would adversely affect the rights and freedoms of others," and the EDPB's Guidelines 01/2022 on the right of access endorse redaction of intertwined third-party personal data. Another member's connection list is that member's personal data about *further* third parties; LinkedIn has a clean and well-established basis to refuse. Do not build a DSAR path. ([gdpr-info.eu Art.15](https://gdpr-info.eu/art-15-gdpr/) · [EDPB Guidelines 01/2022](https://www.edpb.europa.eu/system/files/2022-01/edpb_guidelines_012022_right-of-access_0.pdf) · [arthurcox.com](https://www.arthurcox.com/knowledge/balancing-gdpr-data-access-rights-against-the-rights-of-others/))

**Consequence for the build:** the export gives us the vertex set `M`, complete and free. It gives us zero edges into `T`. The entire edge-acquisition problem lives outside the export.

---

### 2. Official LinkedIn APIs as of 2026

#### 2.1 The general access model

Since 2015 LinkedIn has had no open API. Every product beyond basic sign-in is gated behind a Developer Portal app that requires a **verified company LinkedIn Page** (personal accounts are ineligible), a logo, a real privacy-policy URL that LinkedIn reviews, and acceptance of the API Terms of Use. Marketing products then have Development → Standard tiers; Sales and Talent products are partner-program-gated. ([getphyllo.com — LinkedIn API access 2026](https://www.getphyllo.com/post/linkedin-api-access-in-2026-partner-program-approval-timeline-alternatives) · [learn.microsoft.com — Getting Access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access))

The company-page prerequisite alone disqualifies the individual user this tool is built for.

#### 2.2 Product-by-product

| Product | Access tier / process | Realistic odds for an individual | Returns connection list? | Returns mutual connections? |
|---|---|---|---|---|
| **Sign In with LinkedIn using OpenID Connect** | Self-serve from the Developer Portal Products tab. Scopes: `openid`, `profile`, `email` (+ `w_member_social` for posting). | **High** — this is the one thing an individual can get. | **No** | **No** |
| **Connections API** (`r_1st_connections`) | Partner Program only. Returns the **authenticated member's own** 1st-degree connections. Docs explicitly exclude 2nd-degree. | **~0%** | Yes, but only *your own* — i.e. it duplicates `Connections.csv` at higher cost. | **No** |
| **Connections Size API** (`r_1st_connections_size`) | Partner Program only. `GET https://api.linkedin.com/v2/connections/urn:li:person:{id}`. | **~0%** | **No — count only.** The scope is documented as "READ access to the number of 1st-degree connections within the authenticated member's network." | **No** |
| **Marketing Developer Platform** (Advertising, Community Management, Events, Lead Sync, Conversions) | Open to approved developers via the standard process; Dev → Standard tiers; must reach Standard within 12 months or lose access. Manual review, reported 4 weeks best case / ~4 months typical. Matched Audiences, Audience Insights, Media Planning are further restricted. | **Low** for an individual (needs verified company page + product story) | **No** | **No** |
| **Community Management API** | Part of MDP; two tiers. Organisation-scoped. Has an *Organization Network Size* endpoint — follower/company counts, not member graph. | Low | **No** | **No** |
| **Lead Sync API** (`r_marketing_leadgen_automation`) | Requestable directly in the Developer Portal under the Lead Sync product. Returns Lead Gen Form submissions from **your own ads**. | Medium, but irrelevant — requires you to be running LinkedIn ads. | **No** | **No** |
| **Sales Navigator API / SNAP** (Sales Solutions) | SNAP partner program. **"LinkedIn is not currently accepting new partners for access to the LinkedIn Sales Navigator API."** | **0% — the door is shut.** | **No** | **No** |
| **Talent Solutions / Recruiter System Connect** | ATS partner program. Reported 3–6 months and **<10% approval**, for companies with a shipping ATS. | **~0%** | **No** | **No** |
| **Member Data Portability (Member)** — DMA | Self-serve-ish, but **EEA/EU/Switzerland members only**. `r_dma_portability_member`. Snapshot API `GET https://api.linkedin.com/rest/memberSnapshotData?q=criteria&domain=...` and a 28-day Changelog API. | Medium **if the user is in the EEA** | Only the member's own data — same content class as the archive. | **No** |
| **Member Data Portability (3rd Party)** — DMA | Third-party app fetches a member's data with consent, up to 1 year before re-consent. Same domains. | Medium (EEA) | Own data only. | **No** |

Sources: [learn.microsoft.com — Connections API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api) · [learn.microsoft.com — Connections Size](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-size) · [Microsoft Q&A — requesting r_1st_connections](https://learn.microsoft.com/en-us/answers/questions/1166268/how-do-i-request-access-to-linkedin-connections-ap) · [learn.microsoft.com — Increasing Access](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-07) · [learn.microsoft.com — SNAP docs](https://learn.microsoft.com/en-us/linkedin/sales/) · [business.linkedin.com — Become a SNAP Partner](https://business.linkedin.com/sales-solutions/partners/become-a-partner) · [scale.jobs — ATS partner odds](https://scale.jobs/blog/linkedin-api-integration-with-ats-step-by-step-guide) · [learn.microsoft.com — Member Data Portability (Member)](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member/?view=li-dma-data-portability-2026-05) · [learn.microsoft.com — Member Changelog API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api)

#### 2.3 Verdict on `r_1st_connections_size` and the graph scopes

- `r_1st_connections_size` is a **count**, not a list. It cannot produce an edge. It is also scoped to the *authenticated member*, so it cannot even tell you how many connections a target has.
- `r_network`, the old v1 scope that once permitted network-distance and shared-connection queries, does not survive in the v2/`rest` era. It appears in no current permissions documentation. The v1 platform was deprecated from 1 May 2019 and the wider developer lockdown dates to the 12 May 2015 changes. ([developer.linkedin.com Transition FAQ](https://developer.linkedin.com/blog/posts/2015/transition-faq) · [learn.microsoft.com — self-serve migration FAQ](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/migration-faq))
- **No connection-graph scope of any kind exists today** that returns another member's connections or the intersection of two members' connections. There is no partner tier, no price, no application that unlocks it. LinkedIn treats the graph as its core asset.

**Consequence for the build: do not implement an API adapter.** There is nothing to call. A `SignInWithLinkedIn` OIDC flow would authenticate the user and tell us their name — of no analytical value.

---

### 3. Sales Navigator

#### 3.1 Tiers and pricing (2026)

| Tier | Price | What it adds |
|---|---|---|
| **Core** | **US$119.99/mo**, or US$1,079.88/yr (~25% off) | Advanced search (40+ filters), lead & account lists, saved searches, alerts, 50 InMail credits, **no Commercial Use Limit** |
| **Advanced** | **US$159.99/mo**, or US$1,799.88/yr | Everything in Core **plus** TeamLink, TeamLink Extend, Smart Links, "Who viewed my company page". **Search filters, lists and alerts are identical to Core.** |
| **Advanced Plus** | Custom; reported ~US$1,300–1,600/seat/yr | CRM Sync (the only sanctioned bulk egress), data validation, enterprise admin |

**All plans include a 30-day free trial.** ([skrapp.io](https://skrapp.io/blog/linkedin-sales-navigator-cost-and-pricing/) · [cleanlist.ai — 2026 pricing](https://www.cleanlist.ai/blog/2026-05-08-linkedin-sales-navigator-pricing-guide) · [salesmotion.io](https://salesmotion.io/blog/linkedin-sales-navigator-pricing) · [connectsafely.ai — Core vs Advanced](https://connectsafely.ai/articles/sales-navigator-core-vs-advanced-comparison-2026))

Because the search filters are identical between Core and Advanced, and TeamLink is meaningless for a single user, **Core is the correct tier**. The 30-day trial is enough to run a complete acquisition pass for a typical `M`.

#### 3.2 The crux question: can Sales Navigator show you another person's connections?

**Yes, with a hard constraint, and this is the single most important finding in this document.**

The **"Connections of"** lead filter takes one LinkedIn member at a time and returns that member's connections. The constraint, consistently reported: **the person you name must be your own 1st-degree connection.** You cannot type in a Jane Street MD you have never met and get their connection list.

> "The 'Connections of' filter targets connections of a specific user on LinkedIn, and you can insert only one entry at a time; that person must be your 1st-degree connection." — [Skylead, Sales Navigator filters 2026](https://skylead.io/blog/linkedin-sales-navigator-filters/)

> "With this filter, you can search for and find connections of a specific lead or individual… The person you input needs to be your 1st-degree connection." — [Leadscope](https://leadscope.zendesk.com/hc/en-us/articles/30181045132945-How-to-Use-LinkedIn-and-Sales-Navigator-Filters) / [LeadMatch](https://leadmatch.zendesk.com/hc/en-us/articles/30203291453841-How-to-Use-LinkedIn-and-Sales-Navigator-Filters)

Second constraint, from LinkedIn's own visibility rules: you can see a 1st-degree connection's connection list only if they have **not** set "Who can see your connections" to *Only you*. If they have, you fall back to shared connections only. ([LinkedIn Help — Who can see your connections](https://www.linkedin.com/help/sales-navigator/answer/a540663) · [LinkedIn Help — View your connection's connections](https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections))

Third constraint: **a search using the "Connections of" filter caps at 1,000 leads (40 pages)** rather than the usual 2,500 (100 pages). Irrelevant once a company filter is applied, but relevant if anyone tries the unfiltered version. ([evaboot](https://evaboot.com/blog/see-more-2500-leads-linkedin-sales-navigator-search) · [LinkedIn Help — Search results limit](https://www.linkedin.com/help/sales-navigator/answer/a106030))

#### 3.3 The inversion — why the 1st-degree constraint is not fatal, it is a gift

The naive reading is: "Connections of" is useless because our targets `T` are strangers. That reading is wrong, and inverting it is the key design decision.

We need the bipartite edge set `E ⊆ M × T`. There are two ways to enumerate the same set:

**Direction A — target-indexed.** For each `t ∈ T`, open `t`'s profile and read the mutual-connections surface. This yields `M ∩ connections(t)`. Works for any profile you can view, regardless of `t`'s connection-visibility setting, because *shared* connections are always shown. Cost: **|T| page loads**. With Jane Street + Citadel + Citadel Securities headcount, |T| is on the order of **10,000**.

**Direction B — source-indexed.** For each `m ∈ M`, run one search: **`Connections of = m`** **AND** **`Current company ∈ {Jane Street, Citadel, Citadel Securities}`**. This yields `{t ∈ T : (m,t) ∈ E}` directly. `m` is by definition a 1st-degree connection, so the filter's constraint is *automatically satisfied for every element of M*. Cost: **|M| searches**, typically **500–3,000**, and each returns a handful of rows rather than a page of noise.

Direction B is better on every axis that matters:

- **~10× fewer requests**, which is the dominant driver of both time and account risk.
- **Server-side minimisation.** The company filter runs on LinkedIn's side. You never retrieve, never see, and never store the ~99% of each connection's network that is irrelevant. This is a genuine GDPR data-minimisation argument, not a rationalisation — it materially reduces the third-party personal data the tool touches.
- **Multi-select company filter** means one query covers all three firms, not three.
- **Cheaper result handling** — a typical result set is 0–5 rows, which a human can eyeball and copy in seconds.

Direction B's blind spot is exactly Direction A's strength: connections who hid their connection list. For those `m`, Direction B returns shared-connections-only or nothing. So:

> **Hybrid strategy.** Run Direction B across all of `M`. Then run Direction A over a prioritised slice of `T` (most senior, most likely to be hubs, or those surfaced as near-misses) to backfill edges that B could not see. Both write the same `(m, t)` edge records with a `source_direction` field.

**[MED] confidence on the exact semantics of "Connections of" for a 1st-degree connection with default privacy** — sources agree on the 1st-degree requirement and on the privacy-setting fallback, but none states verbatim "returns their full list." Wave 2 must handle a shared-connections-only result gracefully and must not assume completeness. Mark every Direction-B result set `completeness: unknown`.

#### 3.4 The same facet exists outside Sales Navigator

The mutual-connections link on any profile navigates to an ordinary people-search URL using the same facet:

```
https://www.linkedin.com/search/results/people/?facetNetwork=%5B%22F%22%5D&facetConnectionOf=%5B%22<memberUrn>%22%5D&origin=SHARED_CONNECTIONS_CANNED_SEARCH
```

Newer builds use the un-prefixed `connectionOf=` and `network=["F"]` parameter names. `F` = 1st degree. The free "All filters" panel also exposes a **Connections of** input, subject to the same 1st-degree requirement. **[MED]** — sources support the free-tier filter's existence but none is authoritative; verify in-product before relying on it.

Sources: [apify — LinkedIn Shared Connections](https://apify.com/data_link_miner/linkedin-shared-connections) · [scrapedin issue #120](https://github.com/linkedtales/scrapedin/issues/120) · [LinkedIn Help a545948](https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections) · [skylead — LinkedIn filters](https://skylead.io/blog/linkedin-filters-for-better-prospecting-a-step-by-step-guide/)

**But the free tier cannot carry this workload.** The Commercial Use Limit throttles free accounts at roughly **250–350 people searches per month**, resets at midnight PST on the 1st, is not displayed as a counter, and cannot be lifted on request. For |M| in the thousands, a free account would need most of a year. ([LinkedIn Help — Commercial use limit](https://www.linkedin.com/help/linkedin/answer/a564226) · [phantombuster](https://phantombuster.com/blog/social-selling/linkedin-commercial-use-limit/) · [connectsafely.ai — limits 2026](https://connectsafely.ai/articles/linkedin-limitations-restrictions-guide-2026))

This is the entire economic case for Sales Navigator Core: **US$119.99 (or free on the 30-day trial) buys removal of the Commercial Use Limit**, which is the only thing standing between us and a complete Direction-B pass.

#### 3.5 Other Sales Navigator facets

- **Current company / Past company** — multi-select, entity-backed (resolves to LinkedIn company IDs, not free text). Essential for defining `T`. Hand the entity-resolution problem to R5.
- **Spotlights** — segmentation overlays on a result set: *Changed jobs* (last 3 months), *Shared experiences* (same school / same past employer / same LinkedIn Group as you), *LinkedIn activity* (posted in last 30 days), *Mentioned in the news*, *Following your company*. **"Shared experiences" is directly the degraded-mode inference signal the handoff plan asks for**, computed by LinkedIn, surfaced as a filter — but it is *your* shared experience with the lead, not `m`'s with `t`. Useful for ranking, not for edges. ([LinkedIn Help — Spotlights](https://www.linkedin.com/help/sales-navigator/answer/a105146) · [expandi](https://expandi.io/blog/linkedin-sales-navigator-filters/))
- **TeamLink / TeamLink Extend** (Advanced+) — surfaces which of your *colleagues* is 1st-degree with a lead, and TeamLink Extend can include up to 1,000 non-Sales-Navigator colleagues who opt in. Notably, TeamLink Extend **overrides the colleague's connection-visibility setting** for introduction-path purposes. Powerful, and structurally the closest thing LinkedIn sells to what we want — but it requires a **team account with multiple licences under one contract**. Useless to a single user. ([LinkedIn Help — TeamLink Extend](https://www.linkedin.com/help/sales-navigator/answer/a107021) · [evaboot](https://evaboot.com/blog/linkedin-teamlink-extend) · [derrick-app](https://derrick-app.com/linkedin/sales-navigator/teamlink-filter))
- **Lead lists / account lists** — saving results to a list is sanctioned and persistent. Since CSV export is gone, a saved list is the only durable first-party artifact. It still has to be read out of the UI by hand.

#### 3.6 CSV export — what changed

**Per LinkedIn's own help documentation, CSV/XLS export is not available for Sales Navigator data as of 1 July 2026.** CRM Sync (Advanced Plus, limited CRM list) is the sanctioned alternative. Core and Advanced have **no native CSV or Excel export** for lead lists.

Every third-party "Sales Navigator exporter" — Evaboot, Wiza, Linked Helper, PhantomBuster, NavBulk, SalesNav Exporter — is a browser extension or headless scraper operating in direct violation of §8.2, and is now operating against a platform that actively fingerprints extensions (§5.5).

Sources: [thunderbit](https://thunderbit.com/blog/export-list-from-sales-navigator) · [cleanlist.ai — export methods 2026](https://www.cleanlist.ai/blog/2026-04-25-how-to-export-linkedin-sales-navigator-data) · [leadcrm.io](https://www.leadcrm.io/resources/export-leads-linkedin-sales-navigator/) · [connectsafely.ai](https://connectsafely.ai/articles/export-leads-linkedin-sales-navigator-guide-2026)

**Build consequence:** there will be no Sales Navigator CSV to parse. The Direction-B adapter must accept **hand-pasted / clipboard-captured result rows**, not a file download.

---

### 4. LinkedIn Recruiter and Recruiter Lite

| | Recruiter Lite | Recruiter Corporate |
|---|---|---|
| Price | ~US$170/mo single licence; ~US$1,680–2,040/yr; ~US$270/licence/mo for 2–5 users | Enterprise, materially higher |
| Network reach | **1st, 2nd, 3rd degree only** | Entire LinkedIn network |
| Filters | ~21 | More |
| InMail | 30/mo | Higher |
| Network facet | **"Network Relationships" — degree only (1st / 2nd / 3rd / group members)** | Same facet, wider corpus |

**Recruiter exposes nothing for network-adjacency that Sales Navigator does not.** Its network facet is a degree filter, not a "connections of person X" facet. There is no Recruiter equivalent of the `connectionOf` surface, no mutual-connection filter, and no TeamLink analogue at the Lite tier. Recruiter Lite is *more* restrictive than Sales Navigator on reach (capped at 3rd degree) while costing more than Sales Navigator Core.

**Verdict: reject both.** Recruiter is strictly dominated by Sales Navigator Core for this problem.

Sources: [LinkedIn Recruiter Help — Your network and degrees of connection](https://www.linkedin.com/help/recruiter/answer/a545636) · [LinkedIn Recruiter Help — 2nd and 3rd degree in search](https://www.linkedin.com/help/recruiter/answer/a528043) · [leonar.app — Recruiter filters 2026](https://www.leonar.app/blog/linkedin-recruiter-search-filters/) · [pin.com — Recruiter pricing 2026](https://www.pin.com/blog/linkedin-recruiter-pricing-2026/) · [evaboot — Recruiter vs Sales Navigator](https://evaboot.com/blog/linkedin-recruiter-vs-sales-navigator) · [trykondo.com](https://www.trykondo.com/blog/linkedin-recruiter-guide)

---

### 5. Terms of Service and legal posture

#### 5.1 User Agreement §8.2 ("Don'ts")

The operative clause, quoted:

> "Develop, support or use software, devices, scripts, robots or any other means or processes (including crawlers, browser plugins and add-ons or any other technology) to scrape the Services or otherwise copy profiles and other data from the Services."

Note the breadth: **"browser plugins and add-ons"** is named explicitly. There is no "but I was logged in and it was my own browser" carve-out. The §8.2 list also prohibits bypassing access controls, using bots or automated methods generally, and reverse-engineering the Services. LinkedIn maintains a separate help page, *Prohibited software and extensions*, enumerating tool classes that trigger enforcement: software that copies profile data, sends connection requests, sends or redirects messages, or "accesses the service in ways a human would not."

Sources: [LinkedIn Help — Prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions?lang=en) · [LinkedIn Help — Automated activity on LinkedIn](https://www.linkedin.com/help/linkedin/answer/a1340567) · [scrupp.com — LinkedIn API restrictions](https://scrupp.com/blog/linkedin-api-restrictions-and-what-scrapers-can-and-cannot-do) · [connectsafely.ai — automation ToS 2026](https://connectsafely.ai/articles/is-linkedin-automation-safe-tos-scraping-guide-2026)

#### 5.2 `robots.txt`

`https://www.linkedin.com/robots.txt` ends with a blanket exclusion and a whitelist notice:

```
User-agent: *
Disallow: /
```

> "Notice: If you would like to crawl LinkedIn, please email whitelist-crawl@linkedin.com to apply for white listing."

(The file also carries per-agent blocks and an extensive path-level `Disallow` list above the wildcard.) There is no interpretation under which a crawler of `/search/results/people/` or `/in/*` is permitted. `robots.txt` is not itself law, but post-hiQ it is the evidence a plaintiff uses to establish that access was unauthorised **as a contract matter**, which is the theory that actually wins. **[MED]** on exact current wording — could not fetch the live file.

#### 5.3 Case-law timeline and where it lands in 2026

| Date | Event | Holding / outcome |
|---|---|---|
| 2017 | hiQ Labs v. LinkedIn filed; N.D. Cal. grants preliminary injunction | LinkedIn ordered not to block hiQ from public profiles |
| Sep 2019 | 9th Cir. affirms PI | Scraping public data likely not "without authorization" under CFAA |
| Jun 2021 | *Van Buren v. United States* (S. Ct.) | "Gates-up-or-down" reading of "exceeds authorized access"; hiQ GVR'd |
| **18 Apr 2022** | **9th Cir. affirms again on remand** | **"hiQ raised a serious question as to whether the CFAA 'without authorization' concept is inapplicable where… prior authorization is not generally required but a particular person — or bot — is refused access." On a public website there are no gates, so accessing public data cannot violate the CFAA.** ([Justia](https://law.justia.com/cases/federal/appellate-courts/ca9/17-16783/17-16783-2022-04-18.html) · [EFF](https://www.eff.org/deeplinks/2022/04/scraping-public-websites-still-isnt-crime-court-appeals-declares) · [Proskauer](https://www.proskauer.com/blog/taking-cue-from-the-supreme-courts-van-buren-decision-ninth-circuit-releases-new-opinion-holding-scraping-of-publicly-available-website-data-falls-outside-of-cfaa)) |
| **Nov 2022** | **N.D. Cal. grants LinkedIn summary judgment on breach of contract** | hiQ's scraping and use of fake profiles breached the User Agreement |
| **8 Dec 2022** | **Consent judgment and permanent injunction** | **$500,000 judgment against hiQ; permanent injunction to cease scraping and delete all source code, data and algorithms obtained in violation of the User Agreement; liability established for trespass to chattels and misappropriation.** hiQ ceased operations. ([Privacy World](https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/) · [Morgan Lewis](https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators) · [Proskauer](https://newmedialaw.proskauer.com/2022/12/08/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case/) · [ZwillGen](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/)) |
| 1 Feb 2022 | **LinkedIn v. Mantheos Pte. Ltd.** (N.D. Cal.) filed | Alleged scraping of millions of profiles using hundreds of fake accounts and prepaid debit cards under fake names to obtain Sales Navigator access |
| May 2022 | Mantheos settles; consent judgment (Judge Gilliam) | Permanent deletion of all scraped data, destruction of scraping software, permanent bar on scraping and on selling the data. Defendants denied liability. ([Law Street](https://lawstreetmedia.com/news/tech/linkedin-settles-data-scraping-suit-with-singapore-based-mantheos/) · [Bloomberg Law](https://news.bloomberglaw.com/privacy-and-data-security/linkedin-settles-data-scraping-lawsuit-against-mantheos)) |
| **27 Jan 2025** | **LinkedIn Corp. v. Nubela Pte. Ltd.** (3:25-cv-00828, N.D. Cal.) — Proxycurl's parent | Six claims: breach of contract, fraud and deceit, CFAA, California UCL, Lanham Act, misappropriation. Alleged hundreds of thousands of fake accounts. ([Justia docket](https://dockets.justia.com/docket/california/candce/3:2025cv00828/443258) · [Law.com](https://www.law.com/therecorder/2025/01/27/linkedin-suit-says-millions-of-profiles-scraped-by-singapore-firms-fake-accounts/)) |
| **4 Jul 2025** | **Proxycurl settles and shuts down** | Confidential settlement; product wound down. Founder's stated reason: the American Rule means winning still costs you your fees, against a Microsoft-owned adversary. ([nubela.co — Proxycurl Shuts Down](https://nubela.co/blog/goodbye-proxycurl/) · [nubela.co — Is Scraping LinkedIn Legal in 2026?](https://nubela.co/blog/is-scraping-linkedin-legal-in-2026/)) |
| **28 Jul 2025** | Permanent injunction entered | Court requires Proxycurl to delete LinkedIn data obtained by unauthorized means |

**What is actually settled in 2026:**

- **Criminal/CFAA exposure for scraping genuinely public pages is low** in the Ninth Circuit. That is the hiQ holding and it stands. But note the holding was on a *preliminary injunction* standard ("serious questions"), not a final merits judgment.
- **Contract exposure is the live risk, and LinkedIn wins it.** Every case since hiQ has been resolved on breach-of-contract and fraud theories, not CFAA. The pattern is identical each time: injunction, mandatory deletion, destruction of code, and — where litigated — money.
- **The CFAA shield evaporates entirely once you are logged in.** hiQ scraped *logged-out* public profiles. Mutual connections are visible only to an authenticated member. Accessing them programmatically means you accepted the User Agreement, you passed a gate, and you are squarely in breach-of-contract territory with a plausible CFAA overlay. **Nothing in hiQ protects an authenticated scraper.** This is the single most important legal distinction for this project.
- **Fake accounts are the aggravating factor** in Mantheos and Nubela. Neither case is about a person analysing their own network. But neither is the injunction relief narrow.

#### 5.4 The four acquisition modes, distinguished and rated

| Mode | What it is | Account risk | Legal risk | Notes |
|---|---|---|---|---|
| **(a) Exporting your own data** | Settings → Get a copy of your data | **None.** LinkedIn built the feature. | **None.** Your own personal data; GDPR Art. 15/20 right. | Downstream *use* of third-party data in `Connections.csv` still engages GDPR as a controller — minimise, store locally, honour deletion. |
| **(b) Manually viewing pages in your own browser** | Human clicks, human reads, human copies a handful of rows | **Very low.** Explicitly not prohibited: "What LinkedIn does not prohibit: using their website from your own browser." Commercial Use Limit applies on free tier; Sales Navigator removes it. Pathological volume could still trip behavioural heuristics. | **Very low.** No §8.2 violation. Copying a small volume by hand is not "software, devices, scripts, robots." | **This is the sanctioned path.** It is slow. That is the price. |
| **(c) Browser-extension-assisted capture during human browsing** | Extension injects into LinkedIn pages and harvests the DOM while a human drives | **HIGH — and materially higher in 2026 than in 2024.** §8.2 names "browser plugins and add-ons" verbatim. LinkedIn's *Prohibited software and extensions* page targets exactly this. Per the April 2026 BrowserGate investigation, LinkedIn probes for **6,167 extensions** (up from 5,459 in Dec 2025, ~12/day growth) and builds a **48-signal device fingerprint attached to every API request**. Detection is not probabilistic; it is instrumented. | **Medium.** Breach of contract. Individual analysts are not the litigation target — vendors are — but the injunctive template exists. | LinkedIn permanently removed HeyReach's company page and banned its founder's profile in March 2026. Do not ship this as a default. |
| **(d) Headless automated scraping** | Playwright/Puppeteer/HTTP client against LinkedIn, authenticated or not | **VERY HIGH.** Authenticated automation is the highest-confidence detection signal there is: session behaviour that does not match a human. Expect restriction then permanent suspension. | **HIGH.** Directly the conduct enjoined in hiQ, Mantheos, and Nubela. Authenticated access removes the hiQ CFAA shield. If it involves a second account, it is the *fraud* fact pattern. | **Never build this.** Not as an option, not behind a flag, not with a warning. |

Sources: [bleepingcomputer — LinkedIn scans 6,000+ extensions](https://www.bleepingcomputer.com/news/security/linkedin-secretly-scans-for-6-000-plus-chrome-extensions-collects-data/) · [thenextweb — BrowserGate](https://thenextweb.com/news/linkedin-browsergate-extension-scanning-privacy-fingerprint) · [securityaffairs — LinkedIn BrowserGate](https://securityaffairs.com/191383/security/linkedin-browsergate.html) · [cxtoday — BrowserGate lawsuits](https://www.cxtoday.com/security-privacy-compliance/linkedin-browsergate-privacy-lawsuit/) · [techtimes — BrowserGate, 6 Apr 2026](https://www.techtimes.com/articles/315683/20260406/linkedin-browsergate-investigation-alleges-secret-browser-extension-scanning-within-platform.htm) · [linkedinsider — automation crackdown 2026](https://linkedinsider.blog/linkedin-automation-crackdown-2026) · [northlight.ai](https://northlight.ai/blog/is-linkedin-automation-against-the-rules) · [profilespider — restricted for scraping](https://profilespider.com/blog/linkedin-restricted-my-account-for-scraping)

#### 5.5 One further note on BrowserGate

Two class-action privacy suits were filed against LinkedIn in California federal court on 7–8 April 2026 over the extension-scanning and fingerprinting. LinkedIn's only public response was a comment from a "LinkedIn Help" account on Hacker News framing it as anti-scraping security. This litigation does **not** help us — it constrains LinkedIn's telemetry, not its enforcement rights, and it may well make LinkedIn more aggressive about defending the anti-scraping justification. Treat the fingerprinting as a permanent fact of the environment.

---

## Options table

| # | Path | What it yields | Cost | Effort | Account risk | Legal risk | Gives the bipartite edges `E`? |
|---|---|---|---|---|---|---|---|
| 1 | **Official data export — fast file, Connections only** | `M`: 7-column list of all 1st-degree connections | Free | ~10 min wait, one CSV parse | **None** | **None** | **No** — vertices only |
| 2 | Official data export — full archive | `M` + endorsements, recommendations, messages, invitations, groups, ad-targeting inferences | Free | 24–72 h wait, many parsers | **None** | **None** (but ingesting `messages.csv`/`Contacts.csv` creates real GDPR exposure) | **No** — weak intra-`M` weights only |
| 3 | GDPR Art. 15 DSAR for graph data | Nothing beyond #2 | Free | Weeks, near-certain refusal | None | None | **No** — Art. 15(4) bars it |
| 4 | Member Data Portability API (DMA) | Same content class as #2, via API, EEA members only | Free | Developer app + EEA eligibility | None | None | **No** |
| 5 | Sign In with LinkedIn / OIDC | Name, photo, email of the signed-in user | Free | Low | None | None | **No** |
| 6 | Connections API (`r_1st_connections`) | `M` — a worse `Connections.csv` | Partner-gated | Unobtainable | n/a | n/a | **No** |
| 7 | Connections Size API (`r_1st_connections_size`) | An integer | Partner-gated | Unobtainable | n/a | n/a | **No** |
| 8 | Marketing Developer Platform / Community Mgmt / Lead Sync | Ads and page analytics | Free tier exists; needs verified company page | 4 wks–4 mo | None | None | **No** |
| 9 | SNAP / Sales Navigator API | n/a | **Closed to new partners** | n/a | n/a | n/a | **No** |
| 10 | Talent Solutions / RSC | ATS integration | Partner-gated, <10% approval, 3–6 mo | Unobtainable | n/a | n/a | **No** |
| 11 | **Direction A — manual mutual-connections browsing** (`connectionOf=t`, free or Sales Nav) | For each `t` visited: `M ∩ connections(t)` | Free (≤~300 searches/mo) or SN Core $119.99/mo | **High** — ~10,000 page visits for full `T` | **Low** (manual) | **Low** | **YES — directly** |
| 12 | **Direction B — manual `Connections of m` + company filter** (Sales Navigator Core) | For each `m ∈ M`: `{t ∈ T : (m,t) ∈ E}` | **$119.99/mo, free on 30-day trial** | **Medium** — ~500–3,000 searches, small result sets | **Low** (manual) | **Low** | **YES — directly, and ~10× cheaper than #11** |
| 13 | Recruiter Lite | Degree filter only | ~$170/mo | Medium | Low | Low | **No** |
| 14 | Recruiter Corporate | Degree filter only, wider corpus | Enterprise | High | Low | Low | **No** |
| 15 | Sales Navigator Advanced — TeamLink / TeamLink Extend | Which *teammates* are 1st-degree with a lead; overrides their privacy setting | $159.99/seat/mo **and requires a multi-seat team contract** | High | Low | Low | Partially, but only for a *team's* network — not applicable to a solo user |
| 16 | Sales Navigator Advanced Plus — CRM Sync | Bulk egress of saved lists into a supported CRM | ~$1,300–1,600/seat/yr | High | Low | Low | Only as a transport for #12's results; does not itself produce edges |
| 17 | Browser-extension-assisted capture | Automates #11/#12 | Extension cost | Low effort, high consequence | **HIGH** — §8.2 names add-ons; 6,167-extension fingerprinting | **Medium** | Yes — **rejected on risk** |
| 18 | Headless automated scraping | Automates everything | Proxy/infra cost | Low effort, catastrophic consequence | **VERY HIGH** | **HIGH** — the enjoined conduct in hiQ/Mantheos/Nubela | Yes — **rejected outright** |
| 19 | Degraded inference mode (no `E`) | Affinity scores from shared employer / school / tenure overlap / group, from `Connections.csv` + public target attributes | Free | Medium | None | None | **No** — clearly-labelled inference, not edges |

---

## Recommendation

Build support for exactly four first-party paths, in this priority order.

**P0 — Official data export, fast file, Connections checkbox. (Option 1.)**
Mandatory, non-optional backbone. It defines `M`, and every downstream identifier resolves against it. Zero cost, zero risk, ten minutes. The parser must be robust to the preamble quirk, to blank emails, and to encoding variance. Ship this first; it is the only component that can be fully tested against a real artifact on day one.

**P1 — Direction B: `Connections of m` × `Current company ∈ T-firms`, on Sales Navigator Core, driven by a human. (Option 12.)**
This is the primary edge-acquisition path and it should be what the tool is *designed around*. The system generates a work queue of pre-built search URLs (one per `m ∈ M`, with the three company filters pre-applied), the human opens them at their own pace, and the tool provides a fast capture surface for the small result sets — paste-a-block, or a clipboard watcher, explicitly **not** an injected extension. Persist progress so the queue survives across sessions and across the 30-day trial boundary.

Why P1 and not P2: ~10× fewer requests than Direction A, server-side data minimisation (we never touch non-target people), and the "must be 1st-degree" constraint is automatically satisfied for every element of `M`.

**P2 — Direction A: mutual-connections surface for a prioritised slice of `T`. (Option 11.)**
The backfill. Covers the `m` who hid their connection lists, which Direction B is blind to. Same human-paced capture surface, same edge schema, different `source_direction`. Feed it a *prioritised* target list from R5 — do not attempt all ~10,000.

**P3 — Degraded inference mode. (Option 19.)**
Must exist and must run by default when `E` is empty, so the tool produces value from `Connections.csv` alone. Output must be labelled `inferred`, never merged into the same field as an observed edge, and never rendered in a way that lets a reader mistake one for the other.

**Tier guidance:** Sales Navigator **Core**, not Advanced. The search filters are identical; Advanced only adds TeamLink and Smart Links, which a solo user cannot use. Start on the 30-day free trial — for a typical `M` of 500–1,500 that is enough time for a complete Direction-B pass at a sane pace.

---

## Rejected

| Rejected | Reason |
|---|---|
| **Any LinkedIn API adapter** | Nothing to call. `r_1st_connections` duplicates the export at partner-only cost; `r_1st_connections_size` is a count; SNAP is closed to new partners; MDP is EEA-only and returns the same first-person data as the archive. No connection-graph scope exists at any tier or price. Building an API client would be pure dead code. |
| **`r_1st_connections_size` specifically** | Returns an integer for the authenticated member. Cannot produce an edge and cannot describe a target. |
| **GDPR Art. 15 DSAR for graph data** | Art. 15(4) and EDPB Guidelines 01/2022 give LinkedIn a clean basis to refuse third-party graph data. Weeks of latency for a near-certain no. |
| **Recruiter and Recruiter Lite** | Degree-only network facet, no `connectionOf` equivalent, capped at 3rd degree, costs more than Sales Navigator Core. Strictly dominated. |
| **TeamLink / TeamLink Extend** | Requires a multi-seat team contract. Structurally the closest LinkedIn product to what we want, and completely unavailable to a single user. |
| **Sales Navigator CSV export as an ingest format** | Removed. LinkedIn's help documentation states CSV/XLS export is unavailable for Sales Navigator data as of 1 July 2026. Any file claiming to be one came from a scraper. Do not write a parser for it. |
| **Browser extension for capture** | §8.2 names "browser plugins and add-ons" verbatim; LinkedIn maintains a *Prohibited software and extensions* page; and as of April 2026 LinkedIn probes for 6,167 named extensions and fingerprints 48 device signals on every request. High account risk with an instrumented detector on the other side. If a power user insists, it must be a separate, unbundled, explicitly-warned component — never the default and never in the core repo's default install path. |
| **Headless / automated scraping of any kind** | The exact conduct enjoined in hiQ (2022, $500k + permanent injunction + code destruction), Mantheos (2022), and Nubela/Proxycurl (2025, shutdown + deletion injunction). hiQ's CFAA win does not apply: it concerned logged-out public pages, and mutual connections require authentication. Do not implement, do not flag-gate, do not document as possible. |
| **Ingesting `Contacts.csv`** | Address-book contacts, including people who never joined LinkedIn and never consented to anything. Contributes nothing to the graph. Pure liability. |
| **Ingesting `messages.csv` `CONTENT`** | Message bodies are the most sensitive data in the archive and carry no structural information. Thread *membership* (recipient URL lists) is the only part with analytical value, and even that should be opt-in and default-off. |
| **`Ad_Targeting.csv` as evidence** | LinkedIn's opaque, unversioned inferences about the ad-targetable user. The "company connections" category looks like an edge list and is not one. Using it as evidence would silently corrupt the output. |

---

## Implications for the build

Wave 2 codes directly from this section. Everything here is literal.

### A. `Connections.csv` parser — the one thing that must be bulletproof

Verbatim expected header, in order:

```
First Name,Last Name,URL,Email Address,Company,Position,Connected On
```

Required behaviour:

1. **Sniff the header row; do not hardcode `skiprows`.** Read up to the first 15 lines; select the first line that, when CSV-split, contains all of `First Name`, `Last Name`, `URL` (case-insensitive, whitespace-stripped). Treat everything above it as preamble and discard. Observed preamble length is 2–4 lines and has changed over time.
2. **Fail loudly with the discovered header** if no such row is found, rather than silently mis-parsing.
3. Encoding: try `utf-8-sig`, fall back to `utf-8`, then `latin-1`. LinkedIn has shipped BOMs.
4. Accept the file either as a loose `.csv` or as a member of the archive `.zip`; locate it case-insensitively (`connections.csv`).
5. `Connected On`: parse `"%d %b %Y"` first; fall back to `dateutil` permissive parsing; on failure store `None` and keep the row.
6. `Email Address`: expect mostly empty. Never treat emptiness as a parse error, never use email as a join key, never require it.
7. **Join key is a normalised `URL`.** Normalise: lowercase; strip scheme, `www.`, query string, fragment, and trailing slash; extract the `publicIdentifier` (the path segment after `/in/`); URL-decode it. Store both the raw URL and the normalised `public_id`. Rows with an empty `URL` get a synthetic id derived from a hash of name+company and are flagged `weak_identity=True`.
8. Deduplicate on `public_id`, keeping the earliest `Connected On`.

Canonical internal record:

```python
@dataclass(frozen=True)
class Connection:          # a member of M
    public_id: str         # normalised LinkedIn publicIdentifier — PRIMARY KEY
    profile_url: str       # raw URL column, verbatim
    first_name: str
    last_name: str
    email: str | None      # Email Address; usually None
    company: str | None    # Company column, free text, NOT normalised here
    position: str | None   # Position column, free text
    connected_on: date | None
    weak_identity: bool = False
```

### B. Other archive files — parse only these, and only if configured on

| Config key | File matched (case-insensitive) | Parse to | Default |
|---|---|---|---|
| `export.connections` | `Connections.csv` | `list[Connection]` | **on, mandatory** |
| `export.endorsements` | endorsement given/received CSVs | `list[Endorsement(from_public_id, to_public_id, skill)]` | on |
| `export.recommendations` | recommendation given/received CSVs | `list[Recommendation(from_public_id, to_public_id, text_present: bool)]` — store presence, not text | on |
| `export.invitations` | `Invitations.csv` | `list[Invitation(counterparty_public_id, direction, sent_at)]` | off |
| `export.company_follows` | `Company Follows.csv` | `list[CompanyFollow(company_name, followed_on)]` — feeds target entity resolution | on |
| `export.positions` | `Positions.csv` | `list[SelfPosition(company, title, start, end)]` — feeds tenure-overlap inference | on |
| `export.groups` | group-membership file | `list[str]` group names | on |
| `export.message_headers` | `messages.csv`, **headers only** | `list[ThreadMembership(conversation_id, participant_public_ids, date)]` | **off** |
| — | `Contacts.csv` | **never parsed** | n/a — hard-excluded |
| — | `messages.csv` `CONTENT` column | **never read into memory** | n/a — hard-excluded |
| — | `Ad_Targeting.csv` | **never used as evidence** | n/a — hard-excluded |

`messages.csv` column names, verbatim, for the header-only reader (read `CONVERSATION ID`, `FROM`, `SENDER PROFILE URL`, `TO`, `RECIPIENT PROFILE URLS`, `DATE`; **drop everything else, including `CONTENT`, `SUBJECT`, `ATTACHMENTS`, at the reader boundary so bodies never enter a dataframe**):

```
CONVERSATION ID, CONVERSATION TITLE, FROM, SENDER PROFILE URL, TO,
RECIPIENT PROFILE URLS, DATE, SUBJECT, CONTENT, FOLDER, ATTACHMENTS, IS MESSAGE DRAFT
```

`RECIPIENT PROFILE URLS` is a delimited multi-value field — split, normalise each to `public_id`, and treat >2 participants as a co-occurrence group.

Every non-`Connections.csv` parser must degrade to an empty list if the file is absent, with a single INFO log line. Archive contents vary by account age and locale; **absence is normal, not an error.**

### C. Acquisition adapter interface

One interface, two implementations, plus a null implementation for the compliance-default path.

```python
class CompliancePosture(Enum):
    FIRST_PARTY_EXPORT = "first_party_export"   # zero risk
    MANUAL_BROWSING    = "manual_browsing"      # human-paced, sanctioned
    INFERENCE_ONLY     = "inference_only"       # no acquisition at all

@dataclass(frozen=True)
class BridgeEdge:
    m_public_id: str            # member of M
    t_public_id: str            # member of T
    t_company: str              # as displayed at capture time
    t_title: str | None
    observed_at: datetime
    source_direction: Literal["A_mutual_of_target", "B_connections_of_member"]
    source_url: str             # the exact search/profile URL the human visited — audit trail
    completeness: Literal["complete", "partial", "unknown"] = "unknown"

class EdgeSource(Protocol):
    posture: CompliancePosture
    def plan(self, M: list[Connection], T: TargetSet) -> list[WorkItem]: ...
    def ingest(self, item: WorkItem, payload: str) -> list[BridgeEdge]: ...
```

- `WorkItem` carries the pre-built LinkedIn URL, the direction, and the anchor id (`m_public_id` for B, `t_public_id` for A). The tool **generates URLs and never fetches them.** Network egress from the acquisition module must be structurally impossible — enforce with a guardrail test.
- `ingest` takes a **human-supplied paste** (block of copied text, or a saved-page HTML fragment) and parses it. No HTTP client in this module.
- **Guardrail test (must exist):** assert that no module under the acquisition package imports `requests`, `httpx`, `urllib.request`, `aiohttp`, `selenium`, `playwright`, or `bs4`-plus-network. Assert `CompliancePosture` of the default configuration is `INFERENCE_ONLY` or `FIRST_PARTY_EXPORT`, never anything else.

### D. URL construction

Direction B (primary) — Sales Navigator lead search:

```
https://www.linkedin.com/sales/search/people?query=(
  filters:List(
    (type:CONNECTION_OF, values:List((id:<m_member_urn_or_public_id>))),
    (type:CURRENT_COMPANY, values:List((id:<company_id_JS>),(id:<company_id_CIT>),(id:<company_id_CITSEC>)))
  )
)
```

**[VERIFY IN-PRODUCT]** — Sales Navigator's query DSL is undocumented and changes. Wave 2 must make this a **template string in config**, not a hardcoded literal, so the user can paste in a working URL captured from their own browser and have the tool substitute the `m` id. Config key: `acquisition.sales_nav_url_template`.

Direction A (backfill) — classic people search / mutual-connections surface:

```
https://www.linkedin.com/search/results/people/?facetNetwork=%5B%22F%22%5D&facetConnectionOf=%5B%22<t_id>%22%5D
```

with a fallback to the newer un-prefixed parameter names:

```
https://www.linkedin.com/search/results/people/?network=%5B%22F%22%5D&connectionOf=%22<t_id>%22
```

Both go in config as templates: `acquisition.mutual_url_template`, `acquisition.mutual_url_template_legacy`.

### E. Config keys implied

```yaml
export:
  archive_path: null            # path to .zip or extracted dir
  connections: true             # mandatory
  endorsements: true
  recommendations: true
  invitations: false
  company_follows: true
  positions: true
  groups: true
  message_headers: false        # never defaults to true

acquisition:
  posture: inference_only       # inference_only | first_party_export | manual_browsing
  direction: B                  # B | A | hybrid
  sales_nav_url_template: "..." # user-pasteable
  mutual_url_template: "..."
  mutual_url_template_legacy: "..."
  work_queue_path: "data/work_queue.jsonl"   # resumable across sessions
  target_priority_limit: 500    # cap on Direction-A backfill
  human_pace_reminder: true     # UI nudge; the tool never auto-advances

privacy:
  local_only: true              # hard-coded true; no remote sink may be configured
  retention_days: 90            # auto-purge captured third-party data
  redact_names_in_output: false
  purge_on_exit: false

inference:
  enabled: true                 # degraded mode; always available
  signals: [shared_employer, shared_school, tenure_overlap, shared_group]
  label_as_inferred: true       # hard-coded true
```

### F. Non-negotiables for Wave 2

1. **Default configuration must be `posture: inference_only`.** The tool must produce useful output with nothing but `Connections.csv` and never require the user to opt into anything riskier.
2. **No HTTP client anywhere in the acquisition path.** Enforced by test, not by convention.
3. **Observed edges and inferred affinities are separate types with separate fields**, and must remain distinguishable through every layer including the final report.
4. **Every `BridgeEdge` carries `source_url` and `observed_at`** so the user can reconstruct exactly what was looked at and when — this is the compliance log, and it is also what makes a deletion request answerable.
5. **The work queue is resumable.** A 1,500-item Direction-B pass takes days of human time. Losing progress is a product failure.
6. **Do not write a Sales Navigator CSV parser.** There is no such export as of 1 July 2026.
7. **Do not hardcode `skiprows=3`.**

---

## Source list

LinkedIn export: [connectsafely.ai](https://connectsafely.ai/articles/export-linkedin-contacts-connections-guide-2026) · [networkcleaner.com](https://www.networkcleaner.com/blog/export-linkedin-connections-csv) · [espirian.co.uk](https://espirian.co.uk/linkedin-data-archive/) · [magicpost.in](https://magicpost.in/blog/export-linkedin-contacts) · [lagrowthmachine.com](https://lagrowthmachine.com/export-linkedin-contacts/) · [getboothiq.com](https://getboothiq.com/blog/how-to-export-linkedin-contacts) · [salesrobot.co](https://www.salesrobot.co/blogs/export-linkedin-contacts) · [linkedhelper.com — 1st/2nd/3rd degree](https://www.linkedhelper.com/blog/how-to-download-1st-2nd-3rd-degree-linkedin-connections-to-excel-2/) · [bradley-schoeneweis](https://bradley-schoeneweis.medium.com/visualizing-your-linkedin-connections-with-python-pandas-networkx-pyvis-40bf846a532) · [krischase.com](https://www.krischase.com/blog/build-your-own-networkexplorer-linkedin-data-visualization) · [JasonToups/linkedin-export-sorting](https://github.com/JasonToups/linkedin-export-sorting) · [socialmediaexaminer.com](https://www.socialmediaexaminer.com/linkedin-data-export-tool/) · [LinkedIn Help a1339364](https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data) · [LinkedIn Help a566336](https://www.linkedin.com/help/linkedin/answer/a566336/export-connections-from-linkedin)

APIs: [learn.microsoft.com — Connections API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api) · [Connections Size](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-size) · [Getting Access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access) · [Increasing Access](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-07) · [Marketing API FAQ](https://learn.microsoft.com/en-us/linkedin/marketing/lms-faq?view=li-lms-2026-06) · [SNAP docs](https://learn.microsoft.com/en-us/linkedin/sales/) · [Become a SNAP Partner](https://business.linkedin.com/sales-solutions/partners/become-a-partner) · [MDP 3rd Party](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-3rd-party/?view=li-dma-data-portability-2025-11) · [MDP Member](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member/?view=li-dma-data-portability-2026-05) · [Member Changelog API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api) · [getphyllo — API access 2026](https://www.getphyllo.com/post/linkedin-api-access-in-2026-partner-program-approval-timeline-alternatives) · [scale.jobs — ATS partner odds](https://scale.jobs/blog/linkedin-api-integration-with-ats-step-by-step-guide) · [developer.linkedin.com Transition FAQ](https://developer.linkedin.com/blog/posts/2015/transition-faq)

Sales Navigator / Recruiter: [skylead — SN filters 2026](https://skylead.io/blog/linkedin-sales-navigator-filters/) · [Leadscope](https://leadscope.zendesk.com/hc/en-us/articles/30181045132945-How-to-Use-LinkedIn-and-Sales-Navigator-Filters) · [LinkedIn Help — Who can see your connections](https://www.linkedin.com/help/sales-navigator/answer/a540663) · [LinkedIn Help a545948](https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections) · [LinkedIn Help — search results limit](https://www.linkedin.com/help/sales-navigator/answer/a106030) · [evaboot — >2500](https://evaboot.com/blog/see-more-2500-leads-linkedin-sales-navigator-search) · [LinkedIn Help — Spotlights](https://www.linkedin.com/help/sales-navigator/answer/a105146) · [LinkedIn Help — TeamLink Extend](https://www.linkedin.com/help/sales-navigator/answer/a107021) · [cleanlist — SN pricing 2026](https://www.cleanlist.ai/blog/2026-05-08-linkedin-sales-navigator-pricing-guide) · [skrapp — SN cost](https://skrapp.io/blog/linkedin-sales-navigator-cost-and-pricing/) · [thunderbit — export](https://thunderbit.com/blog/export-list-from-sales-navigator) · [cleanlist — export methods](https://www.cleanlist.ai/blog/2026-04-25-how-to-export-linkedin-sales-navigator-data) · [LinkedIn Recruiter Help a545636](https://www.linkedin.com/help/recruiter/answer/a545636) · [LinkedIn Recruiter Help a528043](https://www.linkedin.com/help/recruiter/answer/a528043) · [pin.com — Recruiter pricing](https://www.pin.com/blog/linkedin-recruiter-pricing-2026/) · [LinkedIn Help — commercial use limit](https://www.linkedin.com/help/linkedin/answer/a564226)

Legal: [LinkedIn Help — Prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions?lang=en) · [LinkedIn Help — Automated activity](https://www.linkedin.com/help/linkedin/answer/a1340567) · [Justia — hiQ 9th Cir. 2022](https://law.justia.com/cases/federal/appellate-courts/ca9/17-16783/17-16783-2022-04-18.html) · [EFF](https://www.eff.org/deeplinks/2022/04/scraping-public-websites-still-isnt-crime-court-appeals-declares) · [Proskauer — Van Buren/hiQ](https://www.proskauer.com/blog/taking-cue-from-the-supreme-courts-van-buren-decision-ninth-circuit-releases-new-opinion-holding-scraping-of-publicly-available-website-data-falls-outside-of-cfaa) · [Privacy World — consent judgment](https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/) · [Morgan Lewis](https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators) · [ZwillGen](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/) · [Law Street — Mantheos settlement](https://lawstreetmedia.com/news/tech/linkedin-settles-data-scraping-suit-with-singapore-based-mantheos/) · [Bloomberg Law — Mantheos](https://news.bloomberglaw.com/privacy-and-data-security/linkedin-settles-data-scraping-lawsuit-against-mantheos) · [Justia docket — LinkedIn v. Nubela](https://dockets.justia.com/docket/california/candce/3:2025cv00828/443258) · [Law.com — Nubela suit](https://www.law.com/therecorder/2025/01/27/linkedin-suit-says-millions-of-profiles-scraped-by-singapore-firms-fake-accounts/) · [nubela.co — Proxycurl shutdown](https://nubela.co/blog/goodbye-proxycurl/) · [nubela.co — Is scraping legal 2026](https://nubela.co/blog/is-scraping-linkedin-legal-in-2026/) · [bleepingcomputer — extension scanning](https://www.bleepingcomputer.com/news/security/linkedin-secretly-scans-for-6-000-plus-chrome-extensions-collects-data/) · [thenextweb — BrowserGate](https://thenextweb.com/news/linkedin-browsergate-extension-scanning-privacy-fingerprint) · [securityaffairs — BrowserGate](https://securityaffairs.com/191383/security/linkedin-browsergate.html) · [cxtoday — BrowserGate lawsuits](https://www.cxtoday.com/security-privacy-compliance/linkedin-browsergate-privacy-lawsuit/) · [techtimes — BrowserGate](https://www.techtimes.com/articles/315683/20260406/linkedin-browsergate-investigation-alleges-secret-browser-extension-scanning-within-platform.htm) · [gdpr-info.eu Art.15](https://gdpr-info.eu/art-15-gdpr/) · [EDPB Guidelines 01/2022](https://www.edpb.europa.eu/system/files/2022-01/edpb_guidelines_012022_right-of-access_0.pdf) · [Arthur Cox — Art.15(4)](https://www.arthurcox.com/knowledge/balancing-gdpr-data-access-rights-against-the-rights-of-others/)
