# interlayer

Find the people in your own LinkedIn network who sit closest to a target firm —
by default **Jane Street**, **Citadel LLC**, and **Citadel Securities**.

Runs entirely on your machine, on data you already have. Ships no scraper.

---

## What this can and cannot know

LinkedIn exposes no interface — official API, partner API, or export — that returns
another member's connection list. That data is private. So the question "who is in Jane
Street's employees' networks?" has no obtainable answer, at any price.

But that is not quite the question worth asking. The useful one is

```
{ your connections } ∩ { a Jane Street employee's connections }
```

and LinkedIn renders exactly that, as a clickable list, on any 2nd-degree profile — for
free, and it keeps working even when the other person has hidden their connections list.
Reading it is ordinary use of the product. Automating the reading of it is not.

interlayer therefore works on two tiers:

| Tier | Source | Effort | Status of the result |
|---|---|---|---|
| **1** | Your `Connections.csv` export | None | **Inferred.** Affiliation evidence — who works where, alongside whom, when |
| **2** | Mutual-connection lists you read and record | Manual | **Observed.** A named connection of yours who provably knows a named target-firm person |

Tier 1 works immediately and is wrong some of the time. Tier 2 takes an afternoon and is
right by construction. The distinction is enforced in the type system, carried through
every stage, and shown on every row of the report — an inferred link is never displayed as
though it were a fact.

## Install

```bash
git clone https://github.com/adotlex/interlayer && cd interlayer
uv sync --locked
```

## Use

Request your archive at **Settings → Data privacy → Get a copy of your data**, tick
*Connections*, and wait for the email.

```bash
uv run interlayer run ~/Downloads/Connections.csv
```

That writes `artifacts/report.html` — one self-contained file that makes no network
requests when you open it. Stages also run individually:

```
ingest → normalize → enrich → build → cluster → score → report
```

To add Tier 2 ground truth, copy `docs/observations-template.yaml`, fill it in while
reading LinkedIn, and re-run. See **[docs/collecting-observations.md](docs/collecting-observations.md)**
for the workflow and the account-safety rules.

Useful flags: `--redact` (pseudonymous output, safe to share), `--include-speculative`
(show near-zero-confidence rows, hidden by default), `--seed` (reproducibility),
`interlayer purge` (delete every artifact).

## How it decides

1. **Ingest** — parse the export. The header row is found by scanning, not by skipping a
   fixed preamble, because the preamble's length has changed across export versions.
2. **Normalize** — resolve employer strings to real firms. This is where most of the
   difficulty lives: *Citadel* is a hedge fund, a separate market maker, and a military
   college with roughly ten times the fund's headcount. They are never merged.
3. **Enrich** — fold in whatever mutual-connection observations you recorded.
4. **Build** — a weighted graph. Two people get an edge only if their tenures actually
   overlapped, weighted down by how large the shared employer is: sharing a 40-person firm
   is evidence, sharing a 200,000-person one is barely anything.
5. **Cluster** — consensus Leiden over 50 seeded runs. A single run is unstable enough
   that you would see different groups each time you looked.
6. **Score** — a random walk seeded on target-firm people, plus additive evidence. Every
   score equals the sum of the evidence behind it, and a score with no evidence is refused
   rather than shown.
7. **Report** — one HTML file, no external requests, every row auditable.

## ACCEPTABLE USE

interlayer processes a file describing real people who never agreed to be analysed. That
fact sets the boundaries of what it is for.

**Built for:** warm-intro pathfinding for your own job search; understanding the shape of
the network you have actually accumulated; deciding which handful of people are worth a
real conversation.

**Not built for, and deliberately not optimised for:**

- **Bulk unsolicited outreach.** No message generation, no mail-merge, no outreach-tool
  export.
- **Building a dataset about third parties.** Do not sell, license, publish, or pool the
  output.
- **Evaluating people without their knowledge in a hiring decision.** Using inferred,
  unverified, frequently stale relational data to decide someone's employment is precisely
  the application this refuses to serve.
- **Any use where you would be uncomfortable showing someone their own entry.** That is a
  good working test, and it is the one this is designed against.

**If you are using this for work** — recruiting, sales, business development — the GDPR
"purely personal or household activity" exemption does not apply to you. You are a data
controller with real obligations: a documented lawful basis, a privacy notice owed to
people who have never heard of you, and a way to answer access and deletion requests. This
tool discharges none of that for you.

## LIMITATIONS

**Everything in Tier 1 is inferred. None of it is verified.**

1. **The data is stale by construction.** An export reflects what people last bothered to
   update — often years ago. Someone shown at a firm may have left long since.
2. **"Adjacent" is a guess, not a relationship.** It does not mean two people know each
   other, have spoken, or would recognise the connection. Confidence scores rank within
   your own data; they are not calibrated probabilities.
3. **Matching is fuzzy and fallible.** Firms rebrand, subsidiaries share names, people
   share names, titles lie. False positives are expected.
4. **Absence means nothing.** Someone missing from a cluster is not evidence they are
   unconnected — only that your export carries no signal about them.
5. **Your export is not your network.** It has no interaction strength and no record of
   whether you have ever spoken. A conference connection from 2014 and a former manager
   look identical.
6. **The gazetteer is hand-curated and incomplete.** It reflects one person's judgement at
   one point in time. Read `data/gazetteer/firms.yaml` and change it if it is wrong.

**Rule of thumb:** treat every output as a hypothesis to check, never a fact to forward.

## On scraping

This repository ships no scraper and will not accept one. Automating a logged-in LinkedIn
session breaches the User Agreement, and the practical risk is not litigation — it is
losing the account the entire workflow depends on. The best-known commercial scraper of
this data was sued in January 2025 and shut down that July; the case usually cited as
having legalised scraping ended with the scraper losing on contract, paying a $500,000
judgment, and being ordered to destroy the data.

Other data sources plug in behind a documented adapter interface. What you are licensed to
use is your call to make and yours to defend.

## Development

```bash
uv run pytest -q                     # full suite
uv run pytest --lf --lfnf=none -q    # only what failed last time
uv run ruff format . && uv run ruff check --fix . && uv run mypy
```

Architecture and the reasoning behind each decision: **[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)**.
Underlying research, including what was measured rather than assumed:
**[docs/research/](docs/research/)**.
