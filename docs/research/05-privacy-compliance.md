# 05 — Privacy, Compliance & Safety Guardrails

**Status:** Normative. The MUST / SHOULD / MUST NOT list in §3 is a build contract, not advice.
**Author:** Wave 1 / Agent 5 · **Date:** 2026-08-21
**Audience:** Wave 2 build agents, Wave 3 test agents.

Every technical claim in §3 and Appendix A was verified by execution in this repo's Python 3.11.15 /
git environment. Where a "well-known" idiom turned out to be wrong, that is called out explicitly.

---

## 1. Nature of the data

`Connections.csv` from the official LinkedIn export contains, per row:

| Field | Personal data? | Notes |
|---|---|---|
| First Name, Last Name | Yes — direct identifier | |
| URL (public profile) | Yes — direct identifier | Stable, resolvable to a living person |
| Email Address | Yes — direct identifier | Present only where the connection enabled *"Allow connections to export my email"* (off by default; realistically 10–40% populated) |
| Company | Yes — employment data | |
| Position | Yes — employment data | |
| Connected On | Yes — relational metadata | Reveals *when* a relationship formed |

Two things follow immediately and are not negotiable:

1. **This is a file of real third parties who never consented to this analysis.** They consented to
   connecting with the user on LinkedIn. That is a different thing.
2. **The tool's output is *inferred* personal data**, which is still personal data, and which
   attracts the accuracy principle (GDPR Art. 5(1)(d)) and the right to rectification (Art. 16).
   An inference does not have to be true to cause harm — an inaccurate inference still shapes how
   a person is viewed and treated.

Building the graph is **profiling** within the meaning of GDPR Art. 4(4): automated processing of
personal data to evaluate personal aspects, specifically "economic situation" and professional
relationships. Note this word — it recurs below, because "profiling" is the specific trigger that
strips away several otherwise-available exemptions.

### 1.1 Is the user a controller? The household exemption

GDPR Art. 2(2)(c) removes from scope processing "by a natural person in the course of a purely
personal or household activity." Recital 18 defines the edge precisely:

> "This Regulation does not apply to the processing of personal data by a natural person in the
> course of a purely personal or household activity and **thus with no connection to a professional
> or commercial activity**. Personal or household activities could include correspondence and the
> holding of addresses, or social networking and online activity undertaken within the context of
> such activities."

The CJEU reads this **narrowly**, consistently, across three decades:

- **C-101/01 *Lindqvist* (2003)** — the exemption relates "only to activities which are carried out
  in the course of private or family life of individuals," which is "clearly not the case" where
  data is published on the internet to an indefinite number of people. *Publication kills it.*
- **C-212/12 *Ryneš* (2014)** — a homeowner's CCTV camera capturing public pavement fell outside the
  exemption. The Court restated that a derogation from a fundamental-rights protection must be
  interpreted strictly, and stressed the word **"purely."** *Any spill beyond the domestic sphere
  kills it.*
- **C-25/17 *Jehovan todistajat* (2018)** — handwritten door-to-door notes by individual members
  were **not** purely personal, and constituted a "filing system"; the community was a joint
  controller. *A structured, searchable file assembled for a purpose that reaches outside private
  life kills it — even when kept by an individual, on paper, at low volume.*

**When the exemption falls away for this tool:**

| Use | Exemption? | Why |
|---|---|---|
| Mapping your own network out of curiosity; keeping a private contact map | Likely applies | Analogous to "the holding of addresses" in Recital 18 |
| Finding a warm intro for **your own** job search | **Contested — do not rely on it** | Recital 18's "no connection to a professional activity" is a real problem. Your own career *is* a professional matter. No CJEU case is on point; the safest planning assumption is that the exemption is unavailable. |
| Any use on behalf of, or for the benefit of, an employer | Does not apply | Professional context, per Recital 18 |
| Recruiting / sourcing candidates | Does not apply | *Jehovan todistajat* is directly analogous: structured file, non-domestic purpose |
| Sales / BD prospecting | Does not apply | Commercial |
| Sharing or publishing the output beyond the user's own household | Does not apply | *Lindqvist* |
| Selling, licensing, or pooling the dataset | Does not apply | Commercial, and separately unlawful on several other grounds |

**Design consequence — the load-bearing point for build agents:** because the exemption's
availability is a property of *what the user does*, not of *what the code does*, the code cannot
know which side of the line it is on. **Therefore the code must be built to controller-grade
standards unconditionally.** The exemption, where it applies, becomes head-room the user did not
need — never a design assumption. This is what "safe by construction" means here.

### 1.2 What "you are a controller" actually costs

If the exemption is unavailable, the user takes on, at minimum:

- **A lawful basis.** Realistically Art. 6(1)(f) legitimate interests, which requires a documented
  three-part balancing test (purpose / necessity / balance against the data subject's rights).
  Consent is not obtainable at this scale; contract and legal obligation do not fit.
- **Art. 14 transparency.** Because the data was not obtained from the data subjects, notice is owed
  within one month or at first contact, whichever is earlier. The Art. 14(5)(b) "disproportionate
  effort" carve-out is **not** a blanket excuse: the ICO requires a documented balancing assessment,
  and the more significant the effect on the individual, the less available it is. In practice: if
  you contact someone, the first contact must carry the notice.
- **Art. 5(1)(d) accuracy** — every reasonable step to erase or rectify inaccurate data, which for
  this tool means inferences must be labelled, scored, and correctable.
- **Arts. 15–21 data subject rights** — access, rectification, erasure, and objection, including
  the absolute right to object to direct marketing (Art. 21(2)).
- **Art. 35 DPIA** — likely triggered where profiling is systematic and extensive and used for
  decisions producing legal or similarly significant effects (hiring qualifies).
- **Art. 32 security** — appropriate technical measures. This is the direct hook for the file-
  permission and no-egress requirements in §3.

### 1.3 CCPA / CPRA

CCPA/CPRA binds a **"business"**, defined by thresholds. For the 2026 compliance year, a for-profit
entity doing business in California that determines the purposes and means of processing is covered
if **any one** of these is true:

1. Annual gross revenue over **$26,625,000** (the $25M statutory figure, inflation-adjusted);
2. Annually buys, sells, or shares the personal information of **100,000+** California consumers or
   households (raised from 50,000 by CPRA); or
3. Derives **50%+** of annual revenue from selling or sharing personal information.

An individual running this on a laptop for their own job search meets none of these and is not a
"business." **But note the trap in threshold 3:** it has no revenue floor. A one-person side
business whose income comes mainly from selling contact data is a covered business at any size. And
threshold 1 is *global* gross revenue — a recruiter at a mid-size firm is inside a covered business
the moment the tool is used for work, and the connections file becomes that business's record,
inheriting notice-at-collection, opt-out-of-sale/share, deletion, and correction duties. Names,
professional history, and inferences drawn from them are all "personal information"; the inferred
adjacency graph is expressly within CCPA's definition, which covers inferences drawn to create a
profile.

Also relevant, briefly: the **EU AI Act** exempts natural persons deploying AI systems in the course
of a purely personal non-professional activity (Art. 2(10)) — the same personal/professional seam,
with the same problem that **profiling of natural persons is the category least likely to keep the
benefit of such carve-outs**. Employment and recruitment systems sit in Annex III (high-risk). If a
build agent ever adds an ML ranking model *and* the tool is used in hiring, this stops being a
footnote. It is a further reason the tool must never present itself as a candidate-evaluation system.

### 1.4 Plain-English verdict

> **Personal use** — mapping your own network, finding who might introduce you to someone at a firm
> you want to work at, keeping a private CRM: **likely fine.** Nobody is coming after you. The
> guardrails below still apply, because they cost you almost nothing and they are what make the
> answer "likely fine" instead of "it depends."
>
> **Recruiting, sales, or any commercial use** — you are a **data controller** with real, audited
> obligations: a documented lawful basis, a privacy notice you owe to people who have never heard of
> you, subject-rights machinery, and probably a DPIA. This tool does not discharge those for you and
> is not designed to. If that is your use case, talk to a lawyer before you run it, not after.
>
> **Selling, publishing, or pooling the output**: don't. There is no reading of the above under which
> that is defensible, and it is the single fastest way to convert a private analysis into a
> regulatory and contractual problem.

---

## 2. Scraping posture

A companion agent's research on the LinkedIn User Agreement will conclude that scraping is
prohibited. Section 8.2 of the User Agreement bars developing, supporting, or using "software,
devices, scripts, robots or any other means or processes (such as crawlers, browser plugins and
add-ons or any other technology) to scrape or copy the Services, including profiles and other data,"
and bars bots or unauthorised automated methods to access the Services or download contacts.

The *hiQ Labs v. LinkedIn* saga is the case people cite for "scraping public data is legal." Read
what actually happened: the Ninth Circuit's 2022 ruling was favourable on the **CFAA** question, but
LinkedIn then **won summary judgment on breach of contract**, and the matter closed in December 2022
with a **$500,000 stipulated judgment against hiQ** plus injunctive relief barring future scraping,
with liability acknowledged for breach of contract, CFAA, California's unauthorised-access statute,
trespass to chattels, and misappropriation. The headline "scraping is legal" is a misreading of a
case the scraper lost.

**Posture this codebase adopts:**

- **(a)** Full functionality on the official export. The export is the user's own data, obtained
  through LinkedIn's own supported mechanism. This is the supported, first-class, only-shipped path.
- **(b)** **No scraper ships.** No HTTP client aimed at any LinkedIn host, no headless browser, no
  cookie/`li_at` handling, no rate-limit-evasion helper, no "bring your own session" convenience.
  Not behind a flag, not in `contrib/`, not commented out, not in tests. Enforced by P-2 and P-33.
- **(c)** A documented, narrow **adapter interface** (`DataSourceAdapter`, §3.6) so a user with a
  licensed or authorised source — an ATS export, a CRM they own, LinkedIn's official partner APIs,
  a purchased dataset with rights they can evidence — can plug it in. Per the handoff plan the
  **export adapter is the only source shipped**; alongside it, at most `NullAdapter` (returns
  nothing) and `LocalFileAdapter` (reads a CSV/JSON the user placed on disk). No adapter in this
  repository may open a socket.

This is not squeamishness. It is that (b) is the difference between a tool whose worst-case outcome
is "I learned something about my own network" and one whose worst-case outcome is a banned account
and a contract claim.

### 2.1 Keeping the user's LinkedIn account intact

If the user supplements the export with anything manual, the risk is entirely to *their* account.
This belongs in the docs because a banned account is the most likely concrete harm this project can
cause its own user. Practical guidance only:

**Safe.**

- **The official export** (Settings → Data Privacy → Get a copy of your data). This is the supported
  mechanism. It costs nothing, carries no risk, and it is the whole reason the tool works.
- **Reading profiles by hand at human speed**, and copying a handful of facts into a local file by
  hand. Browsing your own network is what the account is for.
- **Official, licensed surfaces** you already pay for and whose terms you have actually read —
  Recruiter, Sales Navigator exports where the licence permits it, official partner APIs.

**Restriction risk, in rough order of severity.**

- **Installed automation extensions are detectable before you ever run them.** LinkedIn probes for
  the static resource URLs of thousands of known Chrome extensions, so tools of the Dux-Soup /
  Octopus / LinkedIn Helper family are identifiable from the fact of installation — no behavioural
  analysis needed. *Uninstall them; don't merely stop using them.*
- **Cloud automation** (PhantomBuster-style) is worse than local extensions: it runs from datacentre
  IPs that never match the account's normal login geography.
- **Email-finder / contact-append extensions** are described consistently as the fastest route to a
  restriction, because bulk contact extraction is exactly what §8.2 targets.
- **Volume, even by hand.** Free accounts hit a monthly **commercial use limit** on search
  (~300 profile views before search results are cut off). Profile-view ceilings sit around 500/day
  free and ~2,000/day on Recruiter/Sales Navigator; the practical advice is to stay under half.
  Connection requests: ~100/week is the standard cap, reputation-adjusted (100–200 for a healthy
  account, sometimes 80 or fewer for a weak one).
- **Low acceptance rate on connection requests** is itself a restriction trigger, independent of
  volume — which is a direct argument for what this tool is designed to do: send five good requests
  instead of two hundred speculative ones.
- **Machine-regular timing.** Perfectly even intervals, 3am activity, no idle gaps.

**If restricted.** First or minor incidents typically clear in 24 hours to 7 days. Repeat or severe
cases can require a manual appeal with government-ID verification, taking roughly 3–7 business days,
with no guaranteed outcome. A permanently closed account takes the connection graph with it.

**The design consequence:** the tool's job is to *reduce* the number of LinkedIn actions the user
takes, not increase it. Every feature that would push a user toward higher-volume activity is a
feature that endangers their account — which is the second, self-interested reason for P-40.

---

## 3. Engineering guardrails — normative requirements

Phrased as testable requirements. `MUST` = build blocker. `SHOULD` = default yes, deviation must be
recorded in the PR description. `MUST NOT` = reject the PR.

### 3.1 Network egress

- **P-1 (MUST)** The core pipeline — `ingest → normalize → build-graph → cluster → score → report` —
  MUST perform zero network I/O. Verifiable by running the full pipeline with the socket layer
  disabled (§6, `test_no_network_egress`).
- **P-2 (MUST)** The package MUST NOT import `requests`, `httpx`, `aiohttp`, `urllib3`, `selenium`,
  `playwright`, or `socket` at module scope anywhere under `src/interlayer/` except in a single
  quarantined module `src/interlayer/net.py`, which MUST NOT be imported by the core pipeline. An
  AST-based import test enforces this (`test_core_imports_no_network_libs`).
- **P-3 (MUST)** No telemetry, no analytics, no crash/error reporting, no update check, no license
  phone-home. Not opt-out — absent.
- **P-4 (MUST)** No personal data may be sent to any hosted LLM or third-party API. If an LLM is used
  at all it MUST be a local model, and the prompt-construction path MUST pass through the redaction
  layer (P-12) first.
- **P-5 (MUST)** Any future networked feature MUST require an explicit `--allow-network` flag, MUST
  be off by default, MUST print what it is about to send and to where, MUST require interactive
  confirmation unless `--yes`, and MUST log the egress event to the run manifest (P-20).
- **P-6 (MUST)** The generated HTML report MUST be fully self-contained and MUST make **zero**
  outbound requests when opened: no CDN scripts or stylesheets, no webfonts, no remote images, no
  tracking pixels, no `<iframe>`, and **no `<img src="…licdn.com…">` profile photos** — rendering a
  page of remote avatars would hand the entire analysed list to a third party the moment the user
  opens their own report. Assets MUST be inlined or omitted. The report MUST carry
  `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">`.
  Enforced by `test_report_has_no_external_references`.

**How to test it (verified, dependency-free).** A naive `monkeypatch.setattr(socket, "socket", raiser)`
**breaks**: `ssl.SSLSocket` subclasses `socket.socket`, so if `ssl` has not yet been imported the
replacement causes `TypeError: function() argument 'code' must be code, not str` at import time.
Patch the **methods and module-level helpers** instead. This was executed and confirmed to block
`urllib`, `http.client`, `requests`, and `urllib3` over both HTTP and HTTPS:

```python
# tests/conftest.py
import socket
import pytest

class NetworkEgressError(RuntimeError):
    """Raised when code under test attempts network access."""

@pytest.fixture
def no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise NetworkEgressError("network access attempted during test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)   # blocks DNS too
    return _blocked
```

Apply it to the whole suite via an `autouse=True` fixture in `tests/conftest.py`, with an
`@pytest.mark.enable_socket`-style escape hatch for the (currently empty) set of tests that need
sockets. Optionally *also* adopt `pytest-socket` (`addopts = --disable-socket --allow-unix-socket`,
raises `SocketBlockedError`) as belt-and-braces — but the hand-rolled fixture above is the
requirement, because it adds no dependency and it is the thing Wave 3 must not be able to skip.

### 3.2 Email addresses

- **P-7 (MUST)** Default email handling is **drop**. `--emails=drop|hash|keep`, default `drop`. In
  `drop` mode the address MUST NOT reach any artifact, cache, log, database, or report.
- **P-8 (MUST)** `hash` mode MUST use **HMAC-SHA256 with a locally generated 32-byte secret**, not a
  bare digest. A bare `sha256(email)` is trivially reversed by dictionary attack over a known email
  corpus and provides no protection. EDPB Guidelines 01/2025 treat hashing without further controls
  as pseudonymisation, never anonymisation — hashed emails remain personal data and remain subject
  to every other requirement here.
- **P-9 (MUST)** Address normalisation before hashing MUST be limited to `strip()` + `casefold()`.
  MUST NOT strip Gmail dots or `+tags` — that is deliberate cross-identity linkage.
- **P-10 (MUST)** `keep` mode MUST print a one-time warning naming the artifact path that will hold
  plaintext addresses, and MUST record `emails=keep` in the run manifest.
- **P-11 (MUST)** The chosen mode MUST be stated in the report header, so a shared report is never
  ambiguous about what it contains.

### 3.3 Redaction / shareable output

- **P-12 (MUST)** `--redact` MUST replace every direct identifier — first name, last name, full name,
  profile URL, profile slug, email (any form) — with a stable pseudonymous ID of the form
  `PC-XXXXXXXXXXXX` (12 chars, Crockford-safe base32 of an HMAC-SHA256 over the profile URL, or over
  `casefold(name)|casefold(company)` where the URL is absent).
- **P-13 (MUST)** The pseudonym key MUST be generated once via `secrets.token_bytes(32)` and stored
  at `<state_dir>/pseudonym.key`, mode `0600`. It MUST NOT be written into any artifact, report, or
  redacted export. Same key ⇒ stable IDs across runs (so a user can re-share an updated report);
  different install ⇒ unlinkable IDs.
- **P-14 (MUST)** Redaction MUST be applied at **serialisation** time by a single chokepoint function,
  not sprinkled through templates. A field added to the model later MUST be redacted by default:
  the serialiser MUST operate on an allowlist of emit-in-redacted-mode fields and drop unknown
  fields, so forgetting to update the redaction list fails closed.
- **P-15 (MUST)** Free-text fields (job titles, company names, user notes) MUST be scanned for
  embedded personal names before emission in redacted mode — a title like `"EA to Jane Doe"` defeats
  field-level redaction. Minimum: substring-match every known first/last name from the input against
  every emitted free-text field, replace with `[REDACTED]`.
- **P-16 (MUST)** Redacted output MUST NOT contain `Connected On` at day precision — quantise to
  month or year. Exact connection dates are a strong re-identification vector against anyone holding
  a similar export.
- **P-17 (SHOULD)** `--redact` output SHOULD suppress clusters with fewer than 3 members; a cluster of
  1 with a company label is a re-identification of that person.

### 3.4 Storage, permissions, retention

- **P-18 (MUST)** All personal data MUST live under one documented root. Default `./artifacts/`
  (overridable with `--artifact-dir` / `INTERLAYER_ARTIFACT_DIR`), state under `./.interlayer/`.
  Documented in the README with an explicit sentence naming what is written where.
- **P-19 (MUST)** Directories holding personal data MUST be `0700`; files MUST be `0600`. **Enforced
  with `os.chmod` after creation, and asserted, not assumed** — see Appendix A for why
  `mkdir(mode=…, exist_ok=True)` and `Path.touch(mode=…)` are insufficient and why relying on the
  ambient umask is unsound.
- **P-20 (MUST)** Every run MUST write a `manifest.json` into its artifact directory recording: tool
  version, UTC timestamp, input file path and SHA-256, row count, `--emails` mode, `--redact` state,
  gazetteer version/hash, rule-set version, and any egress events. This is the audit trail that makes
  "why was this person flagged" answerable months later.
- **P-21 (MUST)** `interlayer purge` MUST exist and MUST delete the artifact directory and state
  directory. Flags: `--dry-run` (default behaviour is to list, then require `--yes`), `--older-than
  <N>d`, `--keep-gazetteer`. It MUST print what it removed. Documentation MUST state plainly that
  unlinking does not guarantee media-level erasure on SSDs/copy-on-write filesystems, and that
  full-disk encryption is the real control.
- **P-22 (MUST)** `interlayer exclude add <id|url|name>` MUST add a person to a persistent exclusion
  list, and every subsequent run MUST drop them at ingest, before any processing. This is the
  implementable core of Arts. 17 and 21: when someone says "take me out of your thing," there must be
  a command for that.
- **P-23 (SHOULD)** A configurable `retention_days` (default 90). On startup, artifacts older than
  the threshold SHOULD produce a warning naming them and the `purge` command.
- **P-24 (MUST)** Logs at INFO MUST contain counts and identifiers, never names, emails, or row
  contents. Exception handlers MUST NOT interpolate row data into messages — wrap parse errors as
  `"row 412, column 'Company': <ValueError>"`, never echo the value.

### 3.5 Repository hygiene

- **P-25 (MUST)** The `.gitignore` in §4 MUST be committed in the initial commit, before any code.
- **P-26 (MUST)** CI MUST run a check that no file matching `Connections.csv`, `Contacts.csv`,
  `*LinkedInDataExport*`, or `*.csv` outside `tests/fixtures/` and `data/gazetteer/` is tracked.
- **P-27 (MUST)** All test fixtures MUST be **synthetic**. No real names, no real profile URLs, no
  real email addresses (use `@example.com`, reserved by RFC 2606). Enforced by
  `test_fixtures_contain_no_real_domains`.
- **P-28 (SHOULD)** Ship a `pre-commit` hook that runs `git diff --cached --name-only` against the
  same denylist. Cheap; catches the one mistake that actually matters.

### 3.6 Provenance and the adapter boundary

- **P-29 (MUST)** Every derived claim — every edge, every firm attribution, every cluster membership —
  MUST carry an `Evidence` list. Minimum shape:

  ```python
  @dataclass(frozen=True)
  class Evidence:
      source: str          # "Connections.csv" | "gazetteer:firms.yaml" | "adapter:local_file"
      locator: str         # "row 412, col 'Company'" | "entry: jane_street.aliases[3]"
      rule_id: str         # "exact_employer_match"
      rule_version: str    # "1.2.0"
      matched_text: str    # the literal text that fired the rule (redactable)
      weight: float        # this evidence's contribution to the score
      observed_at: str     # ISO-8601 UTC
  ```

- **P-30 (MUST)** No score may be produced by a code path that cannot enumerate the evidence that
  produced it. A claim with an empty `Evidence` list is a bug and MUST raise, not silently emit.
- **P-31 (MUST)** The report MUST expose the full evidence chain for every flagged person — inline or
  one click away. "Why is this person here" must be answerable without reading the source.
- **P-32 (MUST)** `DataSourceAdapter` MUST be a documented ABC with a stable contract; all adapters
  MUST stamp their `Evidence.source` with their own name so adapter-derived claims are separable
  from export-derived claims. In-tree adapters MUST NOT open sockets.
- **P-33 (MUST NOT)** No in-repo adapter, example, docstring, test, or documentation page may contain
  code that fetches from a LinkedIn host, handles LinkedIn session cookies, or drives a browser
  against LinkedIn — including as a commented-out example.

### 3.7 Accuracy, framing, and prohibited features

- **P-34 (MUST)** Every edge and every firm attribution MUST carry a numeric `confidence` in
  `[0.0, 1.0]` and a band label. No output surface — HTML, JSON, CSV, terminal — may show a link
  without both.
- **P-35 (MUST)** Bands are fixed, and none of them is "confirmed":

  | Score | Band | Report treatment |
  |---|---|---|
  | ≥ 0.80 | **Strong signal** | Shown, sorted first |
  | 0.55–0.79 | **Moderate signal** | Shown |
  | 0.30–0.54 | **Weak signal** | Shown, visually de-emphasised |
  | < 0.30 | **Speculative** | Hidden behind `--include-speculative` |

- **P-36 (MUST)** Every inferred edge MUST be labelled **"Inferred — not observed"** in the UI. Only
  facts read literally from the export (this row said this company; you connected on this date) may
  be presented unqualified, and those MUST be visually distinguished from inferences.
- **P-37 (MUST NOT)** No output may assert an unqualified relational fact. Not *"Alex works at Jane
  Street"* but *"Employer field reads 'Jane Street' (from your export, 2023-04-11) — may be out of
  date."* Not *"Alex knows Sam"* but *"Both appear adjacent to the same firm cluster — no direct
  relationship is observed or implied."*
- **P-38 (MUST)** The report MUST embed a short limitations block (§5) — visible in the document, not
  only in the README, because the HTML file is the artifact that gets forwarded.
- **P-39 (MUST NOT)** The tool MUST NOT infer, store, or display special-category data (Art. 9):
  racial or ethnic origin, political opinions, religious or philosophical beliefs, trade union
  membership, genetic or biometric data, health, sex life, or sexual orientation. The gazetteer
  loader MUST reject entries whose category field is outside an allowlist of
  `{employer, alumni, industry, location, role_family}`. Inferring protected characteristics from a
  professional graph is both the most likely way this tool causes real harm and the point at which
  the legal exposure changes character entirely.
- **P-40 (MUST NOT)** No feature may exist for: bulk message generation, mail-merge, CSV export shaped
  for an outreach tool, contact-info enrichment/append, "candidate quality" or "hireability" scoring,
  or ranking people against a job requisition. See §5.
- **P-41 (MUST)** First run MUST display the notice in §4.2 and require acknowledgement (a
  `--accept-notice` flag or a keypress); acknowledgement is recorded in the state directory so it
  shows once.

**Why P-34 through P-38 are hard requirements.** In a professional context a false positive is not a
neutral error. The concrete harms: a person is approached about a firm they have no connection to,
which is at best embarrassing and at worst professionally damaging if their current employer is a
competitor; a person is *skipped* because the tool wrongly judged them non-adjacent; a stale employer
field — LinkedIn profiles routinely lag reality by months — produces confident-looking claims about
where someone works today; and an inferred link, once written down and forwarded, is repeated as fact
by people who never saw the confidence score. The scholarship on inferred personal data is blunt
about this: inferences may be entirely inaccurate and still determine how a person is viewed and
evaluated, and data-subject rights over inferences are weaker than over supplied data — the person
harmed has less recourse, not more. Surfacing confidence is the mechanism that keeps the user's own
epistemics honest, and it is also the Art. 5(1)(d) accuracy answer: the data is not represented as
more certain than it is.

### 3.8 Confidence in the report UI — concrete spec

1. **Per-person card header:** name (or `PC-…` when redacted), then a band pill — colour-coded, with
   the numeric score in text: `Moderate signal · 0.61`. Colour MUST NOT be the only carrier of the
   band; the text label is mandatory (accessibility, and it survives copy-paste).
2. **An "Inferred" badge** on every derived claim, adjacent to the claim, not in a page footer.
3. **Evidence disclosure** — a collapsible `Why?` listing each `Evidence` row as
   `rule_id · source · locator · matched_text · +weight`.
4. **Staleness marker** — where a claim rests on `Connected On` or an employer field, show the age:
   `employer data is 2y 4m old`.
5. **Cluster headers** MUST show member count and mean confidence, and MUST use hedged language:
   *"Possible Jane Street cluster (7 people, mean confidence 0.58)."*
6. **Sort by confidence descending by default.** Never hide the score to make the output look tidier.
7. **A persistent header line** on every generated report:
   `Inferred from your LinkedIn export on <date>. Not verified. Not observed. Check before acting.`

---

## 4. Ready-to-paste text

### 4.1 `.gitignore` (exact content — verified against real `git check-ignore`)

Two `git`-specific traps this content works around: (i) you **cannot** re-include a file whose parent
directory is excluded by a `dir/` pattern — git never descends into it — which is why the first rule
is `/data/*` and not `/data/`; and (ii) the blanket `*.csv` net needs explicit negations, which must
come *after* it.

```gitignore
# ============================================================
# interlayer — personal data must never enter version control.
# Rules are ordered: broad denials first, narrow allowances last.
# ============================================================

# --- LinkedIn exports & any raw personal data ---
# NOTE: `/data/*` (not `/data/`) — git cannot re-include a file inside an
# excluded directory, so the directory itself must stay traversable.
/data/*
!/data/gazetteer/
*LinkedInDataExport*
Connections.csv
Contacts.csv
Invitations.csv
messages.csv

# --- Generated artifacts, reports, state ---
/artifacts/
/exports/
/out/
/.interlayer/
*.sqlite
*.sqlite3
*.db

# --- Belt-and-braces: no spreadsheets anywhere except fixtures & gazetteer ---
*.csv
*.tsv
*.xlsx
!/tests/fixtures/**/*.csv
!/data/gazetteer/**/*.csv
!/data/gazetteer/**/*.tsv

# --- Secrets & keys ---
.env
.env.*
*.key
*.pem

# --- Python ---
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
dist/
build/

# --- Editors / OS ---
.DS_Store
.idea/
.vscode/
```

Verified: `data/Connections.csv`, `data/raw/Skills.csv`, `Connections.csv`, `Contacts.csv`,
`artifacts/graph.json`, `exports/report.html`, `.interlayer/salt.key`,
`Basic_LinkedInDataExport_2026-08-21.zip`, `my_export.xlsx`, `.env`, `.env.local` → all ignored;
`data/gazetteer/firms.yaml`, `data/gazetteer/firms.csv`, `data/gazetteer/aliases/alt.csv`,
`data/gazetteer/README.md`, `tests/fixtures/synthetic.csv`, `tests/fixtures/deep/more.csv` → all
tracked.

### 4.2 Data-source warning (README §Data sources · CLI first run · `docs/adapters.md`)

Paste verbatim in all three places. Identical wording everywhere, so it is recognisably one rule and
not three hedges.

> **⚠️ Data sources — read before plugging anything in.**
> interlayer is built for **your own LinkedIn data export** (Settings → Data Privacy → Get a copy of
> your data). It ships no scraper and never will. Scraping LinkedIn — with scripts, bots, browser
> extensions, or commercial scraping tools — violates the LinkedIn User Agreement (§8.2), and in
> *hiQ Labs v. LinkedIn* that breach-of-contract theory produced a $500,000 judgment and a permanent
> injunction against the scraper. Your own account can be restricted or permanently closed. The
> adapter interface exists so you can supply data **you already have the right to use** — your ATS,
> your CRM, an official API, a licensed dataset. If you cannot point to where that right comes from,
> you do not have it. Anything you plug in is your responsibility, not the tool's.

### 4.3 CLI first-run notice

Shown once, gated by P-41.

```
interlayer — local network analysis of your own LinkedIn export

Before you start, three things:

  1. This file describes real people who did not consent to this analysis.
     Everything stays on this machine. interlayer makes no network calls.
  2. Everything this tool tells you is INFERRED, not observed. Employer data
     in a LinkedIn export is often months or years stale. Every result carries
     a confidence score. Read it before you act on it.
  3. This tool is for mapping your own network and finding warm introductions.
     It is not built for bulk outreach, candidate screening, or building a
     dataset about other people — and it will not be.

Artifacts are written to ./artifacts/ (mode 0700). Remove them with:
    interlayer purge

Full terms: README.md § ACCEPTABLE USE · § LIMITATIONS

Press Enter to continue, or Ctrl-C to abort.
```

---

## 5. Ready-to-paste README sections

### ACCEPTABLE USE

```markdown
## ACCEPTABLE USE

interlayer processes a file describing real people who never agreed to be analysed.
That fact sets the boundaries of what this tool is for.

**Built for:**

- **Warm-intro pathfinding.** You want to work at a specific firm. Which of your existing
  connections might plausibly get you a conversation?
- **Mapping your own network.** Understanding the shape of the professional network you
  have actually accumulated — where it is dense, where it is thin.
- **A personal CRM.** Keeping track of who you know, where they were last you knew, and
  who you have been meaning to reconnect with.
- **Deciding who to ask.** Prioritising a small number of genuine, individual conversations.

**Not built for, and deliberately not optimised for:**

- **Bulk unsolicited outreach.** There is no message generation, no mail-merge, no
  outreach-tool export format. If you want to spam 400 people, use something else.
- **Building a dataset about third parties.** Do not sell, license, publish, or pool the
  output. It describes people who did not consent to being in your database.
- **Evaluating people without their knowledge in a hiring decision.** No candidate scoring,
  no requisition matching, no "hireability" signal — now or later. Using inferred,
  unverified, frequently stale relational data to decide someone's employment is exactly the
  application this tool refuses to serve.
- **Any use where you would be uncomfortable showing a person their own entry.** That is a
  good working test, and it is the one we design against.

**If you are using this for work** — recruiting, sales, business development, or anything on
behalf of an employer — the GDPR "purely personal or household activity" exemption does not
apply to you. You are a data controller with real obligations: a documented lawful basis, a
privacy notice owed to people who have never heard of you, and machinery to answer access,
correction, and deletion requests. Depending on your employer's size and where it operates,
CCPA/CPRA may also apply. This tool does not discharge any of that for you. Talk to your DPO
or a lawyer before you run it, not after.

**Someone asked to be removed?** `interlayer exclude add <name-or-url>` — they are dropped at
ingest on every subsequent run. Honour it.
```

### LIMITATIONS

```markdown
## LIMITATIONS

**Everything here is inferred. Nothing here is verified.**

1. **The data is stale by construction.** A LinkedIn export reflects what people last
   bothered to update — commonly months or years out of date. Someone shown at a firm may
   have left years ago. Someone shown elsewhere may have joined last week.
2. **"Adjacent" is a guess, not a relationship.** Adjacency is inferred from employer
   strings, alumni overlap, timing, and clustering. It does not mean two people know each
   other, have spoken, or would recognise the connection. Confidence scores are relative
   rankings within your own data — not calibrated probabilities, and not evidence.
3. **Name and employer matching is fuzzy and fallible.** Firms rebrand, subsidiaries share
   names, people share names, job titles lie. False positives are expected and normal.
   Verify anything before acting on it.
4. **Absence means nothing.** Someone missing from a cluster is not evidence they are
   unconnected — only that this export contains no signal. Never treat a negative as a
   finding.
5. **Your export is not your network.** It contains 1st-degree connections only, with no
   interaction strength, no messages, and no indication whether you have ever actually
   spoken. A 2014 conference connection and a former manager look identical here.
6. **The gazetteer is hand-curated and incomplete.** It reflects a human's judgement about
   which firms and aliases matter, made at a point in time. Read `data/gazetteer/README.md`
   and change it if it is wrong.
7. **This tool cannot tell you whether someone will help you.** It can only narrow a list of
   several thousand down to a handful worth a real, individual, human conversation.

**Rule of thumb:** treat every output as a *hypothesis to check*, never a *fact to forward*.
If you are about to repeat something this tool told you as though it were true, stop.
```

---

## 6. Privacy tests for Wave 3

Each name is the pytest function name; the description is the assertion.

**Network isolation**

| Test | Assertion |
|---|---|
| `test_no_network_egress` | Running the full pipeline end-to-end under the `no_network` fixture completes successfully and raises no `NetworkEgressError`. |
| `test_core_imports_no_network_libs` | AST-walking every module under `src/interlayer/` (excluding `net.py`) finds no import of `socket`, `requests`, `httpx`, `aiohttp`, `urllib`, `urllib3`, `http.client`, `selenium`, or `playwright`. |
| `test_report_has_no_external_references` | The generated HTML contains no `http://` or `https://` in `src`, `href`, `action`, or `url()` attributes, and no `<iframe>` / `<script src>`. |
| `test_report_has_csp_meta` | The generated HTML contains the `default-src 'none'` CSP meta tag from P-6. |
| `test_no_telemetry_endpoints` | Grepping the source tree finds no hardcoded hostnames, IPs, or `.com`/`.io`/`.net` URLs outside docs, comments, and test fixtures. |
| `test_adapters_declare_no_network` | Every concrete `DataSourceAdapter` subclass in-tree runs to completion under `no_network`. |

**Email handling**

| Test | Assertion |
|---|---|
| `test_emails_dropped_by_default` | After ingesting a fixture where every row has an email, no artifact, database, log, or report file contains any `@` from the input. |
| `test_emails_hashed_when_requested` | With `--emails=hash`, output contains no plaintext address, and the hash of a known address differs across two installs with different keys. |
| `test_email_hash_is_keyed_not_bare_sha256` | The stored digest for `alice@example.com` is **not** equal to `hashlib.sha256(b"alice@example.com").hexdigest()`. |
| `test_email_normalisation_does_not_strip_plus_tags` | `a+x@example.com` and `a@example.com` hash to different values. |
| `test_emails_kept_only_with_explicit_flag` | `--emails=keep` records `emails=keep` in the manifest and emits a warning to stderr naming the artifact path. |

**Redaction**

| Test | Assertion |
|---|---|
| `test_redact_mode_removes_all_names` | With `--redact`, no first name, last name, or full name from the input appears anywhere in any output byte stream. |
| `test_redact_mode_removes_urls_and_slugs` | No `linkedin.com/in/<slug>` and no bare profile slug appears in redacted output. |
| `test_redact_ids_are_stable_across_runs` | Two runs on the same input with the same key produce identical `PC-…` IDs. |
| `test_redact_ids_differ_across_installs` | Two runs with different pseudonym keys produce disjoint ID sets for the same input. |
| `test_redact_scrubs_names_in_freetext_fields` | A fixture job title `"EA to Jane Doe"` emits as `"EA to [REDACTED]"` when `Jane Doe` is in the input. |
| `test_redact_fails_closed_on_new_fields` | Adding an un-allowlisted field to the person model causes it to be omitted from redacted output, not leaked. |
| `test_redact_quantises_connection_dates` | No day-precision date appears in redacted output. |
| `test_pseudonym_key_never_in_output` | The 32-byte key does not appear (raw, hex, or base64) in any artifact. |

**Storage & permissions**

| Test | Assertion |
|---|---|
| `test_artifact_dir_permissions_0700` | `stat.S_IMODE(os.stat(artifact_dir).st_mode) == 0o700` after a run. |
| `test_artifact_files_permissions_0600` | Every regular file under the artifact and state dirs is `0o600`. |
| `test_permissions_enforced_under_permissive_umask` | With `os.umask(0)` set before the run, the above two still hold — proving `chmod` is explicit, not umask-inherited. |
| `test_permissions_fixed_on_preexisting_dir` | A pre-existing `0755` artifact dir is corrected to `0700` on the next run. |
| `test_state_key_file_permissions_0600` | `pseudonym.key` is `0o600` and its parent is `0o700`. |
| `test_atomic_write_never_leaves_world_readable_file` | During a write, no intermediate file with mode broader than `0600` exists in the artifact dir. |

**Retention & subject rights**

| Test | Assertion |
|---|---|
| `test_purge_removes_all_artifacts` | After `purge --yes`, the artifact and state dirs contain no files; the gazetteer is untouched. |
| `test_purge_dry_run_removes_nothing` | `purge --dry-run` lists paths and leaves every one on disk. |
| `test_purge_keeps_gazetteer` | `purge --keep-gazetteer` leaves `data/gazetteer/` byte-identical. |
| `test_excluded_person_never_appears_in_output` | A person on the exclusion list is absent from every artifact, including intermediate caches. |
| `test_exclusion_applied_at_ingest` | The excluded person's data is not present in the in-memory model after ingest, not merely filtered at render. |

**Provenance, confidence & framing**

| Test | Assertion |
|---|---|
| `test_every_edge_has_evidence` | No edge or firm attribution in the output graph has an empty `Evidence` list. |
| `test_claim_without_evidence_raises` | Constructing a scored claim with no evidence raises, rather than emitting. |
| `test_every_edge_has_confidence_and_band` | Every edge carries a float in `[0.0, 1.0]` and a band label from the fixed set. |
| `test_confidence_band_boundaries` | Scores of 0.79/0.80 and 0.29/0.30 map to the bands specified in P-35. |
| `test_speculative_hidden_by_default` | Edges scoring < 0.30 are absent from the report unless `--include-speculative`. |
| `test_inferred_label_on_every_derived_claim` | Every derived claim in the HTML is accompanied by the string `Inferred`. |
| `test_report_contains_limitations_block` | The generated report embeds the §5 limitations text. |
| `test_no_unqualified_relational_assertions` | The report contains no template string matching `\b(works at|knows|is connected to)\b` without an adjacent hedge or band label. |
| `test_manifest_records_run_parameters` | `manifest.json` contains tool version, input SHA-256, row count, email mode, redact flag, and gazetteer hash. |

**Repo hygiene & special categories**

| Test | Assertion |
|---|---|
| `test_no_personal_data_files_tracked` | `git ls-files` matches nothing in the P-26 denylist. |
| `test_gitignore_covers_export_filenames` | `git check-ignore` returns true for each of the seven canonical export filenames. |
| `test_fixtures_contain_no_real_domains` | Every email in `tests/fixtures/` ends in `@example.com`, `@example.org`, or `@example.net`. |
| `test_gazetteer_rejects_special_category_terms` | A gazetteer entry with `category: religion` fails to load with a clear error. |
| `test_logs_contain_no_personal_data` | Capturing logs at INFO during a full run yields no name, email, or profile URL from the input. |
| `test_first_run_notice_shown_once` | The notice prints on first invocation and not on the second. |

---

## Appendix A — verified permission idioms (Python 3.11, Linux)

Executed in this environment. Several widely repeated idioms are **wrong**:

| Operation | umask 022 | umask 000 | Verdict |
|---|---|---|---|
| `Path.mkdir(mode=0o700)` | `0o700` | `0o700` | Safe — umask only *clears* bits |
| `Path.mkdir(mode=0o777)` | `0o755` | `0o777` | Never request permissive modes |
| pre-existing `0o755` + `mkdir(0o700, exist_ok=True)` | **`0o755`** | **`0o755`** | ❌ **silently does not fix the mode** |
| `Path.write_text(...)` (plain `open`) | `0o644` | **`0o666`** | ❌ **world-writable under a permissive umask** |
| `Path.touch(mode=0o600)` on a new file | `0o600` | `0o600` | OK for new files only |
| `touch(mode=0o600)` on a pre-existing `0o644` file | **`0o644`** | **`0o644`** | ❌ **does not fix the mode** |
| `os.open(p, O_WRONLY\|O_CREAT\|O_TRUNC, 0o600)` | `0o600` | `0o600` | ✅ correct |
| `tempfile.mkstemp()` | `0o600` | `0o600` | ✅ correct by default |
| `os.replace(mkstemp_path, final)` | `0o600` | `0o600` | ✅ mode is preserved through the rename |

**Required idioms.**

```python
import os, stat, tempfile
from pathlib import Path

def secure_dir(path: Path) -> Path:
    """Create/confirm a 0700 directory. chmod is unconditional: mkdir(exist_ok=True)
    does NOT correct the mode of a directory that already exists."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700, f"{path} is not 0700"
    return path

def secure_write(path: Path, data: str) -> None:
    """Atomic write, 0600 at every instant. mkstemp is 0600 from creation and
    os.replace preserves it, so no window exists where the file is readable."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)          # atomic; preserves 0600
    except BaseException:
        os.unlink(tmp)
        raise
```

Never call `os.umask()` to achieve this: it is process-global, not thread-safe, and hostile to any
library sharing the interpreter. Set the mode explicitly and **assert it**.

---

## Sources

- [Recital 18 GDPR — Not applicable to personal or household activities](https://gdpr-info.eu/recitals/no-18/)
- [Article 2 GDPR — Material scope (GDPRhub, incl. Lindqvist/Ryneš analysis)](https://gdprhub.eu/Article_2_GDPR)
- [CJEU C-25/17 — Jehovan todistajat (GDPRhub)](https://gdprhub.eu/index.php?section=1&title=CJEU_-_C-25%2F17_-_Jehovan_todistajat)
- [Art. 14 GDPR — Information where data not obtained from the data subject](https://gdpr-info.eu/art-14-gdpr/)
- [ICO — Right to be informed / disproportionate effort](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-be-informed/)
- [ICO — A guide to the data protection exemptions](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/exemptions/a-guide-to-the-data-protection-exemptions/)
- [EDPB Guidelines 01/2025 on Pseudonymisation (PDF)](https://www.edpb.europa.eu/system/files/2025-01/edpb_guidelines_202501_pseudonymisation_en.pdf)
- [GDPR Art. 5(1)(d) accuracy principle](https://gdpr-text.com/read/article-5/)
- [Wachter & Mittelstadt — A Right to Reasonable Inferences](https://www.ssrn.com/abstract=3248829)
- [The Right to Rectification and Inferred Personal Data (EJLT)](https://ejlt.org/index.php/ejlt/article/view/1004)
- [CCPA applicability thresholds 2026 (Clym)](https://www.clym.io/blog/ccpa-applicability-guide)
- [Jackson Lewis — CCPA FAQs incl. regulations effective 1 Jan 2026](https://www.jacksonlewis.com/insights/navigating-california-consumer-privacy-act-30-essential-faqs-covered-businesses-including-clarifying-regulations-effective-1126)
- [EU AI Act Article 2 — Scope](https://artificialintelligenceact.eu/article/2/)
- [hiQ Labs v. LinkedIn — final judgment and lessons (ZwillGen)](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/)
- [Morgan Lewis — LinkedIn v. hiQ guidance for data scrapers](https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators)
- [pytest-socket README](https://github.com/miketheman/pytest-socket/blob/main/README.md)
- [gitignore(5) — negation and excluded parent directories](https://git-scm.com/docs/gitignore)
