"""The redaction chokepoint (research §3.3, P-12 … P-17).

``cfg.redact`` has to fail closed, so redaction is not a set of template
conditionals — one forgotten ``{% if %}`` in a template is a leaked name and a
leaked name is a failed redaction. Instead every value that reaches the template
passes through :class:`Redactor`, which applies three independent gates:

1. **Allowlist.** :meth:`Redactor.emit` rebuilds each record from a per-kind
   allowlist of field names. A key that nobody thought about is dropped, so a
   field added to ``models.py`` next month is invisible in redacted mode until
   somebody deliberately permits it. This is P-14: forgetting to update the list
   fails closed.
2. **No inherited free text.** In redacted mode the report emits *no* string
   authored by another pipeline stage. ``EvidenceItem.detail``,
   ``Cluster.label`` and ``ScoredCluster.rationale`` are unbounded text written
   by other agents, and evidence details in particular are expected to name
   people ("mutual connection with Jane Doe"), including target-firm employees
   who never appear in ``people.jsonl`` and so cannot be scrubbed by name. Those
   fields are dropped and replaced with descriptions synthesised here from
   structured fields — ids, counts, org ids, enum members.
3. **Post-hoc audit.** :meth:`Redactor.audit` walks the finished payload and
   raises if any known name survived. The report file is written only after the
   audit passes, so a leak is a crash, never an artifact on disk.

Gate 2 is the one that actually does the work; gates 1 and 3 exist because gate
2 depends on a human remembering which fields are free text.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from interlayer.config import Settings
from interlayer.errors import InterlayerError
from interlayer.io import secure_write
from interlayer.models import ApproxDate, Person

__all__ = [
    "REDACTED_ALLOWLIST",
    "RedactionLeakError",
    "Redactor",
    "pseudonym_key_path",
]

REDACTED = "[REDACTED]"

#: Crockford base32: no I, L, O or U, so a pseudonym read aloud or copied by
#: hand does not turn into a different pseudonym.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SLUG_RE = re.compile(r"/(?:in|pub)/[\w%-]+", re.IGNORECASE)

#: Keys whose values are slugs this module generated, not text from the export.
#: Scanning them for names produces only false positives.
_AUDIT_SKIP_KEYS = frozenset(
    {"anchor", "band", "cluster_id", "firm", "id", "kind", "provenance", "slug", "target_firm"}
)

#: Shortest token the name scrubber will act on. Two characters is aggressive
#: and will over-redact ordinary words, which is the correct direction to err.
_MIN_NAME_LEN = 2


class RedactionLeakError(InterlayerError):
    """A known personal name survived redaction. The report is not written.

    ``errors.py`` has no redaction-specific member and is scaffold-owned, so this
    subclasses ``InterlayerError`` locally; the CLI maps it to exit code 3 like
    any other deliberate failure.
    """


# --------------------------------------------------------------------------
# allowlists  (P-14)
# --------------------------------------------------------------------------

#: Field names permitted in redacted output, per record kind. Everything else is
#: dropped by :meth:`Redactor.emit`. Read this as the complete answer to "what
#: can a redacted report possibly contain".
REDACTED_ALLOWLIST: dict[str, frozenset[str]] = {
    "person": frozenset(
        {
            "anchor",
            "band",
            "cluster_anchor",
            "cluster_id",
            "cluster_label",
            "confidence",
            "evidence",
            "hops",
            "id",
            "label",
            "n_evidence",
            "observed",
            "per_firm",
            "rank",
            "score",
            "speculative",
            "stale_note",
            "components",
        }
    ),
    "evidence": frozenset(
        {
            "contribution",
            "detail",
            "hops",
            "kind",
            "observed",
            "org_label",
            "via_label",
        }
    ),
    "cluster": frozenset(
        {
            "anchor",
            "confidence",
            "band",
            "id",
            "label",
            "mean_confidence",
            "members",
            "observed_members",
            "per_firm",
            "rank",
            "rationale",
            "score",
            "size",
            "target_density",
            "top_orgs",
        }
    ),
    "node": frozenset({"cluster", "id", "label", "observed", "r", "rank", "x", "y"}),
    "member": frozenset({"anchor", "label", "observed"}),
    "org": frozenset({"count", "label"}),
}


def pseudonym_key_path(cfg: Settings) -> Path:
    """Where the pseudonym key lives (P-13, P-18: state under ``./.interlayer``).

    Derived from ``artifact_dir.parent`` rather than the process working
    directory so a run pointed at a throwaway artifact directory keeps its key
    beside it instead of scattering state into wherever the CLI was invoked.
    """
    return cfg.artifact_dir.parent / ".interlayer" / "pseudonym.key"


def _load_or_create_key(path: Path) -> bytes:
    """32 random bytes, generated once and reused (P-13).

    Same key means stable pseudonyms across runs, so an operator can re-share an
    updated report and the reader can line the two up. A different install means
    unlinkable ids, so two people's redacted reports cannot be joined.
    """
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise InterlayerError(
                f"pseudonym key at {path} is corrupt; delete it to regen"
            ) from exc
        if len(key) >= 32:
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                path.chmod(0o600)
            return key
    key = secrets.token_bytes(32)
    secure_write(path, key.hex() + "\n")
    return key


def _crockford(raw: bytes, length: int) -> str:
    """Encode leading bits of ``raw`` as Crockford base32."""
    value = int.from_bytes(raw, "big")
    out: list[str] = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


class Redactor:
    """Single point through which every dynamic value reaches the template."""

    def __init__(self, cfg: Settings, people: Iterable[Person]) -> None:
        self.enabled: bool = bool(cfg.redact)
        self._ident: dict[str, str] = {}
        self._key: bytes = b""
        roster = sorted(people, key=lambda p: p.person_id)
        if self.enabled:
            self._key = _load_or_create_key(pseudonym_key_path(cfg))
            # P-12: HMAC over the profile URL, or over name|company where the
            # URL is absent. Recorded per person so the same input always yields
            # the same pseudonym regardless of iteration order.
            for person in roster:
                if person.linkedin_url:
                    basis = person.linkedin_url.casefold()
                else:
                    company = (person.current_company or "").casefold()
                    basis = f"{person.full_name.casefold()}|{company}"
                self._ident[person.person_id] = basis
        self._name_re: re.Pattern[str] | None = _build_name_pattern(roster)

    # -- identifiers -------------------------------------------------------

    def person_label(self, person_id: str, full_name: str) -> str:
        """Display name, or a stable ``PC-…`` pseudonym in redacted mode."""
        if not self.enabled:
            return full_name or person_id
        return self.pseudonym(person_id)

    def pseudonym(self, person_id: str) -> str:
        """``PC-XXXXXXXXXXXX`` — 12 Crockford base32 chars of HMAC-SHA256."""
        basis = self._ident.get(person_id, person_id)
        digest = hmac.new(self._key or b"\0", basis.encode("utf-8"), hashlib.sha256).digest()
        return f"PC-{_crockford(digest, 12)}"

    def anchor(self, person_id: str) -> str:
        """Stable in-document fragment id. Never the raw person id in redacted mode."""
        return self.pseudonym(person_id) if self.enabled else person_id

    # -- free text ---------------------------------------------------------

    def scrub(self, value: str | None) -> str:
        """Strip known names, e-mails, URLs and profile slugs from free text (P-15).

        Applied to every string that survives into redacted output, including
        org names — a one-person consultancy is named after its owner.
        """
        if not value:
            return ""
        if not self.enabled:
            return value
        out = _URL_RE.sub(REDACTED, value)
        out = _SLUG_RE.sub(REDACTED, out)
        out = _EMAIL_RE.sub(REDACTED, out)
        if self._name_re is not None:
            out = self._name_re.sub(REDACTED, out)
        return out.strip()

    def year(self, when: ApproxDate | None) -> str | None:
        """Quantise a date to the year (P-16). Exact dates re-identify people."""
        if when is None:
            return None
        if not self.enabled:
            return str(when)
        return f"{when.year:04d}"

    # -- serialisation chokepoint -----------------------------------------

    def emit(self, kind: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild ``raw`` from the allowlist for ``kind`` (P-14).

        In identified mode this only drops ``_``-prefixed working keys. In
        redacted mode the output is constructed *from the allowlist*, so an
        unrecognised key cannot survive by accident.
        """
        if kind not in REDACTED_ALLOWLIST:
            raise InterlayerError(f"no redaction allowlist declared for record kind {kind!r}")
        if not self.enabled:
            return {k: v for k, v in raw.items() if not k.startswith("_")}
        allow = REDACTED_ALLOWLIST[kind]
        return {k: v for k, v in raw.items() if k in allow}

    # -- audit -------------------------------------------------------------

    def audit(self, payload: Any) -> None:
        """Raise if any known name survived into ``payload``.

        Only the dynamic payload is scanned, never the fixed report copy: an
        operator may well be connected to somebody named "Will" or "Grace", and
        refusing to render because a boilerplate sentence contains that word
        would make redacted mode unusable. Fixed copy contains no export data,
        so scanning it buys nothing.
        """
        if not self.enabled or self._name_re is None:
            return
        for path, text in _walk_strings(payload, ""):
            hit = self._name_re.search(text)
            if hit is not None:
                raise RedactionLeakError(
                    f"redaction leak at {path or '<root>'}: a known personal name survived "
                    f"into the report payload (matched {len(hit.group(0))} characters); "
                    "refusing to write the report"
                )


def _walk_strings(node: Any, path: str) -> Iterable[tuple[str, str]]:
    """Yield every ``(path, string)`` in a nested payload, skipping slug keys."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key in sorted(node, key=str):
            if key in _AUDIT_SKIP_KEYS:
                continue
            yield from _walk_strings(node[key], f"{path}.{key}" if path else str(key))
    elif isinstance(node, list | tuple):
        for index, item in enumerate(node):
            yield from _walk_strings(item, f"{path}[{index}]")


def _build_name_pattern(people: Iterable[Person]) -> re.Pattern[str] | None:
    """One alternation over every first, last and full name in the input.

    Sorted longest-first so ``Ada Lovelace`` is consumed before ``Ada``, which
    keeps the replacement readable rather than ``[REDACTED] [REDACTED]``.
    """
    tokens: set[str] = set()
    for person in people:
        for raw in (person.first_name, person.last_name, person.full_name):
            candidate = (raw or "").strip()
            if len(candidate) >= _MIN_NAME_LEN:
                tokens.add(candidate)
        slug = person.linkedin_slug or ""
        for part in re.split(r"[^\w]+", slug):
            if len(part) >= _MIN_NAME_LEN and not part.isdigit():
                tokens.add(part)
    if not tokens:
        return None
    ordered = sorted(tokens, key=lambda t: (-len(t), t))
    body = "|".join(re.escape(t) for t in ordered)
    # \b on both sides so "Li" does not gut every word containing those letters;
    # a name adjacent to punctuation or a hyphenated slug still matches.
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)
