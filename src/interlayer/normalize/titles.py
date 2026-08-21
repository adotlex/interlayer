"""Title parsing: seniority and role family.

This is not decoration. A target-firm **recruiter** and a target-firm **trader**
are opposite kinds of connection. The recruiter is a high-reach, low-signal node
whose job is to know everybody, so sharing an employer with one says almost
nothing about how close anyone is to the firm; the trader is a low-reach,
high-signal node and a genuine introduction path. Collapsing the two produces a
ranked list topped by people whose presence in the network means nothing.

Two orthogonal axes, parsed by ordered regexes with the most specific first,
first match winning. They are kept separate because they answer different
questions: seniority says how much the person can decide, function says what
they actually do.

Ambiguities the ordering encodes deliberately:

* ``Summer Analyst`` is an internship, not an analyst rung, so the intern rules
  run before the analyst rules.
* ``Senior Analyst`` is a rung *below* ``Senior <anything else>``.
* ``MD`` is Doctor of Medicine outside finance, so it only reads as Managing
  Director when the employer resolved to a finance entity.
* ``Principal Engineer`` is an individual-contributor track, not management,
  which is why ``PRINCIPAL``-style titles map to ``LEAD`` rather than executive.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from interlayer.models import Seniority

__all__ = [
    "RoleFamily",
    "extract_role",
    "extract_seniority",
    "fold_title",
    "role_weight",
]


class RoleFamily(StrEnum):
    """What the person does. Not in ``models.py``; local to this stage."""

    QUANT_TRADER = "quant_trader"
    TRADER = "trader"
    QUANT_RESEARCHER = "quant_researcher"
    PORTFOLIO_MANAGER = "portfolio_manager"
    QUANT_DEV = "quant_dev"
    SWE = "swe"
    INFRA = "infra"
    DATA = "data"
    OPS = "ops"
    RISK = "risk"
    RECRUITER = "recruiter"
    SALES = "sales"
    BIZOPS = "bizops"
    OTHER = "other"


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def fold_title(title: str) -> str:
    """Casefold, de-accent and de-punctuate so the patterns can stay simple.

    Punctuation becomes whitespace rather than disappearing, so ``Vice-President``
    and ``Vice President`` fold together instead of becoming ``vicepresident``.
    """
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _PUNCT_RE.sub(" ", folded.casefold())
    return _WS_RE.sub(" ", folded).strip()


# Ordered: first match wins. Do not reorder without re-reading the module
# docstring -- several entries only behave because of what precedes them.
_SENIORITY_RULES: tuple[tuple[re.Pattern[str], Seniority], ...] = tuple(
    (re.compile(pattern), level)
    for pattern, level in (
        (r"\b(co\s?founder|founder|founding (?:partner|member|engineer))\b", Seniority.FOUNDER),
        (r"\bchief\s+(?:\w+\s+){0,3}officer\b", Seniority.EXECUTIVE),
        (r"\b(?:ceo|cto|cfo|coo|cio|ciso|cro|cmo|chro|cdo|cpo)\b", Seniority.EXECUTIVE),
        (r"(?<!vice )\bpresident\b", Seniority.EXECUTIVE),
        (
            r"\b(?:managing director|executive director|managing partner|"
            r"general partner|global head|head of)\b",
            Seniority.EXECUTIVE,
        ),
        (
            r"(?<!business )(?<!channel )\bpartner\b"
            r"(?!\s+(?:success|manager|engineer|engineering|marketing|relations|development))",
            Seniority.EXECUTIVE,
        ),
        (r"\b(?:vice president|vp|svp|evp|avp|director)\b", Seniority.SENIOR),
        (
            r"\b(?:intern|interning|internship|summer (?:analyst|associate|intern)|"
            r"co\s?op|trainee|apprentice)\b",
            Seniority.INTERN,
        ),
        (
            r"\b(?:principal|distinguished|fellow|architect|senior staff|"
            r"staff (?:engineer|scientist|researcher))\b",
            Seniority.LEAD,
        ),
        (r"\b(?:tech lead|team lead|lead)\b", Seniority.LEAD),
        (r"\bsenior analyst\b", Seniority.MID),
        (r"\b(?:senior|sr)\b", Seniority.SENIOR),
        (r"\bassociate\b", Seniority.MID),
        (
            r"\b(?:analyst|junior|jr|new grad|graduate (?:analyst|program|scheme)|"
            r"campus hire|rotational)\b",
            Seniority.JUNIOR,
        ),
    )
)

# Only meaningful once the employer is known to be a finance entity: bare "MD"
# on a profile is otherwise overwhelmingly a physician.
_FINANCE_ONLY_SENIORITY: tuple[tuple[re.Pattern[str], Seniority], ...] = (
    (re.compile(r"\bmd\b"), Seniority.EXECUTIVE),
)

_ROLE_RULES: tuple[tuple[re.Pattern[str], RoleFamily], ...] = tuple(
    (re.compile(pattern), role)
    for pattern, role in (
        (r"\bquant(?:itative)?\s+trader\b", RoleFamily.QUANT_TRADER),
        (r"\bquant(?:itative)?\s+(?:developer|engineer)\b", RoleFamily.QUANT_DEV),
        (r"\bquant(?:itative)?\s+research(?:er)?\b|\bqr\b", RoleFamily.QUANT_RESEARCHER),
        (r"\bportfolio manager\b", RoleFamily.PORTFOLIO_MANAGER),
        (
            r"\brecruit(?:er|ing|ment)\b|\btalent\b|\bsourcer\b|\bhr\b|"
            r"\bpeople (?:ops|team|operations)\b|\bcampus\b|\bhuman resources\b",
            RoleFamily.RECRUITER,
        ),
        (
            r"\bsales\b|\bcoverage\b|\brelationship manager\b|"
            r"\bbusiness development\b|\binvestor relations\b",
            RoleFamily.SALES,
        ),
        (
            r"\brisk\b|\bcompliance\b|\blegal\b|\bcounsel\b|\baudit\b|\bregulatory\b",
            RoleFamily.RISK,
        ),
        (
            r"\btrading systems\b|\blow latency\b|\bfpga\b|\bexecution engineer\b",
            RoleFamily.QUANT_DEV,
        ),
        (r"\btrader\b|\btrading\b|\bmarket mak(?:er|ing)\b", RoleFamily.TRADER),
        (
            r"\bresearch(?:er)?\b|\bstrategist\b|\bscientist\b(?!.*\bdata\b)",
            RoleFamily.QUANT_RESEARCHER,
        ),
        (
            r"\bdata (?:scientist|engineer|analyst)\b|\bmachine learning\b|\bml\b|\bai\b",
            RoleFamily.DATA,
        ),
        (
            r"\binfrastructure\b|\bsre\b|\bplatform\b|\bdevops\b|\bnetwork\b|"
            r"\bsystems engineer\b|\bsecurity engineer\b",
            RoleFamily.INFRA,
        ),
        (
            r"\bsoftware engineer\b|\bdeveloper\b|\bswe\b|\bprogrammer\b|"
            r"\bfull stack\b|\bengineer\b",
            RoleFamily.SWE,
        ),
        (
            r"\boperations\b|\bmiddle office\b|\bback office\b|\bsettlement\b|"
            r"\btreasury\b|\bclearing\b",
            RoleFamily.OPS,
        ),
        (
            r"\bstrategy\b|\bbusiness ops\b|\bchief of staff\b|\bproduct manager\b",
            RoleFamily.BIZOPS,
        ),
    )
)

_FINANCE_ONLY_ROLES: tuple[tuple[re.Pattern[str], RoleFamily], ...] = (
    (re.compile(r"\bpm\b"), RoleFamily.PORTFOLIO_MANAGER),
)

# Multiplied into ``Affiliation.weight``. Nothing is weighted *up*: 1.0 is the
# ceiling so an untitled affiliation keeps the model's own default and only
# roles with a known reach/signal problem are damped.
_ROLE_WEIGHTS: dict[RoleFamily, float] = {
    RoleFamily.RECRUITER: 0.35,
    RoleFamily.SALES: 0.5,
}


def extract_seniority(title: str, *, finance_context: bool = False) -> Seniority:
    """Map a raw position string onto ``Seniority``.

    ``finance_context`` should be True only when the employer resolved to a
    finance entity in the gazetteer; it unlocks the finance-only readings of
    otherwise dangerous abbreviations.
    """
    folded = fold_title(title)
    if not folded:
        return Seniority.UNKNOWN
    rules = _SENIORITY_RULES
    if finance_context:
        rules = _FINANCE_ONLY_SENIORITY + rules
    for pattern, level in rules:
        if pattern.search(folded):
            return level
    return Seniority.UNKNOWN


def extract_role(title: str, *, finance_context: bool = False) -> RoleFamily:
    """Map a raw position string onto a ``RoleFamily``."""
    folded = fold_title(title)
    if not folded:
        return RoleFamily.OTHER
    rules = _ROLE_RULES
    if finance_context:
        rules = _FINANCE_ONLY_ROLES + rules
    for pattern, role in rules:
        if pattern.search(folded):
            return role
    return RoleFamily.OTHER


def role_weight(role: RoleFamily) -> float:
    """Damping factor for a role whose network reach outruns its signal.

    A recruiter shares an employer with everyone they hired, so a shared-employer
    edge through one carries far less evidence of a real working relationship
    than the same edge through a trader. Capped at 1.0 -- this only ever damps.
    """
    return _ROLE_WEIGHTS.get(role, 1.0)
