# Operator runbook — acquiring the edges

**Audience:** you, the person running `interlayer`, sitting in front of a browser.

**What this document is:** the manual procedure that produces the one thing the tool cannot
obtain for you — the edges between your connections and the people at your target firms. The
software does the analysis. You do the acquisition.

**What this document is not:** a compliance opinion. See [§6](#6-the-risk-gradient) — there is
no reading of LinkedIn's User Agreement under which any of this is expressly permitted, and
this runbook does not pretend otherwise. It lays out a gradient and leaves the choice to you.

> **UI drift warning.** This procedure was written on **2026-08-17** against second-hand
> descriptions of LinkedIn's interface. No agent involved in building this tool could reach
> `linkedin.com` to verify a single filter name, URL parameter or menu label. LinkedIn changes
> its interface frequently and without notice. **Expect names and positions to differ, and
> trust what is on your screen over what is written here.** The strategy below does not depend
> on any particular label; the labels are hints for finding the right control.

---

## 0. Before you start

```bash
interlayer init
interlayer ingest ~/Downloads/Connections.csv
interlayer targets
```

`Connections.csv` comes from **Settings → Data privacy → Get a copy of your data**, ticking
**Connections** for the fast archive. It defines `M`, your connection set, and it contains
**zero edges** — every person in it is connected to *you*, and the file says nothing about who
else they know. Producing those edges is what the rest of this document is about.

Two sets, used throughout:

- **`M`** — your 1st-degree connections. From `Connections.csv`. Typically 500–3,000.
- **`T`** — people at Jane Street, Citadel and Citadel Securities. Enumerated by you, from
  company-filtered people searches. Possibly ~10,000 across all three; you will work a
  prioritised slice.

The goal is `E ⊆ M × T`: which of your people know which of their people.

---

## 1. Degree pruning — do this first, it is nearly free

**A 3rd-degree or out-of-network target has exactly zero connections in common with you.** Not
"few". Zero, by the definition of degree. There is nothing to capture, and visiting one is
wasted quota.

Degree badges — the little `1st` / `2nd` / `3rd` next to a name — are readable **in bulk** off a
company-filtered people-search results page, roughly ten per page. A handful of searches
classifies your whole target set at no per-target cost.

On a plausible 2,000-connection, finance-adjacent network against 300 targets:

| Degree | Share | Count | Visit? | Mutuals |
|---|---|---|---|---|
| 1st | ~2% | 6 | yes | ≥ 0 (they are already in `M`) |
| 2nd | ~45% | 135 | **yes** | **≥ 1, guaranteed** |
| 3rd / out of network | ~53% | 159 | **no** | **0, provably** |

**141 of 300 targets need visiting. Fifty-three percent of the work disappears for the cost of a
few searches, with no information loss whatsoever.**

The 2nd-degree share is the single biggest unknown in your cost estimate, and it depends on how
finance-adjacent your network actually is:

| Relevant professional pool | Interpretation | P(2nd degree) | Targets to visit |
|---|---|---|---|
| 10M | your network is unrelated to finance | ~18% | ~60 |
| 2M | finance/tech, relevant geographies | ~63% | ~190 |
| 500k | NYC / London quant-adjacent | ~98% | ~295 |

Do not guess which row you are on. **Measure it** — that is what the pruning pass gives you, and
the ratio *is* your cost estimate.

**Staleness caveat.** A `3rd` that has since become a `2nd` is silently dropped. Re-badge and
re-prune if a campaign runs longer than about a month.

---

## 2. The inversion — the main event

### 2.1 The obvious plan, and why it is wrong

The obvious plan is one query per target: open each `t ∈ T`, read their mutual-connections list,
record the overlap. That is `|T|` queries — up to ~10,000 across three firms. It works, but it
is an order of magnitude more expensive than it needs to be.

### 2.2 The inversion

Invert the enumeration. Run **one query per connection**:

> **`Connections of` = `m`**  **AND**  **`Current company` ∈ {Jane Street, Citadel, Citadel
> Securities}**
>
> — once for each `m ∈ M`.

Each such search returns exactly `{t ∈ T : (m, t) ∈ E}` — the people at your target firms whom
`m` knows. That is the same bipartite edge set, indexed the other way, in **`|M|` queries
(typically 500–3,000) instead of `|T|` (~10,000)**.

### 2.3 Why it works

The `Connections of` filter carries a hard constraint: **the person you name must be your own
1st-degree connection.** You cannot type in a Jane Street MD you have never met and get their
connection list. That constraint is what makes the naive reading — "this filter is useless, our
targets are strangers" — and the naive reading is exactly backwards.

**Every element of `M` is, by definition, a 1st-degree connection of yours.** So when you put an
`m` into that filter, the constraint is *automatically satisfied*, every time, for every person
in your export. The filter that looks useless for reaching strangers is precisely the right tool
for enumerating what your friends can reach.

### 2.4 Why it is also the better-behaved option

The company filter is applied **server-side**. You never retrieve, view or store a single person
who is not at a target firm. Compare that with the naive direction, where you page through a
target's entire mutual list. The inversion is roughly ten times cheaper *and* strictly better on
data minimisation — the same choice wins on both axes, which does not happen often.

Result sets are small: a handful of rows per query rather than a page of noise.

### 2.5 Running it

Work through `M` at your own pace, one search per person, and record what comes back. Persist
progress — the queue must survive across sessions and across the end of a trial subscription.
Capture options are in [§4](#4-capturing-what-you-see).

**Mark every result set `completeness: unknown`.** Sources agree on the 1st-degree requirement
and on the privacy fallback below, but none states verbatim that the filter returns a complete
list. Do not assume it does.

---

## 3. The mutuals backfill — and the finding that makes it free

### 3.1 The inversion's blind spot

Some of your connections set **"Who can see your connections"** to *Only you*. For those people
the inversion degrades: `Connections of = m` falls back to showing only connections shared with
you, or nothing useful. The inversion is blind to exactly those `m`.

### 3.2 The backfill

For that subset — and for any high-priority target you want to be certain about — go the other
way. Open the **target's** profile and click **"N mutual connections"** in the introduction
section. That view shows `M ∩ connections(t)` directly.

### 3.3 The finding: target-side privacy costs you nothing

**A target hiding their connection list does not suppress the mutual view.** LinkedIn's own help
text is explicit: when a member hides their connections there is no "See connections" option,
but *"you can view shared connections from the People tab of the search results page or you can
click Mutual Connections in the introduction section of their profile"*, and choosing *Only you*
means no one can see the full list, but *"mutual connections will always be visible to people
who share them with you"*.

The mechanism is obvious once stated. Every person in `M ∩ connections(t)` is **already your own
1st-degree connection**, whose profile you are independently entitled to see. LinkedIn is
disclosing nothing new about `t` beyond the bare fact of the edge.

**So the setting governs the full list and has no effect on the mutual subset. Coverage impact:
zero.** Do not model target-side privacy as a loss, and do not let it discourage you from
visiting a target who has hidden their connections.

The real coverage losses are, in order: quota exhaustion; truncated target enumeration; stale
degree badges; a blocked relationship in either direction; and weak identity matching if you
capture display names rather than profile URLs.

*Confidence: high on the direction — LinkedIn's help text is unambiguous and consistent across
two pages. Medium on there being no edge cases at all, since LinkedIn ships regional and
account-type variations. This is why `interlayer` reports `observed / eligible` rather than
asserting completeness.*

### 3.4 A note on the URL shortcut

You can construct a mutual-connections search URL by hand instead of clicking through the
profile. It saves one page load and one quota unit per target, and it does **not** register a
profile view on the target — a genuine privacy benefit *to them*. But the UI's own picker only
offers your 1st-degree connections, so hand-building the URL for a 2nd-degree target does
something the interface does not itself offer. If you take that route, the 1st-degree network
filter is **mandatory**, or you will pull in people who are not in `M` at all.

The click-through is the default instruction here because it is unambiguously an intended UI
action.

---

## 4. Capturing what you see

Every capture path ends in the same edge records, so the choice is about your time and your risk
tolerance, not about what the tool can consume.

**Manual transcription.** Type or paste names into a spreadsheet. Slowest, and the only method
that survives any LinkedIn interface change, because it depends on nothing but your eyes. Its
weakness is identity: display names collide inside a 2,000-person network, so treat manual
capture as lower-confidence input and prefer to record the profile URL alongside the name.

**HAR capture.** Open DevTools → Network, turn on **Preserve log**, browse normally, then
**Export HAR** and hand the file to `interlayer`. Roughly 2.5× faster than transcription, and —
the real argument — it yields profile identifiers rather than display names, which join
*exactly* against `Connections.csv` instead of fuzzily. Keep sanitisation on (the browser
default); `interlayer` wants no cookies and rejects files containing them. The traffic LinkedIn
sees is identical to the manual path: HAR export is a client-side browser feature that LinkedIn
cannot observe.

**A target with zero mutuals still needs a record.** "Visited, found nothing" and "not yet
visited" are different states, and conflating them is how a thin harvest starts reading as a
thin network. Record the empty result explicitly.

**Pacing.** Work in sessions. Stop at the first CAPTCHA or "unusual activity" prompt and do not
push through a restriction. This is courtesy and quota management, not evasion.

---

## 5. Quota arithmetic — the number that sets your timeline

### 5.1 The Commercial Use Limit

LinkedIn caps "profile searching" on non-commercial accounts with a rolling monthly quota, the
**Commercial Use Limit**. LinkedIn deliberately does not publish the number and states that it
varies by account.

- **Observed magnitude:** third-party estimates cluster around **~300 people-searches per
  month** on a free account; individual reports range from ~100 to ~1,000 depending on account
  age and activity. Treat it as a budget to be measured, not a constant.
- **What counts:** people searches, viewing 2nd/3rd-degree profiles, "People Also Viewed", the
  People tab of a company page, filtered searches.
- **What does not count:** searching a person by name in the top search box; browsing your own
  1st-degree connections.
- **Reset:** midnight Pacific on the 1st of each month.
- **When you hit it:** search results truncate to about three rows and profile browsing beyond
  1st degree stops until the reset.

**Budget 1–2 quota units per target** — one for the mutuals search, two if you also load the
profile page. A free account therefore covers roughly **150–250 targets per calendar month.**

### 5.2 The table that settles the design

Pruned workload of 141 targets out of 300:

| Method | Seconds/target | Wall-clock | Quota units | Months on a free account | Coverage |
|---|---|---|---|---|---|
| Manual transcription | 60–90 | **2.4–3.5 h** | 141–282 | **1** | ~100% of the pruned set |
| HAR capture | 20–30 | **47–70 min** | 141–282 | **1** | ~100% of the pruned set |
| Browser extension | 10–15 | 24–35 min | 141–282 | **1** | ~100% |
| Paced automation | 30–60 | 1.2–2.4 h | 141–282 | **1** | ~100%, → 0 once restricted |
| Headless at scale | 2–5 | 5–12 min | blocked | — | collapses at the wall |

Read down the "Months" column. **Every method lands in the same month and delivers the same
coverage**, because coverage is gated by a server-side monthly quota that no client-side
technique raises.

- The extension buys about **35 minutes** over HAR capture.
- Paced automation buys **nothing at all** — honestly paced, it is slower than manual.
- Headless buys minutes, and the litigation record.

**Automation has no coverage upside on this problem.** That is the whole argument. The low-risk
default is not the cautious choice; it is the correct one.

### 5.3 The one lever that actually moves coverage

Pay LinkedIn for a higher quota — the sanctioned way to want more.

**Sales Navigator Core: US$119.99/month, with a 30-day free trial.** It removes the Commercial
Use Limit for people search and raises the per-search result cap. All 141 targets then fit into
a **single session**: with HAR capture, roughly a one-hour job in one evening.

**Take Core, not Advanced.** Advanced costs US$159.99/month and adds TeamLink, TeamLink Extend
and Smart Links. **The search filters, lists and alerts are identical between the two tiers** —
including `Connections of` and `Current company`, the only two filters this procedure needs.
TeamLink is the one LinkedIn product that is structurally close to what you want, and it
**requires a multi-seat team contract**; it is unavailable to a single user at any price.
Paying the Advanced premium as an individual buys you nothing you can use.

For a typical `M` of 500–1,500, the 30-day trial is enough time to complete a full inversion pass
at a sane pace. Do the capture on the flagship `linkedin.com` surface — the quota entitlement is
account-level, so the flagship mutual-connections view becomes unlimited too.

Recruiter and Recruiter Lite are strictly dominated: no `Connections of` equivalent, capped at
3rd degree, more expensive than Core.

**Sales Navigator has no CSV or Excel export.** LinkedIn removed it on 1 July 2026. Any file
claiming to be a Sales Navigator export came from a scraper. `interlayer` deliberately ships no
parser for one.

---

## 6. The risk gradient

**This section does not tell you that anything here is compliant, because it is not clear that
anything here is.**

### 6.1 What the User Agreement actually says

§8.2 of LinkedIn's User Agreement prohibits, among other things:

1. developing, supporting or using "software, devices, scripts, robots or any other means or
   processes (**including crawlers, browser plugins and add-ons** or any other technology) to
   scrape the Services or otherwise copy profiles and other data from the Services";
2. using "bots or other automated methods to access the Services";
3. **"Copy, use, disclose or distribute any information obtained from the Services … without the
   consent of LinkedIn."**

**Be honest about clause 3. On its face it reaches manual copying.** There is no reading of §8.2
under which transcribing a mutual-connections list into a spreadsheet is expressly permitted.

What there *is* is a very large gap between that clause — universally unenforced against
individual members doing ordinary research on their own network — and automated collection,
which LinkedIn litigates. That gap is real, and it is a distinction about **enforcement risk**,
not about compliance. Anyone who tells you otherwise is selling something.

### 6.2 The gradient

| Path | Account risk | Legal risk | Note |
|---|---|---|---|
| **Official data export** | **None** | **None** | The only genuinely blessed path. Gives you `M`, and no edges. |
| **Manual browsing and transcription** | **Negligible** — indistinguishable from normal use | **Low.** §8.2(3) technically reaches it; no enforcement precedent against individuals | The guaranteed floor. Works with nothing but a text editor. |
| **HAR / DevTools capture** | **Negligible** — the traffic is identical to manual browsing, and HAR export is client-side and unobservable to LinkedIn | **Low** — same clause as manual; no automated access, no redistribution | Fastest low-risk option, and the best identity fidelity. |
| **Browser extension** | **Material** | **Medium** — squarely inside §8.2's "browser plugins and add-ons" | See below. |
| **Paced automation (Playwright/Selenium on your own session)** | **High** | **High** — the paradigm §8.2 violation | Buys no coverage at all. |
| **Headless scraping at scale** | **Severe** — permanent ban | **Severe** | The exact fact pattern LinkedIn litigates. |

### 6.3 Why the extension row is worse than it looks

LinkedIn's page-load script now enumerates **roughly 6,200 specific browser-extension IDs** plus
about 48 device-fingerprint attributes **on every page load** — up from a few hundred IDs in
2024. Extensions are not an unmonitored side door; they are the category LinkedIn is actively
hunting. It forced Kleo's 70,000-user extension to shut down in June 2025, restricted Taplio in
April 2025, and in March 2026 removed HeyReach's company page and banned the personal profiles
of its founder, CEO and CMO.

You would be accepting that risk to save **about half an hour, once**.

### 6.4 Why automation is worse still

Detection in 2026 reaches well past `navigator.webdriver` into input-event physics and GPU
signatures exposed through the debugging protocol. And the asset you would be risking is your
real professional identity — **the exact thing this whole project exists to leverage.** Losing
the account destroys the value of every edge you collected with it.

The enforcement record is one-directional:

- **hiQ Labs** — consent judgment December 2022: $500k, permanent injunction, destroy the source
  code, the data and the derived algorithms. The famous "scraping public data is legal" headline
  was about a preliminary injunction. Cite the ending, not the middle.
- **Mantheos** — 2022 settlement: delete all scraped data, destroy the software, cease automated
  access.
- **Proxycurl** — sued January 2025, shut down 4 July 2025 at roughly $10M ARR, permanent
  injunction plus an obligation to notify its own customers.

And the case usually cited in the other direction does not help here. **Meta v. Bright Data**
(January 2024) turned on **logged-off** scraping of **public** pages. Mutual connections exist
only behind authentication, so the safe harbour does not reach this activity. This project is on
the wrong side of that distinction and should not lean on it.

The pattern across all four: LinkedIn pursues **redistribution at commercial scale**, **fake
accounts**, and **automated access**. One individual analysing their own network locally, with
no resale and no automation, is not the shape of any of those cases.

### 6.5 What `interlayer` does about it

Nothing in this repository touches LinkedIn. There is no HTTP client aimed at it, no browser
driver, no extension, and no code path that reads a session cookie — enforced by tests, not by
policy. The two paths this runbook actually documents are manual transcription and HAR capture,
because they sit at the low end of the gradient and, per [§5.2](#52-the-table-that-settles-the-design),
give up nothing in coverage.

**Choose your own row. Do not let anyone, including this document, tell you the choice is free.**

---

## 7. Third parties who never agreed to this

The people at your target firms are data subjects who have not been asked and are not aware this
analysis exists. That is a genuine obligation, not a formality:

- Store the minimum. `interlayer` keeps no email, no phone number and no free-text profile
  content for targets.
- Reports strip contact details unconditionally and state that they must not be redistributed.
  Honour that; forwarding one is a further disclosure of someone else's personal data.
- Erase on request. `interlayer purge --person <id>` and `--target <id>` write tombstones so an
  erased person is not restored by the next re-ingest.
- Records expire on the retention clock, swept at startup.

Read `PRIVACY.md`, or run `interlayer privacy`, before you begin.

---

## 8. The loop

```bash
interlayer analyse                       # rebuild after each capture session
interlayer next -n 10                    # which targets would change the picture most
interlayer report --format html -o bridges.html
```

`interlayer next` ranks by how much a target would change the result, not by seniority — the
point is to spend your quota where it buys information.

Two numbers to keep your eye on:

- **Coverage.** While the harvest is incomplete, every reach figure is written as `≥ n`. Until
  coverage is high, "this person has little reach" means "we have not looked", not "they are
  poorly connected."
- **The review queue.** Anything counted there is held *out* of the graph. `Jane Street Capital
  LLC` and `Jane Street Entertainment` score identically under every fuzzy matcher tested, so
  the tool asks rather than guesses. Clear the queue before treating a result as final.

---

## 9. One-page summary

1. `interlayer init && interlayer ingest Connections.csv`.
2. **Prune by degree first.** 3rd degree means exactly zero mutuals. Roughly half the work
   vanishes for the cost of a few searches.
3. **Invert.** `Connections of = m` × `Current company ∈ {Jane Street, Citadel, Citadel
   Securities}`, once per connection. Every `m` satisfies the 1st-degree constraint
   automatically, and the company filter runs server-side so you never touch a non-target.
4. **Backfill via mutuals** for connections who hid their lists. Target-side privacy does not
   suppress the mutual view, so this costs zero coverage.
5. **Budget the quota, not the clock.** ~150–250 targets/month free; every capture method hits
   the same ceiling. Sales Navigator **Core** at **$119.99/month with a 30-day trial** removes
   it — **not** Advanced, whose filters are identical and whose TeamLink needs a multi-seat
   contract.
6. **Pick your row on the risk gradient knowingly.** Nothing here is cleanly compliant, and
   automation buys no coverage.
7. `interlayer analyse` → `report` → `next`, and repeat.
