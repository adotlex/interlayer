"""HTML renderer — the format a human actually reads.

Self-contained by construction: no external stylesheet, no font, no script, no
image. A report about other people's professional relationships should not make
a network request when it is opened, and an offline artefact is the only kind
that can be archived honestly.

Autoescaping is on. Names in this document are third-party free text that
arrived from a web page, and the renderer treats them as hostile input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import Environment, select_autoescape

from interlayer.report import (
    UI_DRIFT_NOTICE,
    ReportContext,
    basis_label,
    coverage_caveat,
    fmt,
    fmt_pct,
    harvest_is_complete,
    header_rows,
    inference_banner,
    is_inferred,
    reach_display,
    review_statement,
    sorted_bridges,
    sorted_clusters,
    sorted_next_targets,
)
from interlayer.report.redact import redact_text, redact_value

if TYPE_CHECKING:  # pragma: no cover - typing only
    from interlayer.core.models import AnalysisResult

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>{{ title }}</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9;
    --surface: #ffffff;
    --surface-2: #f0f2f5;
    --ink: #16191d;
    --ink-2: #565d68;
    --ink-3: #7c8592;
    --line: #dfe3e8;
    --accent: #1d4ed8;
    --warn-ink: #7a4a03;
    --warn-bg: #fdf3e0;
    --warn-line: #e0a33d;
    --danger-ink: #8a2020;
    --danger-bg: #fbeceb;
    --danger-line: #d98b86;
    --ok: #1a7a4c;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115;
      --surface: #171a20;
      --surface-2: #1e222a;
      --ink: #e8eaee;
      --ink-2: #a8b0bc;
      --ink-3: #79828f;
      --line: #2b313b;
      --accent: #7ea2ff;
      --warn-ink: #f0c07a;
      --warn-bg: #2a2013;
      --warn-line: #8a6520;
      --danger-ink: #f0a09a;
      --danger-bg: #2a1616;
      --danger-line: #8a4038;
      --ok: #5fd0a0;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 2rem 1.25rem 5rem;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
  }
  .wrap { max-width: 1080px; margin: 0 auto; }
  h1 { font-size: 1.6rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
  h2 { font-size: 1.1rem; margin: 2.25rem 0 .75rem; letter-spacing: -.005em; }
  h3 { font-size: .98rem; margin: 0 0 .35rem; }
  p  { margin: .5rem 0; }
  .sub { color: var(--ink-2); margin: 0 0 1.5rem; font-size: .9rem; }
  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 1rem 1.1rem;
  }
  .notice { border-left: 4px solid var(--accent); }
  .notice dl { margin: 0; display: grid; grid-template-columns: 10rem 1fr; gap: .45rem 1rem; }
  .notice dt { color: var(--ink-2); font-weight: 600; font-size: .82rem;
               text-transform: uppercase; letter-spacing: .04em; padding-top: .1rem; }
  .notice dd { margin: 0; }
  .banner {
    border-radius: var(--radius); padding: .8rem 1rem; margin: 1rem 0;
    border: 1px solid var(--line); background: var(--surface-2); font-size: .92rem;
  }
  .banner.warn { background: var(--warn-bg); border-color: var(--warn-line);
                 color: var(--warn-ink); }
  .banner.alert { background: var(--danger-bg); border-color: var(--danger-line);
                  color: var(--danger-ink); }
  .banner strong { font-weight: 700; }
  .tiles { display: grid; gap: .75rem;
           grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .tile { background: var(--surface); border: 1px solid var(--line);
          border-radius: var(--radius); padding: .8rem .9rem; }
  .tile .k { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
             color: var(--ink-3); }
  .tile .v { font-size: 1.5rem; font-weight: 650; letter-spacing: -.02em; margin-top: .15rem; }
  .tile .n { font-size: .78rem; color: var(--ink-2); }
  .meter { height: 8px; border-radius: 999px; background: var(--surface-2);
           border: 1px solid var(--line); overflow: hidden; margin: .9rem 0 .3rem; }
  .meter > span { display: block; height: 100%; background: var(--accent); }
  .scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; font-size: .9rem; min-width: 720px; }
  caption { text-align: left; color: var(--ink-2); font-size: .85rem; padding-bottom: .5rem; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line);
           white-space: nowrap; }
  th { font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
       color: var(--ink-3); font-weight: 650; }
  td.num { font-variant-numeric: tabular-nums; }
  tbody tr.inferred { background: var(--warn-bg); }
  tbody tr.inferred td { color: var(--warn-ink); font-style: italic; }
  tbody tr.inferred td:first-child { border-left: 3px solid var(--warn-line); }
  .pill { display: inline-block; font-size: .68rem; font-weight: 700; letter-spacing: .06em;
          padding: .1rem .4rem; border-radius: 4px; border: 1px solid var(--line);
          color: var(--ink-3); background: var(--surface-2); vertical-align: baseline; }
  .pill.inferred { color: var(--warn-ink); background: var(--warn-bg);
                   border: 1px dashed var(--warn-line); font-style: normal; }
  .lower-bound { font-variant-numeric: tabular-nums; }
  .cards { display: grid; gap: .85rem;
           grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
  .cluster .meta { color: var(--ink-2); font-size: .82rem; margin-bottom: .6rem; }
  .chips { display: flex; flex-wrap: wrap; gap: .3rem; }
  .chip { font-size: .8rem; padding: .15rem .5rem; border-radius: 999px;
          background: var(--surface-2); border: 1px solid var(--line); color: var(--ink); }
  .chip.inferred { border: 1px dashed var(--warn-line); background: var(--warn-bg);
                   color: var(--warn-ink); font-style: italic; }
  .chip.target { border-style: solid; color: var(--ink-2); }
  .lbl { font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
         color: var(--ink-3); margin: .7rem 0 .25rem; }
  ol.next { list-style: none; counter-reset: n; margin: 0; padding: 0; }
  ol.next li { counter-increment: n; display: flex; gap: .75rem; align-items: baseline;
               padding: .55rem .2rem; border-bottom: 1px solid var(--line); }
  ol.next li::before { content: counter(n); color: var(--ink-3); font-variant-numeric: tabular-nums;
                       min-width: 1.4rem; font-size: .85rem; }
  ol.next .who { font-weight: 600; }
  ol.next .why { color: var(--ink-2); font-size: .84rem; margin-left: auto; text-align: right; }
  footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid var(--line);
           color: var(--ink-3); font-size: .8rem; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85em;
         word-break: break-all; }
  .empty { color: var(--ink-3); font-style: italic; }
</style>
</head>
<body>
<div class="wrap">

  <h1>{{ title }}</h1>
  <p class="sub">Generated {{ generated_at }} by interlayer {{ tool_version }}</p>

  <section class="card notice" aria-label="Handling notice">
    <dl>
    {%- for label, statement in header %}
      <dt>{{ label }}</dt><dd>{{ statement }}</dd>
    {%- endfor %}
    </dl>
  </section>

  <div class="banner {{ 'warn' if used_inferred else '' }}">
    <strong>Basis of ties.</strong> {{ inference_banner }}
  </div>

  <div class="banner {{ 'alert' if unadjudicated_count else '' }}">
    <strong>Review queue.</strong> {{ review_statement }}
  </div>

  <h2>Coverage</h2>
  <div class="tiles">
    <div class="tile">
      <div class="k">Targets harvested</div>
      <div class="v">{{ cov.n_targets_harvested }}<span
        class="n"> / {{ cov.n_targets_total }}</span></div>
      <div class="n">{{ cov.pct }} of the known target set</div>
    </div>
    <div class="tile">
      <div class="k">Connections in scope</div>
      <div class="v">{{ cov.n_members_total }}</div>
      <div class="n">the set M</div>
    </div>
    <div class="tile">
      <div class="k">Bridges found</div>
      <div class="v">{{ cov.n_bridges }}</div>
      <div class="n">connections with at least one tie</div>
    </div>
    <div class="tile">
      <div class="k">Harvest</div>
      <div class="v">{{ 'complete' if cov.complete else 'partial' }}</div>
      <div class="n">{{ 'reach is exact' if cov.complete else 'reach is a lower bound' }}</div>
    </div>
  </div>
  <div class="meter" role="img" aria-label="Coverage {{ cov.pct }}">
    <span style="width: {{ cov.pct_width }}%"></span>
  </div>
  <div class="banner {{ '' if cov.complete else 'warn' }}">{{ cov.caveat }}</div>

  <h2>Bridge ranking</h2>
  {%- if bridges %}
  <div class="scroll">
  <table>
    <caption>Ranked by composite brokerage score. Ties break on ascending member id.</caption>
    <thead>
      <tr>
        <th>#</th><th>Bridge</th><th>Basis</th><th>Reach</th><th>Rarity</th>
        <th>Betweenness</th><th>Eff. size</th><th>Autonomy</th><th>Composite</th>
      </tr>
    </thead>
    <tbody>
    {%- for b in bridges %}
      <tr class="{{ 'inferred' if b.inferred else 'observed' }}">
        <td class="num">{{ b.rank }}</td>
        <td>{{ b.name }}{% if b.inferred %}
            <span class="pill inferred">{{ b.basis }}</span>{% endif %}</td>
        <td><span class="pill {{ 'inferred' if b.inferred else '' }}">{{ b.basis }}</span></td>
        <td class="num lower-bound">{{ b.reach }}</td>
        <td class="num">{{ b.rarity }}</td>
        <td class="num">{{ b.betweenness }}</td>
        <td class="num">{{ b.effective_size }}</td>
        <td class="num">{{ b.autonomy }}</td>
        <td class="num">{{ b.composite }}</td>
      </tr>
    {%- endfor %}
    </tbody>
  </table>
  </div>
  {%- else %}
  <p class="empty">No bridges. Either nothing has been harvested yet, or none of your
     connections has a tie into the target set.</p>
  {%- endif %}

  <h2>Clusters</h2>
  {%- if clusters %}
  <div class="cards">
  {%- for c in clusters %}
    <section class="card cluster">
      <h3>{{ c.label }}</h3>
      <div class="meta">cluster {{ c.cluster_id }} &middot; {{ c.n_members }} bridges &middot;
        stability {{ c.stability }} &middot; score {{ c.score_sum }}</div>
      <div class="lbl">Bridges</div>
      <div class="chips">
      {%- for m in c.members %}
        <span class="chip {{ 'inferred' if m.inferred else '' }}">{{ m.name
          }}{% if m.inferred %} &middot; {{ m.basis }}{% endif %}</span>
      {%- else %}
        <span class="empty">none</span>
      {%- endfor %}
      </div>
      {%- if c.top_targets %}
      <div class="lbl">Reaches into</div>
      <div class="chips">
      {%- for t in c.top_targets %}
        <span class="chip target">{{ t }}</span>
      {%- endfor %}
      </div>
      {%- endif %}
    </section>
  {%- endfor %}
  </div>
  {%- else %}
  <p class="empty">No clusters. Either no bridges were found, or the harvest is too thin
     to partition.</p>
  {%- endif %}

  <h2>Who to look at next</h2>
  <p class="sub">Ranked by how much each unharvested target would change the picture,
     not by how senior the person is.</p>
  {%- if next_targets %}
  <ol class="next">
  {%- for n in next_targets %}
    <li>
      <span class="who">{{ n.name }}</span>
      <span class="why">priority {{ n.priority }} &middot; {{ n.n_known_bridges }} known bridges
        &middot; {{ n.n_clusters_touched }} clusters &middot; novelty {{ n.novelty }}</span>
    </li>
  {%- endfor %}
  </ol>
  {%- else %}
  <p class="empty">No suggestions. Every known target has been harvested, or there is not
     yet enough structure to rank them.</p>
  {%- endif %}

  <footer>
    <p>Fingerprint <code>{{ fingerprint }}</code></p>
    {%- if params %}
    <p>Parameters:
      {%- for k, v in params %} <code>{{ k }}={{ v }}</code>{% if not loop.last %},{% endif %}
      {%- endfor %}
    </p>
    {%- endif %}
    <p>{{ ui_drift }}</p>
  </footer>

</div>
</body>
</html>
"""


def _environment() -> Environment:
    env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))
    return env


def render_html(result: AnalysisResult, ctx: ReportContext | None = None) -> str:
    """Render *result* as a single self-contained HTML document."""
    ctx = ctx or ReportContext()
    cov = result.coverage
    complete = harvest_is_complete(cov)

    bridges: list[dict[str, Any]] = []
    for rank, b in enumerate(sorted_bridges(result), start=1):
        inferred = is_inferred(result, ctx, b.member_id)
        bridges.append(
            {
                "rank": rank,
                "name": ctx.display_name(b.member_id),
                "inferred": inferred,
                "basis": basis_label(inferred),
                "reach": reach_display(b.reach, complete),
                "rarity": fmt(b.rarity),
                "betweenness": fmt(b.betweenness),
                "effective_size": fmt(b.effective_size, 2),
                "autonomy": fmt(b.autonomy),
                "composite": fmt(b.composite),
            }
        )

    clusters: list[dict[str, Any]] = []
    for c in sorted_clusters(result):
        clusters.append(
            {
                "cluster_id": c.cluster_id,
                "label": c.label,
                "n_members": len(c.member_ids),
                "stability": fmt(c.stability),
                "score_sum": fmt(c.score_sum),
                "members": [
                    {
                        "name": ctx.display_name(mid),
                        "inferred": is_inferred(result, ctx, mid),
                        "basis": basis_label(is_inferred(result, ctx, mid)),
                    }
                    for mid in c.member_ids
                ],
                "top_targets": [ctx.display_name(t) for t in c.top_targets],
            }
        )

    next_targets = [
        {
            "name": ctx.display_name(n.target_id),
            "priority": fmt(n.priority),
            "n_known_bridges": n.n_known_bridges,
            "n_clusters_touched": n.n_clusters_touched,
            "novelty": fmt(n.novelty),
        }
        for n in sorted_next_targets(result)
    ]

    context: dict[str, Any] = {
        "title": ctx.title,
        "generated_at": ctx.generated_at.isoformat(),
        "tool_version": ctx.tool_version,
        "header": list(header_rows(ctx)),
        "used_inferred": result.used_inferred_edges,
        "inference_banner": inference_banner(result),
        "review_statement": review_statement(result),
        "unadjudicated_count": result.unadjudicated_count,
        "cov": {
            "n_targets_total": cov.n_targets_total,
            "n_targets_harvested": cov.n_targets_harvested,
            "n_members_total": cov.n_members_total,
            "n_bridges": cov.n_bridges,
            "pct": fmt_pct(cov.fraction),
            "pct_width": f"{min(100.0, max(0.0, cov.fraction * 100)):.1f}",
            "complete": complete,
            "caveat": coverage_caveat(cov),
        },
        "bridges": bridges,
        "clusters": clusters,
        "next_targets": next_targets,
        "fingerprint": result.fingerprint or "not recorded",
        "params": sorted(result.params.items()),
        "ui_drift": UI_DRIFT_NOTICE,
    }

    # Redact the view model, then redact the rendered document. The first pass
    # keeps a contact detail out of an attribute where escaping might disguise
    # it; the second catches anything assembled inside the template.
    rendered = _environment().from_string(_TEMPLATE).render(**redact_value(context))
    return redact_text(rendered)


__all__ = ["render_html"]
