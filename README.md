# postingsegment-engine

Multi-model retrieval libraries.

Python 3.11 or newer. The primary interfaces are importable component libraries and a local JSON HTTP service.

## Install

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pip install --no-deps -e .
```

## Run

```sh
./.venv/bin/python -m postingsegment demo
./.venv/bin/python -m postingsegment serve --port 8080
./.venv/bin/python -m pytest -q
```

The server binds only to `127.0.0.1`. `GET /health` reports process readiness; `GET /capabilities` lists operations.
Each component has `POST /v1/<operation>` accepting its JSON request object. Responses contain `result` or an explicit error. The service accepts bodies up to 1 MB and at most four concurrent operation handlers. It does not expose a shell, arbitrary file paths or remote URLs.

## Components

| Operation | Behavior |
|---|---|
| `segments` | Persistent immutable text segments, positional phrase matching, updates/deletes and snapshot readers. |
| `vectors` | A connected nearest-neighbor graph with tombstones and an explicit traversal budget. |
| `density` | DBSCAN-style density clustering over a vector index with evidence-aware labels and versioned snapshots. |
| `paths` | Thompson-style path automata and product-graph reachability for label, sequence, alternative and star. |
| `spatial` | Immutable bounding hierarchy with closed-rectangle intersection and exact leaf predicates. |
| `morphology` | Nonnegative weighted lexical transducers with ranked ambiguous outputs. |

### Density clustering

`density` takes sample identities with their vectors, a distance threshold
`epsilon`, a `min_samples` density gate and an optional traversal `budget`.
It reports per-sample membership (`core`, `border`, `noise`,
`unconfirmed`), each cluster's size and member set, and within-cluster
distance distributions (pairwise and nearest-neighbor squared-distance
stats plus a histogram).

The graph is only a candidate channel. A traversal truncated by the budget
is never interpreted as "this sample has no neighbors": labels are confirmed
only on complete neighborhood evidence, and unsupported samples are reported
as `unconfirmed` rather than false noise. The response carries `exact`,
`truncated_searches` and `visited_total`, and each assignment flags whether
its neighborhood search completed.

All memberships and statistics are computed against one immutable snapshot
identified by a version digest. Batch corrections (`add` / `delete` /
`replace` via `VectorIndex.apply`) are atomic: any failed operation rolls the
whole batch back, so an update failure never publishes results that mix
sample versions. Acceptance compares cluster member sets (frozen sets), not
cluster numbers; on small data the results are cross-checked against an
independent exhaustive O(n²) neighborhood reference. The analysis enforces a
working-set limit and reports traversal cost so recall shortfalls on highly
clustered data stay visible.

## Request examples

Run `python -m postingsegment.examples` to print valid JSON requests. The output is generated locally and contains no credentials.

```sh
./.venv/bin/python -m postingsegment.examples > requests.json
curl -s http://127.0.0.1:8080/capabilities
```

Use one operation request from that output as the JSON body for its `/v1/<operation>` endpoint. The `demo` command executes all five operations and prints their computed results.

## Supported scope

Text indexes use a single writer per directory; old segments remain available and automatic compaction is not implemented. Vector search is approximate when its traversal budget is truncated and is not presented as exact top-k. Density clustering is likewise approximate under truncation: unconfirmed samples are labeled explicitly and noise is asserted only from complete neighborhood evidence. Rectangles are planar and finite. Morphology rejects epsilon cycles. None of these libraries infer business entity identity.

Runtime state uses explicit temporary directories or caller-provided library objects. Service examples are per-request computations; use the library interfaces for persistent state and long-lived readers.

## Dependencies

Pinned Python dependencies are listed in `requirements.txt`; the development test runner is pinned in `requirements-dev.txt`. System libraries or a native compiler, when required above, are detected at runtime. No accounts, network connection strings, tokens or private-key files are required.
