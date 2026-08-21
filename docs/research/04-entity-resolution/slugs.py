import re, unicodedata, urllib.parse
SLUG_RE = re.compile(r"(?:^|/)(?:in|pub)/([^/?#]+)", re.I)
LOCALE = re.compile(r"^(?:[a-z]{2}|[a-z]{2}-[a-z]{2})$", re.I)
def norm_slug(u):
    if not u: return None
    u = urllib.parse.unquote(u.strip())
    u = unicodedata.normalize("NFKC", u)
    if "://" not in u and not u.startswith("/"):
        if "linkedin.com" in u.lower(): u = "https://" + u
        elif "/" not in u: u = "https://www.linkedin.com/in/" + u
    p = urllib.parse.urlsplit(u)
    m = SLUG_RE.search(p.path)
    if not m: return None
    slug = m.group(1).rstrip("/")
    # LEGACY /pub/ URLs carry the discriminating digits in LATER path segments;
    # casefolding just the first segment would collide unrelated people.
    if re.search(r"/pub/", p.path, re.I):
        rest = p.path.split("/pub/",1)[1].strip("/")
        return "pub:" + rest.casefold()
    # Member-URN slugs (ACoAAA...) are CASE-SENSITIVE base64. Casefolding them
    # destroys entropy and can merge two different humans. Preserve case.
    if re.match(r"^ACoA[A-Za-z0-9_-]+$", slug):
        return "urn:" + slug
    return slug.casefold() or None
CASES = [
 "https://www.linkedin.com/in/jane-doe-12345678/",
 "http://linkedin.com/in/jane-doe-12345678",
 "https://uk.linkedin.com/in/jane-doe-12345678",
 "https://www.linkedin.com/in/jane-doe-12345678?trk=people-guest_people_search-card",
 "https://www.linkedin.com/in/jane-doe-12345678/en",
 "https://www.linkedin.com/in/jane-doe-12345678/?originalSubdomain=uk",
 "www.linkedin.com/in/jane-doe-12345678",
 "linkedin.com/in/jane-doe-12345678/",
 "/in/jane-doe-12345678",
 "jane-doe-12345678",
 "https://www.linkedin.com/in/JANE-DOE-12345678/",
 "https://www.linkedin.com/in/%C3%A9lodie-martin-4b2a1c",
 "https://www.linkedin.com/in/ACoAAABcDeFgHiJkLmNoP",
 "https://www.linkedin.com/in/ACoAAABcDEFGHIJKLMNOP",
 "https://www.linkedin.com/pub/jane-doe/12/345/679",
 "https://www.linkedin.com/pub/jane-doe/12/345/678",
 "https://www.linkedin.com/in/jane-doe-12345678/overlay/contact-info/",
 "https://de.linkedin.com/in/j%C3%B6rg-m%C3%BCller-9a8b7c",
 "https://www.linkedin.com/company/citadel-llc",
 "",
]
if __name__ == "__main__":
    print("="*100); print("EXPERIMENT K - LinkedIn public-profile slug normalization"); print("="*100)
    seen={}
    for c in CASES:
        s=norm_slug(c); seen.setdefault(s,[]).append(c)
        print(f"  {c[:66]:<68} -> {s!r}")
    print("\ncollapsed groups (same slug == same human):")
    for s,g in seen.items():
        if s and len(g)>1: print(f"  {s!r}: {len(g)} distinct URL spellings")
