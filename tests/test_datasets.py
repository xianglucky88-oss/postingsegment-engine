import json
import random

import pytest

from postingsegment import datasets, segments

MAPPING = {"id": "key", "version": "rev", "revision": "edit", "retracted": "withdrawn", "text": "body"}


def ingest(dataset, records, batch=None, mapping=MAPPING, source="cms", **extra):
    if isinstance(records, dict):
        records = [records]
    request = {"source": source, "records": records, **extra}
    if mapping is not None:
        request["mapping"] = mapping
    if batch is not None:
        request["batch"] = batch
    return dataset.ingest(request)


def rec(key, rev, body=None, edit=0, withdrawn=False):
    value = {"key": key, "rev": rev, "edit": edit}
    if body is not None:
        value["body"] = body
    if withdrawn:
        value["withdrawn"] = True
    return value


class Naive:
    """Independent document version table used to cross-check the index."""

    def __init__(self):
        self.docs = {}

    def apply(self, source, record):
        doc_id = source + "#" + str(record["key"])
        current = self.docs.get(doc_id)
        order = (datasets._version_key(record["rev"]), record.get("edit", 0))
        state = {
            "order": order,
            "version": record["rev"],
            "revision": record.get("edit", 0),
            "live": not record.get("withdrawn", False),
            "words": segments.tokens(record.get("body") or ""),
        }
        if current is not None:
            if order < current["order"]:
                return
            if order == current["order"]:
                identical = (
                    current["live"] == state["live"]
                    and current["words"] == state["words"]
                )
                if not identical:
                    return
                self.docs[doc_id] = dict(current, order=order)
                return
        self.docs[doc_id] = state

    def query(self, text, phrase=False):
        terms = segments.tokens(text)
        if not terms:
            return []
        found = []
        for doc_id, doc in self.docs.items():
            if not doc["live"]:
                continue
            positions = []
            for term in terms:
                positions.append([i for i, word in enumerate(doc["words"]) if word == term])
            if any(not p for p in positions):
                continue
            if not phrase:
                found.append(doc_id)
                continue
            for start in positions[0]:
                if all(start + k in positions[k] for k in range(len(terms))):
                    found.append(doc_id)
                    break
        return sorted(found)


def test_conversion_records_documents_and_batches(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    result = ingest(dataset, [rec("a", 1, "red oak tree"), rec("b", 1, "oak red tree")], batch="b1")
    assert result["committed"] and result["batch"] == "b1"
    assert result["mapping"] == MAPPING
    pairs = {(entry["source_id"], entry["doc_id"], entry["batch"]) for entry in result["accepted"]}
    assert pairs == {("a", "cms#a", "b1"), ("b", "cms#b", "b1")}
    snapshot = dataset.reader()
    assert snapshot.query("red oak", phrase=True) == ["cms#a"]
    assert snapshot.query("oak red") == ["cms#a", "cms#b"]
    audit = dataset.batch("b1", "cms")
    assert {entry["text"] for entry in audit["accepted"]} == {"red oak tree", "oak red tree"}
    assert {entry["doc_id"] for entry in audit["accepted"]} == {"cms#a", "cms#b"}
    snapshot.close()


def test_queries_match_naive_version_table_after_revisions(tmp_path):
    rng = random.Random(11)
    vocabulary = ["red", "oak", "tree", "ash", "elm", "birch", "maple"]
    dataset = datasets.Dataset(tmp_path)
    naive = Naive()
    dataset.ingest({"source": "cms", "mapping": MAPPING, "records": [], "batch": "empty"})
    for step in range(12):
        records = []
        for _ in range(rng.randint(1, 4)):
            key = rng.choice("abcdef")
            version = rng.randint(1, 4)
            revision = rng.randint(0, 2)
            if rng.random() < 0.2:
                records.append(rec(key, version, edit=revision, withdrawn=True))
            else:
                body = " ".join(rng.choice(vocabulary) for _ in range(rng.randint(1, 6)))
                records.append(rec(key, version, body, edit=revision))
        outcome = ingest(dataset, records, batch="b%d" % step, conflict="quarantine")
        assert outcome["committed"] or outcome["counts"]["quarantined"] == 0
        for record in records:
            naive.apply("cms", record)
        snapshot = dataset.reader()
        versions = snapshot.versions()
        assert set(versions) == set(naive.docs)
        for doc_id, doc in naive.docs.items():
            assert versions[doc_id]["version"] == doc["version"]
            assert versions[doc_id]["revision"] == doc["revision"]
            assert versions[doc_id]["retracted"] == (not doc["live"])
        for text in ("red", "oak tree", "red oak", "oak oak", "elm birch", "tree"):
            for phrase in (False, True):
                assert snapshot.query(text, phrase) == naive.query(text, phrase), (step, text, phrase)
        snapshot.close()
        if step % 3 == 2:
            merged = dataset.merge()
            assert merged["merged"] in (True, False)
            after = dataset.reader()
            for text in ("red", "oak tree", "red oak", "elm birch"):
                for phrase in (False, True):
                    assert after.query(text, phrase) == naive.query(text, phrase), ("merged", step, text, phrase)
            after.close()
            assert dataset.reclaim()["reclaimed"] is True


def test_revision_then_retraction_leaves_no_stale_or_half_state(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak"), rec("b", 1, "oak red")], batch="b1")
    old = dataset.reader()
    result = ingest(dataset, [rec("a", 2, "red oak tree"), rec("b", 2, withdrawn=True)], batch="b2")
    assert result["counts"] == {"records": 2, "accepted": 2, "unchanged": 0, "quarantined": 0}
    current = dataset.reader()
    assert current.versions()["cms#a"]["version"] == 2
    assert current.versions()["cms#a"]["retracted"] is False
    assert current.versions()["cms#b"]["retracted"] is True
    assert current.query("red oak", phrase=True) == ["cms#a"]
    assert current.query("red") == ["cms#a"]
    assert current.query("oak red") == ["cms#a"]
    assert current.query("oak red", phrase=True) == []
    # The pinned reader still sees the original generation in full.
    assert old.query("red oak", phrase=True) == ["cms#a"]
    assert old.query("oak red") == ["cms#a", "cms#b"]
    assert old.generation != current.generation
    old.close()
    current.close()


def test_stale_and_conflicting_versions_are_rejected(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 2, "red oak")], batch="b1")
    before = dataset.reader().generation
    with pytest.raises(datasets.SourceConflict) as caught:
        ingest(dataset, [rec("a", 1, "old body")], batch="b2")
    assert caught.value.result["accepted"] == []
    assert caught.value.result["quarantined"][0]["reason"] == "stale version"
    with pytest.raises(datasets.SourceConflict):
        ingest(dataset, [rec("a", 2, "different body")], batch="b3")
    assert dataset.reader().generation == before
    assert dataset.reader().query("old") == []
    assert dataset.reader().query("red oak", phrase=True) == ["cms#a"]
    # A higher revision of the same version is a legitimate correction.
    correction = ingest(dataset, [rec("a", 2, edit=1, body="red oak ash")], batch="b4")
    assert correction["counts"]["accepted"] == 1
    after = dataset.reader()
    assert after.query("ash") == ["cms#a"]
    assert after.query("old") == []
    after.close()


def test_conflict_quarantine_isolates_records(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 2, "red oak")], batch="b1")
    result = ingest(
        dataset,
        [rec("a", 1, "stale"), rec("c", 1, "elm tree")],
        batch="b2",
        conflict="quarantine",
    )
    assert result["committed"] is True
    assert result["counts"] == {"records": 2, "accepted": 1, "unchanged": 0, "quarantined": 1}
    assert [entry["doc_id"] for entry in result["quarantined"]] == ["cms#a"]
    assert dataset.reader().versions()["cms#a"]["version"] == 2
    assert dataset.reader().query("red oak", phrase=True) == ["cms#a"]
    assert dataset.reader().query("stale") == []
    assert dataset.reader().query("elm") == ["cms#c"]
    quarantined = dataset.quarantine("b2", "cms")
    assert quarantined["conflicts"][0]["reason"] == "stale version"
    assert quarantined["conflicts"][0]["published"]["version"] == 2


def test_sources_are_isolated_and_mappings_are_pinned(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1", source="cms")
    ingest(dataset, [rec("a", 1, "elm tree")], batch="b2", source="archive")
    snapshot = dataset.reader()
    assert snapshot.query("red") == ["cms#a"]
    assert snapshot.query("elm") == ["archive#a"]
    assert set(snapshot.versions()) == {"cms#a", "archive#a"}
    with pytest.raises(datasets.MappingError):
        ingest(dataset, [rec("a", 2, "x")], batch="b3", source="cms",
               mapping={"id": "key", "version": "rev", "text": "content"})
    assert dataset.mappings()["cms"] == MAPPING


@pytest.mark.parametrize("stage", ["segment", "generation", "pointer"])
def test_failure_before_pointer_keeps_previous_generation(tmp_path, stage):
    first = datasets.Dataset(tmp_path)
    ingest(first, [rec("a", 1, "red oak")], batch="b1")
    before = first.reader().generation
    first.reader().close()

    def fault(point):
        if point == stage:
            raise RuntimeError("injected %s failure" % point)

    broken = datasets.Dataset(tmp_path, fault=fault)
    with pytest.raises(RuntimeError):
        ingest(broken, [rec("a", 2, "elm tree")], batch="b2")
    healthy = datasets.Dataset(tmp_path)
    snapshot = healthy.reader()
    assert snapshot.generation == before
    assert snapshot.query("red oak", phrase=True) == ["cms#a"]
    assert snapshot.query("elm") == []
    assert snapshot.versions()["cms#a"]["version"] == 1
    snapshot.close()
    reclaimed = healthy.reclaim()
    assert reclaimed["reclaimed"] is True


@pytest.mark.parametrize("stage", ["published", "batch"])
def test_lost_acknowledgement_after_commit_replays(tmp_path, stage):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")

    def fault(point):
        if point == stage:
            raise RuntimeError("injected %s failure" % point)

    broken = datasets.Dataset(tmp_path, fault=fault)
    with pytest.raises(RuntimeError):
        ingest(broken, [rec("a", 2, "elm tree"), rec("c", 1, "red oak")], batch="b2")
    healthy = datasets.Dataset(tmp_path)
    committed = healthy.reader()
    assert committed.query("elm") == ["cms#a"]
    assert committed.query("red oak", phrase=True) == ["cms#c"]
    committed.close()
    retry = healthy.ingest({"source": "cms", "batch": "b2", "records": [rec("a", 2, "elm tree"), rec("c", 1, "red oak")]})
    assert retry["replayed"] is True
    assert retry["committed"] is False
    assert retry["counts"]["accepted"] == 0
    again = healthy.reader()
    assert again.query("elm") == ["cms#a"]
    assert again.query("red oak", phrase=True) == ["cms#c"]
    assert again.versions()["cms#a"]["batch"] == "b2"
    again.close()
    with pytest.raises(datasets.RecordError):
        healthy.ingest({"source": "cms", "batch": "b2", "records": [rec("a", 3, "other")]})


def test_merge_is_a_publication_and_interruption_preserves_view(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    ingest(dataset, [rec("b", 1, "oak red")], batch="b2")
    before = dataset.reader()
    before_generation = before.generation
    assert len(before.segment_names) == 2
    assert before.query("red oak", phrase=True) == ["cms#a"]
    assert before.query("oak red") == ["cms#a", "cms#b"]
    before.close()

    def fault(point):
        if point == "pointer":
            raise RuntimeError("injected merge failure")

    broken = datasets.Dataset(tmp_path, fault=fault)
    with pytest.raises(RuntimeError):
        broken.merge()
    healthy = datasets.Dataset(tmp_path)
    assert healthy.reader().generation == before_generation
    assert len(healthy.reader().segment_names) == 2
    merged = healthy.merge()
    assert merged["merged"] is True and merged["segments"] == 1
    after = healthy.reader()
    assert len(after.segment_names) == 1
    assert after.query("red oak", phrase=True) == ["cms#a"]
    assert after.query("oak red") == ["cms#a", "cms#b"]
    after.close()


def test_reclaim_waits_for_readers_then_removes_old_segments(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    ingest(dataset, [rec("a", 2, "red oak tree")], batch="b2")
    old = dataset.reader()
    assert len(old.segment_names) == 2
    assert dataset.merge()["merged"] is True
    busy = dataset.reclaim()
    assert busy["busy"] is True and busy["reclaimed"] is False
    assert old.query("red oak", phrase=True) == ["cms#a"]
    old.close()
    reclaimed = dataset.reclaim()
    assert reclaimed["busy"] is False
    assert len(reclaimed["removed_segments"]) >= 2
    snapshot = dataset.reader()
    assert snapshot.query("red oak", phrase=True) == ["cms#a"]
    assert snapshot.query("tree") == ["cms#a"]
    snapshot.close()
    with pytest.raises(datasets.StateError):
        dataset.reader("g-" + "0" * 32)


def test_repeated_word_positions_survive_merge(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "go go now"), rec("b", 1, "go now")], batch="b1")
    ingest(dataset, [rec("c", 1, "now go go")], batch="b2")
    assert dataset.reader().query("go go", phrase=True) == ["cms#a", "cms#c"]
    dataset.merge()
    snapshot = dataset.reader()
    assert snapshot.query("go go", phrase=True) == ["cms#a", "cms#c"]
    assert snapshot.query("go go", phrase=False) == ["cms#a", "cms#b", "cms#c"]
    snapshot.close()


def test_request_validation_and_batch_identity(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    with pytest.raises(datasets.MappingError):
        ingest(dataset, rec("a", 1, "x"), mapping={"id": "key", "version": "rev"})
    with pytest.raises(datasets.MappingError):
        ingest(dataset, rec("a", 1, "x"), mapping={"id": "key", "version": "rev", "text": "body", "extra": "x"})
    with pytest.raises(datasets.MappingError):
        ingest(dataset, rec("a", 1, "x"), mapping={"id": "key", "version": "rev", "text": "key"})
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [rec("a", 2, "x")], batch="b1")
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [rec("a", 2, "x")], batch="bad batch!")
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [{"key": "a", "rev": 2}])
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [rec("a", 2, "x" * 100001)])
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [rec("a", 2, "x")], conflict="ignore")
    snapshot = dataset.reader()
    assert snapshot.query("red oak", phrase=True) == ["cms#a"]
    snapshot.close()


def test_snapshot_detaches_state(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    snapshot = dataset.reader()
    snapshot.documents["cms#a"]["version"] = 99
    assert snapshot.get("cms#a")["version"] == 1
    snapshot.close()
    with pytest.raises(datasets.StateError):
        snapshot.query("red")


def test_same_batch_revision_then_retraction(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    result = ingest(
        dataset,
        [rec("a", 1, "red oak tree"), rec("a", 2, "red oak"), rec("a", 2, withdrawn=True)],
        batch="b1",
    )
    assert result["counts"] == {"records": 3, "accepted": 1, "unchanged": 0, "quarantined": 0}
    snapshot = dataset.reader()
    assert snapshot.versions()["cms#a"] == {"version": 2, "revision": 0, "retracted": True, "batch": "b1"}
    assert snapshot.query("red") == []
    assert snapshot.query("oak") == []
    snapshot.close()

    # The same version twice with different payloads is ambiguous.
    with pytest.raises(datasets.SourceConflict):
        ingest(dataset, [rec("b", 1, "elm")], batch="b2")
        ingest(dataset, [rec("b", 1, "ash")], batch="b3")
    assert dataset.reader().query("elm") == ["cms#b"]
    assert dataset.reader().query("ash") == []


def test_batch_identity_is_namespaced_by_source(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="shared", source="cms")
    ingest(dataset, [rec("a", 1, "elm tree")], batch="shared", source="archive")
    assert dataset.batch("shared", "cms")["source"] == "cms"
    assert dataset.batch("shared", "archive")["source"] == "archive"
    replay = ingest(dataset, [rec("a", 1, "red oak")], batch="shared", source="cms")
    assert replay["replayed"] is True and replay["counts"]["accepted"] == 0
    with pytest.raises(datasets.RecordError):
        ingest(dataset, [rec("a", 1, "red oak tree")], batch="shared", source="cms")
    snapshot = dataset.reader()
    assert snapshot.versions()["cms#a"]["batch"] == "shared"
    assert snapshot.versions()["archive#a"]["batch"] == "shared"
    assert snapshot.query("elm") == ["archive#a"]
    snapshot.close()


def test_api_dataset_operation_reports_versions_and_pinned_view():
    from postingsegment import api

    payload = api.dataset_call(api.EXAMPLES["datasets"])
    current = payload["queries"]
    assert current[0] == {"text": "red oak", "phrase": True, "matches": ["cms#b"]}
    assert current[1] == {"text": "oak", "phrase": False, "matches": ["cms#b"]}
    pinned = payload["pinned"]
    assert pinned["queries"][0]["matches"] == ["cms#a"]
    assert pinned["queries"][1]["matches"] == ["cms#a", "cms#b"]
    assert payload["versions"]["cms#a"]["retracted"] is True
    assert payload["merge"]["merged"] is True
    assert payload["generation"] == payload["merge"]["generation"]


def test_api_rejects_unbounded_requests():
    from postingsegment import api

    with pytest.raises(ValueError):
        api.dataset_call({"batches": []})
    with pytest.raises(ValueError):
        api.dataset_call({"batches": [{"source": "x", "mapping": MAPPING, "records": []}] * 17})


def test_concurrent_ingest_is_serialized(tmp_path):
    import multiprocessing as mp

    ctx = mp.get_context("fork")

    def worker(path, index):
        dataset = datasets.Dataset(path)
        for step in range(1, 6):
            ingest(dataset, [rec("shared", step, "red oak %d" % step)], batch="w%d-%d" % (index, step),
                   conflict="quarantine")

    processes = [ctx.Process(target=worker, args=(str(tmp_path), index)) for index in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    snapshot = datasets.Dataset(tmp_path).reader()
    state = snapshot.versions()["cms#shared"]
    assert state["version"] == 5
    assert state["retracted"] is False
    assert snapshot.query("red oak", phrase=True) == ["cms#shared"]
    snapshot.close()


def test_cross_process_snapshot_blocks_reclaim(tmp_path):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    ingest(dataset, [rec("a", 2, "red oak tree")], batch="b2")
    dataset.merge()
    ready = ctx.Event()
    release = ctx.Event()

    def holder(path):
        snapshot = datasets.Dataset(path).reader()
        ready.set()
        release.wait(20)
        assert snapshot.query("red oak", phrase=True) == ["cms#a"]
        snapshot.close()

    process = ctx.Process(target=holder, args=(str(tmp_path),))
    process.start()
    assert ready.wait(20)
    assert dataset.reclaim()["busy"] is True
    release.set()
    process.join(20)
    assert process.exitcode == 0
    assert dataset.reclaim()["busy"] is False


def test_republish_after_retraction_and_empty_text(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak"), rec("e", 1, "")], batch="b1")
    snapshot = dataset.reader()
    assert snapshot.query("red oak", phrase=True) == ["cms#a"]
    assert snapshot.versions()["cms#e"]["retracted"] is False
    snapshot.close()
    ingest(dataset, [rec("a", 2, withdrawn=True)], batch="b2")
    assert dataset.reader().query("red") == []
    # Publication after retraction must clear the tombstone and the old body.
    ingest(dataset, [rec("a", 3, "elm birch")], batch="b3")
    snapshot = dataset.reader()
    assert snapshot.versions()["cms#a"] == {"version": 3, "revision": 0, "retracted": False, "batch": "b3"}
    assert snapshot.query("elm birch", phrase=True) == ["cms#a"]
    assert snapshot.query("red") == []
    assert snapshot.query("oak") == []
    snapshot.close()
    dataset.merge()
    merged = dataset.reader()
    assert merged.query("elm birch", phrase=True) == ["cms#a"]
    assert merged.query("red") == []
    assert set(merged.versions()) == {"cms#a", "cms#e"}
    merged.close()


def test_retracted_document_does_not_leak_through_old_segment(tmp_path):
    dataset = datasets.Dataset(tmp_path)
    ingest(dataset, [rec("a", 1, "red oak")], batch="b1")
    ingest(dataset, [rec("b", 1, "red elm")], batch="b2")
    ingest(dataset, [rec("a", 2, withdrawn=True)], batch="b3")
    snapshot = dataset.reader()
    assert len(snapshot.segment_names) == 3
    assert snapshot.query("red") == ["cms#b"]
    assert snapshot.query("oak") == []
    assert snapshot.query("red oak", phrase=True) == []
    snapshot.close()
