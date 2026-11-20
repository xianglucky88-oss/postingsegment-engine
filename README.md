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
| `paths` | Thompson-style path automata and product-graph reachability for label, sequence, alternative and star. |
| `spatial` | Immutable bounding hierarchy with closed-rectangle intersection and exact leaf predicates. |
| `coverage` | Exact footprint-coverage and reference representativeness: overlap-free union on an exhaustive compressed grid, effective weighted denominators, duplicate-identity merging, gap attribution and split/merge conservation. |
| `morphology` | Nonnegative weighted lexical transducers with ranked ambiguous outputs. |

## Request examples

Run `python -m postingsegment.examples` to print valid JSON requests. The output is generated locally and contains no credentials.

```sh
./.venv/bin/python -m postingsegment.examples > requests.json
curl -s http://127.0.0.1:8080/capabilities
```

Use one operation request from that output as the JSON body for its `/v1/<operation>` endpoint. The `demo` command executes all five operations and prints their computed results.

## Supported scope

Text indexes use a single writer per directory; old segments remain available and automatic compaction is not implemented. Vector search is approximate when its traversal budget is truncated and is not presented as exact top-k. Rectangles are planar and finite. Morphology rejects epsilon cycles. None of these libraries infer business entity identity.

Coverage analysis is exact while its compressed grids fit the request budget; otherwise the affected zones and the independent conservation grid are reported as `incomplete` rather than counted as zero coverage. Boundary-only contact contributes no area, zero-area reference units are flagged, unknown weights are excluded from weighted denominators and listed separately, and records sharing a source identity are merged so duplicates never enlarge the covered area. Every coverage gap is attributed to missing observations or missing reference material, and per-zone totals are reconciled against an independent whole-extent grid.

Runtime state uses explicit temporary directories or caller-provided library objects. Service examples are per-request computations; use the library interfaces for persistent state and long-lived readers.

## Dependencies

Pinned Python dependencies are listed in `requirements.txt`; the development test runner is pinned in `requirements-dev.txt`. System libraries or a native compiler, when required above, are detected at runtime. No accounts, network connection strings, tokens or private-key files are required.
