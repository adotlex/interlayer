"""Stage 4 -- the weighted person-person graph.

Weighting matters more here than any later algorithm choice: a naive bipartite
projection let one very large employer produce 65% of all edges, and the
corrections in :mod:`interlayer.graph.weights` cut its share of edge weight from
60.8% to 5.4%. Everything downstream -- clustering, PageRank, the report -- is
reading the weights this stage writes.

Layout:

* :mod:`interlayer.graph.weights` -- the weighting model and its swappable
  firm-size damping strategies.
* :mod:`interlayer.graph.sizes` -- real-world headcounts from the gazetteer.
* :mod:`interlayer.graph.projection` -- co-tenure-filtered bipartite projection.
* :mod:`interlayer.graph.disparity` -- Serrano backbone plus the top-k rescue.
* :mod:`interlayer.graph.observed` -- Tier-2 bridges, never pruned.
* :mod:`interlayer.graph.build` -- ``run(cfg)`` and the in-memory ``build_graph``.
"""

from interlayer.graph.build import STATS_FILENAME, GraphResult, build_graph, run

__all__ = ["STATS_FILENAME", "GraphResult", "build_graph", "run"]
