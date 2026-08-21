"""Fixed report copy.

Every string here is authored in this repository and contains no data derived
from the operator's export. That property is load-bearing: the redaction audit
in :mod:`interlayer.report.redact` scans *dynamic* values for leaked names and
deliberately does not scan this boilerplate, because an operator's connection
may legitimately be named "Will" or "Grace" and blocking the report on a
collision with fixed prose would make the tool unusable.

Wording is lifted from ``docs/research/05-privacy-compliance.md`` §3.7, §4.2 and
§5 rather than paraphrased, so the report, the README and the CLI notice say the
same thing in the same words.
"""

from __future__ import annotations

__all__ = [
    "BAND_LEGEND",
    "EVIDENCE_KIND_LABELS",
    "FIRM_NOTE",
    "HEADER_LINE",
    "LIMITATIONS",
    "NOT_BUILT_FOR",
    "PROVENANCE_LEGEND",
    "REPORT_SUBTITLE",
    "REPORT_TITLE",
    "SOURCES_NOTE",
]

REPORT_TITLE = "interlayer — network report"

REPORT_SUBTITLE = (
    "A ranked set of hypotheses about your own LinkedIn connections. Not a set of findings."
)

#: §3.8 item 7 requires a persistent header on every generated report. The
#: research draft ends it with "on <date>"; the date is omitted here because a
#: clock in the document body would break byte-identical reruns. The run
#: timestamp lives in ``manifest.json`` next to this file, which is also where
#: an auditor would look for it.
HEADER_LINE = "Inferred from your LinkedIn export. Not verified. Not observed. Check before acting."

FIRM_NOTE = (
    "Citadel LLC (the hedge fund) and Citadel Securities (the market maker) are separate "
    "companies with separate LinkedIn pages and separate staff. Their columns are reported "
    "separately and are never added together."
)

PROVENANCE_LEGEND: tuple[tuple[str, str, str], ...] = (
    (
        "observed",
        "Observed — human-read",
        "A person sat and read this off LinkedIn's own mutual-connections list. "
        "This is ground truth: the bridge exists.",
    ),
    (
        "inferred",
        "Inferred — not observed",
        "This tool derived the link from employer strings, alumni overlap, timing and "
        "clustering. No relationship has been observed and none is implied. It may be wrong.",
    ),
)

#: §3.7 P-35. The bands are fixed and none of them is "confirmed".
BAND_LEGEND: tuple[tuple[str, str, str], ...] = (
    ("strong", "Strong signal", "0.80 and above — reserved for observed readings only."),
    ("moderate", "Moderate signal", "0.55 to 0.79 — several independent inferred signals agree."),
    ("weak", "Weak signal", "0.30 to 0.54 — one thin inferred signal. De-emphasised."),
    ("speculative", "Speculative", "below 0.30 — collapsed by default. Probably noise."),
)

EVIDENCE_KIND_LABELS: dict[str, str] = {
    "observed_mutual": "Mutual connection read from LinkedIn",
    "direct_employment": "Employer field names a target firm",
    "past_employment": "Former-employer field names a target firm",
    "shared_employer": "Shared employer with someone adjacent to a target",
    "shared_school": "Shared school with someone adjacent to a target",
    "cluster_membership": "Sits in a cluster that leans toward a target",
    "title_signal": "Job-title wording resembles the target's roles",
    "path_proximity": "Short path through the inferred graph to a target",
}

#: §5 LIMITATIONS, verbatim, as (heading, body) pairs so the template can render
#: them as a numbered list without the markdown round-trip.
LIMITATIONS: tuple[tuple[str, str], ...] = (
    (
        "The data is stale by construction.",
        "A LinkedIn export reflects what people last bothered to update — commonly months or "
        "years out of date. Someone shown at a firm may have left years ago. Someone shown "
        "elsewhere may have joined last week.",
    ),
    (
        "“Adjacent” is a guess, not a relationship.",
        "Adjacency is inferred from employer strings, alumni overlap, timing, and clustering. "
        "It does not mean two people know each other, have spoken, or would recognise the "
        "connection. Confidence scores are relative rankings within your own data — not "
        "calibrated probabilities, and not evidence.",
    ),
    (
        "Name and employer matching is fuzzy and fallible.",
        "Firms rebrand, subsidiaries share names, people share names, job titles lie. False "
        "positives are expected and normal. Verify anything before acting on it.",
    ),
    (
        "Absence means nothing.",
        "Someone missing from a cluster is not evidence they are unconnected — only that this "
        "export contains no signal. Never treat a negative as a finding.",
    ),
    (
        "Your export is not your network.",
        "It contains 1st-degree connections only, with no interaction strength, no messages, "
        "and no indication whether you have ever actually spoken. A 2014 conference connection "
        "and a former manager look identical here.",
    ),
    (
        "The gazetteer is hand-curated and incomplete.",
        "It reflects a human's judgement about which firms and aliases matter, made at a point "
        "in time. Read data/gazetteer/README.md and change it if it is wrong.",
    ),
    (
        "This tool cannot tell you whether someone will help you.",
        "It can only narrow a list of several thousand down to a handful worth a real, "
        "individual, human conversation.",
    ),
)

RULE_OF_THUMB = (
    "Rule of thumb: treat every output as a hypothesis to check, never a fact to forward. "
    "If you are about to repeat something this tool told you as though it were true, stop."
)

NOT_BUILT_FOR = (
    "This report is not built for bulk outreach, for building a dataset about third parties, "
    "or for evaluating anyone in a hiring decision. If you would be uncomfortable showing a "
    "person their own entry here, do not act on it."
)

SOURCES_NOTE = (
    "Built from your own LinkedIn data export plus any mutual-connection observations you "
    "recorded by hand. interlayer ships no scraper and makes no network calls. This file "
    "loads nothing from the internet when you open it — no fonts, no scripts, no images, and "
    "specifically no LinkedIn profile photographs, which would hand the entire list below back "
    "to LinkedIn the moment you opened your own report."
)

REDACTION_NOTE = (
    "Redacted mode. Names, profile URLs, e-mail addresses and free text written by earlier "
    "pipeline stages have been dropped; people appear as stable pseudonyms of the form PC-… "
    "derived from a local key that is never written into any artifact. Dates are quantised to "
    "the year and clusters smaller than three people are suppressed. Everything visible below "
    "was assembled from an allowlist: a field that is not explicitly permitted is dropped."
)
