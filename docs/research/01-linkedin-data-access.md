# 01 — LinkedIn Data Access: What Is Actually Obtainable, How, and At What Risk

**Author:** Wave 1 / Agent 1 (research fleet)
**Date:** 2026-08-21
**Scope:** Feasibility of identifying clusters of the user's own 1st-degree LinkedIn connections who are *also* in the direct (1st-degree) network of Jane Street and Citadel employees.

---

## 0. Research method, provenance, and confidence conventions

**Method.** All findings below were gathered by live web research on 2026-08-21. A hard constraint applies to this report's provenance: **the sandbox egress proxy blocked direct fetches of `linkedin.com`, `learn.microsoft.com`, `developer.linkedin.com`, `en.wikipedia.org`, `web.archive.org`, and most law-firm/blog domains.** Primary-source pages could not be read verbatim. Two channels *did* work and were used heavily:

1. **`raw.githubusercontent.com`** — fetched directly. Used to read open-source LinkedIn-export parsers, which encode exact CSV filenames and column headers observed in real archives. This is the strongest evidence in this report for Section 1.
2. **GitHub code search (global)** — used to corroborate column headers across many independent repositories. Agreement across ~15 unrelated repos is treated as high confidence.
3. **Web search result summaries** — used for everything else. These are *secondary* and in several cases the underlying sources are SEO marketing blogs from vendors with a commercial interest. Flagged accordingly.

**Confidence labels used throughout:**

- **[VERIFIED]** — corroborated by ≥2 independent sources, at least one of which is primary or near-primary (source code parsing real exports, court docket, official vendor doc).
- **[LIKELY]** — consistent across multiple secondary sources, no contradiction found, but not primary-verified.
- **[UNCERTAIN]** — single source, vendor-marketing source, or internally inconsistent evidence. **Do not build on these without re-verification.**
- **[UNVERIFIED — MUST CHECK]** — I could not establish this. A downstream agent must verify against the live product/docs before relying on it.

> **Standing instruction to the downstream pipeline agent:** the single highest-value verification step available is *the user downloading their own LinkedIn archive and you reading the actual file list and headers.* That collapses all Section 1 uncertainty to zero in ~10 minutes to 24 hours. Do that before writing parsing code against this document.

---

## 1. LinkedIn's own data export ("Get a copy of your data")

### 1.1 Where it lives and how the two tiers work

Path: **Settings & Privacy → Data Privacy → Get a copy of your data**.

There are two request modes [LIKELY, consistent across many sources]:

| Tier | UI label (approx.) | Contents | Latency | Availability window |
|---|---|---|---|---|
| **Fast file** | "Want something in particular?" — pick specific categories | A subset: articles, imported contacts, invitations, recommendations, **connections**, messages, profile, registration | **~10 minutes** | 72 hours |
| **Full archive** | "Download larger data archive" | Everything LinkedIn holds — the ~30–45 file set below | **~24 hours** (some report up to 48h) | 72 hours |

Key practical points:

- **`Connections.csv` is available in the FAST tier.** You do not need to wait 24h for the core dataset this project needs. [LIKELY]
- The full archive is where the *behavioural* files live (Reactions, Comments, Search Queries, Logins, Ad Targeting, Inferences). [VERIFIED — corroborated by the file-enum source code and multiple write-ups]
- The archive arrives as a **ZIP of CSVs (plus a few JSON and an `Articles/` folder)**. Delivered by email link. [LIKELY]
- **You only receive a file if it applies to your account.** Absent activity ⇒ absent file. Parsers must treat every file as optional. [VERIFIED]

### 1.2 Exact filename set

The most reliable enumeration comes from `humandataincome/hudi-packages-connectors`, an open-source GDPR-export connector library whose LinkedIn enum lists the filenames it has observed in real archives. Fetched verbatim from `raw.githubusercontent.com`:

```
Account Status History.csv
Ad_Targeting.csv
Ads Clicked.csv
Company Follows.csv
Connections.csv
Contacts.csv
Education.csv
Email Addresses.csv
Endorsement_Received_Info.csv        (also seen as "Endorsement Received Info.csv")
Inferences_about_you.csv
Invitations.csv
Job Applicant Saved Answers.csv
Job Applicant Saved Screening Question Responses.csv
Jobs/Job Applications.csv
Jobs/Job Seeker Preferences.csv
Jobs/Saved Jobs.csv
Learning.csv
Logins.csv
Member_Follows.csv
messages.csv                          (lowercase 'm' in this library; other sources show "Messages.csv")
PhoneNumbers.csv
Positions.csv
Profile.csv
Reactions.csv
Registration.csv
Rich Media.csv
SavedJobAlerts.csv
SearchQueries.csv
Security Challenges.csv
Skills.csv
Votes.csv
```

Source: <https://raw.githubusercontent.com/humandataincome/hudi-packages-connectors/00eeaa422dcc787cfa5418cf043010e6b085cb89/src/source/linkedin/enum.linkedin.ts>

Additional filenames reported elsewhere but **not** in that enum [LIKELY]: `Certifications.csv`, `Organizations.csv`, `Volunteering.csv`, `Shares.csv`, `Comments.csv`, `Recommendations_Received.csv`, `Recommendations_Given.csv`, `Honors.csv`, `Publications.csv`, `Causes You Care About.csv`, `Events.csv`, `Hashtag_Follows.csv`, `Receipts.csv`, `Mobile Applications.csv`, `Endorsement_Given_Info.csv`, and an `Articles/` folder.

> **[IMPORTANT — NAMING IS UNSTABLE]** Filenames drift between LinkedIn export versions and across accounts: underscores vs spaces (`Ad_Targeting.csv` vs `Ad Targeting.csv`), casing (`messages.csv` vs `Messages.csv`), and singular/plural. **Do not hard-code filenames.** Match case-insensitively on a normalised key (lowercase, strip `_`/space/`.csv`). Multiple independent parsers in the wild do exactly this.

### 1.3 `Connections.csv` — the file that matters

**Exact header row [VERIFIED — identical across ~10 independent repositories and a real export fixture]:**

```
First Name,Last Name,URL,Email Address,Company,Position,Connected On
```

| Column | Type | Notes |
|---|---|---|
| `First Name` | string | May be empty for restricted/closed accounts |
| `Last Name` | string | |
| `URL` | string | Canonical public profile URL, e.g. `https://www.linkedin.com/in/drishtisanghavi`. **This is your join key.** |
| `Email Address` | string | **Frequently blank** — see 1.4 |
| `Company` | string | **Connection's CURRENT company at export time**, not at connect time |
| `Position` | string | **Connection's CURRENT title at export time** |
| `Connected On` | date | Format `DD Mon YYYY`, e.g. `22 Jan 2026`. Locale-dependent in some exports. |

**Critical parsing gotcha [VERIFIED — flagged independently by at least 5 repos]:** real `Connections.csv` files begin with a **2–3 line `Notes:` preamble paragraph before the real header row.** Example text observed:

> `"When exporting your connection data, you may notice that some fields (such as email addresses) are blank for connections who have not synced their address with LinkedIn."`

Parsers must **scan forward for the line beginning `First Name`** rather than assuming row 0 is the header. Several repos hard-code "skip the first 3 lines"; that is fragile — scan instead.

**Second gotcha:** column *order* has varied across export generations. Older exports (as modelled in `hudi-packages-connectors`) had **no `URL` column** — 6 columns positional `[firstName, lastName, emailAddress, company, position, dateConnection]`. **Map by header name, never by index.** [VERIFIED]

### 1.4 Are emails included?

**Conditionally.** `Email Address` is populated **only for connections who have enabled the setting allowing their connections to see/download their email address.** Everyone else is blank. In practice the fill rate is low (commonly cited as roughly 10–40%, but I found **no authoritative figure** — treat fill rate as [UNVERIFIED — MUST CHECK] and measure it empirically on the user's own file). [VERIFIED that the condition exists; the rate is not verified.]

**Implication for this project:** email is *not* a usable join key. `URL` (the `/in/` public identifier) is the only reliable one.

### 1.5 Are 1st-degree connections' current company and title included?

**Yes — `Company` and `Position`.** This is the single most valuable field pair for this project.

Caveats [VERIFIED]:
- It is a **snapshot at export time**, not a time series. No refresh, no sync.
- It reflects whatever the connection has on their profile — stale/blank if they don't maintain it. B2B contact data is commonly cited as decaying ~2%/month.
- It gives you **only the current role**, not employment history. There is **no** past-employer field in `Connections.csv`. To get "used to work at Jane Street," you need the connection's profile Positions — which the export does **not** contain for other people (only `Positions.csv` for *yourself*).

### 1.6 **Is there ANY file that reveals a connection's own connections?**

**No. Categorically no.** [VERIFIED — by exhaustive enumeration of the file set and by the semantics of every file in it]

Walking the full list:

- `Connections.csv` — *your* edges only (you ↔ them).
- `Invitations.csv` — invitations **you** sent or received. Columns: `From`, `To`, `Sent At`, `Message`, `Direction` (values `OUTGOING`/`INCOMING`), and in newer exports `inviterProfileUrl`, `inviteeProfileUrl`. Still only edges incident to you.
- `messages.csv` — conversations **you** are party to. Columns: `CONVERSATION ID`, `CONVERSATION TITLE`, `FROM`, `SENDER PROFILE URL`, `TO`, `RECIPIENT PROFILE URLS`, `DATE`, `SUBJECT`, `CONTENT`, `FOLDER`, `ATTACHMENTS`, `IS MESSAGE DRAFT`. **Group conversations are the one place a third-party↔third-party co-occurrence appears**, and even then it is co-membership in *your* thread, not their edge.
- `Contacts.csv` — address book **you** imported.
- `Member_Follows.csv` — accounts **you** follow.
- Everything else — your own profile/activity/ads data.

**The LinkedIn data export is, by construction, an ego-network export of depth 1. It contains zero information about edges not incident to the requesting member.** There is no second-degree file, no "connections of connections" file, no shared-connection-count file.

### 1.7 Exact column headers for the other relevant files

All of the following are from the `hudi-packages-connectors` service parser (literal column-name string reads) plus the `tdimino/claude-code-minoan` format reference, cross-checked. [VERIFIED for the ones marked, [LIKELY] otherwise.]

| File | Exact columns |
|---|---|
| `Connections.csv` | `First Name`, `Last Name`, `URL`, `Email Address`, `Company`, `Position`, `Connected On` **[VERIFIED]** |
| `Invitations.csv` | `From`, `To`, `Sent At`, `Message`, `Direction` (+ `inviterProfileUrl`, `inviteeProfileUrl` in newer exports) **[VERIFIED]** |
| `messages.csv` | `CONVERSATION ID`, `CONVERSATION TITLE`, `FROM`, `SENDER PROFILE URL`, `TO`, `RECIPIENT PROFILE URLS`, `DATE`, `SUBJECT`, `CONTENT`, `FOLDER`, `ATTACHMENTS`, `IS MESSAGE DRAFT` **[VERIFIED for the first 9; last 3 LIKELY]** |
| `Contacts.csv` | `Source`, `FirstName`, `LastName`, `Companies`, `Title`, `Emails`, `PhoneNumbers`, `CreatedAt`, `Addresses`, `Sites`, `InstantMessageHandles`, `FullName`, `Birthday`, `Location`, `BookmarkedAt`, `Profiles` **[VERIFIED]** — note camelCase, unlike most files |
| `Profile.csv` | `First Name`, `Last Name`, `Maiden Name`, `Address`, `Birth Date`, `Headline`, `Summary`, `Industry`, `Zip Code`, `Geo Location`, `Twitter Handles`, `Websites`, `Instant Messengers` **[VERIFIED]** |
| `Positions.csv` | `Company Name`, `Title`, `Description`, `Location`, `Started On`, `Finished On` **[VERIFIED]** |
| `Education.csv` | `School Name`, `Start Date`, `End Date`, `Notes`, `Degree Name`, `Activities` **[VERIFIED]** |
| `Skills.csv` | `Name` (single column) **[VERIFIED]** |
| `Endorsement_Received_Info.csv` | `Endorsement Date`, `Skill Name`, `Endorser First Name`, `Endorser Last Name`, `Endorsement Status` **[VERIFIED]** |
| `Company Follows.csv` | `Organization`, `Followed On` **[VERIFIED]** |
| `Member_Follows.csv` | `Date`, `FullName`, `Status` **[VERIFIED]** |
| `Email Addresses.csv` | `Email Address`, `Confirmed`, `Primary`, `Updated On` **[VERIFIED]** |
| `PhoneNumbers.csv` | `Extension`, `Number`, `Type` **[VERIFIED]** |
| `Inferences_about_you.csv` | `Category`, `Type of inference`, `Description`, `Inference` **[VERIFIED]** |
| `Ad_Targeting.csv` | Wide, ~27 columns, no stable public schema. The parser reads positional indices `[0,3,7,9,11,15,16,17,19,20–23,26]`. Contains LinkedIn's advertising segments about you (member age, company names, degrees, job titles, interests, groups, skills…). **[UNCERTAIN — schema unstable; inspect the real file]** |
| `Logins.csv` | `Login Date`, `IP Address`, `User Agent`, `Login Type` **[VERIFIED]** |
| `Learning.csv` | `Content Title`, `Content Description`, `Content Type`, `Content Last Watched Date (if viewed)`, `Content Completed At (if completed)`, `Content Saved`, `Notes taken on videos (if taken)` **[VERIFIED]** |
| `Account Status History.csv` | `Time`, `Event` **[VERIFIED]** |
| `Ads Clicked.csv` | `Ad clicked Date`, `Ad Title/Id` **[VERIFIED]** |
| `Reactions.csv` | `Date`, `Type`, `Link` **[LIKELY]** |
| `Shares.csv` | `Date`, `ShareLink`, `ShareCommentary`, `SharedUrl`, `MediaUrl` **[LIKELY]** |
| `Recommendations_Received.csv` | `First Name`, `Last Name`, `Company`, `Title`, `Body`, `Created Date` **[LIKELY]** |
| `Certifications.csv` | `Name`, `Url`, `Authority`, `Started On`, `Finished On`, `License Number` **[LIKELY]** |
| `Jobs/Job Applications.csv` | `Application Date`, `Contact Email`, `Contact Phone Number`, `Company Name`, `Job Title`, `Job Url`, `Resume Name`, `Question And Answers` **[VERIFIED]** |
| `Jobs/Saved Jobs.csv` | `Saved Date`, `Job Url`, `Job Title`, `Company Name` **[VERIFIED]** |
| `SearchQueries.csv` | Your own search history. Schema **[UNVERIFIED]** |
| **"Saved Items"** | **[UNVERIFIED — MUST CHECK]** I could not confirm a file with this exact name. Saved *jobs* are `Jobs/Saved Jobs.csv`; saved *posts* appear to be exported inconsistently and multiple third-party tools exist specifically because LinkedIn's saved-posts export is unreliable. Treat as not-available. |

### 1.8 Section 1 verdict

The export gives you a **clean, complete, zero-risk, first-party list of the user's own 1st-degree connections with each one's current employer and title.** That is exactly one hop. It gives you **nothing** about the second hop.

---

## 2. Official LinkedIn APIs

### 2.1 The product catalogue as of 2026

LinkedIn's Developer Platform organises products into roughly: **Consumer, Marketing, Sales, Talent, Learning, Plugins.** [LIKELY]

| API product | What it grants | Approval bar | Exposes social graph? |
|---|---|---|---|
| **Sign In with LinkedIn using OpenID Connect** | OIDC identity of the *authenticating member only*: `sub`, name, picture, email. Scopes `openid`, `profile`, `email`. | **Self-serve.** Create an app tied to a verified LinkedIn Company Page → immediate access. | **No.** No connections, no degree, no counts. |
| **Share on LinkedIn** | Post on behalf of the authenticated member. Scope `w_member_social`. | **Self-serve.** | **No.** |
| **Marketing Developer Platform (MDP)** | Ad accounts, campaigns, creatives, org page admin, analytics. Includes the **Community Management API** (org posts, comments, social actions) and **Lead Sync API** (Lead Gen Form submissions the member consented to). | **Partner-gated.** Application with use-case, privacy policy, product demo; manual review. Reported 4–8 weeks fast path, ~3–4 months typical. Reported pricing ~$699+/mo for approved partners [UNCERTAIN — vendor-blog figure]. | **No.** Aggregate/demographic reporting only. Lead Sync returns only members who filled *your* form. |
| **Community Management API** | Organization page content, comments, reactions, `network update social actions`. Part of MDP family. | **Partner-gated.** | **No member-to-member edges.** Reactions/comments on *your org's* posts only. |
| **Lead Sync API** | Lead Gen Form submissions. | **Partner-gated** (MDP). | **No.** |
| **Talent Solutions / Recruiter System Connect (RSC)** | Two-way ATS↔Recruiter sync: candidate stubs, InMail, application status, job posting. Sibling products: **Apply Connect**, **Apply with LinkedIn (AWLI)**, **Premium Job Posting**. | **Partner-gated, very high bar.** LinkedIn Talent Solutions Partner application; reported 3–6 months and <10% approval rate [UNCERTAIN — secondary]. **AWLI stopped accepting new partners from 1 Oct 2025** [LIKELY]. | **No connection lists.** RSC surfaces recruiter-candidate context, not member-member edges. |
| **Sales Navigator Application Platform (SNAP)** — a.k.a. "Sales Navigator API" | Three service families: **Display Services** (embed Sales Nav widgets in your UI — *partner never receives the data, it renders in the browser from LinkedIn*), **Analytics Services** (seat/usage reporting), **Sync Services** (CRM↔Sales Nav sync, e.g. `SalesNavigatorProfileAssociations`). | **Invitation-only partner program.** LinkedIn's own SNAP docs state: *"We are not currently accepting new partners for access to the LinkedIn Sales Navigator API."* [LIKELY — reported consistently; could not read the page directly] | **No.** Even TeamLink relationship data is surfaced via *Display* (rendered, not returned as data). See §4.4. |
| **Connections API** (`/v2/connections`) | Returns the **1st-degree connections of the authenticating member only**. Permission: `r_1st_connections` or `r_compliance`. | **Partner-only.** Not obtainable self-serve. | **Your own ego network only.** |
| **Connections Size API** | Integer count of the authenticated member's 1st-degree connections. Scope `r_1st_connections_size`. | **Partner-only** ("you must apply and be accepted to one of LinkedIn's Partner Programs"). | **Count only.** |
| **Profile API** (`/v2/me`, `/v2/people`) | Authenticated member's own profile. (v1 `r_basicprofile` → v2 `r_liteprofile` → now largely OIDC.) | Self-serve for own profile; anything richer is partner-gated. | **No.** |
| **Learning API, Plugins** | Course catalogue; embeddable widgets. | Mixed. | **No.** |

### 2.2 The load-bearing quote

Microsoft Learn's **Connections API** page contains the decisive restriction. Reproduced from search-result extraction (I could not fetch `learn.microsoft.com` directly, but this sentence is quoted consistently across sources) [VERIFIED by multiple independent quotations]:

> *"The use of this API is restricted to those developers approved by LinkedIn and subject to applicable data restrictions in their agreements."*

> *"You can only get the connections of the user who granted your application access. **You cannot get connections of that user's connections (2nd-degree connections).**"*

Docs URL (blocked to me, cite anyway): <https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api>
Connections Size: <https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-size>

### 2.3 Historical context

- LinkedIn **closed the open developer platform in 2015**; the `/v1/people` and legacy Connections endpoints (which once returned richer people data) were sunset for new developers. [LIKELY]
- **v1 APIs fully deprecated 1 May 2019.** [LIKELY]
- The v1 profile object once carried a `distance` / degree-of-separation field. **[UNVERIFIED — MUST CHECK]** I could not confirm the exact field name (`distance`, `DISTANCE_1`, etc.) or the removal date. It is irrelevant operationally — no currently obtainable API returns it — but do not cite it as fact.

### 2.4 Verdict on the claim: *"no LinkedIn API exposes another member's connection list"*

**CONFIRMED.** [VERIFIED]

Precisely stated, with the nuances that matter:

1. **No LinkedIn API returns the connection list of anyone other than the OAuth-authenticating member.** The Connections API is explicit that 2nd-degree is out of scope.
2. Even the *own-connections* case is partner-gated — `r_1st_connections` is not available on self-serve apps. So for this project the API is *strictly worse than the data export*, which gives the same data with zero approval.
3. **No API returns degree-of-separation** between the authenticated member and an arbitrary third party.
4. **No API returns shared/mutual connections** between the authenticated member and a third party.
5. **No API returns a count of anyone's connections except the authenticated member's own** (`r_1st_connections_size`, partner-gated).

The corollary that matters for the project: **the second hop is not purchasable through any official channel at any price.** Being a LinkedIn partner does not unlock it; the capability does not exist as a product.

---

## 3. In-product UI signals that reveal 2nd-degree adjacency

This is where the second hop *does* exist — for a human, in a browser, at human speed.

### 3.1 Degree badges

Every person entity in the LinkedIn UI (search results, profile header, feed, company People tab) carries a **degree badge: `1st`, `2nd`, `3rd`, or `3rd+`/none.** [VERIFIED — universal product behaviour]

- `1st` — directly connected.
- `2nd` — connected to at least one of your 1st-degree connections.
- `3rd` — connected to at least one of your 2nd-degrees.

**A `2nd` badge on a Jane Street employee is itself the signal the project wants**: it *proves* at least one of the user's 1st-degree connections is adjacent to that person. It does not, by itself, say *which* one.

### 3.2 "X mutual connections" / "Shared connections" — **the key mechanism**

On a 2nd- or 3rd-degree profile, LinkedIn renders a line such as **"Sarah Chen, Rajiv Patel, and 12 other mutual connections"**, which is a **link**. [VERIFIED]

Clicking it opens a **people-search results page filtered to the mutual set** — i.e. **an ENUMERABLE LIST, not just a count.** [VERIFIED]

**This is the crux.** For a target person T (a Jane Street employee), the mutual-connections list is exactly:

```
{ my 1st-degree connections } ∩ { T's connections }
```

which is **precisely the true edge set the project wants, restricted to the user's own network** — which is all the project actually needs, since the goal is "clusters of MY connections who are adjacent to Jane Street/Citadel."

**Caveats and caps [MIXED CONFIDENCE]:**

- The list is rendered as search results and is **paginated**. [LIKELY]
- Multiple sources state that when the mutual count is large, **LinkedIn may not display the full list**, prompting you to filter or search within it. **The exact cap is not publicly documented.** [UNCERTAIN — no authoritative number found; commonly-cited search-result caps are ~1,000 results for free accounts and ~2,500 for Sales Navigator, but I could not confirm these apply to the mutual-connections view specifically.] **[UNVERIFIED — MUST CHECK empirically]**
- **Privacy override — and the good news:** a member can set their connections list to "Only you". In that case, per LinkedIn Help "View Your Connection's Connections", *"there won't be a See connections option on the member's profile."* **However, MUTUAL connections remain visible even then.** [VERIFIED — corroborated by LinkedIn Help "Who can see your connections" (<https://www.linkedin.com/help/linkedin/answer/a540663>) and multiple independent guides: hiding your list *"only prevents others from browsing your full list"*; *"even if you hide your connection list, others can still see mutual connections."*]

  **This is a major, load-bearing finding for this project.** The mutual-connections surface is *not* defeated by the privacy setting that quant-finance professionals most commonly enable. The §7.2 workflow survives target-side privacy hardening.
- **Human-speed only.** Reading this in a browser as a logged-in member is ordinary product use. **Automating the reading of it is scraping** — see §6.

### 3.3 The "Connections of" and "Followers of" search filters

LinkedIn's People search "All filters" panel reportedly includes, as of 2026 [LIKELY — consistent across several 2026-dated guides, but all are SEO/vendor blogs; **[UNVERIFIED — MUST CHECK in the live product]**]:

`Connections` (1st / 2nd / 3rd+), **`Connections of`**, **`Followers of`**, `Locations`, `Current company`, `Past company`, `School`, `Industry`, `Profile language`, `Service categories`, `Open to volunteering`, `Keywords`, `Actively hiring` (Premium).

**`Connections of` semantics [LIKELY]:**
- Accepts **one person at a time**.
- **That person must be your 1st-degree connection.**
- Returns *their* network **intersected with what you are permitted to see** (i.e. still bounded by your own 1st/2nd/3rd visibility).

**Why this is less useful than it sounds for this project:** the pivot must be *your* 1st-degree. Jane Street and Citadel employees are, by hypothesis, **not** the user's 1st-degrees (if they were, this project would be trivial). So `Connections of` cannot be pivoted on a Jane Street employee. It can only be pivoted on the user's own connections — which inverts the query into "enumerate connection C's network and look for Jane Street," an O(N) manual crawl over the user's whole connection list. Not tractable manually, and automating it is scraping.

**Legacy URL form (do not automate — documented for identification only):** the historical faceted-search URL was

```
https://www.linkedin.com/search/results/people/?facetConnectionOf=["<memberUrn>"]&facetNetwork=["F","S"]&origin=MEMBER_PROFILE_CANNED_SEARCH
```

where `facetNetwork` values are `F`=1st, `S`=2nd, `O`=3rd+. [VERIFIED as historical — found in multiple 2018–2020-era scrapers and in an archived LinkedIn profile HTML page.] Modern LinkedIn uses different parameter names (`connectionOf`, `network`) and the internal `/voyager/api/...` endpoints. **[UNVERIFIED — MUST CHECK]** exact modern parameter names. **Regardless of form, hitting these programmatically is a §8.2 violation.**

### 3.4 Company page → "People" tab

On `linkedin.com/company/<firm>/people/` LinkedIn shows employees with facet breakdowns [LIKELY]:

- "Where they live" / "Where they studied" / "What they do" / "What they are skilled at" / **"How you are connected"**
- Degree filter: **1st / 2nd / 3rd+ / out of network**
- Free-text keyword search over employees

**This is a genuinely useful, ToS-compliant, manual mechanism**: go to Jane Street's People tab, filter to **2nd degree**, and you get a list of Jane Street employees who are 2nd-degree to you — i.e. each one is provably adjacent to at least one of your connections. Then open each and read the mutual-connections list (§3.2) to resolve *which* connections.

**Caveats:**
- The People tab shows only a subset of employees (LinkedIn does not guarantee completeness, and it is capped/paginated). **[UNVERIFIED — MUST CHECK]** the cap.
- Employee counts on company pages are self-reported by members listing that employer. Jane Street / Citadel headcounts on LinkedIn will *undercount* (many quant-finance employees keep sparse profiles) and *overcount* (recruiters, contractors, alumni with stale profiles).
- **⚠️ ENTITY SPLIT — Citadel is TWO separate LinkedIn companies. [VERIFIED]** They must be ingested as distinct targets and never merged:

  | Entity | LinkedIn slug | People tab URL | Approx. LinkedIn headcount |
  |---|---|---|---|
  | **Citadel** (the hedge fund, Citadel LLC, est. 1990) | `citadel-llc` | `linkedin.com/company/citadel-llc/people/` | ~4,400 |
  | **Citadel Securities** (the market maker, est. 2002) | `citadel-securities` | `linkedin.com/company/citadel-securities/people/` | ~1,790 |

  A related page `citadel-global-equities` also exists. Jane Street's page is `linkedin.com/company/jane-street/` **[UNVERIFIED — MUST CHECK the exact slug]**. Note also that `Connections.csv` stores whatever free-text string the member typed as `Company`, so normalisation must map `"Citadel"`, `"Citadel LLC"`, `"Citadel Securities"`, `"Citadel Securities LLC"`, `"Jane Street"`, `"Jane Street Capital"`, `"Jane Street Group"` etc. to canonical entities — and must preserve the Citadel/Citadel Securities distinction rather than collapsing it.
- Free accounts hit the **Commercial Use Limit** — reportedly ~250–350 people-searches/month, after which results are throttled to ~3 per query until the 1st of the next month. [LIKELY]

### 3.5 Summary: count vs enumerable list

| UI signal | Count or list? | Who it's about | Cap |
|---|---|---|---|
| Degree badge (1st/2nd/3rd) | Neither — a boolean-ish label | Any person you can see | none |
| "X mutual connections" **number** | Count | You ↔ target | none |
| "X mutual connections" **clicked** | **ENUMERABLE LIST** | You ↔ target | unknown; large lists reportedly truncated **[MUST CHECK]** |
| "See connections" on a 1st-degree's profile | **ENUMERABLE LIST** (their connections, filtered to your visibility) | Your 1st-degree only; hideable by them | search-result caps |
| `Connections of` filter | **ENUMERABLE LIST** | Pivot must be your 1st-degree | search-result caps |
| Company People tab + degree filter | **ENUMERABLE LIST** | Employees of a firm, badged by degree | paginated, unknown cap |
| Connection count on a profile ("500+") | Count, and **censored at 500+** | Anyone | 500+ is a ceiling, not a number |

---

## 4. Sales Navigator specifics

### 4.1 Pricing (2026) [LIKELY — vendor-blog sourced, verify at purchase]

| Tier | Monthly | Annual equivalent | Key adds |
|---|---|---|---|
| **Core** | ~$119.99/seat/mo | ~$89.99/seat/mo billed annually | Advanced search, lead/account lists, 50 InMail/mo |
| **Advanced** | ~$159.99/seat/mo | ~$149.99/seat/mo billed annually | **TeamLink**, TeamLink Extend, Smart Links, admin reporting |
| **Advanced Plus** | Custom enterprise | ~$1,600+/seat/year reported | CRM sync (Data Validation, ROI reporting), SNAP integrations |

Note: several sources emphasise that **Advanced adds no additional data** — same database, same filters, same InMail allowance as Core. What Advanced buys is **TeamLink**, i.e. *relationship* visibility. That is directly relevant here.

### 4.2 TeamLink and TeamLink Extend

- **TeamLink** (Advanced+): lets each seat see **the 1st-degree connections of everyone else on the same Sales Navigator contract**, and surfaces "your teammate X knows this prospect." [LIKELY]
- **TeamLink Extend**: broadens the pool to colleagues at your company who **don't** have a Sales Navigator seat, by having them opt in and link their LinkedIn accounts. [LIKELY]

**Hard constraint:** TeamLink pools **your own organisation's** seats. It does **not** let you see the connections of people outside your contract. **A Sales Navigator seat gives you exactly zero additional visibility into who at Jane Street is connected to whom.** Jane Street employees are not on your TeamLink contract. [VERIFIED by construction of the feature]

### 4.3 Search filters and spotlights relevant here

- **"Connections of"** filter (Sales Nav): same semantics as §3.3 — pivot on **your** 1st-degree. Sometimes surfaced as **"Best path in"** / "ask for intro."
- **Shared connections**: the Lead Page "Relationship" section aggregates shared connections, shared experiences, and interaction history. **This is the Sales Navigator analogue of §3.2 and is the strongest legitimate second-hop surface in the product.** [LIKELY]
- **Spotlights**: `TeamLink connections`, `Recent posts on LinkedIn`, `Changed jobs`, `Mentioned in the news`, `Following your company`, `Shared experiences`. The **`TeamLink connections` spotlight** filters a search to leads connected to someone on your team.
- **Account Map**: a manual org-chart builder for an account (drag people into a hierarchy). It is a **user-authored artefact**, not LinkedIn-inferred relationship data. It does **not** reveal internal connections between Jane Street employees. [LIKELY]
- **Past colleague** filters: `Past company` exists; "people who worked with X at Y" is *inferable* by intersecting `Past company` + date ranges but is not a first-class edge.

### 4.4 Does the Sales Navigator API expose any of this programmatically?

**No, and it is closed anyway.** [VERIFIED / LIKELY]

- SNAP splits into **Display Services**, **Analytics Services**, **Sync Services**.
- **Display Services deliberately do not hand data to the partner.** LinkedIn's own privacy documentation states that for Display integrations *"partners just display content within the context of their product, within a browser. All the information is accessed directly from LinkedIn within the browser, and the partners do not actually get access to the LinkedIn data."* This is exactly the architecture that keeps relationship data (TeamLink, shared connections) from leaving LinkedIn. [LIKELY — quoted from <https://business.linkedin.com/sales-solutions/sales-navigator-data-and-privacy-resources-new>]
- **Sync/Analytics Services** cover CRM record association (`SalesNavigatorProfileAssociations`), seat usage, and lead/account list sync — **not member-to-member edges.**
- **New partner onboarding is closed**: LinkedIn's SNAP docs reportedly state *"We are not currently accepting new partners for access to the LinkedIn Sales Navigator API."* [LIKELY]

### 4.5 Sales Navigator verdict for this project

**Sales Navigator is not worth buying for this use case.** It adds no visibility into Jane Street's or Citadel's internal graph, TeamLink is scoped to your own org, and the one genuinely useful surface (shared connections on a Lead Page) is a nicer rendering of what free LinkedIn already shows on a profile. The only real benefits are **higher search caps** and **better filtering** — worth ~$100–160/mo *only if* the manual workflow in §7 proves rate-limited.

---

## 5. Third-party providers

### 5.1 The headline finding

**Not one of the providers surveyed sells LinkedIn connection-graph edges.** Every LinkedIn-derived commercial dataset on the market is **node data** (profiles, employment, education, contact info, company rosters), not **edge data** (who is connected to whom). [VERIFIED by absence — I searched specifically for vendors selling mutual-connection/edge data and found none; the only relationship-graph vendors found, e.g. Connect the Dots, build graphs from *email/calendar*, not LinkedIn.]

This is not an accident. The connection graph is only visible in the logged-in environment, per-member, and is the exact asset LinkedIn litigates hardest to protect.

### 5.2 Provider-by-provider

| Provider | LinkedIn-derived data sold | Connection graph? | Rough cost | Legal standing (2026) |
|---|---|---|---|---|
| **Proxycurl / Nubela** | *(historically)* Person + company profile API scraped from LinkedIn | **No** | *(was ~$0.01–0.03/profile)* | **DEAD. [VERIFIED]** LinkedIn Corp. v. Nubela Pte. Ltd. et al, N.D. Cal. **3:25-cv-00828**, filed **24 Jan 2025**. Six claims: breach of contract; fraud & deceit; **CFAA** (18 U.S.C. §1030); Cal. UCL; **Lanham Act** (15 U.S.C. §1125(c)); misappropriation. Core allegation: hundreds of thousands of **fake accounts** used to scrape millions of profiles incl. non-public data. **Settled mid-2025; Proxycurl shut down 4 July 2025.** Founder Steven Goh published a post-mortem stating they settled rather than fight a Microsoft subsidiary, and that ~half of ~$10M ARR came from LinkedIn scraping. Team now runs **NinjaPear** at nubela.co — explicitly **does not scrape LinkedIn**; canonical key is company website, not LinkedIn URL. **Do not attempt to use Proxycurl. It does not exist.** |
| **Bright Data** | Proxy/unblocking infra + pre-built "LinkedIn profile/company/job" datasets | **No** | Datasets ~$0.001–0.01/record; proxy usage-based; enterprise contracts | **Mixed but comparatively strong.** Won summary judgment in **Meta Platforms v. Bright Data**, N.D. Cal., **23 Jan 2024** (Judge Edward M. Chen): Meta's terms *"do not bar logged-off scraping of public data; perforce [they do] not prohibit the sale of such public data."* Reasoning turned on Meta having removed the clause binding mere *visitors*. **Crucially the court expressly did NOT decide logged-in scraping**, and it was a *Meta* contract, not LinkedIn's. **Do not read Bright Data's win as authorising LinkedIn scraping.** |
| **PhantomBuster** | Browser-automation "Phantoms" driving *your* LinkedIn session via your `li_at` session cookie | **Partially — it can drive the UI that shows mutual connections/`Connections of`** | ~$56–439/mo | **High personal risk.** Cookie-based automation is a direct §8.2 violation. Multiple documented permanent bans of primary accounts, 2025–2026; LinkedIn rarely reverses automation bans. Cloud execution means the IP won't match the user's usual location — a strong detection signal. |
| **Apify** (LinkedIn actors) | Marketplace of third-party LinkedIn scrapers | **No** (profile/company/job/post scrapers) | Pay-per-actor-run, ~$5–50/mo typical | Actors carry disclaimers; users instructed to cease within 48h of any LinkedIn C&D. Actor availability is volatile — actors get pulled. |
| **Clay** | Enrichment orchestration; blends 50+ waterfall providers | **No** | ~$149–800+/mo | Not primarily a LinkedIn scraper; survived the March 2025 crackdown that hit Apollo/Seamless. Risk is inherited from whichever sub-providers you enable. |
| **Apollo.io** | B2B contact DB (~275M contacts) incl. LinkedIn-sourced fields | **No** | ~$49–149/user/mo | **LinkedIn banned Apollo's extension workflow ~6 March 2025** and removed its company page. Platform still exists; the LinkedIn-overlay scraping mechanism is gone. [LIKELY] |
| **Seamless.ai** | B2B contact DB | **No** | ~$147+/user/mo | **Same March 2025 LinkedIn enforcement action as Apollo.** [LIKELY] |
| **ZoomInfo** | B2B contact/company intelligence | **No** | ~$15k–40k+/yr typical | **Comparatively clean** — reportedly does *not* scrape LinkedIn directly; its extension overlays ZoomInfo's own data. Was not swept up in the 2025 enforcement. |
| **People Data Labs (PDL)** | Person/company enrichment; largest person-level DB; job history, education, social handles | **No** | Free 100 calls/mo; ~$300–600/mo teams; enterprise higher | Aggregator model, sources not fully disclosed. GDPR exposure sits with *you* as controller once you hold the data (§6.4). |
| **Coresignal** | Bulk employee/company/job-posting datasets, incl. historical snapshots | **No** | From ~$250 / 100k records | Same as PDL. Strong for "who works at Jane Street over time" — which *is* useful here (see §7). |
| **Dripify** | LinkedIn outreach automation (cloud, session-cookie) | **No** | ~$39–99/user/mo | §8.2 violation; account-ban risk. |
| **Evaboot** | Chrome extension that exports + cleans **Sales Navigator** search results | **No** | ~$29–99/mo | Requires your own Sales Nav seat; drives your session. §8.2 violation; ban risk. |
| **Lix** | LinkedIn/Sales Nav data extension + API | **No** | ~$49–99/mo+ | Same category and same risk as Evaboot. |
| **Fullenrich** | Waterfall email/phone enrichment from a LinkedIn URL | **No** — enrichment only | ~$29–299/mo | Consumes LinkedIn URLs you already hold; adds contact data. No graph. |
| **Unipile / LinkedAPI / similar "unofficial LinkedIn API as a service"** | REST façade over the **logged-in Voyager** surface using **your account's session**: messaging, invitations, profile reads, **connection retrieval** | **Closest thing that exists — but it's your own session doing the scraping** | Unipile from ~€49/$55/mo (≤10 linked accounts) | **This is automation of your own logged-in account. Squarely §8.2-prohibited.** LinkedIn's own Messages API is partner-only, 1st-degree-only, and forbids automated/scheduled sends — these vendors exist precisely to route around that. Ban risk falls on *the user's account*, not the vendor. **[UNVERIFIED — MUST CHECK]** whether any of them expose *mutual connections* specifically; I found evidence of connection *retrieval* and *deletion* endpoints, not mutual-connection endpoints. |

### 5.3 Broader enforcement context (2025–2026)

LinkedIn has moved from cease-and-desist letters to **routine federal litigation**:

- **LinkedIn v. hiQ Labs** — 2017–2022 (see §6.2)
- **LinkedIn v. Mantheos Pte. Ltd.** — filed ~Jan/Feb 2022, N.D. Cal.; **settled ~3 months later** with a consent injunction signed by **Judge Haywood S. Gilliam**. Mantheos used **hundreds of fake accounts + virtual debit cards under fake names to fraudulently obtain Sales Navigator subscriptions**, then scraped profile data available only in the logged-in environment. Terms: permanently delete all scraped data, **destroy the scraping software**, cease all automated access, and stop selling the data.
- **LinkedIn v. Nubela/Proxycurl** — filed 24 Jan 2025; settled; Proxycurl dead by 4 Jul 2025.
- **LinkedIn v. ProAPIs Inc. and Rehmat Alam** — filed **2 Oct 2025**, N.D. Cal. Alleged an *"industrial-scale fake account mill"* registering **over 1 million fake accounts** to scrape members, companies, academic data, posts, reactions and comments — **including data behind the password wall** — and renting the capability out **for up to $15,000/month**. **Settled** (terms not public) per early-2026 reporting.
- **March/April 2025**: LinkedIn blocked **Apollo.io** and **Seamless.ai** extension workflows and removed their company pages.

**Pattern:** the fact pattern LinkedIn attacks hardest is **fake accounts + logged-in scraping + resale.** Everything that touches the connection graph necessarily requires logged-in access — which is why no vendor sells it.

---

## 6. Legal / ToS landscape

### 6.1 LinkedIn User Agreement §8.2 ("Don'ts")

The operative prohibitions [LIKELY — I could not fetch the UA text directly; the following is consistently quoted across sources. **Verify the current text at <https://www.linkedin.com/legal/user-agreement> before relying on section numbering**, which LinkedIn has renumbered historically]:

§8.2 prohibits, among other things:

1. *"Develop, support or use software, devices, scripts, robots, or any other means or processes (including crawlers, browser plugins and add-ons, or any other technology) to scrape the Services or otherwise copy profiles and other data from the Services."*
2. Using **bots or other automated methods** to access the Services, add or download contacts, or send/redirect messages.
3. **Overlaying or otherwise modifying the Services or their appearance** (this is the clause that kills browser extensions that render UI over LinkedIn pages — the Apollo/Seamless fact pattern).
4. *"Copy, use, display or distribute any information (including content) obtained from the Services, whether directly or through third parties (such as search tools or data aggregators or brokers), without the consent of the content owner."*

**Clause 4 is the one people miss.** It means **buying LinkedIn-derived data from a third-party broker is itself framed as a User Agreement violation** for a LinkedIn member. Whether that is enforceable against an individual purchaser is a different question — but it removes the "I didn't scrape it, I bought it" defence as a matter of *contract*.

Related: LinkedIn maintains a **"Prohibited software and extensions"** help page and states that members using such tools *"risk having their accounts restricted or shut down."*

### 6.2 hiQ Labs v. LinkedIn — the full history (and the widely-misreported ending)

This case is routinely cited as "scraping public data is legal." **That is a serious misreading of how it ended.** Full sequence:

| Date | Event | Holding |
|---|---|---|
| May 2017 | LinkedIn sends hiQ a cease-and-desist and blocks it technically | — |
| Aug 2017 | N.D. Cal. grants hiQ a **preliminary injunction** | LinkedIn must not block hiQ |
| **9 Sep 2019** | **Ninth Circuit affirms** the injunction | Scraping *public* data likely does **not** violate CFAA's "without authorization" |
| **14 Jun 2021** | **US Supreme Court GVRs** (grants, vacates, remands) in light of **Van Buren v. United States** | Ninth Circuit must reconsider |
| **18 Apr 2022** | **Ninth Circuit reaffirms** (No. 17-16783) | **CFAA does not apply to scraping publicly available data.** "Without authorization" requires circumventing an authorization gate; public pages have none. |
| **Nov 2022** | **N.D. Cal. grants LinkedIn summary judgment on BREACH OF CONTRACT** | **hiQ breached the LinkedIn User Agreement** — by automated scraping of profiles **and by hiring crowdsourced workers to create fake profiles** to access the platform. |
| **6 Dec 2022** | Parties file stipulation + proposed consent judgment | Confidential settlement |
| **8 Dec 2022** | **Court enters Consent Judgment and Permanent Injunction** | **$500,000 judgment against hiQ**; established hiQ's liability for California common-law **trespass to chattels** and **misappropriation**; **permanent injunction** requiring hiQ to **cease all scraping of LinkedIn** and **destroy all source code, data, and algorithms derived from scraped LinkedIn profile data.** |

**The correct reading:**

> **hiQ won the CFAA question and lost the war.** Scraping public LinkedIn data is not a *federal crime* under the CFAA. It is still a **breach of contract**, and it exposed hiQ to **state-law tort liability** (trespass to chattels, misappropriation) — which is what actually ended the company. hiQ ceased operations.

The CFAA holding also **does not help anyone doing what this project would need**, because the connection graph is **not public data** — it is only visible **behind the login wall**, to an authenticated member. Van Buren/hiQ's "no authorization gate" reasoning **inverts** the moment you log in: there *is* an authorization gate, you passed it under a contract, and that contract says no bots.

Key citations: Ninth Circuit 2022 opinion <https://cdn.ca9.uscourts.gov/datastore/opinions/2022/04/18/17-16783.pdf> · <https://law.justia.com/cases/federal/appellate-courts/ca9/17-16783/17-16783-2022-04-18.html> · Nov 2022 SJ order <https://caselaw.findlaw.com/court/us-dis-crt-n-d-cal/2182242.html> · settlement analysis <https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators> · <https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/>

### 6.3 Practical risk to an individual

Ranked by likelihood, for a private individual running automation against their own account:

| Risk | Likelihood | Severity | Notes |
|---|---|---|---|
| **Soft rate-limit / "You've reached the commercial use limit"** | **Near-certain** at any scale | Low | Resets 1st of month |
| **Temporary account restriction** (CAPTCHA wall, feature freeze, "unusual activity") | **High** | Medium | Typically days; appealable |
| **Permanent account ban** | **Moderate** | **High** | Widely documented for cookie-based tools (PhantomBuster et al.). LinkedIn *rarely reverses* automation bans. For a professional, losing a 10-year network permanently is a severe, uninsurable loss. |
| **Cease & desist letter** | Low for an individual | Medium | LinkedIn reserves this for commercial-scale actors |
| **Litigation** | **Very low** for a non-commercial individual | Catastrophic | Every LinkedIn suit found targets **commercial resale at scale**, usually with **fake accounts**. An individual analysing their own network is not the target profile. **But note: creating even one fake/secondary account to scrape moves you into the exact fact pattern LinkedIn litigates.** |

**The dominant risk is not being sued. It is losing the account** — which is also the asset the whole project is built on.

### 6.4 GDPR / CCPA on holding this data

**GDPR (if any connection is an EU/UK/EEA resident — near-certain for a finance network):**

- A name + job title + employer **is personal data** (Art. 4(1)). The moment you collect and process it you are a **controller** and need an Art. 6 lawful basis.
- **Purely personal/household activity exemption (Art. 2(2)(c))** is the user's best position: analysing *your own contacts* for *your own* networking purposes plausibly falls outside GDPR's scope entirely. **This exemption is construed narrowly** and is generally lost the moment the activity becomes professional/commercial (e.g. recruiting, fundraising, selling) or the data is shared outside the household. **[This is a genuine legal question, not a settled one — get advice if the use is commercial.]**
- If the exemption does not apply, **legitimate interest (Art. 6(1)(f))** is the only realistic basis, and the ICO's stated view is that scraping usually **fails the balancing test** because data subjects don't know it's happening. You would also owe Art. 14 transparency notices to people whose data you obtained indirectly — impractical at scale.
- Enforcement precedent that scraping public data is not a free pass: **CNIL fined KASPR €240,000 (2024)** specifically for scraping LinkedIn contacts. **Clearview AI** — €30.5M (Dutch DPA, 2024), €20M (CNIL), ~€100M cumulative across the EU — for scraping *public* images with no lawful basis. The Dutch DPA further signalled it would explore **personal liability for directors**. Non-EU establishment is no shield (Art. 3(2)).

**CCPA/CPRA (California residents):**

- Personal-use analysis by an individual is not a "business" under CCPA (revenue/volume thresholds).
- If this ever becomes a product, **California's data-broker registration regime** bites: registration in the **DROP** system, **$6,000/yr fee**, and **$200/day** administrative fines for non-registration. The definition was expanded for the Jan 2026 cycle to cover more businesses.

**Practical takeaway:** *Keeping the user's own `Connections.csv` and deriving inferences from it, for the user's own networking, is the lowest-risk posture available and very likely outside GDPR's material scope.* Building a database of scraped third-party profiles is a materially different legal object.

---

## 7. The verdict

### 7.1 Can you obtain the TRUE edge set "connections of Jane Street employee X"?

**No. Not by any legitimate mechanism, at any price, at any scale.**

Restating the four independent walls, each of which alone is fatal:

1. **The data export is depth-1 by construction.** It contains only edges incident to the requesting member. No file in the ~30–45 file archive contains anyone else's edges. [VERIFIED]
2. **No LinkedIn API exposes it.** The Connections API is explicit: *"You cannot get connections of that user's connections."* No API returns degree-of-separation, mutual connections, or another member's connection count. Partner status does not unlock it — **the capability is not a product.** [VERIFIED]
3. **No third-party vendor sells it.** Every LinkedIn-derived commercial dataset is node data. The graph lives only behind the login wall, per-member, and is the asset LinkedIn litigates hardest to protect. The vendor that came closest (Proxycurl) **is dead** — sued Jan 2025, shut down 4 Jul 2025. [VERIFIED]
4. **The remaining path is logged-in scraping, which is squarely §8.2-prohibited**, is not protected by hiQ (that was about *public* data and the *CFAA*, and hiQ still lost on contract with a $500k judgment and a destruction order), and carries the one risk that would destroy the project's own foundation: **permanent loss of the user's account.**

Additionally, even a hypothetical perfect scrape would be incomplete: **members can hide their connections list**, and finance professionals disproportionately do.

### 7.2 The closest legitimate proxy — and it is much better than it sounds

The project does **not** actually need "all connections of Jane Street employee X." It needs:

> **{ my 1st-degree connections } ∩ { connections of Jane Street / Citadel people }**

That intersection is **exactly what LinkedIn's "shared connections" / "X mutual connections" view renders on any 2nd- or 3rd-degree profile** (§3.2) — and it is **an enumerable list, not a count.**

**The ToS-compliant workflow that actually resolves the true edge set (for the user's own network):**

1. **Ingest** `Connections.csv` from the user's own data export. → the complete, authoritative node set for hop 1, with current `Company` and `Position`. Zero risk.
2. **Enumerate targets manually**: go to `linkedin.com/company/jane-street/people/` and the Citadel **and** Citadel Securities pages (**separate entities — do not merge**), filter **"How you are connected" → 2nd degree**. Every person returned is *provably* adjacent to ≥1 of the user's connections.
3. **For each target T, open the profile and read the "X mutual connections" list.** Each name in that list is a **TRUE, LinkedIn-asserted edge**: `connection C ↔ Jane Street employee T`. Record `(C, T)`.
4. **Aggregate.** Cluster the user's connections by how many Jane Street / Citadel targets they are mutual-connected to. That gives a **ground-truth-quality bipartite graph** between the user's network and the two firms.

**Confidence attachable to this:** **very high — it is not a proxy at all, it is the real edge, asserted by LinkedIn itself.** The limitations are *coverage*, not *accuracy*:
- Misses Jane Street/Citadel people who are 3rd-degree or out-of-network (no mutual connections to find).
- Misses employees absent from the People tab or with hidden/sparse profiles.
- Possible truncation of very large mutual-connection lists **[MUST CHECK]**.
- **It is manual.** Steps 2–3 are human browsing. **Automating them is scraping.** The honest engineering answer is: build the pipeline to *ingest a human-collected paste/CSV of mutual-connection observations*, not to fetch them.

### 7.3 Every ToS-compliant signal correlating with "this connection is adjacent to Jane Street / Citadel"

Ranked by strength. Signals 1–2 are ground truth; 3+ are genuine probabilistic features for a ranking model.

| # | Signal | Source | Strength | Obtainable how |
|---|---|---|---|---|
| **1** | **Named in the mutual-connections list of a JS/Citadel employee** | LinkedIn UI, §3.2 | **Ground truth (P≈1.0)** | Manual browsing; human-entered |
| **2** | **Connection's `Company` == "Jane Street" / "Citadel" / "Citadel Securities"** | `Connections.csv` | **Ground truth** — they *are* the firm | Export. **Free, zero risk.** |
| **3** | **Connection previously worked at the firm** | Their profile Positions | **Very strong** — ex-employees retain dense internal ties | ⚠️ **Not in the export.** Requires reading their profile (manual) or a profile-enrichment vendor (Coresignal/PDL — node data, legal but §8.2 cl.4 friction) |
| 4 | **Shared employer history with a known JS/Citadel employee** (same firm, **overlapping dates**) | Positions of both | **Strong.** Date overlap is what makes it strong — without overlap it is weak | Manual / enrichment vendor |
| 5 | **Peer-firm employment** (other quant/HFT/prop: Two Sigma, DE Shaw, HRT, Optiver, IMC, Jump, SIG, Millennium, Point72, Renaissance, XTX, Tower, DRW, Akuna, Old Mission) | `Connections.csv` `Company` | **Strong prior.** The quant-finance labour market is small, incestuous, and heavily interconnected | Export. **Free.** |
| 6 | **Same school + same graduation cohort** as known JS/Citadel employees | Their profile Education | **Moderate-strong**, and much stronger for the specific feeder set (MIT, Harvard, Princeton, Stanford, CMU, Waterloo, Oxford, Cambridge, IIT) and for competition-math/ICPC/IOI/Putnam backgrounds | Manual / enrichment vendor |
| 7 | **Mutual-connection COUNT with a JS/Citadel employee** (without opening the list) | LinkedIn UI | **Moderate** — a count of ≥5 is a strong adjacency prior even before enumerating | Manual browsing |
| 8 | **Degree badge = 2nd on a JS/Citadel employee** | LinkedIn UI | **Moderate** — proves *someone* in your network is adjacent, doesn't say who | Manual browsing |
| 9 | **Connection follows the Jane Street / Citadel company page** | Visible on their profile "Interests → Companies" | **Weak-moderate.** Also available for *yourself* in `Company Follows.csv` (`Organization`, `Followed On`) | Manual; own-follows free from export |
| 10 | **LinkedIn Group co-membership** (e.g. quant-finance, trading, alumni groups) | Group member lists | **Weak-moderate.** ⚠️ **[UNVERIFIED — MUST CHECK]** LinkedIn Groups have been heavily de-emphasised; member lists may be visible only to fellow members and may not be enumerable in 2026 | Manual, if at all |
| 11 | **Public engagement**: connection reacts to / comments on JS or Citadel posts, or on posts by their employees | Post engagement lists | **Weak-moderate**, and **decays fast**. Your *own* reactions are in `Reactions.csv` | Manual |
| 12 | **Event co-attendance** (LinkedIn Events, recruiting events, conferences) | Event attendee lists | **Weak.** ⚠️ **[UNVERIFIED]** attendee-list visibility rules unclear in 2026 | Manual, if at all |
| 13 | **Shared skills / endorsement clusters** typical of the firm (OCaml, C++, low-latency, market microstructure, FPGA) | Their profile Skills | **Weak** but cheap; useful as a tiebreaker feature | Manual / enrichment |
| 14 | **`Connected On` date clustering** — a burst of connections added right after the user attended a finance recruiting event | `Connections.csv` | **Weak-moderate, and genuinely novel.** Temporal bursts in the user's *own* export can reveal a shared context the profile data doesn't | **Export. Free, zero risk.** |
| 15 | **Group-message co-membership** — third parties appearing together in the user's own group threads | `messages.csv` `RECIPIENT PROFILE URLS` | **Weak-moderate.** The *only* third-party↔third-party co-occurrence anywhere in the export | **Export. Free, zero risk.** |

### 7.4 Recommended architecture

**Tier 1 — free, zero-risk, build this first.** Parse `Connections.csv` (+ `messages.csv`, `Company Follows.csv`, `Invitations.csv`). Normalise `Company`. Score every connection on signals **2, 5, 14, 15**. This alone produces a defensible ranked shortlist of "my connections most likely adjacent to Jane Street / Citadel," with the firm's actual employees identified with certainty.

**Tier 2 — manual enrichment, human-in-the-loop.** Build an ingestion path for **human-collected** observations from the §7.2 workflow: a simple paste/CSV of `(mutual_connection_name_or_url, target_person_url, target_firm)`. This converts probabilistic ranking into **ground truth** for whatever subset the user is willing to browse. **Design the tool to make 30 minutes of human browsing maximally productive** — that is the real engineering problem here.

**Tier 3 — optional, licensed node enrichment.** If past-employer and education signals (3, 4, 6) are wanted at scale, buy **node** data from a licensed provider (Coresignal or People Data Labs) keyed on the `URL` column. Understand the §8.2 clause-4 friction and that you become a GDPR controller.

**Do not build:** anything that automates a logged-in LinkedIn session. It is prohibited, the account-loss risk is real and severe, and it is **not necessary** — the mutual-connections view already hands over the true edges to a human for free.

---

## 8. Summary table: data source → what it gives → obtainable? → ToS risk → cost

| Data source | What it gives | Second hop? | Obtainable? | ToS / legal risk | Cost |
|---|---|---|---|---|---|
| **LinkedIn data export — `Connections.csv`** | Your full 1st-degree list: `First Name`, `Last Name`, `URL`, `Email Address`, `Company`, `Position`, `Connected On` | **No** | **YES — fast tier, ~10 min** | **None.** First-party right | **Free** |
| **LinkedIn data export — full archive** | ~30–45 CSVs incl. `messages.csv`, `Invitations.csv`, `Company Follows.csv`, `Reactions.csv`, `Ad_Targeting.csv` | **No** | **YES — ~24h, 72h window** | **None** | **Free** |
| **Sign In with LinkedIn (OIDC)** | Authenticating member's identity only | No | YES, self-serve | None | Free |
| **Share on LinkedIn** | Post as member | No | YES, self-serve | None | Free |
| **Connections API** (`r_1st_connections`) | Authenticated member's own 1st-degree list | **No — explicitly excluded** | **NO** — partner-only | N/A | Partner agreement |
| **Connections Size API** (`r_1st_connections_size`) | Count of own 1st-degrees | No | **NO** — partner-only | N/A | Partner agreement |
| **Marketing Developer Platform / Community Mgmt / Lead Sync** | Ads, org pages, org post engagement, consented leads | **No** | Partner-gated, 4wk–4mo | Contractual | ~$699+/mo reported [UNCERTAIN] |
| **Talent Solutions / Recruiter System Connect** | ATS↔Recruiter sync | **No** | Partner-gated, 3–6mo, <10% approval | Contractual | ~$900+/seat/mo reported [UNCERTAIN] |
| **Sales Navigator API (SNAP)** | Display widgets / analytics / CRM sync | **No** — Display never hands over data | **NO — closed to new partners** | N/A | Partner agreement |
| **LinkedIn UI — degree badge** | 1st/2nd/3rd on any visible person | **Proves adjacency exists** | YES, manual | None (human browsing) | Free |
| **LinkedIn UI — "X mutual connections" (clicked)** | **ENUMERABLE LIST of your connections adjacent to target** | **YES — this is the true edge set** | **YES, manual only** | None manually; **automation = §8.2 violation** | Free |
| **LinkedIn UI — company People tab + degree filter** | Firm employees badged 1st/2nd/3rd | Yes, as a target list | YES, manual | None manually | Free |
| **LinkedIn UI — `Connections of` filter** | A **1st-degree's** network, bounded by your visibility | Only pivoting on YOUR connections | LIKELY yes [MUST CHECK]; commercial-use-limit capped | None manually | Free / Premium |
| **Sales Navigator (Core/Advanced)** | Higher caps, better filters, Relationship panel, TeamLink | **No new visibility into JS/Citadel** | YES, purchasable | None (licensed use) | ~$99–160/seat/mo |
| **Proxycurl / Nubela** | *(historically profiles)* | No | **NO — SHUT DOWN 4 Jul 2025** | Sued by LinkedIn, settled | N/A |
| **Bright Data** | Public profile/company/job datasets | **No** | YES | Won *Meta* case on **logged-off public** scraping; **LinkedIn logged-in undecided**; §8.2 cl.4 friction | ~$0.001–0.01/record |
| **Coresignal / People Data Labs** | Bulk person + company + employment-history records | **No** | YES, licensed | Aggregator risk; **you become GDPR controller** | ~$250/100k; ~$300–600/mo |
| **ZoomInfo** | B2B contact/company intelligence | **No** | YES, licensed | Comparatively clean; not LinkedIn-scraped | ~$15k–40k+/yr |
| **Clay** | Enrichment orchestration | **No** | YES | Inherits sub-provider risk | ~$149–800+/mo |
| **Apollo.io / Seamless.ai** | B2B contact DB | **No** | Partly — **LinkedIn blocked extensions Mar 2025** | High | ~$49–150/user/mo |
| **PhantomBuster / Dripify / Evaboot / Lix** | Automated extraction driving **your** session cookie | Can drive the UI that shows mutuals | Technically yes | **HIGH — §8.2 violation; documented permanent bans** | ~$29–439/mo |
| **Unipile / LinkedAPI (unofficial API-as-a-service)** | REST façade over logged-in Voyager using your session | Closest available; mutual-connections endpoint **[UNVERIFIED]** | Technically yes | **HIGH — automation of your own account; §8.2** | from ~€49/mo |
| **Fullenrich** | Email/phone from a LinkedIn URL | **No** | YES | Low-moderate | ~$29–299/mo |
| **Manual browsing by the user** | **The true edge set, for free** | **YES** | **YES** | **None** | **Time only** |

---

## 9. Open questions the downstream agent must resolve

1. **[HIGH] Read a real archive.** Have the user run the export and confirm the actual filename set, the `Connections.csv` header, the preamble line count, and the `Email Address` fill rate. This eliminates every [LIKELY]/[UNCERTAIN] in Section 1.
2. **[HIGH] Mutual-connections list cap.** Open a 2nd-degree profile with many mutuals and determine whether the list is fully enumerable or truncated, and at what N.
3. ~~**[HIGH] Mutual visibility under a hidden connections list.**~~ **RESOLVED during research — mutual connections DO remain visible when a member hides their connections list.** See §3.2. Worth one spot-check in the live product, but no longer a project risk.
4. **[MEDIUM] `Connections of` / `Followers of` filters.** Confirm presence in the live 2026 free-account People search "All filters" panel. All my evidence is vendor blogspam.
5. **[MEDIUM] Company People tab pagination cap** for Jane Street / Citadel / Citadel Securities. (The Citadel entity split is now **resolved** — see §3.4.) Also confirm Jane Street's exact company-page slug.
6. **[MEDIUM] LinkedIn User Agreement current section numbering** — confirm the anti-scraping clause is still §8.2 at <https://www.linkedin.com/legal/user-agreement>.
7. **[LOW] LinkedIn Groups and Events** member/attendee-list visibility in 2026 (signals 10 and 12).
8. **[LOW] `Ad_Targeting.csv` schema** — no stable public schema exists; inspect the real file if that signal is wanted.

---

## 10. Sources

**LinkedIn export format (primary-ish, source code parsing real exports)**
- <https://raw.githubusercontent.com/humandataincome/hudi-packages-connectors/00eeaa422dcc787cfa5418cf043010e6b085cb89/src/source/linkedin/enum.linkedin.ts>
- <https://raw.githubusercontent.com/humandataincome/hudi-packages-connectors/00eeaa422dcc787cfa5418cf043010e6b085cb89/src/source/linkedin/service.linkedin.ts>
- <https://raw.githubusercontent.com/tdimino/claude-code-minoan/main/skills/integration-automation/linkedin-export/references/linkedin-export-format.md>
- Corroborating repos (GitHub code search): `brainless/dwata`, `clockless-org/html-anything`, `skygkruger/semblance-core`, `the-sniper/Dayspring`, `mitwilli-create/career-ops`, `fmhall/knowledge-base`, `Arkmurus/crucix`, `sandgraal/compass`, `lobu-ai/lobu`, `Coding-with-Adam/Dash-by-Plotly`, `srcole/personal-data-requests`

**LinkedIn official (cited but egress-blocked to this agent — verify directly)**
- <https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data>
- <https://www.linkedin.com/help/linkedin/answer/a566336/export-connections-from-linkedin>
- <https://www.linkedin.com/help/linkedin/answer/a545948/view-your-connection-s-connections>
- <https://www.linkedin.com/help/linkedin/answer/a545636/your-network-and-degrees-of-connection>
- <https://www.linkedin.com/help/linkedin/answer/a540663> (Who can see your connections)
- <https://www.linkedin.com/company/citadel-llc> · <https://www.linkedin.com/company/citadel-securities>
- <https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions>
- <https://www.linkedin.com/legal/user-agreement>
- <https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api>
- <https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-size>
- <https://learn.microsoft.com/en-us/linkedin/talent/recruiter-system-connect>
- <https://developer.linkedin.com/product-catalog>
- <https://business.linkedin.com/sales-solutions/sales-navigator-data-and-privacy-resources-new>
- <https://news.linkedin.com/2022/february/taking-legal-action-to-protect-members-against-scraping>

**Litigation**
- hiQ 9th Cir. 2022: <https://cdn.ca9.uscourts.gov/datastore/opinions/2022/04/18/17-16783.pdf> · <https://law.justia.com/cases/federal/appellate-courts/ca9/17-16783/17-16783-2022-04-18.html>
- hiQ Nov 2022 SJ: <https://caselaw.findlaw.com/court/us-dis-crt-n-d-cal/2182242.html>
- hiQ settlement: <https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators> · <https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/> · <https://newmedialaw.proskauer.com/2022/12/08/hiq-and-linkedin-reach-proposed-settlement-in-landmark-scraping-case/>
- LinkedIn v. Nubela/Proxycurl (3:25-cv-00828): <https://www.pacermonitor.com/public/case/56626528/LinkedIn_Corporation_v_Nubela_Pte_Ltd_et_al> · <https://www.law.com/therecorder/2025/01/27/linkedin-suit-says-millions-of-profiles-scraped-by-singapore-firms-fake-accounts/>
- Proxycurl shutdown (founder's own account): <https://nubela.co/blog/goodbye-proxycurl/> · <https://nubela.co/blog/is-scraping-linkedin-legal-in-2026/> · <https://nubela.co/blog/what-is-proxycurl-api-now-in-2026-im-the-founder/>
- LinkedIn v. Mantheos: <https://lawstreetmedia.com/news/tech/linkedin-settles-data-scraping-suit-with-singapore-based-mantheos/> · <https://news.bloomberglaw.com/tech-and-telecom-law/linkedin-settles-data-scraping-lawsuit-against-mantheos>
- LinkedIn v. ProAPIs: <https://www.bleepingcomputer.com/news/legal/linkedin-sues-proapis-for-using-1m-fake-accounts-to-scrape-user-data/> · <https://chatgptiseatingtheworld.com/2025/10/05/linkedin-sues-proapis-inc-complaint/> · <https://therecord.media/linkedin-sues-data-scraping-company>
- Meta v. Bright Data: <https://www.quinnemanuel.com/the-firm/news-events/client-alert-what-does-the-meta-v-bright-data-summary-judgment-ruling-mean-for-web-scraping/> · <https://blog.ericgoldman.org/archives/2024/01/game-on-bright-data-scores-major-victory-in-web-scraping-dispute-with-meta-guest-blog-post.htm> · <https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/>

**Privacy / regulatory**
- Clearview AI (Dutch DPA): <https://www.edpb.europa.eu/news/dutch-supervisory-authority-imposes-a-fine-on-clearview-because-of-illegal-data-collection-for_en> · <https://www.hunton.com/privacy-and-information-security-law/dutch-regulator-fines-clearview-ai-30-5-million-euros>
- Clearview AI (CNIL): <https://edpb.europa.eu/news/national-news/2022/french-sa-fines-clearview-ai-eur-20-million_nl>
- LinkedIn's own €310M DPC fine: <https://www.goodwinlaw.com/en/insights/publications/2024/11/insights-finance-dpc-irish-data-protection-commission-fines-linkedin>
- CA data broker registration: <https://cppa.ca.gov/data_brokers/> · <https://www.gtlaw-dataprivacydish.com/2026/01/california-data-broker-registration-deadline-arrives-jan-31-applying-to-more-businesses-than-ever/>

**Vendor / product landscape (secondary — treat with skepticism)**
- <https://www.getphyllo.com/post/linkedin-api-access-in-2026-partner-program-approval-timeline-alternatives>
- <https://linkedapi.io/guides/linkedin-sales-navigator-api> · <https://linkedapi.io/guides/proxycurl-alternatives>
- <https://www.unipile.com/pricing-api/> · <https://www.unipile.com/what-linkedin-data-can-be-pulled-from-their-api/>
- <https://www.leadgenius.com/resources/linkedins-crackdown-on-data-scrapers-why-apollo-io-and-seamless-ai-were-targeted--and-whos-next>
- <https://coresignal.com/coresignal-vs-peopledatalabs/> · <https://research.aimultiple.com/linkedin-dataset/>
- <https://www.cleanlist.ai/blog/2026-05-08-linkedin-sales-navigator-pricing-guide> · <https://evaboot.com/blog/linkedin-advanced-search>
