# interlayer

Find and cluster the people in your own LinkedIn network who bridge into a target firm.

You want an introduction to someone at Jane Street, Citadel or Citadel Securities. Somewhere in
your connections there are a handful of people who already know several of them. `interlayer`
finds those people, ranks them by how much brokerage they actually hold, groups them by which
part of the target firm they reach, and tells you which target to look at next.

It is an offline analysis tool. It reads files you produce and writes reports. It never opens a
connection to LinkedIn — not as a matter of policy, but because no such code path exists.

---

## What it cannot do

**You cannot buy the connection graph, and you cannot scrape it.** No data provider sells
LinkedIn connection edges or mutual-connection lists, because neither is ever rendered on a
logged-out page and mutuals are computed server-side per viewer — there is no artefact to
collect. The first-party routes are just as empty: the official data export, the DMA
portability API and a GDPR Article 15 request all return first-person data only, so
`Connections.csv` gives you the set of people you know and exactly zero edges between them and
anyone else. The only surface that yields those edges is your own authenticated session, viewed
by you, one target at a time — and LinkedIn's monthly Commercial Use Limit caps *every* method
at the same rate, roughly 150–250 targets a month on a free account. Automation does not raise
that ceiling; it converts a three-hour job into a twenty-minute one inside an identical quota,
while risking the account whose network is the entire asset. That arithmetic, not caution, is
why this tool ships no scraper, no browser extension and no stored session cookie. The
acquisition step is a procedure you perform; `docs/02-operator-runbook.md` is that procedure,
including an honest account of its risks.

---

## Install

Python 3.11 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

This installs the `interlayer` command.

---

## Quickstart

```bash
# 1. Create ~/.interlayer (database, audit log, snapshots) with private defaults.
interlayer init

# 2. Load your connection list. Ask LinkedIn for it at
#    Settings > Data privacy > Get a copy of your data > Connections.
#    Arrives in minutes for the small archive.
interlayer ingest ~/Downloads/Connections.csv

# 3. See which firms are being targeted.
interlayer targets

# 4. Capture edges. This is the manual step -- read the runbook first.
#    docs/02-operator-runbook.md
#    Then load what you captured. The manual-capture collectors are off in the
#    shipped config, so enabling them is a deliberate act:
interlayer collect ~/captures/ --allow-manual-capture

# 5. Adjudicate anything the matcher would not resolve on its own.
#    "Jane Street Capital LLC" and "Jane Street Entertainment" are not
#    separable by any fuzzy threshold, so the tool asks instead of guessing.
interlayer review

# 6. Build the graph. Offline, deterministic, same inputs -> same fingerprint.
interlayer analyse

# 7. Read the result.
interlayer report --format html --out bridges.html
interlayer report                       # markdown to stdout
interlayer report --format json         # machine-readable

# 8. Decide what to harvest next.
interlayer next
```

---

## Commands

| Command | What it does |
|---|---|
| `init` | Create the state directory and database with private defaults. |
| `ingest` | Load `Connections.csv` — the set `M` — from the official export. |
| `targets` | Show the target registry. |
| `collect` | Load captured mutual connections — the edge set `E` — from HAR files or hand-filled CSVs. Needs `--allow-manual-capture`. |
| `review` | List ambiguous company matches held out of the graph. |
| `analyse` | Build, project, cluster and rank. Writes a snapshot. |
| `report` | Render the snapshot as Markdown, HTML or JSON. |
| `next` | Rank the targets worth harvesting next, with reasons. |
| `privacy` | Print `PRIVACY.md`. |
| `purge` | `--all`, `--person <id>` or `--target <id>`, with tombstones. |

Run `interlayer <command> --help` for options.

---

## What the reports guarantee

Every rendered report, in every format:

- **Strips email addresses and phone numbers unconditionally**, whatever `retain_emails` is set
  to. Keeping an address in your local database and putting it in a document that leaves your
  machine are different decisions, and only the first is configurable.
- **Carries a handling notice** stating the purpose, the retention period in force, that the
  document contains third-party personal data, and that it must not be redistributed.
- **Marks every inferred value.** An inferred tie was never observed — it is a co-affiliation
  guess — and it never renders the way an observed one does.
- **States how many `needs_review` items are unadjudicated.** Those records are held out of the
  graph, so their absence is not a finding.
- **Reports coverage before it reports anything else**, and writes reach as `≥ n` while the
  harvest is incomplete. An unharvested target cannot produce an edge, so a thin harvest and a
  thin network are indistinguishable from the numbers alone. The report refuses to let them
  look alike.

The HTML report is a single self-contained file: no stylesheet, font, script or image is
fetched when you open it.

---

## Privacy

The people in your target list are third-party data subjects who were never asked. `PRIVACY.md`
sets out the controller position, the lawful basis, the retention period, the Article 14
position and how to exercise access and erasure. `interlayer privacy` prints it.

Everything stays on your machine. `purge --person` and `purge --target` write tombstones, so an
erased person is not quietly restored by the next re-ingest of the same export.

---

## Documentation

- `docs/02-operator-runbook.md` — the acquisition procedure, the quota arithmetic, and an
  honest risk gradient rather than a compliance claim.
- `PRIVACY.md` — the data-protection position.
- `docs/01-setup-guide.md` — the build contract.
- `docs/research/` — the five research documents the design is derived from.

`interlayer` is offline and deterministic by design: nothing under `core/`, `ingest/`, `graph/`
or `report/` may import an HTTP client, and identical inputs produce an identical fingerprint
across processes and hash seeds.

## Licence

MIT.
