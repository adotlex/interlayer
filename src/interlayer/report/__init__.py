"""Report rendering: one self-contained HTML file plus the run manifest.

The stage entry point is :func:`run`, kept here so the CLI's
``importlib.import_module("interlayer.report")`` finds it the same way it finds
every other stage.
"""

from __future__ import annotations

from interlayer.report.pipeline import assert_self_contained, run

__all__ = ["assert_self_contained", "run"]
