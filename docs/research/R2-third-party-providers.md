# R2 — Third-Party Data Providers and Enrichment APIs

**Research agent:** R2 (Wave 1)
**Date of research:** 2026-08-17. All prices and operating statuses verified against live sources on this date; this market moves fast, re-verify before contracting.
**Question this brief exists to answer:** can we buy the bipartite edge set `{(m,t) : m ∈ connections(t)}` from anyone, and if not, what are third-party providers actually good for here?

---

## Summary

- **No commercially available provider sells LinkedIn connection-graph or mutual-connection data. None. This is the finding that governs the whole build.** The reason is structural, not commercial: a member's connection list is not rendered on any logged-out surface, so scrapers — which is what every "LinkedIn data API" actually is — physically cannot see it. `M ∩ connections(t)` is computed by LinkedIn server-side and rendered *only* to the authenticated viewer, per-viewer. There is no dataset to sell because there is no artefact to scrape.
- What every dataset provider does expose is **`connections_count` — a single integer** ("Count of profile connections", capped at 500+ on the source). Coresignal's Base and Multi-source Employee APIs return `connections_count: int`; Bright Data's LinkedIn profile record returns `"connections": <number>`. A scalar degree, never an adjacency list. Treat any vendor claiming otherwise as either mislabelling this integer or selling access to *your own* authenticated session.
- **Proxycurl is dead.** LinkedIn/Microsoft sued parent Nubela in January 2025 (six claims incl. breach of contract, fraud, CFAA; alleging hundreds of thousands of fake accounts and resale of non-public data). Nubela settled rather than fight Microsoft and shut Proxycurl down on **4 July 2025**, at roughly $10M bootstrapped ARR. The team now runs NinjaPear on the same domain, which **deliberately does not scrape LinkedIn** and uses company websites — not LinkedIn URLs — as its canonical identifier. "Datasets by Nubela" is gone with it. Do not design around Proxycurl or any of its endpoints.
- **Enforcement is live and escalating, and it targets vendors, not end users.** LinkedIn sued ProAPIs Inc. on 2 October 2025 (N.D. Cal. 5:25-cv-08393) over ~1M fake accounts scraping password-walled data and reselling at up to $15k/month; the parties reached an agreement in principle in February 2026. The lesson for us: vendors that scrape *behind the login* get destroyed; vendors that scrape *logged-off public pages* have so far survived.
- **Bright Data is the only vendor in this space with a winning legal record.** Meta v. Bright Data (N.D. Cal., Judge Chen, Jan 2024) granted summary judgment that logged-off scraping of public data does not breach Meta's terms; X Corp v. Bright Data was dismissed in May 2024 on preemption and failure-to-state-a-claim grounds. That is a materially different risk posture from every other option, and it is why Bright Data is the recommended network adapter.
- **The single most relevant regulatory precedent is CNIL v. KASPR (5 Dec 2024, €240,000).** KASPR — *a Cognism-owned company* — was fined specifically for collecting contact details of LinkedIn users **who had restricted visibility to their 1st- and 2nd-degree connections**, for 5-year auto-renewing retention, for English-only notice, and for stonewalling source-disclosure on access requests. This is a regulator saying, in terms, that harvesting data a member restricted to their connection graph is unlawful. It maps directly onto our problem domain and it should shape our data-minimisation defaults.
- **The realistic role of third-party providers here is enrichment of already-known people, powering *inferred* affinity — not true edges.** Given a person, they return title, seniority, employer history with dates, education, location, skills. That supports "these two plausibly know each other" (shared employer + overlapping tenure is the only strong signal; shared school is moderate; shared city/industry is noise). It cannot produce, and must never be presented as, an edge.
- **Cheapest viable path for a single user is close to free.** Bright Data's LinkedIn Profiles Web Scraper API has a 5,000 records/month free tier and lists from ~$1.50/1K records thereafter — so 500–5,000 lookups is $0–$8. Everything else is 10×–1000× that: Coresignal ~$98 (2×Starter) to $800/mo (Pro, 10k credits); People Data Labs ~$140 at 500 and ~$1,400 at 5,000 (list, $0.28/credit); ZoomInfo/Cognism are $15k–$40k/year annual contracts and are simply not in the conversation.
- **A whole category of tools — PhantomBuster, Dux-Soup, Waalaxy, Expandi, Linked Helper, Wiza, Evaboot, ContactOut, Kaspr, and the Apify "mutual connections" actors — do not sell data at all. They rent automation of *your* account.** Several Apify actors (`dead00/linkedin-mutual-connection-analyzer`, `data_link_miner/linkedin-shared-connections`, `addeus/get-connections`) genuinely can read mutuals — because they take your `li_at` session cookie and act as you, in someone else's cloud. That is credential surrender plus an unambiguous ToS breach. **Build no adapter for any of these.**
- **The search layer is for *discovery*, not enrichment, and it got worse in 2026.** Bing Search APIs retired 11 Aug 2025. Google Custom Search JSON API is closed to new customers and fully retires 1 Jan 2027. Brave killed its free tier on 12 Feb 2026. Google is actively litigating against SerpApi (DMCA claims dismissed 21 Jul 2026; amended complaint filed 10 Aug 2026). Exa ($7/1K) and Tavily (1,000 credits/mo free, then $0.008/credit) are the sane picks, used only to resolve `site:linkedin.com/in` candidate URLs for target enumeration.
- **For algorithm validation, use SNAP `com-DBLP` and the SNAP ego-network collection, plus OpenAlex.** OpenAlex is the best structural analogue we have: author↔work is a bipartite graph whose co-authorship projection is *the identical computation* to our bridge↔target projection, it is free in bulk snapshot form, and it carries no privacy exposure. There is no public LinkedIn social-graph corpus — LinkedIn's own Economic Graph "Data for Impact" releases only aggregated/anonymised data to governments and DSA Art 40(12) requesters.
- **One commercial product does expose connection-graph structure, and it is first-party: LinkedIn Sales Navigator TeamLink** (Advanced, ~$149.99/mo billed annually). It surfaces shared connections between *your team members* and a prospect. It still does not expose `connections(t)`. Named here so nobody rediscovers it in Wave 2 and thinks it solves the problem — it does not, and it belongs to R1's territory.

---

## Provider matrix

Cost column = realistic all-in cost for the brief's 500–5,000 profile lookups, single user.

### Dataset / enrichment APIs

| Provider | Status 2026 | Data offered | Connection-graph data? | Pricing (500 → 5,000) | GDPR posture | Verdict |
|---|---|---|---|---|---|---|
| **Proxycurl / Nubela** | **DEAD** — shut 4 Jul 2025 after LinkedIn/MSFT suit | n/a | n/a | n/a | n/a | **Do not design around.** Successor NinjaPear explicitly avoids LinkedIn |
| **Coresignal** | Operating | Employee/company/jobs; Base + Multi-source Employee API; bulk datasets | **No.** `connections_count` integer only | $49/mo Starter = 250 collect credits (~$0.13–0.20/rec) → $800/mo Pro = 10k credits (~$0.05–0.08/rec). Datasets from ~$1,000 | Public-only claim; "no aggregation within secured login areas"; EWDCI-certified; opt-out form; explicit compliance page | **Build second.** Best-documented schema; good fallback when Bright Data misses |
| **People Data Labs** | Operating | Person/company enrichment + search; 1 credit per successful enrich | **No.** `profiles[]` is the person's *own* accounts across networks (network/id/url/username/first_seen/last_seen) — easy to misread as connections. It is not | ~$0.28/credit Tier-1 monthly → ~$140 / ~$1,400. Pro $98/mo = 350 credits | Registered CA data broker (reg. 190662); legitimate-interest basis; right to object; opt-out does not retract already-licensed downstream copies | **Optional third.** Costly per record for what we need |
| **Bright Data** (LinkedIn datasets + Web Scraper API) | Operating; **won Meta (2024) and X (2024)** | LinkedIn Profiles dataset 728.1M+ records (906.8M+ across LinkedIn products); Web Scraper API; ~42–50 JSON fields | **No.** Sample output shows `"connections": <int>` — a count. No names/URLs of connections | Web Scraper API **free 5,000 rec/mo**, then from ~$1.50/1K (some tiers $0.75/1K subscription) → **$0 – ~$8**. Datasets $2.50/1K, $250 minimum | Logged-off public-data-only doctrine, judicially tested; DPA/enterprise compliance program | **Build first.** Cheapest, best legal record, adequate fields |
| **Crustdata** | Operating | Real-time people/company; aggregates LinkedIn + SEC + funding + Glassdoor + G2 + news | **No** | Credit model, additive; ≤5 credits per enriched person; per-credit price not published — confirm in dashboard | Standard broker posture; no dedicated transparency page found | **Skip for v1.** Unpriced publicly = cannot budget |
| **Revelio Labs** | Operating | Workforce data, 1.1B+ profiles, transitions, headcounts, WRDS-distributed | **No.** Aggregated workforce flows, not person-to-person edges | Enterprise/academic (WRDS) | Public-profile derivation | Skip — wrong granularity |
| **BoardEx / RelSci** (Altrata) | Operating | Curated *relationship* graphs: BoardEx 1.6M executives / 2.1M profiles; RelSci 10.5M decision-makers | **Adjacent but no.** Edges are inferred from boards, filings, employment and education overlap — not LinkedIn connections | Enterprise, unpublished | Curated/public-record sourcing | Skip. Conceptually instructive (the market's answer to "who knows whom" is *inferred co-affiliation*) but JS/Citadel staff are overwhelmingly not board members — coverage would be near-zero |

### Account-automation tools (rent your session — not data vendors)

| Provider | Status 2026 | What it actually is | Connection-graph data? | Pricing | GDPR posture | Verdict |
|---|---|---|---|---|---|---|
| **Apify** (LinkedIn actors) | Operating marketplace | Two distinct classes: (a) cookie-free public-profile scrapers (~$10/1K, HarvestAPI company from $3/1K); (b) **cookie-driven actors that do read mutuals** | (a) No. (b) **Yes — by driving your authenticated session** | Free $0 with $5/mo usage; Starter $29; Scale $199. ~$0.20/CU | Marketplace; compliance sits with each actor author — i.e. nowhere | (a) acceptable fallback; **(b) hard no** — hands `li_at` to a third-party cloud |
| **PhantomBuster** | Operating | Cloud automation of your account; ≤1,500 profiles/day | Only via your session | $56–$439/mo (Pro ~$127/mo annual) | Tool vendor, not controller of the scrape | **No adapter.** Ban risk reported as materially higher in 2026 |
| **Clay** | Operating | Orchestration + waterfall over PDL/Clearbit/Hunter etc. | **No native mutual-connections source**; community reports only PhantomBuster/Apify workarounds | Launch $185/mo (10k credits), Growth $495/mo (25k) since Mar 2026. Credits burn **per attempt, not per success**. Most LinkedIn workflows need a Sales Nav seat | Inherits every downstream provider's posture | **No adapter.** A paid wrapper over sources we can call directly |
| **Dux-Soup** | Operating | Browser/cloud automation | Only via your session | from $14.99/mo; Cloud $99/mo | ToS-violating by design | No adapter |
| **Waalaxy** | Operating | Extension + desktop companion | Only via your session | Free tier upward | ToS-violating by design | No adapter |
| **Expandi** | Operating | Cloud automation | Only via your session | $99/mo | ToS-violating by design | No adapter |
| **Linked Helper 2** | Operating | Extension driving your real browser | Only via your session | ~$15–$45/mo | ToS-violating by design | No adapter |

### Contact-data / export tools

| Provider | Status 2026 | Data offered | Connection-graph data? | Pricing | GDPR posture | Verdict |
|---|---|---|---|---|---|---|
| **Lix** | Operating | LinkedIn export + small API | No | Free 50 credits/mo; Leads £39/mo; Data Plus £109/mo; API packs $49/500 or $49/2,500; +$0.15/email | Standard broker | Skip — email-centric, we need no emails |
| **Evaboot** | Operating | Sales Nav list export/clean | No | $9–$99/mo **plus Sales Navigator ~$100/mo** | Runs against your Sales Nav session | Skip — ToS risk to a paid subscription |
| **Wiza** | Operating | Sales Nav export + verified emails | No | from $49/mo (100 reveals) | Self-declared GDPR/CCPA; extension-based, gray area | Skip |
| **ContactOut** | Operating | Email/phone from profiles | No | ~$99/mo | SOC 2; claims Art. 6(1)(f) legitimate interest + "manifestly made public" | Skip — contact data we neither need nor should hold |
| **Prospeo** | Operating | Email/phone finder | No | $19/mo (1k emails); Starter $49; Business $249 | Standard | Skip |
| **Datagma** | Operating | Real-time enrichment, API + extension | No | $39/mo (1k credits) → $249/mo (20k) | Claims GDPR compliance | Skip |
| **FullEnrich** | Operating | Waterfall across 15–20 providers (Hunter, Dropcontact, **Kaspr**, Datagma, Apollo…) | No | Credit packs | **Weakest-link problem**: one non-compliant provider contaminates the waterfall; the roster includes CNIL-fined Kaspr | **Avoid** |
| **Dropcontact** | Operating | Algorithmic email find/verify, **no stored personal database** | No | ~€24/mo (1k credits); Business €79/mo (5k) | **Strongest GDPR story in the category** — nothing retained | Not applicable to our problem, but note the pattern |
| **Lusha** | Operating | B2B contacts | No | Seat-based | ISO 27001 / 27701 / SOC 2 Type II; GDPR + CCPA claims | Skip |
| **Apollo.io** | Operating | B2B contacts + enrichment API | No | $49–$119/user/mo annual; 30k–72k credits/yr; extras ~$0.03–0.05/credit; 1 credit/email, 8/phone | **No published ISO/SOC certification** of the kind peers hold | Skip — cheap, but contact-data creep with no graph payoff |
| **ZoomInfo** | Operating | B2B intelligence | No | ~$15k/yr Professional → $40k+ Elite; median contract $31,875/yr; 3-seat minimum, annual only | ISO/SOC; long compliance history | **Out of scope** on cost alone |
| **Cognism** | Operating | B2B intelligence | No | ~$22.5k/yr (Grow, 5 seats) → ~$37.5k/yr (Elevate) | Documented LIAs, DSAR handling — **but owns Kaspr, fined €240k by CNIL** | **Out of scope**; the Kaspr fine is the cautionary tale of this whole brief |

### Search layer

| Provider | Status 2026 | Use here | Pricing | Verdict |
|---|---|---|---|---|
| **Exa** | Operating | Resolve `site:linkedin.com/in` candidate URLs for target enumeration | $7/1K searches (≤10 results), +$1/1K per extra result; Contents $1/1K pages/type; Websets Core $49/mo (8k credits) | **Recommended discovery adapter** |
| **Tavily** | Operating | Same | **1,000 credits/mo free**; $30/4,000; PAYG $0.008/credit; basic search 1 credit / advanced 2 | **Recommended free-tier default** |
| **SerpApi** | Operating **but under active Google litigation** | Raw Google SERPs | Free 250/mo; $25/1,000; $75/5,000; $150/15,000. No rollover | Avoid — DMCA claims dismissed 21 Jul 2026 but Google filed an amended complaint 10 Aug 2026; unresolved |
| **Serper** | Operating | Cheap Google SERPs | $1/1K (Starter) → $0.30/1K (Ultimate) | Cheapest, same litigation-adjacent category |
| **Brave Search API** | Operating, **free tier retired 12 Feb 2026** | Independent index | $5 monthly credit then $5/1,000 requests | Viable, no longer free |
| **Google Custom Search JSON API** | **Closed to new customers; full retirement 1 Jan 2027** | — | $5/1,000, 100/day free, 10k/day cap | **Do not build on it** |
| **Bing Search API** | **RETIRED 11 Aug 2025** | — | — | Gone |

### Open / academic datasets

| Dataset | Status | Use here | Cost | Verdict |
|---|---|---|---|---|
| **SNAP `com-DBLP`** | Available | Co-authorship network, 317,080 nodes, 5,000+ ground-truth communities (venues as labels); Yang & Leskovec top-5000 convention | Free | **Use for community-detection validation** |
| **SNAP ego-networks** (Facebook/Twitter/Google+) | Available | 1,000+ ego-networks; Facebook set has human-labelled ground-truth circles | Free | **Use for ego-network / bridge-clustering validation** — closest structural match to our problem |
| **OpenAlex** | Available; **Feb 2026 introduced usage-based pricing for the hosted API** (modest daily free allowance); monthly S3 snapshot | Author↔work bipartite → co-authorship projection is *the identical computation* to bridge↔target projection. ~4.6M ORCID-bearing researchers in the Mar 2026 snapshot | Snapshot free | **Use as the structural analogue / integration fixture** |
| **LinkedIn Economic Graph "Data for Impact"** | Available to governments, multilaterals, DSA Art 40(12) | Aggregated + anonymised only | Free but gated | Not usable |
| No public LinkedIn social-graph corpus exists | — | — | — | Confirmed absent; LinkedIn is under-represented in research corpora precisely because the graph is not obtainable |

---

## Findings

### Proxycurl / Nubela — definitively dead, and the reason matters

LinkedIn and Microsoft sued Nubela in January 2025. The complaint pleaded six claims including breach of contract, fraud and CFAA violations, and alleged that Nubela created hundreds of thousands of fake LinkedIn accounts in order to scrape millions of profiles — **including non-public data** — and resold it through the Proxycurl API. Founder Steven Goh's account is that the business had bootstrapped to roughly $10M ARR with no venture funding and therefore no war chest to defend against Microsoft; Nubela settled and wound the service down, posting the goodbye notice on **4 July 2025**.

The successor product, NinjaPear, lives at the same domain. Its API surface covers company data, customer listings, competitor mapping, employee search and RSS monitoring. The endpoints that defined Proxycurl — People API, Jobs API, School API, person and company search — **are no longer documented**, and the founder states the new product deliberately does not scrape LinkedIn and uses company websites rather than LinkedIn URLs as its canonical identifier. "Datasets by Nubela" is not a live product; there is no downloadable Nubela dataset offering in 2026.

The discriminating detail for us: what killed Proxycurl was **fake accounts reaching data behind the login**. That is exactly the technique that would be required to obtain connection lists. The one vendor that plausibly could have sold graph-adjacent data was sued out of existence for the method needed to get it.

- https://nubela.co/blog/goodbye-proxycurl/
- https://nubela.co/blog/what-is-proxycurl-api-now-in-2026-im-the-founder/
- https://www.startuphub.ai/ai-news/startup-news/2025/the-1-linkedin-scraping-startup-proxycurl-shuts-down
- https://linkedapi.io/guides/proxycurl-alternatives
- https://www.cleanlist.ai/alternatives/proxycurl

### Coresignal

Operating and healthy. Sells a Base Employee API, a Multi-source Employee API, company and jobs APIs, plus bulk datasets, over an advertised pool of 4.5B+ records across 50+ countries.

**Connection-graph:** no. The Base Employee API data dictionary defines `connections_count` as "Count of profile connections", `Integer`, example value `500`. The Multi-source Employee API carries the same field. There is no field containing connection identities. This is a degree scalar, and because LinkedIn itself censors above 500, it is a *censored* degree scalar — useful as a weak prior on network size, useless as an edge.

**Pricing:** credit-based with two credit types — Search credits run queries, Collect credits fetch full records; one Collect credit per profile, doubled for multi-source. Starter $49/mo = 250 Collect + 500 Search. Pro from $800/mo = ≥10,000 Collect + 20,000 Search. Premium custom. Effective per-record: ~$0.133–0.196 (Starter), ~$0.050–0.080 (Pro), ~$0.005–0.030 (Premium). Standalone datasets from ~$1,000. Free tier available for testing.
→ **500 lookups ≈ $98** (two Starter months, or one month if you only need 250/mo). **5,000 lookups ≈ $800** (one Pro month).

**GDPR/CCPA:** the most explicit posture of the dataset vendors. Coresignal publishes a Data Transparency page and a Notice of the Right to Opt-Out, states it collects only publicly available, business-related data, and specifically states it "strictly refrains from any data collection that involves any kind of data aggregation within secured login areas." It is certified by the Ethical Web Data Collection Initiative. Opt-out is by form or email.

**API quality:** the best-documented in the category — published data dictionaries per API, sample payloads, webhook subscriptions, dated release notes.

**Exposure:** moderate. Public-only claim is credible and matches the field set (no contact-behind-login, no connection lists). Residual risk is the generic one that LinkedIn's ToS bind scrapers regardless of publicness.

- https://docs.coresignal.com/employee-api/base-employee-api/data-dictionary-base-employee-api
- https://docs.coresignal.com/employee-api/multi-source-employee-api/data-dictionary-multi-source-employee-api
- https://docs.coresignal.com/introduction/pricing-and-subscriptions
- https://coresignal.com/data-transparency/
- https://coresignal.com/privacy-rights/
- https://pipeline.zoominfo.com/sales/coresignal-pricing

### People Data Labs

Operating. Person Enrichment API, Person Search, Company APIs; 1 credit per *successful* enrichment.

**Connection-graph: no — and there is a specific trap here.** The person schema includes a `profiles[]` array with `network`, `id`, `url`, `username`, `first_seen`, `last_seen`. That is a list of **the subject's own accounts across different networks** (their LinkedIn, their GitHub, their Twitter). It is *not* a list of their connections. A careless reading of the schema will produce a wrong architecture. There is no connections field of any kind in PDL beyond what the source page exposes.

Actual useful fields: name components, location, `job_company_*` (name, id, size, industry, location), job title with normalised seniority/role, `experience[]` with `company_id/company_name/title/first_seen/last_seen/num_sources`, `education[]`, `skills[]` (lowercased, whitespace-stripped), `interests[]` (cleaned, no canonical list), `birth_year`. Field bundles differ between base and premium packages with published fill rates.

**Pricing:** published person-enrichment pricing from **$0.28 per credit** on monthly Tier 1, with volume discounts. Pro plan $98/mo = 350 person-enrichment credits.
→ **500 lookups ≈ $140. 5,000 lookups ≈ $1,400** at list; less on a committed tier.

**GDPR/CCPA:** PDL is a **registered California data broker** (CPPA/DOJ registration 190662). Its privacy policy states it processes personal information only with a legal basis — contract, legitimate interests, consent where required, or legal obligation — and grants EEA/UK/Swiss individuals the right to object to legitimate-interest processing and to direct-marketing processing. Notably, PDL states that an opt-out prevents future sharing but **"downstream licensed datasets may update on a later delivery"** — i.e. copies already sold do not vanish. For our purposes that is an argument for not creating another copy.

**Exposure:** moderate-high reputationally (data-broker registration is public and PDL has been named in aggregation-breach coverage historically), low operationally.

- https://docs.peopledatalabs.com/docs/fields
- https://docs.peopledatalabs.com/docs/person-data-overview
- https://docs.peopledatalabs.com/docs/person-data-field-bundles
- https://support.peopledatalabs.com/hc/en-us/articles/25794271805211-Pricing-credits
- https://oag.ca.gov/data-broker/registration/190662
- https://privacy.peopledatalabs.com/policies?name=privacy-policy

### Bright Data — the recommended network adapter

Operating, large, and **the only vendor here with a courtroom record on the right side**.

**Legal position.** In *Meta Platforms v. Bright Data* (N.D. Cal.), Judge Edward Chen granted summary judgment for Bright Data in January 2024, holding that the Facebook and Instagram terms "do not bar logged-off scraping of public data" — Bright Data was logged out when it collected. In *X Corp v. Bright Data* the court dismissed X's complaint in May 2024, finding X had failed to state a claim based on access to its public site and that any claim based on copying public data was preempted by the Copyright Act; X was subsequently granted leave to amend in part. Neither case is LinkedIn, and neither is a licence to do anything one likes — but the doctrine they establish (logged-off + public ≠ contract breach) is exactly the doctrine Bright Data's LinkedIn products are engineered to sit inside.

**Products.** LinkedIn datasets aggregate 906.8M+ records; the Profiles dataset alone is 728.1M+; Posts 119.9M+. Delivery in JSON / NDJSON / Parquet with documented field definitions via a Dataset Metadata API, plus free downloadable samples per dataset. Separately, a Web Scraper API for on-demand collection.

**Connection-graph: no.** The profile record is ~42 documented fields (50+ in the API's JSON), covering `name`, `city`, `position`/`job title`, `about`, `experience[]` (title, company, duration, description), `education`, `skills`, `current_company{name,title}`, `followers`, `url`, `posts`. The connections field in the sample output is `"connections": <int>` — a bare number. **No names, no URLs, no adjacency.** I verified this against the vendor's own published sample rather than marketing copy, because the marketing copy for this product literally uses the word "connections" in a way that reads as if edges were included. They are not.

**Pricing.** LinkedIn Profiles Web Scraper API: **free tier 5,000 records/month**; list from ~$1.50 per 1,000 records, with some subscription tiers quoted at $0.75/1K. Datasets from $2.50/1K with a $250 minimum order.
→ **500 lookups: $0** (free tier). **5,000 lookups: $0** (exactly at the free tier) **or ~$7.50** at list. This is the cheapest credible option by two to three orders of magnitude.

**API quality:** good. Async job model, webhook or poll delivery, schema metadata endpoint, stable field names, free sample downloads for schema validation before writing an adapter.

**GDPR posture:** public-web-only doctrine, judicially tested; enterprise DPA available; trust-centre documentation. Not risk-free — a data subject in the EU can still object to a broker holding their profile — but it is the best-defended position available.

- https://brightdata.com/products/datasets/linkedin/profiles
- https://brightdata.com/products/datasets/linkedin
- https://brightdata.com/products/web-scraper/linkedin/profiles
- https://docs.brightdata.com/datasets/scrapers/linkedin/collection-options
- https://github.com/luminati-io/LinkedIn-Scraper (sample output confirming `"connections"` is numeric)
- https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/
- https://www.proskauer.com/release/proskauer-secures-dismissal-of-scraping-claims-against-bright-data
- https://brightdata.com/blog/web-data/court-rules-in-favor-of-bright-data-in-meta-v-bright-data-case

### Apify — two products wearing one name

Apify is a marketplace, so "is Apify safe" is not a well-formed question. There are two distinct classes of LinkedIn actor and they have opposite risk profiles.

**Class (a): cookie-free public-profile scrapers.** Mass LinkedIn Profile Scraper at ~$10 per 1,000 public profiles, explicitly "no cookies"; HarvestAPI LinkedIn Company Scraper from $3 per 1,000 companies; various jobs scrapers. Same doctrinal footing as Bright Data but with no legal record behind it and no vendor accountability — the actor author, not Apify, is the data controller in practice.

**Class (b): session-cookie actors that genuinely read the connection graph.** These exist and they work:
- `dead00/linkedin-mutual-connection-analyzer`
- `data_link_miner/linkedin-shared-connections` (also exposed as an MCP server)
- `data_link_miner/linkedin-network-scraper`
- `addeus/get-connections`
- plus a general `linkedin-session-cookies` input helper and a cookie/session manager actor that stores and refreshes credentials for other actors

They read mutuals **because you give them your `li_at` cookie and they act as you**. This is the only third-party route to real edges, and it is the one route the build must refuse. A LinkedIn session cookie is a bearer credential for the full account: messages, connections, InMail, Sales Navigator, any linked billing. Handing it to a third-party cloud is unbounded account impersonation, it is a flat ToS violation, and — per CNIL/KASPR below — collecting data the subject restricted to their connection graph is treated by at least one EU regulator as unlawful processing regardless of how it was technically obtained.

**Platform pricing:** Free $0 with $5/month of usage that resets, no card required; Starter $29; Scale $199; Business $999 — each fee is a prepaid usage budget, drawn down by compute units (~$0.20/CU on Free/Starter), proxy bandwidth, storage ops and actor fees, with pay-as-you-go overage after. Residential proxy $8/GB.
→ **500 public-profile lookups ≈ $5 + platform. 5,000 ≈ $50 + $29 Starter.**

- https://apify.com/dead00/linkedin-mutual-connection-analyzer
- https://apify.com/data_link_miner/linkedin-shared-connections/api/mcp
- https://apify.com/addeus/get-connections
- https://apify.com/big-brain.io/linkedin-session-cookies/input-schema
- https://use-apify.com/docs/best-apify-actors/best-linkedin-scrapers
- https://use-apify.com/docs/what-is-apify/apify-pricing

### Crustdata

Operating; positions itself as real-time B2B data for AI agents. People Data API allows retrieving enriched data for one or more LinkedIn profiles in a single request. Aggregates continuously from LinkedIn, SEC filings, funding databases, Glassdoor, G2, web-traffic services and news aggregators.

Credit model; maximum current cost for a single enriched person record is 5 credits, with additive pricing (base profile first, extra credits for extra data). **Crustdata's own docs warn that pricing changes by plan, entitlement and endpoint version and must be confirmed in the dashboard.** No public per-credit dollar figure — which alone disqualifies it for a v1 adapter, because we cannot implement a budget guard against an unknown unit price.

No connection-graph product of any kind.

- https://crustdata.com/pricing
- https://docs.crustdata.com/general/pricing
- https://docs.crustdata.com/docs/discover/people-data-api/

### PhantomBuster

Operating; $56–$439/month depending on volume (Pro ~$127/mo on annual billing; realistic all-in $162–200+ with email verification and CRM add-ons). Cloud-hosted "Phantoms" scrape up to ~1,500 profiles/day and automate connection requests while your machine is off.

It has no dataset. Everything it returns comes from *your* account. Multiple 2026 sources report LinkedIn's anti-automation detection materially sharpened — including behavioural/timing fingerprinting — with account restriction and permanent ban outcomes documented for aggressive use. Recommended mitigations amount to "use a burner account and stay under 100 profile views/day", which is both an admission of the risk and a rate ceiling that makes 5,000 lookups a multi-month project.

- https://lagrowthmachine.com/phantombuster-pricing/
- https://stormy.ai/blog/linkedin-automation-safety-2026-phantombuster-bereach-guide
- https://autoposting.ai/blog/phantombuster-review

### Clay

Operating. Simplified to two plans in March 2026: **Launch $185/mo (10,000 credits)** and **Growth $495/mo (25,000 credits)**, with a split between "credits" (data-provider lookups) and "actions" (workflow steps). Data credit costs dropped 50–90% in that update. 100 credits ≈ 25–50 enrichments depending on waterfall depth.

Two disqualifying details. First, **credits are consumed per attempt, not per success** — a waterfall in which every provider misses still bills. Second, most high-value LinkedIn workflows in Clay require an active Sales Navigator subscription per user, which is a hidden ~$1,200–1,800/year floor.

On connection data: Clay has **no native mutual-connections enrichment source**. Its own community threads confirm users cannot enrich on mutual LinkedIn connections directly; the documented "warm intro" pattern is a heuristic built on cross-referencing "People Also Viewed" against your network, and the genuine workarounds route through PhantomBuster or Apify cookie actors — i.e. back to the unsafe category.

Clay is a waterfall orchestrator over providers we can call directly. For a single-user local tool it adds cost, a Sales Nav dependency, and an extra data controller, for no capability we cannot build.

- https://www.cleanlist.ai/blog/2026-03-12-clay-pricing-changes-2026
- https://salesmotion.io/blog/clay-pricing
- https://community.clay.com/x/community/jj5tfe1vxlg6/exploring-linkedin-connections-for-lead-enrichment
- https://www.clay.com/blog/harnessing-people-also-viewed-find-warm-intros-in-your-linkedin-network-automatically-with-clay

### Dux-Soup, Waalaxy, Expandi, Linked Helper

All operating; all are outreach-automation tools that drive the user's own LinkedIn account; none sells data.

Reported 2026 posture: Dux-Soup from $14.99/mo with a Cloud tier at $99/mo, whose Chrome-extension plans are reported to carry substantially higher ban risk than cloud equivalents; Linked Helper 2 runs as an extension controlling the real browser (higher detection surface); Waalaxy is extension + desktop companion executing from the local browser and is characterised as tolerable only inside conservative limits (<80 invitations/week); Expandi at $99/mo is cloud-routed. Sources disagree about whether cloud or extension is riskier, which is itself informative: nobody actually knows, because LinkedIn does not publish its detection model.

The category-level fact that matters: **LinkedIn's Prohibited Software Policy explicitly bans data-extraction tools**, and using any of these puts the user's primary account — and any Sales Navigator subscription attached to it — at risk. For a tool whose entire premise is that the user's own network is the asset, risking the account that holds that network is a catastrophic trade.

- https://www.dux-soup.com/blog/the-best-linkedin-automation-tools-tried-and-tested
- https://connectsafely.ai/articles/dux-soup-review-linkedin-automation-alternative-2026
- https://linkedintools.site/is-waalaxy-safe/
- https://leadsmonky.com/expandi-vs-dux-soup/
- https://northlight.ai/blog/linkedin-automation-without-getting-banned

### Lix, Evaboot, Wiza, ContactOut, Prospeo, Datagma, FullEnrich, Dropcontact

A single category: turn a LinkedIn profile or a Sales Navigator list into contact details. **None exposes any connection data.** Our problem needs no email addresses and no phone numbers, so integrating any of them would mean acquiring more personal data than the task requires — the opposite of the minimisation the handoff plan mandates.

- **Lix** — Free 50 credits/mo; Leads £39/mo (300–1,000 email credits, 3,000 rows); Data Plus £109/mo (500–10,000 email credits, unlimited search); API packs $49/500 credits (Standard) or $49/2,500 (Organization), 10 free test credits; additional emails $0.15 each. Claims coverage of 756M individuals / 4M companies.
- **Evaboot** — $9/mo (100 credits) to $99/mo (1,200 credits), **plus mandatory Sales Navigator ~$100/mo**. Cleans and exports Sales Nav lists; runs against your session; risks the Sales Nav subscription itself.
- **Wiza** — from $49/mo for 100 email reveals, unlimited export without emails; Sales Nav integration; verification at export. Self-declares GDPR/CCPA compliance; independent reviews describe it as operating in a legal gray area where automated scraping may violate LinkedIn ToS and aggressive use triggers anti-automation detection.
- **ContactOut** — ~$99/mo entry. The most legally articulate of the group: SOC 2 certified, states the data it processes has been "manifestly made public" by the data subject, and asserts a legitimate interest under **GDPR Article 6(1)(f)**. That is a real argument, but it is an argument, not an adjudication — and the KASPR decision shows a regulator rejecting a materially similar framing.
- **Prospeo** — Email Finder $19/mo (1,000 emails), Starter $49/mo, Business $249/mo.
- **Datagma** — Discover $39/mo (1,000 credits), Growth $99/mo (5,000), Business $249/mo (20,000); API + Chrome extension on all paid plans, 12-month credit rollover; claims GDPR compliance. Independently operating, no acquisition found.
- **FullEnrich** — waterfall across 15–20 providers including Hunter, Dropcontact, **Kaspr**, Datagma and Apollo. This is the structural problem with waterfalls: compliance is min(), not max(). One non-compliant provider contaminates the result, and the roster includes the company CNIL fined. **Avoid.**
- **Dropcontact** — ~€24/mo for 1,000 credits, Business €79/mo for 5,000. Genuinely different architecture: algorithmic real-time find/verify with **no stored personal-data database**, which is the strongest GDPR position in the entire brief. Irrelevant to our problem (it finds emails, it does not describe people) but worth noting as the model of what a defensible provider looks like.

- https://www.capterra.com/p/10017049/Lix/pricing/
- https://derrick-app.com/tools/evaboot-pricing
- https://puzzleinbox.com/blog/evaboot-pricing-review/
- https://derrick-app.com/en/linkedin-email-finder-comparison-2026/
- https://contactout.com/privacy
- https://syncgtm.com/blog/prospeo-alternatives
- https://www.saleshandy.com/blog/datagma-pricing/
- https://www.knowlee.ai/blog/tools/fullenrich-alternatives
- https://syncgtm.com/blog/dropcontact-review

### Lusha, Apollo.io, ZoomInfo, Cognism — and the KASPR fine

**Certifications.** Lusha, ZoomInfo and Cognism hold ISO 27001, ISO 27701 and SOC 2 Type II. **Apollo has no published compliance certification of that kind**, despite being one of the most widely used platforms in the category. Cognism publishes documented GDPR process depth including legitimate-interest assessments, DSAR handling and breach-notification timelines. All four are B2B data brokers holding professional profiles.

**Pricing.** Apollo $49/$79/$119 per user per month on annual ($65/$99/$149 monthly), with 30,000 (Basic) / 48,000 (Professional) / 72,000 (Organization) credits per year; extra credits roughly $0.03–0.05 each or ~$25 per 1,000; 1 credit per email reveal, 8 per phone, 1–8 per enrichment. ZoomInfo ~$14,995/yr Professional to $40,000+ Elite, median contract $31,875/yr, annual-only with a three-seat minimum. Cognism ~$15k platform + ~$1.5k/user (Grow, ≈$22.5k/yr for 5 seats) to ~$25k + ~$2.5k/user (Elevate, ≈$37.5k/yr). Neither ZoomInfo nor Cognism publishes rates.

**None of them sells connection data.**

**The KASPR decision is the most important regulatory fact in this brief.** On **5 December 2024 the CNIL fined KASPR — a Cognism-owned company — €240,000**. KASPR sells a Chrome extension that reveals professional contact details of people whose LinkedIn profiles you visit, backed by a database of ~160 million contacts. The findings:

1. It collected contact details of LinkedIn users **who had expressly chosen to limit visibility to their 1st- and 2nd-degree connections**.
2. It retained contact details for five years from each data update, and the retention clock **auto-renewed** whenever someone changed employer — effectively indefinite retention.
3. Until 2022 it did not inform data subjects at all; from 2022 the notice was English-only, which the CNIL held insufficiently clear and transparent.
4. On subject access requests asking for the source of the data, it merely said "publicly available sources" — inadequate under the right of access.

The CNIL ordered compliance within six months (deadline 18 June 2025), specifically requiring KASPR to **cease collecting data from individuals who have chosen to limit the visibility of their contact details and delete data already collected that way**; the order was later closed.

Read that against our problem. Our targets are Jane Street and Citadel employees — a population unusually likely to restrict profile visibility. A regulator has already held that collecting data those people restricted to their connection graph is unlawful, and has ordered deletion. That is a direct, on-point precedent, and it is why the build's defaults must be minimisation, local-only storage and a working purge path.

- https://www.cnil.fr/en/data-scraping-kaspr-fined-eu240000
- https://www.edpb.europa.eu/news/news/2025/data-scraping-french-supervisory-authority-fined-kaspr-eu240-000_en
- https://www.cnil.fr/en/closure-order-issued-against-kaspr
- https://www.gerrishlegal.com/blog/cnil-fines-kaspr-240000-for-illegally-collecting-linkedin-users-contact-details
- https://www.scworld.com/brief/linkedin-data-scraping-nets-almost-250k-fine-for-kaspr
- https://salesmotion.io/blog/apollo-pricing
- https://www.pin.com/blog/zoominfo-pricing/
- https://www.cognism.com/blog/zoominfo-pricing
- https://puzzleinbox.com/blog/apollo-vs-zoominfo-vs-cognism-vs-lusha-2026/

### LinkedIn's own enforcement posture, 2025–2026

Two live datapoints beyond the Nubela settlement:

- **LinkedIn Corp. v. ProAPIs Inc.**, N.D. Cal. **5:25-cv-08393**, filed **2 October 2025**. LinkedIn alleged ProAPIs and its founder/CTO built an industrial-scale system of **more than a million fake accounts** to collect member information, posts, reactions and comments — **including data available only behind LinkedIn's password wall** — and resold it for up to $15,000/month, with a Pakistan-based technical enabler (Netswift) also named. The parties filed a stipulation to stay proceedings on 6 February 2026 (modified 9 February), and reached an **agreement in principle to settle**, terms undisclosed.
- Bloomberg Law characterises this as a perpetual struggle rather than a resolved question: LinkedIn keeps suing, scrapers keep appearing.

The consistent pattern across Nubela and ProAPIs: **LinkedIn pursues vendors who use fake accounts to reach data behind the login.** It has not, in this period, pursued end users of enrichment data or vendors confining themselves to logged-off public pages. That asymmetry is what makes a Bright-Data-class adapter defensible and a cookie-actor adapter indefensible.

- https://www.courtlistener.com/docket/71527603/linkedin-corporation-v-proapis-inc/
- https://chatgptiseatingtheworld.com/2025/10/05/linkedin-sues-proapis-inc-complaint/
- https://www.bleepingcomputer.com/news/legal/linkedin-sues-proapis-for-using-1m-fake-accounts-to-scrape-user-data/
- https://thelinkedblog.com/2026/linkedin-reaches-deal-in-data-scraping-lawsuit-against-proapis-3857/
- https://news.bloomberglaw.com/artificial-intelligence/linkedin-battles-online-scrapers-in-perpetual-struggle-over-data

### Search layer

The programmatic-search market degraded significantly in the last twelve months and two of the obvious defaults are gone.

- **Bing Search APIs: retired 11 August 2025.** Fully decommissioned; no new signups; Microsoft points users to Grounding with Bing Search inside Azure AI Agents, which is a different product at a different price point.
- **Google Custom Search JSON API: closed to new customers as of 2025, existing users supported until 1 January 2027, then retired.** 100 queries/day free, then $5 per 1,000, capped at 10,000/day. Google recommends Vertex AI Search instead. **Building on it now would be building on a scheduled retirement.**
- **SerpApi:** free 250 searches/mo; $25/1,000; $75/5,000; $150/15,000; $275/30,000; no rollover. **Google sued SerpApi.** The court granted SerpApi's motion to dismiss on 21 July 2026, rejecting Google's DMCA anti-circumvention theory and holding search results are not protected by copyright — but Google filed an **amended complaint on 10 August 2026**, rebuilt around written permission from copyright owners. This is unresolved a week before this brief's date. Not a foundation for a tool we want to still work next year.
- **Serper:** $1 per 1,000 (Starter) to $0.30 per 1,000 (Ultimate). Cheapest, same litigation-adjacent category as SerpApi.
- **Brave Search API:** on **12 February 2026** Brave retired its perpetual free tier, replacing it with a $5 monthly credit; Search bills at $5 per 1,000 requests, Answers $4/1,000 queries plus $5/M tokens. Roughly 1,000 free searches/month before the card is charged. Independent index, which is a genuine differentiator.
- **Exa:** $7 per 1,000 searches (up to 10 results), +$1 per 1,000 for each result beyond the tenth; Deep Search $12, Deep-Reasoning $15, Answer $5; Contents endpoint $1 per 1,000 pages per content type. Websets Core $49/mo (8,000 credits), Pro $449/mo (100,000). Pure usage-based, no seats, no monthly minimum.
- **Tavily:** **1,000 credits/month free**; Project $30/4,000, Bootstrap $100/15,000, Startup $220/38,000, Growth $500/100,000; PAYG $0.008/credit. Basic search 1 credit, advanced 2.

**Applicability to `site:linkedin.com/in`.** LinkedIn's robots.txt is broadly restrictive (`Disallow: /` among other rules) while public profiles remain indexed by major engines. A `site:` query therefore returns profile URLs and snippet text — enough to *enumerate candidate targets* and resolve names to public profile slugs, and nothing more. It cannot return connection data, and snippet text is a poor enrichment source (truncated, stale, inconsistent). Use the search layer for **target discovery only**, then hand the resolved URLs to an enrichment adapter.

- https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement
- https://developers.google.com/custom-search/v1/overview
- https://blog.expertrec.com/google-custom-search-json-api-simplified/
- https://serpapi.com/blog/google-v-serpapi-motion-to-dismiss-why-were-in-the-right/
- https://scrapebadger.com/blog/google-sued-a-scraper-under-copyright-law-and-lost-heres-what-the-serpapi-ruling-actually-says
- https://ppc.land/serpapi-faces-revived-google-scraping-claims-built-on-reddit-licensing-terms/
- https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/
- https://exa.ai/pricing
- https://coldiq.com/blog/tavily-pricing
- https://apiserpent.com/blog/serp-api-pricing-comparison

### Academic / open datasets for validation

**There is no public LinkedIn social-graph corpus.** LinkedIn is deliberately under-represented in network-science research for exactly the reason this brief exists — the graph is not obtainable. LinkedIn's own Economic Graph "Data for Impact" programme releases only anonymised, aggregated datasets (100+ countries, 148 industries, 50,000 skill categories) and prioritises governments, multilateral organisations and EU DSA Art 40(12) requesters. Not usable as a stand-in for edges.

What *is* usable:

- **SNAP (snap.stanford.edu/data)** — around 80 real-world social, citation, collaboration, web and communication networks.
  - **`com-DBLP`**: co-authorship network, **317,080 nodes**, publication venues as **5,000+ ground-truth communities**. The standard evaluation convention (Yang & Leskovec) is to take the top-5,000 highest-quality communities and discard those with fewer than 20 members. This is the right dataset for validating our community-detection step against labels.
  - **SNAP ego-networks** — 1,000+ ego-networks from Facebook, Twitter and Google+. The Facebook set comprises 10 ego-networks whose central user **hand-labelled the social circles** their friends belong to. This is the closest published analogue to our actual problem: given one person's neighbourhood, recover the meaningful sub-groups. Use it for bridge-clustering validation.
  - `com-Amazon`, `com-Youtube`, `com-LiveJournal`, `com-Orkut` for scale testing.
- **OpenAlex** — fully open catalogue from the nonprofit OurResearch; free API plus a **monthly S3 database snapshot**; ~4.6M ORCID-bearing researchers in the March 2026 snapshot; aggregates and standardises ORCID identifiers. Note that in **February 2026 OpenAlex introduced usage-based pricing for the hosted API** with a modest daily free allowance — **the bulk snapshot remains the free path** and is what we should use.

  OpenAlex is the best *structural* analogue available. Author↔work is a bipartite graph; projecting it onto authors yields co-authorship, which is mathematically the same operation as projecting our bridge↔target bipartite graph onto bridges. Every algorithm in R3's scope — bipartite projection, weighting schemes, resolution parameters, brokerage scoring — can be developed and regression-tested on an OpenAlex slice with zero privacy exposure and zero acquisition risk, then applied unchanged to real data. **Recommend building the offline fixture generator on OpenAlex.**

- https://snap.stanford.edu/data
- https://arxiv.org/pdf/1606.07550 (SNAP library paper)
- https://arxiv.org/pdf/2205.01833 (OpenAlex paper)
- https://casrai.org/guides/openalex-api
- https://economicgraph.linkedin.com/data-for-impact

### The near-miss category: relationship-intelligence platforms

Worth documenting because someone in Wave 2 will find these and think they solve the problem.

- **BoardEx and RelSci** (both Altrata, part of Euromoney) sell genuine *relationship graphs* — BoardEx maps relationship paths across 1.6M executives and 2.1M profiles; RelSci covers 10.5M decision-makers and 1.8M organisations. But the edges are inferred from board memberships, filings, employment overlap and shared education — **public-record co-affiliation, not social-network connections**. Enterprise pricing, unpublished. Coverage of Jane Street and Citadel non-executive staff would be close to nil.
- **Connect The Dots (ctd.ai)** builds a scored relationship graph from **your own email plus your own uploaded LinkedIn connections**, and merges colleagues' networks into a shared graph. That is architecturally the same first-party pattern interlayer uses; it is not a source of `connections(t)`. Free tier exists — useful as a competitive reference point for UX, nothing more.
- **LinkedIn Sales Navigator TeamLink** (Advanced $159.99/mo, $149.99/mo billed annually = $1,799.88/yr; Advanced Plus ~$1,600+/seat/yr at 10+ seats) surfaces shared connections between *your team's* networks and a prospect. It is the only shipping commercial product that exposes connection-graph structure — and it exposes *your side* of the graph, aggregated across colleagues, never the target's. R1 owns this.

The pattern across all three: **the commercial market's answer to "who knows whom" is either your own first-party graph or inferred co-affiliation.** Nobody sells other people's social edges, because nobody can get them.

- https://www.g2.com/products/boardex/reviews
- https://www.capterra.com/p/234445/RelSci/
- https://ctd.ai/
- https://evaboot.com/blog/linkedin-teamlink-extend
- https://derrick-app.com/linkedin/sales-navigator/teamlink-filter

---

## Synthesis

### 1. Does ANY commercially available provider sell LinkedIn connection-graph or mutual-connection data?

**No. Essentially and categorically no, and the reason is structural rather than commercial.**

The mechanism: LinkedIn does not render a member's connection list on any logged-out surface. Mutual connections are computed server-side, per-viewer, and shown **only to the authenticated viewer** — the set `M ∩ connections(t)` literally does not exist as a document anywhere a crawler can reach. Every "LinkedIn data API" on the market is, underneath, a crawler over public profile pages. A crawler cannot scrape a set that is never rendered to it. There is no dataset for sale because there is no artefact to harvest.

What providers actually expose is a **censored degree scalar**: Coresignal's `connections_count` ("Count of profile connections", Integer), Bright Data's `"connections": <int>`. LinkedIn itself caps this display at "500+", so for exactly the senior, well-connected finance professionals we care about, the field is saturated and carries almost no information. It is not an edge, not a partial edge, and not a usable proxy for one.

Three near-misses that must not be mistaken for a yes:

- **People Data Labs' `profiles[]` array** looks like a connections list in the schema browser. It is the subject's own accounts across different networks. Not connections.
- **Bright Data's marketing copy** for the LinkedIn Scraper API lists "connections" among what it extracts. The published sample output resolves this: `"connections": 2`. A number.
- **Apify's cookie-driven actors** (`linkedin-mutual-connection-analyzer`, `linkedin-shared-connections`, `get-connections`) genuinely do return mutuals. They are not selling data. They are selling remote operation of *your* LinkedIn session in exchange for your `li_at` cookie. The data still comes from the authenticated viewer — you — exactly as the handoff plan's structural analysis predicts. This is not a market for connection data; it is a market for automating the one person who can see it.

The one shipping commercial product that surfaces connection-graph structure is **Sales Navigator TeamLink**, which is first-party LinkedIn and exposes your *team's* networks, not the target's.

**Consequence for interlayer: the bipartite edge set cannot be purchased. It can only be observed from the user's own authenticated session, one target at a time — which is R4's problem, not R2's. No amount of third-party spend changes this.**

### 2. Given that, what is the realistic role of third-party providers?

**Enrichment of already-known people, in service of *inferred* affinity — explicitly labelled as inference, never as edges.**

Providers can reliably return, for a person we can already identify: normalised current title, seniority and function; employer history with start/end dates; education history with institution, field and years; location; skills; and the censored connections count. That is enough to compute a co-affiliation affinity score over `M × T` pairs.

**How good can that inference be? Honest assessment, tiered:**

- **Strong signal — shared employer with overlapping tenure.** Two people at the same firm, in overlapping date ranges, at comparable seniority, ideally the same office. This is the only feature with a genuinely high hit rate as a proxy for acquaintance. Overlap duration and org size should both modulate it: twelve months together at a 40-person desk is a very different claim from twelve months together at a 20,000-person bank.
- **Moderate signal — shared school with overlapping years.** Same institution, same programme, same cohort years. Real but noisy; degrades fast as the institution gets larger and as the year gap widens beyond ±1.
- **Weak signal — shared prior-employer chains at one remove**, shared unusual skill clusters, shared narrow industry niche.
- **Noise — same city, same broad industry, same generic title.** These will dominate the candidate set by volume and must be down-weighted to near zero or they will drown everything useful. "Both work in finance in New York" is not evidence.

**Three limits Wave 2 must build around:**

1. **Inference produces scored candidate *pairs*, not edges.** The output type is different from the true output type. The UI must never say "connected"; it must say "plausible tie, score 0.72, because: 3 years overlap at Company X". Every inferred relationship must carry its own explanation, because an unexplained score is indistinguishable from a fabrication.
2. **It is uncalibrated by construction.** We have no ground truth, so precision@k is unknown. The only honest remedy: let the user supply a small set of *observed* mutuals from their own account as labels, and report measured precision@k against that sample. Until they do, the score is an ordering, not a probability, and must be presented as such.
3. **It answers a different question.** True edges answer "who is connected to a Jane Street person". Inference answers "who plausibly knows a Jane Street person". For the user's actual goal — finding warm paths — the second is genuinely useful and is a legitimate graceful-degradation mode, per constraint 4 of the handoff plan. It is not a substitute, and the product must not blur them.

One further point: **for `M`, third-party enrichment is largely redundant.** LinkedIn's own `Connections.csv` export already carries name, current company and current position for every 1st-degree connection. The marginal value of paying a provider to enrich `M` is low. The real enrichment need is on `T` — the target-firm employees, whom the user does not have first-party data for. That halves the volume and therefore the cost, and it concentrates spend on the population where GDPR obligations are heaviest. Design accordingly: **enrich `T`, not `M`.**

### 3. Which providers are worth building an adapter for, ranked?

1. **`null` / offline (no provider)** — the default. Not optional; the tool must run end-to-end on `Connections.csv` plus a target list with no network access at all.
2. **`local_file`** — reads a user-supplied CSV/JSONL of enrichment records. Zero cost, zero legal exposure, zero rate limits. Covers manual research, prior exports, and anything the user obtained through a channel we do not want to encode. **This is the highest value-per-line-of-code adapter in the whole set** and should ship before any network adapter.
3. **Bright Data — LinkedIn Profiles Web Scraper API.** The recommended network adapter and **the cheapest viable path for a single user**: 5,000 records/month free, then ~$1.50/1K. For the brief's 500–5,000 lookups that is **$0 to about $8**. Best legal record in the industry (Meta and X both lost). Public logged-off data only. Documented schema with free downloadable samples so the adapter can be written and tested against real payload shapes before spending anything.
4. **Coresignal — Base/Multi-source Employee API.** Second network adapter, for coverage gaps. Best-documented schema in the category, an explicit public-data-only compliance statement and opt-out mechanism, and predictable credit pricing ($49/mo Starter for 250 records; $800/mo Pro for 10,000).
5. **People Data Labs.** Third, and only if 3 and 4 both miss. Good schema, strong normalisation, but ~$0.28/credit makes 5,000 lookups ~$1,400 — roughly 175× Bright Data for data we mostly already have.
6. **Search adapter — Tavily (free tier) with Exa as the paid upgrade.** For **target discovery only**: resolving names to `linkedin.com/in` slugs so the enrichment adapters have keys to work with. Tavily gives 1,000 credits/month free, Exa is $7/1K. Explicitly *not* an enrichment source — snippets are truncated and stale.

Everything else: no adapter.

**Cheapest viable path for a single user, stated plainly:** LinkedIn `Connections.csv` (free, first-party) + Tavily free tier for target URL resolution (free) + Bright Data Web Scraper API free tier for target enrichment (free up to 5,000 records/month) = **$0/month for the volumes in this brief.** If Bright Data coverage disappoints, one month of Coresignal Starter at $49 is the next step. There is no scenario in which this project needs to spend more than about $50.

### 4. Which are outright unsafe or unwise to integrate, and why?

**Category A — unsafe: anything that requires the user's LinkedIn session credential.**
Apify's cookie-driven actors (`dead00/linkedin-mutual-connection-analyzer`, `data_link_miner/linkedin-shared-connections`, `data_link_miner/linkedin-network-scraper`, `addeus/get-connections`, plus the session-cookie manager actors), PhantomBuster, Dux-Soup, Waalaxy, Expandi, Linked Helper, Evaboot, Wiza.

Three independent reasons, each sufficient:
- **Credential surrender.** `li_at` is a bearer token for the entire account — messages, connections, InMail, Sales Navigator, linked billing. Shipping it to a third-party cloud is unbounded, revocable-only-by-logout account impersonation. For a tool whose premise is that the user's network is the asset, this is the one thing that must never happen.
- **ToS breach with account-loss consequences.** LinkedIn's Prohibited Software Policy explicitly bans data-extraction tools; 2026 reporting describes materially sharpened behavioural detection and documented permanent bans. Losing the account destroys the input to the entire product.
- **Regulatory exposure on the target side.** CNIL/KASPR establishes that collecting data a member restricted to their connection graph is unlawful processing. These tools are precisely mechanisms for reaching restricted data.

**Category B — unwise: contact-data providers.**
ContactOut, Wiza, Prospeo, Datagma, Lix, Dropcontact, Lusha, Apollo.io. They sell emails and phone numbers. Our problem needs neither. Integrating them would mean acquiring more personal data about third-party data subjects than the task requires, in direct conflict with handoff-plan constraint 3 and with the data-minimisation principle the KASPR decision enforced. **The build should default `allow_contact_fields = false` and drop contact fields at the adapter boundary even when a provider volunteers them.**

**Category C — unwise: waterfall aggregators.**
FullEnrich specifically. Compliance across a waterfall is min(), not max() — one non-compliant provider contaminates every result, and the published roster includes Kaspr, the company CNIL fined €240,000. There is no way to audit which sub-provider answered a given call, which makes provenance-stamping impossible, which breaks a mandatory requirement of our schema.

**Category D — unwise: enterprise sales-intelligence platforms.**
ZoomInfo ($15k–$40k/yr, 3-seat minimum, annual-only) and Cognism ($22.5k–$37.5k/yr). Two to three orders of magnitude over budget for a single-user local tool, no connection data, and in Cognism's case a subsidiary with a live GDPR enforcement history against exactly this use case.

**Category E — unwise: orchestration middleware.**
Clay. $185–$495/mo, credits burn per attempt rather than per success, most LinkedIn workflows require a separate Sales Navigator seat, no native mutual-connections source, and its documented workarounds route back to Category A. It is a paid wrapper over providers we can call directly.

**Category F — build-risk: scheduled-retirement and litigation-exposed search.**
Google Custom Search JSON API (closed to new customers, dies 1 Jan 2027), Bing Search API (already dead), SerpApi (Google's amended complaint filed 10 Aug 2026, unresolved). Do not make any of these a load-bearing dependency.

---

## Recommendation

Wave 2 should build **four adapters behind one interface**, of which two require no network:

| Adapter | Priority | Network? | Default? | Rationale |
|---|---|---|---|---|
| `NullEnrichmentProvider` | P0 | No | **Yes** | Guarantees the pipeline runs fully offline; makes every enrichment path optional by construction |
| `LocalFileEnrichmentProvider` | P0 | No | No | Reads user-supplied CSV/JSONL; zero cost, zero risk; covers manual research and prior exports |
| `BrightDataEnrichmentProvider` | P1 | Yes | No | Cheapest ($0 at our volumes on the free tier), best legal record, documented schema |
| `CoresignalEnrichmentProvider` | P2 | Yes | No | Coverage fallback; best schema docs; explicit compliance posture and opt-out |

Plus one **discovery** adapter behind a separate, smaller interface (it resolves identities, it does not enrich):

| Adapter | Priority | Network? | Default? | Rationale |
|---|---|---|---|---|
| `TavilyDiscoveryProvider` | P2 | Yes | No | 1,000 free credits/month; resolves names → `linkedin.com/in` slugs for target enumeration |
| `ExaDiscoveryProvider` | P3 | Yes | No | Paid upgrade at $7/1K if Tavily coverage is inadequate |

**Do not build:** any adapter that accepts a LinkedIn session cookie; any contact-data provider; Clay; FullEnrich; ZoomInfo; Cognism; Apollo; SerpApi; Google CSE; PhantomBuster; Dux-Soup; Waalaxy; Expandi; Linked Helper; Proxycurl (it does not exist).

**Validation fixtures:** build the offline fixture generator on **OpenAlex** (bipartite author↔work, free bulk snapshot, structurally identical projection) and validate community detection against **SNAP `com-DBLP`** (5,000+ ground-truth communities) and the **SNAP Facebook ego-networks** (hand-labelled circles). No LinkedIn data required, and therefore no privacy exposure, for the entire algorithm test suite.

**Hard architectural rule for Wave 2:** `ProviderCapabilities.provides_connection_edges` must be a field on the interface, and it must be `False` for every adapter shipped. It exists so that the type system records the central finding of this brief, and so that any future adapter claiming otherwise has to assert it explicitly and be reviewed.

---

## Implications for the build

Wave 2 codes directly from this section. Python 3.11, standard library plus `httpx` and `pydantic` (or dataclasses if pydantic is out of scope). All paths relative to repo root.

### File layout

```
src/interlayer/enrichment/
    __init__.py
    interface.py        # Protocol, dataclasses, enums — no I/O
    normalize.py        # provider payload -> EnrichedPerson
    cache.py            # SQLite-backed content-addressed cache
    ratelimit.py        # token bucket + retry policy
    budget.py           # call/cost guard
    registry.py         # name -> provider factory
    providers/
        __init__.py
        null.py         # NullEnrichmentProvider        (P0, offline)
        local_file.py   # LocalFileEnrichmentProvider   (P0, offline)
        brightdata.py   # BrightDataEnrichmentProvider  (P1)
        coresignal.py   # CoresignalEnrichmentProvider  (P2)
    discovery/
        __init__.py
        interface.py
        tavily.py
        exa.py
```

### The `EnrichmentProvider` interface

```python
# src/interlayer/enrichment/interface.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Protocol, Sequence


class ToSRisk(str, Enum):
    """Declared compliance posture. Every adapter MUST declare one."""
    NONE = "none"                 # first-party or user-supplied data
    PUBLIC_SCRAPE = "public"      # logged-off public pages (Bright Data, Coresignal)
    AGGREGATED = "aggregated"     # broker database of mixed provenance
    ACCOUNT_AUTOMATION = "account_automation"  # requires user credentials — BANNED


class LawfulBasis(str, Enum):
    FIRST_PARTY = "first_party"           # the user's own data
    USER_SUPPLIED = "user_supplied"       # user takes responsibility
    LEGITIMATE_INTEREST = "legitimate_interest"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CompliancePosture:
    tos_risk: ToSRisk
    lawful_basis: LawfulBasis
    requires_user_credentials: bool     # MUST be False for every shipped adapter
    stores_data_offshore: bool
    vendor_opt_out_url: str | None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.requires_user_credentials:
            raise ValueError(
                "Adapters requiring user LinkedIn credentials are prohibited. "
                "See docs/research/R2-third-party-providers.md, Synthesis Q4, Category A."
            )


@dataclass(frozen=True)
class ProviderCapabilities:
    # THE discriminating field. No commercially available provider can set this True.
    # See docs/research/R2-third-party-providers.md, Synthesis Q1.
    provides_connection_edges: bool = False

    provides_connections_count: bool = False   # censored scalar; 500+ saturates
    provides_employment_history: bool = False
    provides_education_history: bool = False
    provides_skills: bool = False
    provides_location: bool = False
    supports_batch: bool = False
    max_batch_size: int = 1
    requires_network: bool = True


@dataclass(frozen=True)
class ProviderCost:
    """Used by budget.py for dry-run estimation. usd_per_lookup may be 0.0."""
    usd_per_lookup: float
    free_tier_lookups_per_month: int = 0
    notes: str = ""


class LookupKeyType(str, Enum):
    LINKEDIN_URL = "linkedin_url"
    LINKEDIN_PUBLIC_ID = "linkedin_public_id"   # the /in/<slug> segment
    NAME_AND_EMPLOYER = "name_and_employer"


@dataclass(frozen=True)
class LookupKey:
    key_type: LookupKeyType
    value: str
    # Only populated for NAME_AND_EMPLOYER
    employer_hint: str | None = None

    def cache_id(self) -> str:
        """Stable identity for caching. Implement in normalize.py:
        sha256(f"{key_type}|{value.casefold().strip()}|{employer_hint or ''}")"""
        ...


@dataclass(frozen=True)
class EmploymentSpan:
    employer_name: str
    employer_domain: str | None
    employer_id: str | None          # provider-scoped
    title: str | None
    title_normalized: str | None     # lowercased, punctuation-stripped
    seniority: str | None            # one of: intern|junior|mid|senior|lead|director|vp|cxo|owner
    start_date: date | None          # normalize partials to first of month/year
    end_date: date | None            # None == current
    is_current: bool
    location: str | None


@dataclass(frozen=True)
class EducationSpan:
    school_name: str
    school_normalized: str | None
    degree: str | None
    field_of_study: str | None
    start_year: int | None
    end_year: int | None


@dataclass(frozen=True)
class Provenance:
    """MANDATORY on every record. Enables audit, purge and honest UI labelling."""
    provider: str                    # adapter name, e.g. "brightdata"
    source_record_id: str | None
    source_url: str | None
    retrieved_at: datetime           # timezone-aware UTC
    confidence: float                # 0.0..1.0
    is_inferred: bool                # True if any field was derived, not observed
    license_note: str | None


@dataclass(frozen=True)
class EnrichedPerson:
    """The ONLY type an adapter may return. Provider payloads never escape normalize.py."""
    person_key: str                  # interlayer-internal stable id
    linkedin_public_id: str | None
    profile_url: str | None

    full_name: str | None
    first_name: str | None
    last_name: str | None
    headline: str | None

    location_country: str | None     # ISO 3166-1 alpha-2 where resolvable
    location_region: str | None
    location_city: str | None

    current_employer_name: str | None
    current_title: str | None
    current_seniority: str | None
    current_start_date: date | None

    employment_history: tuple[EmploymentSpan, ...] = ()
    education_history: tuple[EducationSpan, ...] = ()
    skills: tuple[str, ...] = ()

    connections_count: int | None = None   # censored at 500 by the source; treat >=500 as unknown
    followers_count: int | None = None

    provenance: Provenance | None = None

    # DELIBERATELY ABSENT: email, phone, address, birth_date.
    # Not needed for the graph problem; see Synthesis Q4, Category B.
    # If a provider returns them, normalize.py MUST drop them unless
    # settings.enrichment.allow_contact_fields is explicitly True.


@dataclass(frozen=True)
class EnrichmentResult:
    key: LookupKey
    person: EnrichedPerson | None     # None == miss (cache negatively)
    error: str | None = None
    cache_hit: bool = False
    cost_usd: float = 0.0


class EnrichmentProvider(Protocol):
    name: str
    capabilities: ProviderCapabilities
    compliance: CompliancePosture
    cost: ProviderCost

    def enrich(self, keys: Sequence[LookupKey]) -> Iterable[EnrichmentResult]:
        """Resolve keys to EnrichedPerson records.

        Contract:
          - MUST be a generator; yield results as they resolve.
          - MUST yield exactly one EnrichmentResult per input key, in input order.
          - MUST yield EnrichmentResult(key, person=None) for a miss, never raise.
          - MUST raise only on unrecoverable config/auth errors (invalid key, no credit).
          - MUST NOT perform network I/O if capabilities.requires_network is False.
          - MUST set provenance on every returned person.
          - MUST respect the injected RateLimiter and BudgetGuard.
        """
        ...

    def estimate_cost(self, n_keys: int) -> float:
        """Pre-flight cost estimate in USD. Used by --dry-run. Must not do I/O."""
        ...
```

### Normalized field names (canonical — do not vary per adapter)

Adapters map provider payloads to exactly these. Anything a provider offers that is not on this list is discarded at the adapter boundary.

| Canonical field | Type | Bright Data source | Coresignal source | PDL source | Notes |
|---|---|---|---|---|---|
| `linkedin_public_id` | `str` | slug from `url` | profile shorthand | `profiles[].username` where `network=="linkedin"` | Lowercase, strip trailing `/` |
| `profile_url` | `str` | `url` | profile url | `linkedin_url` | Canonicalise to `https://www.linkedin.com/in/<slug>` |
| `full_name` | `str` | `name` | `name` / `full_name` | `full_name` | |
| `headline` | `str` | `position` / `profile_info.position` | `title` / headline | `job_title` | |
| `location_country` | `str` | `country_code` / parsed `city` | `country` | `location_country` | ISO alpha-2 |
| `location_city` | `str` | `city` | `location` | `location_locality` | |
| `current_employer_name` | `str` | `current_company.name` | current company name | `job_company_name` | |
| `current_title` | `str` | `current_company.title` | current title | `job_title` | |
| `current_start_date` | `date` | from `experience[]` current entry | current experience start | `job_start_date` | Partial dates → first of month/year |
| `employment_history[]` | list | `experience[]` | member experience array | `experience[]` | |
| `education_history[]` | list | `education[]` | member education array | `education[]` | |
| `skills[]` | list | `skills` | skills array | `skills` | Casefold, dedupe, strip whitespace |
| `connections_count` | `int` | `connections` (**integer**) | `connections_count` (**integer**) | not available | **Treat `>= 500` as `None`** — the source censors it |
| `followers_count` | `int` | `profile_info.followers` | follower count | not available | |

**Date normalisation rule:** providers emit `"2019"`, `"Jan 2019"`, `"2019-01"`, `"2019-01-15"` and `"present"`. Normalise to `datetime.date`; `"2019"` → `date(2019,1,1)`; `"Jan 2019"` → `date(2019,1,1)`; `"present"`/empty → `None` with `is_current=True`. Record the original precision in a sibling field if the affinity scorer needs it (an overlap computed from year-only dates is much weaker evidence than one from month-level dates — R3 should weight accordingly).

**Seniority normalisation:** map to the fixed vocabulary `intern|junior|mid|senior|lead|director|vp|cxo|owner|unknown`. Never pass a provider's raw seniority string through.

### Caching (mandatory)

- Store at `data/cache/enrichment.sqlite`, single table, WAL mode.
- Schema: `(cache_id TEXT PRIMARY KEY, provider TEXT, schema_version INT, payload_json TEXT, is_miss INT, retrieved_at TEXT, cost_usd REAL)`.
- **Cache key** = `sha256(f"{provider}|{schema_version}|{key.cache_id()}")`. Bumping `schema_version` invalidates cleanly without deleting the file.
- **Positive TTL: 30 days** (profiles change slowly; job changes are the main churn).
- **Negative TTL: 7 days.** Cache misses too — otherwise a run over a target list with 40% coverage re-pays for the 40% every time.
- Cache is checked before rate limiting and before the budget guard. A fully cached run must make zero network calls and cost zero.
- `interlayer cache purge [--provider X] [--older-than N]` must exist. GDPR Art. 17 is not optional and the KASPR order was specifically a deletion order.

### Rate limiting and retries (mandatory)

- Token bucket per provider. Defaults: **1 request/second, burst 5, max concurrency 2.** Per-provider override in config.
- Retry on `429` and `5xx` only: **3 attempts, base delay 1s, factor 2, jitter ±25%**. Always honour `Retry-After` when present, in preference to the computed backoff.
- Never retry `4xx` other than `429`.
- Bright Data's Web Scraper API is an **async job model** — submit, then poll or receive webhook. The adapter must implement submit-and-poll with a bounded wait (default 300s) and surface a timeout as a miss, not an exception.

### Budget guard (mandatory)

```python
@dataclass
class BudgetGuard:
    max_calls_per_run: int = 1000
    max_estimated_cost_usd: float = 25.0
    dry_run: bool = False
```
- On `dry_run`, call `estimate_cost(n)` for every provider in the chain, print a table, make zero network calls, exit 0.
- Abort the run with a clear, actionable error before the call that would breach either limit. Never silently truncate.

### Making providers genuinely optional

1. **Default config is fully offline.** Ship this:
   ```toml
   [enrichment]
   enabled = false            # OFFLINE BY DEFAULT
   provider = "null"
   cache_ttl_days = 30
   negative_cache_ttl_days = 7
   max_calls_per_run = 1000
   max_estimated_cost_usd = 25.0
   allow_contact_fields = false   # drop emails/phones at the adapter boundary
   requests_per_second = 1.0
   max_concurrency = 2

   [discovery]
   enabled = false
   provider = "null"
   ```
2. **No provider module may be imported at package import time.** `registry.py` resolves names to factories lazily so a missing `httpx`, a missing API key, or a blocked network cannot break `import interlayer`.
3. **The pipeline must type-check and run to completion with `NullEnrichmentProvider`.** Enrichment contributes *optional* attributes to nodes. Bridge detection, bipartite projection, clustering and reporting must all work with every enrichment field `None`.
4. **Affinity scoring degrades, it does not fail.** With no enrichment, the affinity scorer returns an empty candidate set and the report says so explicitly. With partial enrichment it scores only on available features and records which features were available per pair.
5. **Every enrichment-dependent output must be labelled.** Any pair surfaced by inference carries `is_inferred=True`, a per-pair explanation ("3y 4m overlap at Company X, both senior"), and the provider that supplied the underlying facts. Observed edges and inferred pairs must be visually and structurally distinct in the report — separate sections, never interleaved in one ranked list.
6. **A compliance guardrail test must exist and must be in the default suite:**
   ```python
   def test_no_adapter_requires_credentials():
       for factory in registry.all_factories():
           assert factory.compliance.requires_user_credentials is False

   def test_no_adapter_claims_connection_edges():
       for factory in registry.all_factories():
           assert factory.capabilities.provides_connection_edges is False

   def test_pipeline_runs_with_zero_network(monkeypatch):
       monkeypatch.setattr(socket, "socket", _raise_on_use)
       run_pipeline(config=Config(enrichment=EnrichmentConfig(enabled=False)))
   ```

### Test fixtures

- Golden-file fixtures per adapter under `tests/fixtures/enrichment/<provider>/`, captured from each vendor's **free sample download** (Bright Data publishes free dataset samples per dataset; Coresignal publishes sample payloads in its docs; PDL publishes sample responses). Adapters are tested against these offline — the network adapters must have **zero** tests that hit the network.
- Graph-algorithm fixtures generated from an **OpenAlex** slice (bipartite author↔work), plus **SNAP `com-DBLP`** and the **SNAP Facebook ego-networks** for community-detection ground truth. Coordinate the exact fixture format with R3.

### Two specific traps to encode as comments in the adapter source

```python
# Bright Data: the field literally named "connections" is an INTEGER count,
# not a list of connections. Verified against the vendor's published sample:
#   {"name": ..., "profile_info": {...}, "connections": 2, ...}
# See docs/research/R2-third-party-providers.md.
```

```python
# People Data Labs: `profiles[]` is the subject's OWN accounts across networks
# (network/id/url/username/first_seen/last_seen). It is NOT a connections list.
# Do not map it to anything graph-related.
# See docs/research/R2-third-party-providers.md.
```

---

## Sources

Litigation and enforcement
- https://nubela.co/blog/goodbye-proxycurl/
- https://nubela.co/blog/what-is-proxycurl-api-now-in-2026-im-the-founder/
- https://www.startuphub.ai/ai-news/startup-news/2025/the-1-linkedin-scraping-startup-proxycurl-shuts-down
- https://www.courtlistener.com/docket/71527603/linkedin-corporation-v-proapis-inc/
- https://chatgptiseatingtheworld.com/2025/10/05/linkedin-sues-proapis-inc-complaint/
- https://www.bleepingcomputer.com/news/legal/linkedin-sues-proapis-for-using-1m-fake-accounts-to-scrape-user-data/
- https://thelinkedblog.com/2026/linkedin-reaches-deal-in-data-scraping-lawsuit-against-proapis-3857/
- https://news.bloomberglaw.com/artificial-intelligence/linkedin-battles-online-scrapers-in-perpetual-struggle-over-data
- https://www.fbm.com/publications/major-decision-affects-law-of-scraping-and-online-data-collection-meta-platforms-v-bright-data/
- https://www.proskauer.com/release/proskauer-secures-dismissal-of-scraping-claims-against-bright-data
- https://www.cnil.fr/en/data-scraping-kaspr-fined-eu240000
- https://www.edpb.europa.eu/news/news/2025/data-scraping-french-supervisory-authority-fined-kaspr-eu240-000_en
- https://www.cnil.fr/en/closure-order-issued-against-kaspr
- https://www.gerrishlegal.com/blog/cnil-fines-kaspr-240000-for-illegally-collecting-linkedin-users-contact-details
- https://serpapi.com/blog/google-v-serpapi-motion-to-dismiss-why-were-in-the-right/
- https://scrapebadger.com/blog/google-sued-a-scraper-under-copyright-law-and-lost-heres-what-the-serpapi-ruling-actually-says
- https://ppc.land/serpapi-faces-revived-google-scraping-claims-built-on-reddit-licensing-terms/

Provider schemas, pricing, compliance
- https://docs.coresignal.com/employee-api/base-employee-api/data-dictionary-base-employee-api
- https://docs.coresignal.com/employee-api/multi-source-employee-api/data-dictionary-multi-source-employee-api
- https://docs.coresignal.com/introduction/pricing-and-subscriptions
- https://coresignal.com/data-transparency/
- https://coresignal.com/privacy-rights/
- https://pipeline.zoominfo.com/sales/coresignal-pricing
- https://docs.peopledatalabs.com/docs/fields
- https://docs.peopledatalabs.com/docs/person-data-overview
- https://support.peopledatalabs.com/hc/en-us/articles/25794271805211-Pricing-credits
- https://oag.ca.gov/data-broker/registration/190662
- https://privacy.peopledatalabs.com/policies?name=privacy-policy
- https://brightdata.com/products/datasets/linkedin/profiles
- https://brightdata.com/products/web-scraper/linkedin/profiles
- https://docs.brightdata.com/datasets/scrapers/linkedin/collection-options
- https://github.com/luminati-io/LinkedIn-Scraper
- https://crustdata.com/pricing
- https://docs.crustdata.com/general/pricing
- https://apify.com/dead00/linkedin-mutual-connection-analyzer
- https://apify.com/data_link_miner/linkedin-shared-connections/api/mcp
- https://apify.com/addeus/get-connections
- https://use-apify.com/docs/what-is-apify/apify-pricing
- https://use-apify.com/docs/best-apify-actors/best-linkedin-scrapers
- https://lagrowthmachine.com/phantombuster-pricing/
- https://stormy.ai/blog/linkedin-automation-safety-2026-phantombuster-bereach-guide
- https://www.cleanlist.ai/blog/2026-03-12-clay-pricing-changes-2026
- https://salesmotion.io/blog/clay-pricing
- https://community.clay.com/x/community/jj5tfe1vxlg6/exploring-linkedin-connections-for-lead-enrichment
- https://www.dux-soup.com/blog/the-best-linkedin-automation-tools-tried-and-tested
- https://linkedintools.site/is-waalaxy-safe/
- https://leadsmonky.com/expandi-vs-dux-soup/
- https://www.capterra.com/p/10017049/Lix/pricing/
- https://derrick-app.com/tools/evaboot-pricing
- https://derrick-app.com/en/linkedin-email-finder-comparison-2026/
- https://contactout.com/privacy
- https://syncgtm.com/blog/prospeo-alternatives
- https://www.saleshandy.com/blog/datagma-pricing/
- https://www.knowlee.ai/blog/tools/fullenrich-alternatives
- https://syncgtm.com/blog/dropcontact-review
- https://salesmotion.io/blog/apollo-pricing
- https://www.pin.com/blog/zoominfo-pricing/
- https://www.cognism.com/blog/zoominfo-pricing
- https://puzzleinbox.com/blog/apollo-vs-zoominfo-vs-cognism-vs-lusha-2026/
- https://www.g2.com/products/boardex/reviews
- https://www.capterra.com/p/234445/RelSci/
- https://ctd.ai/
- https://evaboot.com/blog/linkedin-teamlink-extend
- https://derrick-app.com/linkedin/sales-navigator/teamlink-filter

Search layer
- https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement
- https://developers.google.com/custom-search/v1/overview
- https://blog.expertrec.com/google-custom-search-json-api-simplified/
- https://exa.ai/pricing
- https://fastcrw.com/blog/exa-pricing-explained
- https://coldiq.com/blog/tavily-pricing
- https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/
- https://apiserpent.com/blog/serp-api-pricing-comparison

Datasets
- https://snap.stanford.edu/data
- https://arxiv.org/pdf/1606.07550
- https://arxiv.org/pdf/2205.01833
- https://casrai.org/guides/openalex-api
- https://economicgraph.linkedin.com/data-for-impact
