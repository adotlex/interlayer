# Collecting mutual-connection observations

**What this is.** Thirty to sixty minutes of browsing that turns interlayer's
output from *guesses* into *facts*. Everything else the tool does is inference
from your `Connections.csv` — affiliation overlap, shared employers, clustering
— and inference is **always wrong sometimes**. This workflow produces ground
truth: edges LinkedIn itself asserts.

**Do it by hand. At human speed. Always.** Reasons in [§6](#6-why-this-is-manual-and-must-stay-manual).
The short version: automating a logged-in LinkedIn session breaches the User
Agreement, and the risk that matters is not being sued — it is **permanently
losing the account this entire workflow depends on**.

Tier 2 is optional. Skip this and the pipeline still runs; every result is just
labelled inferred.

---

## 1. The mechanism, in one paragraph

LinkedIn sells no API and no vendor sells data that returns another member's
connection list. But on any 2nd-degree profile, LinkedIn's own UI renders

```
{ your connections }  ∩  { their connections }
```

as a line reading **"Sarah Chen, Rajiv Patel, and 12 other mutual connections"** —
and that line is a **link**. Click it and you get an *enumerable list*, not a
count. Every name on it is a true edge: that person is connected both to you and
to the target. Better still, this survives the target setting their connections
list to "Only you" — the privacy setting quant-finance people most often enable.

That list is exactly, and only, what this workflow collects.

---

## 2. Build the target list (10 minutes)

Open the company **People** tab and filter to 2nd degree.

| Firm | People tab |
|---|---|
| Citadel LLC — the hedge fund | `linkedin.com/company/citadel-llc/people/` |
| Citadel Securities — the market maker | `linkedin.com/company/citadel-securities/people/` |
| Jane Street | `linkedin.com/company/jane-street/people/` ⚠️ **slug unverified** |

> ⚠️ **Citadel is two companies.** Citadel LLC (the fund, est. 1990) and Citadel
> Securities (the market maker, est. 2002) have separate pages, separate staff
> and separate slugs. interlayer keeps them apart deliberately and will **reject**
> a bare `citadel` in your file. Pick one.

> ⚠️ **Verify the Jane Street slug before you rely on it.** The research
> ([`docs/research/01-linkedin-data-access.md` §3.4](research/01-linkedin-data-access.md))
> records `jane-street` as **UNVERIFIED — MUST CHECK**, and the gazetteer carries
> `jane-street-global` plus the legacy pages `jane-street-capital-llc` and
> `janestreetgroup`. Just type "Jane Street" into LinkedIn search, open the
> company with ~3.7k employees, and use whatever slug the address bar shows.

On the People tab:

1. Find **"How you are connected"** in the facet panel. Choose **2nd**.
2. Every person now listed is *provably* adjacent to at least one of your
   connections. That is why they are worth opening — LinkedIn has already done
   the filtering.
3. Scroll and note the ones worth resolving. The tab is paginated and LinkedIn
   does not guarantee completeness, so treat what you see as a sample.

Start with 20–30 people, not 300. You can always add more later — the file is
append-only in practice, and re-running `enrich` is instant.

---

## 3. Read each profile (the actual work)

For each target:

1. Open their profile.
2. Find the **"N mutual connections"** line under their name. **Write N down
   first.** This single number is what tells interlayer whether your reading was
   complete; without it, every observation you record is treated as incomplete.
3. **Click the link.** You get a search-results page listing the mutuals.
4. Type the names into your file, one per line.
   - Where two of your connections share a name, **paste the profile URL
     instead**. interlayer will not guess between two people — it reports them
     and makes you come back.
   - Pasting URLs is always safe and always stronger than a name. If you are
     hovering anyway, copy the link.
5. If the list is longer than you want to record, **stop wherever you like** and
   write `truncated: yes`. A partial reading is genuinely useful. A partial
   reading recorded *as if it were complete* is not.

### Two things that will slow you down

- **Large lists paginate, and very large ones may truncate.** The exact cap is
  not documented by LinkedIn and we have not measured it. If the page stops
  giving you more names before you reach N, that *is* truncation: record what
  you have, keep the stated N, and let interlayer flag it.
- **The free-tier Commercial Use Limit.** Free accounts get roughly **250–350
  people-searches per month**, after which results throttle to about three per
  query until the 1st of the next month. Clicking a mutual-connections link is a
  people search. Budget accordingly: 30 targets a month costs you nothing, 300
  will burn the allowance. Sales Navigator raises the cap but reveals nothing
  new here.

---

## 4. Write the file

Copy the template and edit it:

```bash
cp docs/observations-template.yaml data/observations.yaml
```

`data/` is git-ignored, so the names you type never reach version control.

```yaml
firm: jane_street          # session default — set it once
observed_on: 2026-08-21

targets:
  - name: Priya Raman
    url: https://www.linkedin.com/in/priya-raman-4821
    title: Quantitative Trader
    mutuals_stated: 4
    mutuals: |
      Sarah Chen
      Rajiv Patel
      Marcus Webb
      Ana Beatriz Souza

  - name: Daniel Osei
    url: https://www.linkedin.com/in/daniel-osei-quant
    mutuals_stated: 31
    truncated: yes
    mutuals: |
      Sarah Chen
      Tom Nakamura
      https://www.linkedin.com/in/j-smith-8827
```

The `|` matters: under it you type **one name per line, with no quotes, no
commas and no brackets**. That is the whole reason the format is YAML rather
than CSV — a CSV would make you retype the target's URL on every row and would
break on the first job title containing a comma.

Full field list is in the template. Only `name` and `firm` are required, and
`firm` can come from the session default. Key aliases are accepted (`url`,
`link`, `profile`, `linkedin_url` are the same field), but an **unrecognised**
key is a hard error — a typo'd `mutual:` would otherwise silently discard
everything you collected.

interlayer looks for the file, in order, at:

1. `$INTERLAYER_OBSERVATIONS`, if you set it
2. `data/observations.yaml` (or `.yml`)
3. `observations.yaml` (or `.yml`) in the working directory

It never looks inside the artifact directory, because `interlayer purge` deletes
that and it must never be able to destroy your typing.

---

## 5. Run it

```bash
uv run interlayer enrich
```

You get three artifacts:

| File | What it holds |
|---|---|
| `artifacts/targets.jsonl` | the people you looked at |
| `artifacts/observations.jsonl` | the true bridge edges, with completeness flags |
| `artifacts/unresolved_mutuals.jsonl` | **your to-do list** |

Read the unresolved file. Every name that could not be matched to one of your
connections is in there with the reason and the near misses:

- `ambiguous_name` — several of your connections match. Paste the URL.
- `weak_match` — closest score was below the accept threshold. Check the
  spelling, or paste the URL.
- `no_match` — nothing resembles it. Usually a typo.
- `url_not_in_connections` — that profile is not in your export. Either they are
  not a 1st-degree connection, or your `Connections.csv` predates the
  connection: re-export and re-run `ingest`.

Nothing is ever guessed and nothing is ever silently dropped. Fix, re-run, repeat.

### What "incomplete" means downstream

An observation is marked `complete: false` when fewer mutuals resolved than the
stated count, when any recorded name failed to resolve, when you wrote
`truncated: yes`, or when you gave no stated count at all. For such a record,
**absence from the list is not evidence of absence** — the bridges you recorded
are still facts, but the set is a sample, not a census. Writing `complete: yes`
does not override arithmetic that contradicts it; the flag flips back and the
tool tells you why.

---

## 6. Why this is manual, and must stay manual

interlayer ships no scraper, has no network code in any stage, and never will.

**The User Agreement (§8.2) prohibits** developing, supporting or using
"software, devices, scripts, robots, or any other means or processes (including
crawlers, browser plugins and add-ons)" to scrape the service, and prohibits
using bots or automated methods to access it. Browser extensions that overlay
LinkedIn's UI are separately prohibited. Reading a page as a logged-in member is
ordinary product use; automating that reading is not.

**The commonly-cited defence does not apply.** *hiQ v. LinkedIn* is routinely
summarised as "scraping is legal". hiQ won the CFAA question about **public**
pages and then **lost on breach of contract**, ending in a **$500,000 judgment**,
a permanent injunction, and an order to destroy the data. The connection graph
is not public: it is behind a login you passed under a contract that says no
bots. The reasoning that helped hiQ inverts the moment you sign in.

**And the real risk is not litigation.** For a private individual analysing their
own network, being sued is very unlikely — LinkedIn's suits target commercial
resale at scale. What is *documented and common* is account restriction and
**permanent bans** for cookie-driven automation tools, which LinkedIn rarely
reverses. Losing a ten-year professional network permanently is uninsurable, and
it is the exact asset this entire project is built on top of. The mutual-
connections view hands you the true edges for free. There is nothing to gain by
automating it and everything to lose.

Two corollaries worth stating plainly:

- **Never create a second or fake account** to do this. That is the precise fact
  pattern LinkedIn litigates, and it was part of what sank hiQ.
- **This data is personal data.** Keep it in `data/`, keep it out of git, and use
  `interlayer purge` when you are done. `interlayer report --redact` exists for
  when you want to show someone the shape of the result without the names.

---

## 7. Realistic expectations

A first session of 25 targets, at two to three minutes each, is about an hour and
typically yields 60–150 true bridge edges across a few dozen of your connections.
That is enough to change the ranking materially, because observed evidence is
weighted at 10.0 against inferred employment's 6.0 — one real bridge outweighs
any amount of inference.

Coverage limits worth knowing, none of which affect *accuracy*:

- Employees who are 3rd-degree or out of network have no mutuals to read.
- The People tab shows a subset, and headcounts there are self-reported.
- Very large mutual lists may truncate — flag them and move on.

Do the highest-value targets first, record the stated counts honestly, and stop
when you get bored. Partial ground truth beats complete inference.
