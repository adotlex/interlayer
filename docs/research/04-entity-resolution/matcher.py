import re, unicodedata, yaml
from rapidfuzz import fuzz

GAZ = yaml.safe_load(open('/home/user/interlayer/data/gazetteer/firms.yaml'))
NORM = GAZ['normalization']
SUFFIXES = set(s.replace('.', '').replace(' ', '') for s in NORM['legal_suffixes'])
NOISE = set(NORM['noise_tokens'])

SEG   = re.compile(r"\s*[|•·/›»]\s*|\s+[-–—]\s+")
PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
WS    = re.compile(r"\s+")
DASH  = re.compile(r"[‐-―−]")
ZW    = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

TARGET_TIERS = {'target', 'adjacent'}

def _initials(toks): return "".join(t[0] for t in toks if t)

def primary_segment(s):
    """LinkedIn employer strings are often 'Company | Location', 'Company - Team',
    'Company / dba Other'. The firm is the FIRST segment."""
    parts = [p for p in SEG.split(s) if p and p.strip()]
    return parts[0] if parts else s

def _strip_suffixes(toks):
    """Strip legal suffixes from the END only, repeatedly. Handles punctuation-
    exploded acronyms ('l p' <- 'L.P.') and the dangling 'and' left by '& Co.'."""
    changed = True
    while changed and len(toks) > 1:
        changed = False
        if toks[-1] in SUFFIXES:
            toks.pop(); changed = True; continue
        for n in (4, 3, 2):
            if len(toks) > n and all(len(t) == 1 for t in toks[-n:]):
                if "".join(toks[-n:]) in SUFFIXES:
                    del toks[-n:]; changed = True; break
        if changed: continue
        if toks[-1] == "and":
            toks.pop(); changed = True
    return toks

def _drop_acronym_echo(toks):
    """Drop a leading/trailing token that is merely the initialism of the rest:
    'jane street js' -> 'jane street', 'sig susquehanna international group' ->
    'susquehanna international group'."""
    if len(toks) >= 3:
        if toks[-1] == _initials(toks[:-1]): toks = toks[:-1]
        elif toks[0] == _initials(toks[1:]): toks = toks[1:]
    return toks

def normalize(s, drop_noise=True, strip_suffix=True, segment=True):
    if s is None: return ""
    if segment: s = primary_segment(s)
    s = unicodedata.normalize("NFKC", s)              # 1 fullwidth/ligature fold
    s = s.translate(ZW)                               # 2 zero-width kill
    s = s.replace(" ", " ")                      # 3 nbsp -> space
    s = s.casefold()                                  # 4 casefold (not .lower())
    s = unicodedata.normalize("NFKD", s)              # 5 decompose
    s = "".join(c for c in s if not unicodedata.combining(c))   # 6 drop diacritics
    s = s.replace("&", " and ")                       # 7 ampersand unify
    s = DASH.sub("-", s)                              # 8 unicode dashes -> ascii
    s = PUNCT.sub(" ", s)                             # 9 strip punctuation
    s = WS.sub(" ", s).strip()                        # 10 collapse whitespace
    toks = s.split()
    if strip_suffix: toks = _strip_suffixes(toks)     # 11 trailing legal suffixes
    if drop_noise:                                    # 12 drop noise tokens
        t2 = [t for t in toks if t not in NOISE]
        if t2: toks = t2
    toks = _drop_acronym_echo(toks)                   # 13 "Jane Street (JS)" -> "jane street"
    return " ".join(toks)

def normalize_raw(s, segment=True):
    """PRESERVES 'the' and legal suffixes. Used for negative-alias lookup, where
    'The Citadel' vs 'Citadel' is precisely the distinction we must not lose."""
    return normalize(s, drop_noise=False, strip_suffix=False, segment=segment)

# --------------------------------------------------------------- index
def build_index(include_negative=True, use_weak=False):
    exact, fuzzy, neg_exact, neg_pat, meta = {}, [], {}, [], {}
    for e in GAZ['entities']:
        key, tier = e['key'], e.get('tier')
        if not include_negative and tier == 'negative': continue
        meta[key] = e
        names = list(e.get('aliases') or []) + [e['canonical_name']]
        if use_weak: names += list(e.get('weak_aliases') or [])
        for a in names:
            exact.setdefault(normalize(a), key)
            exact.setdefault(normalize_raw(a), key)
            fuzzy.append((normalize(a), key))
        for na in (e.get('negative_aliases') or []):
            # RAW ONLY: normalize("The Citadel Group") == "citadel", which would
            # poison the veto index and reject every legitimate "Citadel".
            neg_exact[normalize_raw(na)] = key
        for p in (e.get('negative_patterns') or []):
            neg_pat.append((re.compile(p, re.I), key))
    return dict(exact=exact, fuzzy=fuzzy, neg_exact=neg_exact, neg_pat=neg_pat, meta=meta)

def audit_index(idx):
    pos = set()
    for e in GAZ['entities']:
        if e.get('tier') in TARGET_TIERS:
            for a in list(e.get('aliases') or []) + [e['canonical_name']]:
                pos.add(normalize_raw(a)); pos.add(normalize(a))
    return [k for k in idx['neg_exact'] if k in pos]

# --------------------------------------------------------------- guard
ALLOW = {"global","international","group","holdings","americas","us","usa","uk",
         "europe","european","asia","apac","pacific","emea","japan","china",
         "hong","kong","singapore","india","australia","netherlands","ireland",
         "london","york","new","chicago","miami","amsterdam","austin","stamford",
         "trading","markets","capital","management","investments","financial",
         "a","company","the","and","at","of"}

def containment_guard(q, alias, tok_thr=80):
    """Accept only if every token on BOTH sides is either fuzzily aligned with a
    token on the other side, or is known non-discriminating filler. Kills
    'citadel' -> 'citadel broadcasting' while tolerating typos inside an
    otherwise-aligned token."""
    qt, at = q.split(), alias.split()
    used, extra_q = set(), []
    for t in qt:
        best, bi = -1, None
        for i, u in enumerate(at):
            if i in used: continue
            sc = fuzz.ratio(t, u)
            if sc > best: best, bi = sc, i
        if best >= tok_thr and bi is not None: used.add(bi)
        else: extra_q.append(t)
    extra_a = [u for i, u in enumerate(at) if i not in used]
    extras = (set(extra_q) | set(extra_a)) - {_initials(qt), _initials(at)}
    return extras <= ALLOW

# --------------------------------------------------------------- match
def match(query, idx, scorer=fuzz.token_sort_ratio, threshold=92, use_guard=True,
          use_negatives=True):
    """-> (entity_key_or_None, score, reason)"""
    raw      = normalize_raw(query)
    raw_full = normalize_raw(query, segment=False)
    n        = normalize(query)
    if not n: return None, 0, "empty"
    if use_negatives:
        for probe in (raw, n):
            if probe in idx['neg_exact']:
                return None, 100, "negative_exact:" + idx['neg_exact'][probe]
        for pat, k in idx['neg_pat']:
            if pat.search(raw) or pat.search(n) or pat.search(raw_full):
                return None, 100, "negative_pattern:" + k
    for probe, tag in ((raw, "exact_raw"), (n, "exact")):
        if probe in idx['exact']:
            k = idx['exact'][probe]
            return (k if idx['meta'][k].get('tier') in TARGET_TIERS else None), 100, f"{tag}:{k}"
    best_k, best_s, best_a = None, 0.0, None
    for alias, k in idx['fuzzy']:
        s = scorer(n, alias)
        if s > best_s: best_k, best_s, best_a = k, s, alias
    if best_s < threshold: return None, best_s, "below_threshold"
    if use_guard and not containment_guard(n, best_a):
        return None, best_s, f"guard_reject:{best_k}({best_a})"
    return (best_k if idx['meta'][best_k].get('tier') in TARGET_TIERS else None), best_s, "fuzzy:" + str(best_k)
