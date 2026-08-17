"""Identity, normalisation and stable-key derivation.

FROZEN FOR WAVE 2 alongside ``models.py``. Standard library only.

Two Wave 1 findings are encoded here and must not be undone:

* **Never transliterate names.** ``unidecode`` renders ``李明`` as ``"Li Ming "``
  and ``محمد`` as ``"mHmd"``. We NFKD-fold *combining marks* instead, which maps
  ``é → e`` while leaving CJK and Arabic base characters intact.
* **Never let a parser decide which token is the surname.** ``nameparser``
  inverts ``Nguyễn Văn An`` and ``Kim Min-jun``. The blocking key is instead
  order-free: tokens are sorted, so name order stops mattering.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import unquote, urlsplit

#: Honorific and credential tokens dropped before keying. Kept deliberately
#: small: an over-eager list starts eating real name particles.
_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {
        "jr", "jnr", "sr", "snr", "ii", "iii", "iv", "v",
        "phd", "dphil", "md", "mba", "msc", "bsc", "ba", "ma",
        "cfa", "cpa", "cae", "esq", "pe", "pmp", "frm", "caia",
    }
)

_APOSTROPHES = dict.fromkeys(map(ord, "'’ʼ`´"), None)
_DASHES = re.compile(r"[-‐-―−]+")
_NON_WORD = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+", flags=re.UNICODE)
_SLUG_RE = re.compile(r"/in/([^/?#]+)", flags=re.IGNORECASE)


def sha1_hex(value: str) -> str:
    """Stable hex digest. Used for all synthetic IDs."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def fold_accents(text: str) -> str:
    """Drop combining marks while preserving base characters of every script.

    ``é → e``; ``李明 → 李明``; ``مُحَمَّد → محمد``. This is decomposition plus
    mark removal, not transliteration.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFC", stripped)


def normalize_text(text: str | None) -> str:
    """Casefold, fold accents, neutralise punctuation, collapse whitespace.

    Runs of internal whitespace are collapsed explicitly because rapidfuzz's
    ``utils.default_process`` does not: ``WRatio("JOHN  SMITH", "john smith")``
    scores 95.2 rather than 100, which is enough to push a true match under a
    92 threshold.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = out.translate(_APOSTROPHES)  # O'Brien -> OBrien, so it matches OBrien
    out = _DASHES.sub(" ", out)  # Min-jun -> Min jun
    out = fold_accents(out)
    out = _NON_WORD.sub(" ", out)
    out = _WS.sub(" ", out)
    return out.strip().casefold()


def name_tokens(*parts: str | None) -> tuple[str, ...]:
    """Normalised name tokens with honorifics and credentials removed."""
    joined = " ".join(p for p in parts if p)
    tokens = [t for t in normalize_text(joined).split(" ") if t]
    kept = [t for t in tokens if t not in _SUFFIX_TOKENS]
    return tuple(kept or tokens)  # never return empty if input had content


def name_key(*parts: str | None) -> str:
    """Order-free blocking key for a person's name.

    Sorting the tokens makes the key invariant to name order, so
    ``Nguyễn Văn An`` and ``An Van Nguyen`` collide without anyone having to
    decide which token is the family name.

    This is a *blocking* key: it groups candidates for comparison. It is never
    rendered in a report or a cluster label (PRIV-09).
    """
    return " ".join(sorted(name_tokens(*parts)))


def normalize_slug(value: str | None) -> str | None:
    """Extract and canonicalise a LinkedIn public identifier.

    Accepts a full profile URL, a bare ``/in/slug`` path, or an already-bare
    slug. Returns lowercase, percent-decoded, without trailing slash or query.
    Returns ``None`` when nothing slug-shaped is present.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    match = _SLUG_RE.search(raw)
    if match:
        slug = match.group(1)
    elif "/" in raw or raw.startswith("http"):
        slug = urlsplit(raw).path.rstrip("/").rsplit("/", 1)[-1]
    else:
        slug = raw

    slug = unquote(slug).strip().strip("/").casefold()
    return slug or None


def member_id(
    *,
    slug: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    company: str | None = None,
) -> str:
    """Stable id for one of the user's connections.

    Prefers the LinkedIn slug, which is the only near-stable identifier
    available. Falls back to name plus employer — weaker, because slugs change
    and names collide, but deterministic.
    """
    canonical = normalize_slug(slug)
    if canonical:
        return "m_" + sha1_hex("slug:" + canonical)[:16]
    basis = "name:{}|{}".format(name_key(first_name, last_name), normalize_text(company))
    return "m_" + sha1_hex(basis)[:16]


def target_id(
    *,
    firm: str,
    slug: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    """Stable id for a target-firm person.

    Firm is part of the basis: the same human appearing under two firms is two
    target records, because Citadel and Citadel Securities are distinct entities
    and merging them would corrupt per-firm results.
    """
    canonical = normalize_slug(slug)
    if canonical:
        return "t_" + sha1_hex(f"{firm}|slug:{canonical}")[:16]
    basis = f"{firm}|name:{name_key(first_name, last_name)}"
    return "t_" + sha1_hex(basis)[:16]


def edge_key(member: str, target: str) -> tuple[str, str]:
    """Canonical dedupe key for an edge (T8.5)."""
    return (member, target)


__all__ = [
    "edge_key",
    "fold_accents",
    "member_id",
    "name_key",
    "name_tokens",
    "normalize_slug",
    "normalize_text",
    "sha1_hex",
    "target_id",
]
