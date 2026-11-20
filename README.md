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
| `datasets` | Versioned source records converted into generation batches over the same segments, with mapping, retraction and conflict handling. |
| `vectors` | A connected nearest-neighbor graph with tombstones and an explicit traversal budget. |
| `paths` | Thompson-style path automata and product-graph reachability for label, sequence, alternative and star. |
| `spatial` | Immutable bounding hierarchy with closed-rectangle intersection and exact leaf predicates. |
| `morphology` | Nonnegative weighted lexical transducers with ranked ambiguous outputs. |

## Versioned datasets

`postingsegment.datasets.Dataset` turns source records that carry a version, a
revision and a retraction flag into queryable postings. It reuses the segment
format and the positional reader from `postingsegment.segments`.

```python
from postingsegment.datasets import Dataset

dataset = Dataset("./index")
result = dataset.ingest({
    "source": "cms",
    "batch": "batch-42",
    "mapping": {"id": "key", "version": "rev", "revision": "edit",
                "retracted": "withdrawn", "text": "body"},
    "records": [
        {"key": "a", "rev": 2, "edit": 1, "body": "red oak tree"},
        {"key": "b", "rev": 2, "edit": 1, "withdrawn": True},
    ],
})
snapshot = dataset.reader()          # pinned to this generation
print(snapshot.query("red oak", phrase=True))   # ['cms#a']
print(snapshot.versions())           # naive document version table
```

The request mapping names the source field for each standard document field.
`id`, `version` and `text` are required; `revision` and `retracted` are
optional. Once a source has published a mapping it must stay identical. A
result reports, for every record, the source id, the namespaced target
`source#id` document and the batch that published it.

* Each batch is one generation. Segments, the generation manifest and the
  `CURRENT` pointer are written in that order; the atomic pointer swap is the
  only commit point. A crash before it leaves the previous generation
  current, and `reclaim()` removes the unreachable files.
* The per-batch audit record is written after the swap. Losing that write
  loses an acknowledgement, not data: retrying the same batch identity and
  content returns the committed outcome instead of publishing again, and a
  reused identity with different content is rejected.
* Records inside one batch are folded to the last state per document, so a
  revision followed by a retraction in the same batch publishes only the
  retraction.
* Positions, document ids and live documents are published together. A
  retraction writes a tombstone into the same segment as its text change, so
  an updated body is never visible with an old deletion marker.
* `merge()` rewrites the current segments into one and publishes a new
  generation; interrupting it leaves the previous one intact.
* Snapshots keep a shared lock on a pin file. `reclaim()` refuses to delete
  segments while any snapshot is open, so a reader that holds an old view
  keeps seeing its original data.
* Older versions, same-version different content and cross-source overwrites
  are conflicts. With `conflict="reject"` (the default) the whole batch is
  refused and nothing becomes visible; with `conflict="quarantine"` the
  conflicting records are isolated in `quarantine/` and the rest publish. No
  cross-source entity merging is performed.

`tests/test_datasets.py` cross-checks post-revision term and phrase queries
against an independent naive document version table.

## Request examples

Run `python -m postingsegment.examples` to print valid JSON requests. The output is generated locally and contains no credentials.

```sh
./.venv/bin/python -m postingsegment.examples > requests.json
curl -s http://127.0.0.1:8080/capabilities
```

Use one operation request from that output as the JSON body for its `/v1/<operation>` endpoint. The `demo` command executes every operation and prints their computed results.

## Supported scope

Text indexes use a single writer per directory; old segments remain available and automatic compaction is not implemented. Vector search is approximate when its traversal budget is truncated and is not presented as exact top-k. Rectangles are planar and finite. Morphology rejects epsilon cycles. None of these libraries infer business entity identity; `datasets` keeps each source's documents in its own namespace and never merges records across sources.

Dataset generations accumulate segments until `merge()` is called. `reclaim()` deletes only files unreachable from the current generation, and only while no snapshot holds a pin, so readers that started on an older generation keep their complete view. Batch and quarantine audit files are retained.

Runtime state uses explicit temporary directories or caller-provided library objects. Service examples are per-request computations; use the library interfaces for persistent state and long-lived readers.

## Dependencies

Pinned Python dependencies are listed in `requirements.txt`; the development test runner is pinned in `requirements-dev.txt`. System libraries or a native compiler, when required above, are detected at runtime. No accounts, network connection strings, tokens or private-key files are required.
