# Privacy notice and data-protection position

**Applies to:** `interlayer`, a local command-line tool that finds which of your own
LinkedIn connections can introduce you to people at a small number of named firms.

**Last reviewed:** 2026-08-17. **Print this file with `interlayer privacy`.**

This document is written for the person *running* the tool. Running it makes you a data
controller for the personal data it holds about other people. Nothing here is legal advice;
it is the reasoning the tool was built on, stated plainly enough that you can check it against
your own situation.

---

## 1. Who the controller is

**You are.** Not the authors of this tool, and not any hosted service — there is no hosted
service. All processing happens on your machine, all state lives under a single directory you
choose (`state_root`, default `~/.interlayer/`), and no data is transmitted anywhere by the
analysis layer. There is no telemetry, no analytics, no crash reporting and no version check
(PRIV-01, PRIV-02).

If you run this **for an employer** — business development, recruiting, fundraising — then in
practice the employer is likely the controller and your organisation's own data-protection
processes apply, including its record of processing activities and any DPIA threshold. Check
before you run it on company business.

Two consequences follow from being the controller and they are not optional:

- Data subjects may exercise their rights against **you** (Section 8).
- If you export a report and send it to someone else, you have disclosed third-party personal
  data. The tool redacts contact details and stamps every report with a
  do-not-redistribute header, but it cannot stop you. Don't.

## 2. What the tool is for (purpose)

One purpose, stated narrowly on purpose:

> Identify which of my own existing first-degree connections have a connection to people at
> specific named firms, so that I can ask for a warm introduction.

That is the *only* purpose the design supports. Reusing this data for recruiting pipelines,
competitive intelligence, investment research, lead lists or anything else is a new purpose
requiring a fresh assessment under Art. 5(1)(b) purpose limitation — not a free extension of
this one.

**Not purposes, and not possible with the shipped configuration:** building a resellable
dataset, profiling, automated decision-making, advertising, or contacting people using contact
details they did not give you.

## 3. Whose data, and which categories

| Category | Fields held | Source |
|---|---|---|
| Your own connections (`M`) | first name, last name, LinkedIn public slug, employer string, position, connection date | your own LinkedIn data export (`Connections.csv`) |
| Target-firm people (`T`) | first name, last name, LinkedIn public slug, title, team, firm | surfaces you viewed while logged into your own account, or a file you filled in by hand |
| The relationship | that a given connection of yours is also a connection of a given target person, plus when it was observed and by which adapter | the mutual-connections view, which is exactly the set you are entitled to see |
| Company-match review queue | free-text employer strings awaiting adjudication | derived |
| Tombstones | a SHA-256 of an erased person's key — no readable identity | derived from erasure |

**Email addresses are discarded at parse time** unless you explicitly set
`retain_emails = true` (PRIV-06). Even then they are excluded from every rendered report
(PRIV-17). Phone numbers are never persisted at all.

**Special categories under Art. 9 are never processed.** No racial or ethnic origin, political
opinion, religious or philosophical belief, trade-union membership, genetic, biometric, health,
sex life or sexual orientation data — not directly, and not by proxy. The persisted schema is
pinned to an explicit field allowlist so that adding *any* new column fails a test until it has
been reviewed against Art. 9 (PRIV-08). Name-derived keys (token order, script, phonetic form)
exist only as internal blocking keys for record matching and never reach a report or a cluster
label (PRIV-09) — clustering on name origin would be inference about ethnicity, which
legitimate interest cannot lawfully support.

## 4. Lawful basis: legitimate interest (Art. 6(1)(f))

**The household exemption probably does not apply — do not rely on it.** Art. 2(2)(c) exempts
processing "in the course of a purely personal or household activity", but the CJEU reads it
strictly (*Ryneš* C-212/13: *purely*; *Lindqvist* C-101/01: private or family life), and the
ICO's framing is that the exemption falls away as soon as processing has a professional or
commercial purpose. Warm-intro pathfinding for a job search or business development **is** a
professional purpose. A strictly private, non-commercial use might squeak in, but that is not a
foundation to build on and it evaporates the moment the output is used for career or commercial
ends.

So the basis is **legitimate interest**, and EDPB Guidelines 1/2024 require all three limbs to
be documented:

1. **Interest.** Real and present, not speculative: "identify which of my own contacts can
   introduce me to people at three named firms." Warm introductions are a mainstream
   professional activity — LinkedIn sells the same computation as Sales Navigator TeamLink.
2. **Necessity.** No less intrusive route achieves it. This is why minimisation is a build
   constraint rather than a virtue: you need a name, an employer and the edge. You do not need
   an email, a phone number, a photo, an education history, or anything at all about people who
   turn out not to be bridges — and the tool deletes those people at the end of the run
   (PRIV-07).
3. **Balancing.** In favour: everyone involved published their employer on a professional
   networking site and is already connected to you or to a mutual contact, so being findable by
   a mutual contact for an introduction sits inside their reasonable expectations. Against: the
   data subject has no relationship with *this tool* and did not expect their connection graph
   to be reconstructed offline. What tips the balance is the compensating controls — local-only
   storage, no sharing, no resale, minimal fields, a 90-day default retention, and a real
   erasure path.

**If you change the defaults, you change the balancing.** Enabling a third-party data provider,
turning on `retain_emails`, or extending retention weakens the case that tipped it. The audit
log records the configuration hash for exactly this reason: the record of what you actually ran
is the evidence of what you actually decided.

**CCPA/CPRA:** those obligations attach to a "business" meeting revenue or volume thresholds.
An individual doing professional research meets none of them. Two things flip that — running it
for an employer that *is* a covered business, or selling or "sharing" the derived data. That is
a reason not to commercialise the output, not a reason not to use the tool.

## 5. Retention

**Default: 90 days** (`retention_days`). Every record carries `collected_at` and `source`
(PRIV-10), and a retention sweep runs **on every startup, before any other work**, deleting
anything older (PRIV-11). Storage limitation, Art. 5(1)(e), is enforced by code rather than
documented as an intention.

Three further limits apply:

- Non-bridges do not survive the run at all (PRIV-07).
- All state lives under one root, so `interlayer purge --all` is complete by construction
  (PRIV-03, PRIV-12).
- Tombstones are exempt from the sweep and kept indefinitely. They contain no readable
  identity — only a SHA-256 of the key — and expiring them would silently re-enable
  resurrection of someone who asked to be forgotten.

Shorten retention with `retention_days` in your config file or `INTERLAYER_RETENTION_DAYS`.
Nothing stops you setting `0`, which expires everything at the next startup.

## 6. Where the data goes: nowhere

The analysis layer (`core`, `ingest`, `graph`, `report`) never opens a network connection, and
a test enforces that by running the whole pipeline with sockets blocked. Acquisition adapters
are a separate layer, each declaring a compliance posture, and the shipped configuration enables
`first_party_export` only — your own LinkedIn export (PRIV-04). Enabling anything else is an
explicit act, and it is written into the audit log.

No adapter accepts a LinkedIn session cookie. There is no scraper.

## 7. Art. 14 — the position, stated honestly

Most of this data was **not** obtained from the data subjects. Art. 14 therefore requires
notifying them — controller identity, purposes, categories, source, retention, rights — within a
reasonable period and at most one month. Art. 14(5)(b) relieves that where notice is impossible
or involves disproportionate effort, **but** regulators read the exemption restrictively and
expect a documented, per-project assessment plus compensating measures, not a blanket claim.

Be honest about which side of that line you are on. For one individual holding a few hundred
records about people who are one or two hops away, individual notification is not obviously
disproportionate — unlike a commercial scraper, you generally *can* reach these people. The
defensible posture this tool is built around:

1. Hold the data **locally, briefly and minimally** (Sections 3 and 5).
2. Ship **this notice**, stating purpose, categories, retention and rights.
3. Treat **actual outreach as the moment of disclosure** — when you ask for or receive an
   introduction, say how you found them. "I saw we're both connected to X" is not just
   courteous, it is the Art. 14 information provided at the natural moment.
4. **Honour erasure immediately** via `purge --person` (Section 8).

If you hold this data for months without contacting anyone, you are relying on the exemption
without the compensating disclosure. Purge instead.

## 8. Exercising your rights (Art. 15 access, Art. 17 erasure)

If you are a data subject, address your request to the person running the tool — they hold the
data; the authors of the software hold nothing.

If you are the operator and you receive a request, the tool answers it directly:

| Right | Command | What it does |
|---|---|---|
| Access (Art. 15) | `interlayer report --format json` | Every field held, in machine-readable form. Filter to the individual before you send it — a full export discloses other people. |
| Erasure (Art. 17) | `interlayer purge --person <id or slug>` | Deletes the person, every edge incident to them, and every cached derived artefact — then writes a tombstone so re-ingesting the same `Connections.csv` cannot bring them back (PRIV-13). |
| Erasure, target-side | `interlayer purge --target <entity id>` | Removes all target-side records for that person or firm and every edge orphaned by the removal (PRIV-14). |
| Erasure, everything | `interlayer purge --all` | Empties the state root and exits non-zero if anything survives (PRIV-12). |
| Objection (Art. 21) | as erasure | Legitimate interest is subject to an absolute right to object; the answer is a purge, not a debate. |
| Rectification (Art. 16) | re-ingest, or edit the review queue | Employer strings are adjudicated by a human; inferred values are labelled as inferred (PRIV-19). |

The audit log at `<state_root>/audit.log` is your Art. 5(2) accountability record: one
append-only JSON line per collection, match, export and purge event, carrying counts, adapter
ids, a timestamp and the configuration hash — and **no names**, ever (PRIV-15, PRIV-16). It
is the evidence that the sweep ran and the purge happened. Only `purge --all` removes it, and
it writes its own last line before doing so.

## 9. Lines this tool will not cross

1. **No aggregation into a resellable dataset.** Local files, single user, no shared store.
2. **No inference about protected characteristics** — including implicitly, by clustering on
   name origin or treating affinity-group membership as a signal.
3. **No sharing of the derived graph.** It is personal data about people who never agreed to be
   in it. Reports are for your own decision-making.
4. **No contacting people using data they did not share with you.** If an address is absent from
   your export because they chose not to share it, do not source it elsewhere. Outreach goes
   through the introduction — that is the entire point of the tool.
5. **No covert acquisition.** No credential sharing, no automated scraping in the default
   configuration, no misrepresenting who you are to view a profile.
6. **No silent resolution of ambiguity.** An employer string that cannot be matched
   deterministically is held out of the graph until a human adjudicates it, and the count of
   unadjudicated items appears in the report (PRIV-20). Guessing that "Citadel Technology" is
   the hedge fund would be inventing a fact about someone's employer.

---

*Sources for the legal positions summarised here — EDPB Guidelines 1/2024 on legitimate
interest, GDPR Art. 2(2)(c), 5, 6(1)(f), 9, 14, 15, 17, CJEU C-212/13 Ryneš and C-101/01
Lindqvist, ICO exemptions guidance, and the CPRA "business" thresholds — are cited in full in
`docs/research/R5-targets-and-privacy.md`. This notice is a summary written for an operator, not
a legal opinion.*
