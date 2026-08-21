"""Entity resolution: raw employer/school strings -> canonical ``Org``s.

The Citadel problem is this stage's whole job. The military college has roughly
40,000 living alumni against the fund's 3,150 employees, which makes it the
dominant false-positive source by an order of magnitude: if the college leaks
into the fund bucket, the fund bucket is mostly college alumni. Three rules hold
it apart, and all three are mandatory.

1. **Normalise twice** (:mod:`interlayer.normalize.normalize`). ``norm_raw``
   keeps the definite article and the legal suffix; ``norm`` drops them. Fold
   once and ``The Citadel`` becomes ``Citadel``, after which every scorer returns
   100 and no threshold can help. Negative aliases are indexed under ``norm_raw``
   only -- ``norm("The Citadel Group") == "citadel"``, so indexing them under
   ``norm`` would veto every genuine Citadel employee.
   :func:`~interlayer.normalize.gazetteer.audit_index` runs on every load so that
   cannot regress silently.
2. **Field context decides** (:mod:`interlayer.normalize.match`). ``The Citadel``
   in an education field is the college. In a position field it is genuinely
   under-determined, so it goes to the review queue rather than being accepted or
   dropped.
3. **The fund and the market maker never merge.** ``TargetFirm.CITADEL_LLC`` and
   ``TargetFirm.CITADEL_SECURITIES`` are separate companies with separate pages
   and separate staff; ``securities`` is never in the guard's allow list.

Matching is ``rapidfuzz.WRatio`` behind the negative gazetteer *and* a
token-alignment containment guard. No subset-tolerant scorer is safe on its own:
``partial_ratio``, ``token_set_ratio`` and ``token_ratio`` all score 100 for
``Citadel`` against ``Citadel Broadcasting``.
"""

from interlayer.normalize.gazetteer import Gazetteer, audit_index, load_gazetteer
from interlayer.normalize.match import MatchResult, containment_guard, resolve
from interlayer.normalize.normalize import norm, norm_raw
from interlayer.normalize.stage import ReviewItem, run
from interlayer.normalize.titles import RoleFamily, extract_role, extract_seniority

__all__ = [
    "Gazetteer",
    "MatchResult",
    "ReviewItem",
    "RoleFamily",
    "audit_index",
    "containment_guard",
    "extract_role",
    "extract_seniority",
    "load_gazetteer",
    "norm",
    "norm_raw",
    "resolve",
    "run",
]
