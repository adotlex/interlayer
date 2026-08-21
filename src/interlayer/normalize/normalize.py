"""String folding for entity resolution.

Every input string is folded **twice**, and the reason is the whole product.

``norm_raw`` keeps the definite article and the legal suffix; ``norm`` drops
both. Fold only once and ``The Citadel`` (a 40,000-alumni military college) and
``Citadel`` (a 3,150-person hedge fund) become the same string, at which point
*every* similarity scorer returns 100 and no threshold can separate them. The
distinction has to survive normalisation because it cannot be recovered
afterwards -- see ``docs/research/04-entity-resolution.md`` section 1.2, rule C1.

The suffix data is cleanco's (a curated legal-form list spanning 16 entity types
and 67 countries) but not cleanco's ``basename()``: measured over 28 tricky
inputs it agreed with the required output only 12 times, and it breaks outright
on unbalanced parentheses (``Citadel Securities (Europe) Limited`` ->
``Citadel Securities (Europe``), leaves ``& Co.`` in place, and never casefolds,
NFKC-folds or de-accents.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "LEGAL_SUFFIXES",
    "MULTI_TOKEN_SUFFIXES",
    "NOISE_TOKENS",
    "initials",
    "norm",
    "norm_raw",
    "primary_segment",
    "tokens",
]

# --------------------------------------------------------------------------
# character-class tables
# --------------------------------------------------------------------------

# LinkedIn employer strings are routinely "Company | Location", "Company - Team"
# or "Citadel Servicing Corp / Acra Lending". The firm is the FIRST segment.
_SEGMENT_RE = re.compile(r"\s*[|\u2022\u00b7/\u203a\u00bb]\s*|\s+[-\u2013\u2014]\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"[\u2010-\u2015\u2212]")  # hyphen..horizontal bar, minus sign

# Zero-width characters survive a naive strip() and silently defeat exact
# matching: "Jane Street<ZWSP> Capital" is not "Jane Street Capital".
_ZERO_WIDTH = dict.fromkeys(
    (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF),
    None,
)

NOISE_TOKENS: frozenset[str] = frozenset({"the"})
"""Dropped from ``norm`` only. Retained by ``norm_raw``, where it is the single
most discriminating token in the entire problem."""


def _fold_terms(terms: tuple[str, ...]) -> tuple[frozenset[str], frozenset[str]]:
    """Split raw legal-form terms into single-token and multi-token suffix sets.

    A term is stored both as written and with its dots removed, because
    punctuation stripping turns ``L.P.`` into two single-character tokens that
    the stripper rejoins before looking them up.
    """
    single: set[str] = set()
    multi: set[str] = set()
    for term in terms:
        folded = _WS_RE.sub(" ", term.replace(".", " ").casefold()).strip()
        if not folded:
            continue
        (multi if " " in folded else single).add(folded)
        joined = folded.replace(" ", "")
        if joined:
            single.add(joined)
    return frozenset(single), frozenset(multi)


# cleanco's legal-form terms (16 entity types, 67 countries), folded to bare
# lowercase. This is the *data* worth taking from cleanco; the function is not.
_CLEANCO_TERMS: tuple[str, ...] = (
    # anglosphere
    "inc",
    "inc.",
    "incorporated",
    "incorporation",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "company",
    "and company",
    "co ltd",
    "cos",
    "ltd",
    "ltd.",
    "limited",
    "unlimited",
    "llc",
    "l.l.c.",
    "l.c.",
    "lc",
    "llp",
    "l.l.p.",
    "lp",
    "l.p.",
    "lllp",
    "pllc",
    "p.l.l.c.",
    "plc",
    "p.l.c.",
    "pc",
    "p.c.",
    "psc",
    "pa",
    "p.a.",
    "partnership",
    "limited partnership",
    "general partnership",
    "sole proprietorship",
    "trust",
    "holdings",
    "holding",
    "group",
    "partners",
    # germanic
    "gmbh",
    "ggmbh",
    "mbh",
    "gmbh co kg",
    "kg",
    "kgaa",
    "ohg",
    "gbr",
    "ug",
    "ag",
    "se",
    "ev",
    "e.v.",
    "eg",
    "e.g.",
    "vvag",
    "aG",
    # nordic
    "ab",
    "abp",
    "oyj",
    "oy",
    "as",
    "asa",
    "ans",
    "da",
    "ba",
    "hf",
    "ehf",
    "svf",
    "sf",
    "aps",
    "ivs",
    "ks",
    "kb",
    "hb",
    "ekonomisk forening",
    # romance
    "sa",
    "s.a.",
    "sas",
    "s.a.s",
    "sarl",
    "s.a.r.l.",
    "sca",
    "sci",
    "snc",
    "eurl",
    "srl",
    "s.r.l.",
    "spa",
    "s.p.a.",
    "sapa",
    "sas de cv",
    "sa de cv",
    "s de rl",
    "de cv",
    "sl",
    "s.l.",
    "slu",
    "sll",
    "scp",
    "sac",
    "src",
    "srlcv",
    "ltda",
    "eirl",
    "cia",
    "y cia",
    "lda",
    "unipessoal lda",
    # benelux / iberian
    "nv",
    "n.v.",
    "bv",
    "b.v.",
    "bvba",
    "cvba",
    "vof",
    "cv",
    "comm v",
    "vzw",
    # slavic / baltic / other european
    "sp z oo",
    "sp. z o.o.",
    "spolka akcyjna",
    "zoo",
    "oao",
    "ooo",
    "zao",
    "pao",
    "kft",
    "zrt",
    "nyrt",
    "bt",
    "rt",
    "sro",
    "s.r.o.",
    "as.",
    "ad",
    "dd",
    "doo",
    "d.o.o.",
    "ood",
    "eood",
    "ab publ",
    "uab",
    "sia",
    "ou",
    "tov",
    "pat",
    # asia-pacific
    "pte",
    "pte.",
    "pte ltd",
    "pty",
    "pty ltd",
    "pvt",
    "private",
    "private limited",
    "sdn",
    "bhd",
    "sdn bhd",
    "berhad",
    "tbk",
    "pt",
    "kk",
    "k.k.",
    "yk",
    "gk",
    "godo kaisha",
    "kabushiki kaisha",
    "yugen kaisha",
    "jsc",
    "ojsc",
    "cjsc",
    "co jsc",
    "llc jsc",
    # middle east / africa / latam
    "wll",
    "w.l.l.",
    "fzc",
    "fze",
    "fzco",
    "lllc",
    "sae",
    "s.a.e.",
    "psc.",
    "spc",
    "cc",
    "npc",
    "soc",
    "eeig",
    "sce",
    "scs",
    "scop",
)

# Terms the gazetteer adds on top of cleanco's legal forms. They are not legal
# forms at all -- they are structural nouns that appear at the end of firm names
# ("Jane Street Group") -- but they are stripped from the QUERY and the ALIAS
# symmetrically, so removing them cannot cause a cross-firm merge on its own.
_GAZETTEER_EXTRA_TERMS: tuple[str, ...] = ("holdings", "holding", "group", "trust", "partners")

LEGAL_SUFFIXES, MULTI_TOKEN_SUFFIXES = _fold_terms(_CLEANCO_TERMS + _GAZETTEER_EXTRA_TERMS)


# --------------------------------------------------------------------------
# folding
# --------------------------------------------------------------------------


def initials(toks: list[str]) -> str:
    """First letter of each token, e.g. ``["jane", "street"] -> "js"``."""
    return "".join(t[0] for t in toks if t)


def primary_segment(text: str) -> str:
    """Take the part before the first separator.

    ``Citadel | Chicago`` is the firm plus a location; ``Citadel Servicing Corp /
    Acra Lending`` is one company under two names. Either way the identifying
    part is what comes first.
    """
    parts = [p for p in _SEGMENT_RE.split(text) if p and p.strip()]
    return parts[0] if parts else text


def _strip_suffixes(toks: list[str]) -> list[str]:
    """Remove legal suffixes from the END of the token list, repeatedly.

    End-only and repeated: ``D. E. Shaw & Co., L.P.`` sheds ``l p``, then ``co``,
    then the ``and`` left behind by ``& Co.``, and stops at ``d e shaw``. A
    global filter would instead eat the ``co`` in a firm genuinely named ``Co``.
    """
    while len(toks) > 1:
        for size in (3, 2):
            if len(toks) > size and " ".join(toks[-size:]) in MULTI_TOKEN_SUFFIXES:
                del toks[-size:]
                break
        else:
            if toks[-1] in LEGAL_SUFFIXES:
                toks.pop()
                continue
            # "L.P." arrives here as two single-character tokens; rejoin before
            # looking the acronym up.
            for size in (4, 3, 2):
                if (
                    len(toks) > size
                    and all(len(t) == 1 for t in toks[-size:])
                    and "".join(toks[-size:]) in LEGAL_SUFFIXES
                ):
                    del toks[-size:]
                    break
            else:
                if toks[-1] == "and":
                    toks.pop()
                    continue
                break
    return toks


def _drop_acronym_echo(toks: list[str]) -> list[str]:
    """Drop a token that only restates the initials of the rest.

    ``Jane Street (JS)`` -> ``jane street``; ``HRT (Hudson River Trading)`` ->
    ``hudson river trading``. This is the one parenthetical worth deleting,
    which is why parentheticals are otherwise kept: ``Surveyor Capital (A
    Citadel Company)`` is the most informative string a Citadel employee writes.
    """
    if len(toks) >= 3:
        if toks[-1] == initials(toks[:-1]):
            return toks[:-1]
        if toks[0] == initials(toks[1:]):
            return toks[1:]
    return toks


def _fold(text: str, *, drop_noise: bool, strip_suffix: bool, segment: bool) -> str:
    """Shared folding pipeline. See module docstring for why it runs twice."""
    if not text or not text.strip():
        return ""
    s = primary_segment(text) if segment else text
    s = unicodedata.normalize("NFKC", s)  # fullwidth + ligature fold
    s = s.translate(_ZERO_WIDTH)
    s = s.replace("\u00a0", " ")  # non-breaking space
    s = s.casefold()  # casefold, not lower(): ß -> ss
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))  # é -> e
    s = s.replace("&", " and ")
    s = _DASH_RE.sub("-", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    toks = s.split()
    if strip_suffix:
        toks = _strip_suffixes(toks)
    if drop_noise:
        kept = [t for t in toks if t not in NOISE_TOKENS]
        if kept:  # never let noise-dropping empty the string
            toks = kept
    toks = _drop_acronym_echo(toks)
    return " ".join(toks)


def norm(text: str, *, segment: bool = True) -> str:
    """Fuzzy-index form: articles dropped, legal suffixes stripped.

    ``Jane Street Group, LLC`` -> ``jane street``. Used for similarity scoring
    and nothing else -- it deliberately destroys the ``The`` that ``norm_raw``
    preserves.
    """
    return _fold(text, drop_noise=True, strip_suffix=True, segment=segment)


def norm_raw(text: str, *, segment: bool = True) -> str:
    """Veto-lookup form: articles and legal suffixes retained.

    ``The Citadel`` -> ``the citadel``, which is what makes it distinguishable
    from ``Citadel`` -> ``citadel``. Negative aliases are indexed under this and
    only this.
    """
    return _fold(text, drop_noise=False, strip_suffix=False, segment=segment)


def tokens(folded: str) -> list[str]:
    """Split an already-folded string. Trivial, but it names the contract."""
    return folded.split()
