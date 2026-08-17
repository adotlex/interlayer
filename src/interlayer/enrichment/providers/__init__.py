"""Enrichment adapters.

This module imports **no** provider: the registry resolves names to factories
lazily, so a missing HTTP library, an absent API key or a blocked network can
never break ``import interlayer``.

It does hold the one shared piece of network plumbing, :func:`http_json`, which
imports its HTTP client *inside the call*. That keeps every adapter module
importable — and therefore introspectable by the compliance tests — on a machine
with no ``httpx`` and no network at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = ["http_json"]


def http_json(
    method: str, url: str, body: Any | None, headers: Mapping[str, str]
) -> Any:
    """Perform one JSON request. Never called by the offline providers.

    Prefers ``httpx`` when it is installed and falls back to the standard
    library, so nothing in this package's dependency set is required for the
    offline path to work.
    """
    payload = None if body is None else json.dumps(body).encode("utf-8")

    try:
        import httpx
    except ModuleNotFoundError:
        pass
    else:
        response = httpx.request(
            method, url, content=payload, headers=dict(headers), timeout=60.0
        )
        response.raise_for_status()
        return response.json()

    import urllib.request

    request = urllib.request.Request(url, data=payload, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=60.0) as response:
        return json.loads(response.read().decode("utf-8"))
