"""Versioned document ingest on top of immutable posting segments.

The pipeline reuses the segment format and positional query reader from
:mod:`postingsegment.segments`.  A *generation* is one publication: it names
the ordered list of segment files that form the live view, the per-document
source state used for conflict detection, and the batches that generation
accepted.

Publication order is segment file, generation manifest, then the atomic
replacement of ``CURRENT``.  The swap is the only commit point, so a process
that dies at any step before it leaves the previous generation untouched and
at most orphaned files that :meth:`Dataset.reclaim` removes.  The per-batch
audit record is written after the swap: a crash there can lose an
acknowledgement, never a visible half batch, and a retry with the same batch
identity is answered from the committed generation.

Documents are namespaced by source.  A record from one source can never
update a document created by another source, and no entity resolution is
attempted.

Layout of a dataset directory::

    CURRENT                 generation id of the live view
    LOCK                    writer/reader coordination
    mappings.json           declared source-to-document field mappings
    segments/segment-*.json immutable ``{doc_id: [tokens] | null}`` files
    generations/g-*.json    manifests referenced by CURRENT and its lineage
    batches/<key>.json      accepted source records, keyed by source and batch
    quarantine/<key>.json   explicitly isolated conflicting records
"""

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from . import segments

#: Standard document fields.  A request mapping names the source field that
#: carries each of these values.
STANDARD_FIELDS = ("id", "version", "revision", "retracted", "text")
REQUIRED_FIELDS = ("id", "version", "text")

MAX_RECORDS = 1000
MAX_TEXT = 100000
MAX_BATCH_CHARS = 2000000
MAX_DOCS = 50000
MAX_SEGMENTS = 4096
MAX_SOURCE_ID = 256
MAX_SOURCE = 128
MAX_VERSION = 256
MAX_GENERATION_HISTORY = 256

_SEGMENT_RE = re.compile(r"segment-[0-9a-f]{32}\.json\Z")
_GENERATION_RE = re.compile(r"g-[0-9a-f]{32}\Z")
_BATCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class MappingError(ValueError):
    """The declared source-to-document field mapping is unusable."""


class RecordError(ValueError):
    """A source record violates the declared mapping or a size limit."""


class SourceConflict(ValueError):
    """A batch conflicts with previously published source versions."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


class StateError(ValueError):
    """A file on disk does not describe a valid dataset state."""


# --------------------------------------------------------------------------
# small helpers


def _version_key(version):
    """Order versions naturally; digit runs compare numerically.

    ``5`` and ``"5"`` (or ``"05"``) are the same version.  Strings such as
    ``"v9"`` and ``"v10"`` order numerically as a reader would expect.
    """
    parts = re.findall(r"\d+|\D+", str(version))
    return tuple((1, int(p)) if p.isdigit() else (0, p.casefold()) for p in parts)


def _order(record):
    return (_version_key(record["version"]), record["revision"])


def _doc_id(source, source_id):
    return quote(source, safe="") + "#" + quote(source_id, safe="")


def _write_json(path, value):
    """Write JSON atomically and durably; never leaves a partial file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".write-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _fsync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as error:
        raise StateError("unreadable state file: %s" % Path(path).name) from error


def _scalar(value, field):
    if isinstance(value, bool):
        raise RecordError("%s must not be a boolean" % field)
    if isinstance(value, (int, str)):
        return value
    raise RecordError("%s must be a string or integer" % field)


def _validate_mapping(mapping):
    if not isinstance(mapping, dict) or not mapping:
        raise MappingError("mapping must be a non-empty object")
    unknown = sorted(set(mapping) - set(STANDARD_FIELDS))
    if unknown:
        raise MappingError("unknown standard field: %s" % ", ".join(unknown))
    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        raise MappingError("mapping is missing: %s" % ", ".join(missing))
    declared = list(mapping.values())
    for name, field in mapping.items():
        if not isinstance(field, str) or not field or _CONTROL_RE.search(field):
            raise MappingError("mapping for %s must be a non-empty field name" % name)
    if len(set(declared)) != len(declared):
        raise MappingError("mapping must use distinct source fields")
    return {name: mapping[name] for name in mapping}


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.casefold() in ("true", "false", "1", "0"):
        return value.casefold() in ("true", "1")
    raise RecordError("retracted must be true/false, 1/0 or \"true\"/\"false\"")


def _normalize_record(raw, source, mapping):
    if not isinstance(raw, dict):
        raise RecordError("each record must be an object")
    for standard in ("id", "version"):
        if mapping[standard] not in raw:
            raise RecordError("record is missing mapped field %r" % mapping[standard])

    identity = _scalar(raw[mapping["id"]], "id")
    identity = str(identity)
    if not identity or len(identity) > MAX_SOURCE_ID or _CONTROL_RE.search(identity):
        raise RecordError("source id is empty, too long or contains control characters")

    version = _scalar(raw[mapping["version"]], "version")
    version_text = str(version)
    if not version_text or len(version_text) > MAX_VERSION or _CONTROL_RE.search(version_text):
        raise RecordError("version is empty, too long or contains control characters")

    revision = 0
    if "revision" in mapping:
        if mapping["revision"] in raw:
            value = raw[mapping["revision"]]
            if isinstance(value, bool):
                raise RecordError("revision must be a nonnegative integer")
            if isinstance(value, int):
                revision = value
            elif isinstance(value, str) and value.isdigit() and len(value) <= 18:
                revision = int(value)
            else:
                raise RecordError("revision must be a nonnegative integer")
            if revision < 0:
                raise RecordError("revision must be a nonnegative integer")

    retracted = _parse_bool(raw[mapping["retracted"]]) if "retracted" in mapping and mapping["retracted"] in raw else False

    if mapping["text"] in raw:
        text = raw[mapping["text"]]
    elif retracted:
        text = ""
    else:
        raise RecordError("record is missing mapped field %r" % mapping["text"])
    if not isinstance(text, str):
        raise RecordError("text must be a string")
    if len(text) > MAX_TEXT:
        raise RecordError("text exceeds the %d character limit" % MAX_TEXT)
    if retracted and text:
        raise RecordError("a retraction must not carry replacement text")

    return {
        "source": source,
        "source_id": identity,
        "doc_id": _doc_id(source, identity),
        "version": version,
        "revision": revision,
        "retracted": retracted,
        "text": text,
    }


def _fold_records(records):
    """Collapse repeated document ids in one batch to their final state.

    The last record in the batch wins, provided it is not older than a record
    already seen for that document.  This lets one batch carry a revision and
    the retraction that follows it without leaving a partial state behind.
    """
    folded, order = {}, []
    for record in records:
        doc_id = record["doc_id"]
        previous = folded.get(doc_id)
        if previous is not None and _order(record) < _order(previous):
            continue
        if previous is None:
            order.append(doc_id)
        folded[doc_id] = record
    return [folded[doc_id] for doc_id in order]


def _validate_record_shape(record, source):
    if not isinstance(record, dict):
        raise StateError("document state is malformed")
    identity = record.get("source_id")
    if record.get("source") != source or not isinstance(identity, str) or not identity:
        raise StateError("document state does not match its source")
    if record.get("doc_id") != _doc_id(source, identity):
        raise StateError("document state has an inconsistent doc id")
    if not isinstance(record.get("version"), (str, int)) or isinstance(record.get("version"), bool):
        raise StateError("document state has an invalid version")
    if not isinstance(record.get("revision"), int) or isinstance(record.get("revision"), bool) or record["revision"] < 0:
        raise StateError("document state has an invalid revision")
    if not isinstance(record.get("retracted"), bool):
        raise StateError("document state has an invalid retraction flag")
    if not isinstance(record.get("batch"), str):
        raise StateError("document state is missing its batch")
    return record


# --------------------------------------------------------------------------
# dataset


class Snapshot:
    """A pinned, read-only view of one published generation.

    The snapshot keeps a shared lock on the dataset's ``PIN`` file for its
    whole lifetime, so :meth:`Dataset.reclaim` cannot delete segments while
    the view is open.  Queries always see exactly the generation that was
    current when the snapshot was opened, even if writers publish or merge
    afterwards.
    """

    def __init__(self, generation_id, manifest, documents, reader, pin_fd):
        self._generation_id = generation_id
        self._manifest = manifest
        self._documents = documents
        self._reader = reader
        self._pin_fd = pin_fd
        self._closed = False

    @property
    def generation(self):
        return self._generation_id

    @property
    def documents(self):
        return copy.deepcopy(self._documents)

    @property
    def segment_names(self):
        return tuple(self._manifest["segments"]) if self._manifest else ()

    @property
    def open(self):
        return not self._closed

    def get(self, doc_id):
        state = self._documents.get(doc_id)
        return copy.deepcopy(state) if state is not None else None

    def versions(self):
        """Naive ``doc_id -> version state`` table for cross-checking."""
        return {
            doc_id: {
                "version": state["version"],
                "revision": state["revision"],
                "retracted": state["retracted"],
                "batch": state["batch"],
            }
            for doc_id, state in self._documents.items()
        }

    def query(self, text, phrase=False):
        """Return matching live document ids in sorted order.

        Matching rules are identical to :class:`postingsegment.segments.Reader`:
        all terms must occur in the document and, with ``phrase``, at
        consecutive token positions, of which repeated terms need the full
        number of occurrences.
        """
        if self._closed:
            raise StateError("snapshot is closed")
        return self._reader.query(text, phrase)

    def query_all(self, queries):
        """Run ``[{"text": ..., "phrase": ...}]`` and keep request order."""
        if not isinstance(queries, list) or len(queries) > 32:
            raise RecordError("queries must be a list of at most 32 entries")
        results = []
        for item in queries:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise RecordError("each query must be an object with a text string")
            phrase = item.get("phrase", False)
            if not isinstance(phrase, bool):
                raise RecordError("phrase must be a boolean")
            results.append({
                "text": item["text"],
                "phrase": phrase,
                "matches": self.query(item["text"], phrase),
            })
        return results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._reader = None
        fd, self._pin_fd = self._pin_fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Dataset:
    """A directory holding generations of versioned document postings."""

    def __init__(self, path, fault=None):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        for name in ("segments", "generations", "batches", "quarantine"):
            (self.path / name).mkdir(exist_ok=True)
        self.fault = fault
        self._write_lock = self.path / "LOCK"
        self._pin_path = self.path / "PIN"
        self._mappings_path = self.path / "mappings.json"
        self._current_path = self.path / "CURRENT"

    # -- locking -----------------------------------------------------------

    @contextmanager
    def _writer(self):
        fd = os.open(str(self._write_lock), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _pin(self, exclusive=False):
        fd = os.open(str(self._pin_path), os.O_RDWR | os.O_CREAT, 0o644)
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if exclusive:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            os.close(fd)
            raise
        except Exception:
            os.close(fd)
            raise
        return fd

    def _fault(self, stage):
        if self.fault is not None:
            self.fault(stage)

    # -- state -------------------------------------------------------------

    def _current_id(self):
        try:
            value = _read_json(self._current_path)
        except FileNotFoundError:
            return None
        if value in (None, ""):
            return None
        if not isinstance(value, str) or not _GENERATION_RE.fullmatch(value):
            raise StateError("CURRENT does not name a valid generation")
        return value

    def _generation_path(self, generation_id):
        if not isinstance(generation_id, str) or not _GENERATION_RE.fullmatch(generation_id):
            raise StateError("invalid generation identity")
        return self.path / "generations" / (generation_id + ".json")

    def _load_manifest(self, generation_id, verify_segments=True):
        try:
            manifest = _read_json(self._generation_path(generation_id))
        except FileNotFoundError:
            raise StateError("generation %r does not exist" % generation_id) from None
        if not isinstance(manifest, dict):
            raise StateError("generation manifest must be an object")
        if manifest.get("generation") != generation_id:
            raise StateError("generation manifest identity mismatch")
        segments_list = manifest.get("segments")
        if not isinstance(segments_list, list) or len(segments_list) > MAX_SEGMENTS:
            raise StateError("generation manifest has an invalid segment list")
        for name in segments_list:
            if not isinstance(name, str) or not _SEGMENT_RE.fullmatch(name):
                raise StateError("invalid segment identity in manifest")
            if verify_segments and not (self.path / "segments" / name).is_file():
                raise StateError("generation references a missing segment")
        documents = manifest.get("documents")
        if not isinstance(documents, dict) or len(documents) > MAX_DOCS:
            raise StateError("generation manifest has invalid documents")
        for doc_id, state in documents.items():
            if not isinstance(state, dict):
                raise StateError("document state must be an object")
            if state.get("doc_id", doc_id) != doc_id:
                raise StateError("document state key mismatch")
            _validate_record_shape(state, state.get("source"))
        parent = manifest.get("parent")
        if parent is not None and (not isinstance(parent, str) or not _GENERATION_RE.fullmatch(parent)):
            raise StateError("generation manifest has an invalid parent")
        batch = manifest.get("batch")
        if batch is not None and not isinstance(batch, dict):
            raise StateError("generation manifest has an invalid batch summary")
        return manifest

    def _replay_segments(self, segment_names):
        tokens_by_doc = {}
        for name in segment_names:
            segment = _read_json(self.path / "segments" / name)
            if not isinstance(segment, dict):
                raise StateError("segment must be an object")
            for doc_id, words in segment.items():
                if words is None:
                    tokens_by_doc.pop(doc_id, None)
                    continue
                if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                    raise StateError("segment contains invalid postings")
                tokens_by_doc[doc_id] = words
        return tokens_by_doc

    def _open_snapshot(self, generation_id):
        pin_fd = self._pin()
        try:
            if generation_id is None:
                generation_id = self._current_id()
            if generation_id is None:
                reader = segments.Reader({})
                return Snapshot(None, None, {}, reader, pin_fd)
            manifest = self._load_manifest(generation_id)
            tokens_by_doc = self._replay_segments(manifest["segments"])
            documents = manifest["documents"]
            for doc_id in tokens_by_doc:
                if doc_id not in documents:
                    raise StateError("segment references an unknown document")
            for doc_id, state in documents.items():
                if not state["retracted"] and doc_id not in tokens_by_doc:
                    raise StateError("live document %r has no postings in this generation" % doc_id)
            live = {
                doc_id: words
                for doc_id, words in tokens_by_doc.items()
                if doc_id in documents and not documents[doc_id]["retracted"]
            }
            return Snapshot(generation_id, manifest, documents, segments.Reader(live), pin_fd)
        except Exception:
            try:
                fcntl.flock(pin_fd, fcntl.LOCK_UN)
            finally:
                os.close(pin_fd)
            raise

    def reader(self, generation=None):
        """Pin and return a :class:`Snapshot` of ``generation`` (default: current)."""
        return self._open_snapshot(generation)

    snapshot = reader

    # -- mappings ----------------------------------------------------------

    def mappings(self):
        try:
            value = _read_json(self._mappings_path)
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise StateError("mappings file must be an object")
        return value

    def _resolve_mapping(self, source, mapping):
        """Check ``mapping`` against the source's published mapping, if any."""
        existing = self.mappings().get(source)
        if existing is None:
            return mapping
        if existing != mapping:
            raise MappingError("source %r already publishes a different field mapping" % source)
        return existing

    def _persist_mapping(self, source, mapping):
        current = self.mappings()
        if source in current:
            return
        current[source] = mapping
        _write_json(self._mappings_path, current)

    # -- ingest ------------------------------------------------------------

    def ingest(self, request):
        """Convert one batch of versioned source records and publish it.

        Returns a result describing the batch, generation and the per-record
        correspondence ``source record -> target document -> batch``.  The
        publication is atomic: either the whole batch becomes visible in a
        new generation or the previous generation stays current.
        """
        source, mapping, raw_records, batch_id, policy = self._parse_request(request)
        with self._writer():
            parent_id = self._current_id()
            parent = self._load_manifest(parent_id) if parent_id else None
            documents = dict(parent["documents"]) if parent else {}
            segments_list = list(parent["segments"]) if parent else []
            accepted_mapping = self._resolve_mapping(source, mapping)

            records = [_normalize_record(raw, source, accepted_mapping) for raw in raw_records]
            records = _fold_records(records)
            if sum(len(record["text"]) for record in records) > MAX_BATCH_CHARS:
                raise RecordError("batch text exceeds the %d character limit" % MAX_BATCH_CHARS)
            fingerprint = _fingerprint(source, accepted_mapping, records)
            existing_batch = self._find_batch(parent, source, batch_id)
            if existing_batch is not None:
                if existing_batch.get("fingerprint") != fingerprint:
                    raise RecordError("batch %r was already used for a different request" % batch_id)
                self._persist_mapping(source, accepted_mapping)
                return _replay_result(existing_batch, source, parent_id)

            accepted, quarantined = [], []
            for record in records:
                state = documents.get(record["doc_id"])
                if state is not None:
                    existing_order = _order(state)
                    incoming_order = _order(record)
                    if incoming_order < existing_order:
                        quarantined.append((record, state, "stale version"))
                        continue
                    if incoming_order == existing_order:
                        same = (
                            state["retracted"] == record["retracted"]
                            and state.get("text_hash") == _text_hash(record["text"])
                        )
                        if not same:
                            quarantined.append((record, state, "version already published with different content"))
                            continue
                        accepted.append((record, state, True))
                        continue
                accepted.append((record, state, False))

            if quarantined and policy == "reject":
                raise SourceConflict(
                    "%d record(s) conflict with the published source version" % len(quarantined),
                    result=self._conflict_result(source, batch_id, records, quarantined, parent_id),
                )

            changes = {}
            new_documents = dict(documents)
            batch_entries = []
            unchanged = 0
            for record, state, replay in accepted:
                doc_id = record["doc_id"]
                batch_entries.append(_entry(record, batch_id, "replayed" if replay else "accepted"))
                if replay:
                    unchanged += 1
                    continue
                changes[doc_id] = None if record["retracted"] else segments.tokens(record["text"])
                new_documents[doc_id] = {
                    "doc_id": doc_id,
                    "source": source,
                    "source_id": record["source_id"],
                    "version": record["version"],
                    "revision": record["revision"],
                    "retracted": record["retracted"],
                    "text_hash": _text_hash(record["text"]),
                    "batch": batch_id,
                }

            if len(new_documents) > MAX_DOCS:
                raise RecordError("dataset document limit reached")

            accepted_entries = list(batch_entries)
            quarantine_entries = [_quarantine_entry(rec, state, reason) for rec, state, reason in quarantined]

            segment_name = None
            if changes:
                segment_name = "segment-" + uuid.uuid4().hex + ".json"
                segments_list = segments_list + [segment_name]
            if len(segments_list) > MAX_SEGMENTS:
                raise RecordError("dataset segment limit reached; merge before ingesting")

            generation_id = "g-" + uuid.uuid4().hex
            summary = {
                "id": batch_id,
                "source": source,
                "generation": generation_id,
                "fingerprint": fingerprint,
                "accepted": accepted_entries,
                "quarantined": quarantine_entries,
                "policy": policy,
            }
            manifest = {
                "generation": generation_id,
                "parent": parent_id,
                "segments": segments_list,
                "documents": new_documents,
                "batch": summary,
                "created": time.time(),
            }
            audit = {
                "batch": batch_id,
                "source": source,
                "generation": generation_id,
                "fingerprint": fingerprint,
                "mapping": accepted_mapping,
                "accepted": [_batch_record(record, batch_id) for record, _state, _replay in accepted],
                "quarantined": quarantine_entries,
                "created": time.time(),
            }

            if changes or quarantined:
                # Segment, then manifest, then the atomic pointer swap.  The
                # pointer swap is the sole commit point: if any earlier step
                # (or the swap itself) fails, the parent generation stays
                # current and the leftover files are unreachable.
                if changes:
                    _write_json(self.path / "segments" / segment_name, changes)
                    self._fault("segment")
                _write_json(self._generation_path(generation_id), manifest)
                self._fault("generation")
                self._fault("pointer")
                _write_json(self._current_path, generation_id)
                self._persist_mapping(source, accepted_mapping)
                self._fault("published")
                result = {
                    "batch": batch_id,
                    "source": source,
                    "generation": generation_id,
                    "parent": parent_id,
                    "committed": True,
                    "replayed": False,
                    "accepted": accepted_entries,
                    "quarantined": quarantine_entries,
                    "counts": {
                        "records": len(raw_records),
                        "accepted": len(accepted_entries) - unchanged,
                        "unchanged": unchanged,
                        "quarantined": len(quarantine_entries),
                    },
                    "mapping": accepted_mapping,
                }
            else:
                # Pure replay: the source state is already live.  Still record
                # the batch so a retry is answered identically.
                result = {
                    "batch": batch_id,
                    "source": source,
                    "generation": parent_id,
                    "parent": parent["parent"] if parent else None,
                    "committed": False,
                    "replayed": False,
                    "accepted": accepted_entries,
                    "quarantined": [],
                    "counts": {
                        "records": len(raw_records),
                        "accepted": 0,
                        "unchanged": unchanged,
                        "quarantined": 0,
                    },
                    "mapping": accepted_mapping,
                }
                replay_audit = dict(audit, generation=parent_id)
                _write_json(self._batch_path(source, batch_id), replay_audit)
                self._persist_mapping(source, accepted_mapping)
                self._fault("batch")
                return result

            # Audit records come after the commit point: a crash here means a
            # lost acknowledgement, never a visible half batch.  A retry finds
            # the batch in the generation chain and replays it.
            _write_json(self._batch_path(source, batch_id), audit)
            self._fault("batch")
            if quarantined:
                _write_json(
                    self.path / "quarantine" / (self._batch_key(source, batch_id) + ".json"),
                    {
                        "batch": batch_id,
                        "source": source,
                        "generation": generation_id,
                        "conflicts": quarantine_entries,
                        "created": time.time(),
                    },
                )
            return result

    ingest_batch = ingest

    def _parse_request(self, request):
        if not isinstance(request, dict):
            raise RecordError("request must be an object")
        unknown = sorted(set(request) - {"source", "mapping", "records", "documents", "batch", "conflict"})
        if unknown:
            raise RecordError("unknown request field: %s" % ", ".join(unknown))
        source = request.get("source")
        if not isinstance(source, str) or not source or len(source) > MAX_SOURCE or _CONTROL_RE.search(source):
            raise RecordError("source must be a non-empty identifier")
        if "records" in request and "documents" in request:
            raise RecordError("use either records or documents, not both")
        raw_records = request.get("records", request.get("documents"))
        if not isinstance(raw_records, list):
            raise RecordError("records must be a list")
        if len(raw_records) > MAX_RECORDS:
            raise RecordError("at most %d records per batch" % MAX_RECORDS)
        if "mapping" in request:
            mapping = _validate_mapping(request["mapping"])
        else:
            registered = self.mappings().get(source)
            if registered is None:
                raise MappingError("mapping is required the first time a source is ingested")
            mapping = _validate_mapping(registered)
        batch_id = request.get("batch")
        if batch_id is None:
            batch_id = "b-" + uuid.uuid4().hex
        if not isinstance(batch_id, str) or not _BATCH_RE.fullmatch(batch_id):
            raise RecordError("batch must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
        policy = request.get("conflict", "reject")
        if policy not in ("reject", "quarantine"):
            raise RecordError("conflict must be reject or quarantine")
        return source, mapping, raw_records, batch_id, policy

    def _find_batch(self, manifest, source, batch_id):
        """Locate a previously accepted batch from ``source``.

        The audit file, keyed by source and batch, is written after the commit
        point and survives reclamation.  The generation chain is the fallback
        for a crash between the pointer swap and that write.
        """
        path = self._batch_path(source, batch_id)
        if path.is_file():
            audit = _read_json(path)
            if not isinstance(audit, dict) or audit.get("batch") != batch_id or audit.get("source") != source:
                raise StateError("batch record is inconsistent")
            return audit
        depth = 0
        while manifest is not None and depth < MAX_GENERATION_HISTORY:
            summary = manifest.get("batch")
            if (
                isinstance(summary, dict)
                and summary.get("id") == batch_id
                and summary.get("source") == source
            ):
                return summary
            parent = manifest.get("parent")
            if parent is None or not self._generation_path(parent).is_file():
                return None
            manifest = self._load_manifest(parent)
            depth += 1
        return None

    def _conflict_result(self, source, batch_id, records, quarantined, parent_id):
        return {
            "batch": batch_id,
            "source": source,
            "generation": parent_id,
            "committed": False,
            "replayed": False,
            "accepted": [],
            "quarantined": [_quarantine_entry(rec, state, reason) for rec, state, reason in quarantined],
            "counts": {
                "records": len(records),
                "accepted": 0,
                "unchanged": 0,
                "quarantined": len(quarantined),
            },
        }

    # -- maintenance -------------------------------------------------------

    def merge(self):
        """Rewrite the current generation's segments into one segment.

        The operation is itself a publication: it writes the new segment and
        generation, then swaps ``CURRENT``.  An interrupted merge leaves the
        previous generation fully readable.
        """
        with self._writer():
            current_id = self._current_id()
            if current_id is None:
                raise StateError("dataset has no published generation")
            manifest = self._load_manifest(current_id)
            if len(manifest["segments"]) <= 1:
                return {"merged": False, "generation": current_id, "segments": len(manifest["segments"])}
            tokens_by_doc = self._replay_segments(manifest["segments"])
            documents = manifest["documents"]
            live = {
                doc_id: words
                for doc_id, words in tokens_by_doc.items()
                if doc_id in documents and not documents[doc_id]["retracted"]
            }
            name = "segment-" + uuid.uuid4().hex + ".json"
            _write_json(self.path / "segments" / name, live)
            self._fault("segment")
            generation_id = "g-" + uuid.uuid4().hex
            merged = {
                "generation": generation_id,
                "parent": current_id,
                "segments": [name],
                "documents": documents,
                "batch": None,
                "created": time.time(),
            }
            _write_json(self._generation_path(generation_id), merged)
            self._fault("generation")
            self._fault("pointer")
            _write_json(self._current_path, generation_id)
            self._fault("published")
            return {
                "merged": True,
                "generation": generation_id,
                "parent": current_id,
                "segments": 1,
                "documents": len(live),
            }

    def reclaim(self):
        """Delete segments and generation files no longer reachable.

        Only the current generation is reachable.  Reclamation refuses to run
        while any snapshot is open (in this or another process), because such
        a reader may still be using an older generation.  Batch and
        quarantine records are audit data and are kept.
        """
        with self._writer():
            try:
                pin_fd = self._pin(exclusive=True)
            except BlockingIOError:
                return {"reclaimed": False, "busy": True, "removed_segments": [], "removed_generations": []}
            try:
                current_id = self._current_id()
                keep_segments, keep_generation = set(), None
                if current_id is not None:
                    manifest = self._load_manifest(current_id)
                    keep_segments = set(manifest["segments"])
                    keep_generation = current_id
                removed_segments, removed_generations = [], []
                for path in sorted((self.path / "segments").iterdir()):
                    if path.name.startswith(".write-"):
                        path.unlink(missing_ok=True)
                        continue
                    if not _SEGMENT_RE.fullmatch(path.name):
                        raise StateError("unexpected file in segments directory: %s" % path.name)
                    if path.name not in keep_segments:
                        path.unlink()
                        removed_segments.append(path.name)
                for path in sorted((self.path / "generations").iterdir()):
                    if path.name.startswith(".write-"):
                        path.unlink(missing_ok=True)
                        continue
                    if not path.name.endswith(".json") or not _GENERATION_RE.fullmatch(path.name[:-5]):
                        raise StateError("unexpected file in generations directory: %s" % path.name)
                    if path.name[:-5] != keep_generation:
                        path.unlink()
                        removed_generations.append(path.name[:-5])
                return {
                    "reclaimed": bool(removed_segments or removed_generations),
                    "busy": False,
                    "generation": keep_generation,
                    "removed_segments": removed_segments,
                    "removed_generations": removed_generations,
                    "retained_segments": len(keep_segments),
                }
            finally:
                try:
                    fcntl.flock(pin_fd, fcntl.LOCK_UN)
                finally:
                    os.close(pin_fd)

    # -- audit -------------------------------------------------------------

    @staticmethod
    def _batch_key(source, batch_id):
        if not isinstance(batch_id, str) or not _BATCH_RE.fullmatch(batch_id):
            raise RecordError("invalid batch identity")
        if not isinstance(source, str) or not source or len(source) > MAX_SOURCE:
            raise RecordError("invalid source identity")
        return quote(source, safe="") + "__" + batch_id

    def _batch_path(self, source, batch_id):
        return self.path / "batches" / (self._batch_key(source, batch_id) + ".json")

    def batch(self, batch_id, source):
        """Return the audit record of ``source``'s ``batch_id``."""
        return _read_json(self.path / "batches" / (self._batch_key(source, batch_id) + ".json"))

    def quarantine(self, batch_id, source):
        """Return the isolated conflicts recorded for ``source``'s batch."""
        return _read_json(self.path / "quarantine" / (self._batch_key(source, batch_id) + ".json"))

    def generations(self):
        """Return generation ids from the oldest reachable to the current one."""
        current_id = self._current_id()
        chain = []
        manifest = self._load_manifest(current_id) if current_id else None
        while manifest is not None and len(chain) < MAX_GENERATION_HISTORY:
            chain.append(manifest["generation"])
            parent = manifest.get("parent")
            if parent is None or not self._generation_path(parent).is_file():
                break
            manifest = self._load_manifest(parent)
        return list(reversed(chain))


# --------------------------------------------------------------------------
# request/result helpers


def _text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(source, mapping, records):
    """Stable identity of the *content* of a batch, independent of order."""
    ordered = sorted(records, key=lambda record: record["doc_id"])
    canonical = json.dumps(
        {"source": source, "mapping": mapping, "records": ordered},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry(record, batch_id, status):
    return {
        "batch": batch_id,
        "source": record["source"],
        "source_id": record["source_id"],
        "doc_id": record["doc_id"],
        "version": record["version"],
        "revision": record["revision"],
        "retracted": record["retracted"],
        "status": status,
    }


def _batch_record(record, batch_id):
    value = _entry(record, batch_id, "accepted")
    value["text"] = record["text"]
    return value


def _quarantine_entry(record, state, reason):
    return {
        "source": record["source"],
        "source_id": record["source_id"],
        "doc_id": record["doc_id"],
        "reason": reason,
        "incoming": {
            "version": record["version"],
            "revision": record["revision"],
            "retracted": record["retracted"],
            "text_hash": _text_hash(record["text"]),
        },
        "published": {
            "version": state["version"],
            "revision": state["revision"],
            "retracted": state["retracted"],
            "text_hash": state.get("text_hash"),
            "batch": state.get("batch"),
        },
    }


def _replay_result(summary, source, current_id):
    accepted = []
    for entry in summary.get("accepted", []):
        entry = copy.deepcopy(entry)
        entry.pop("text", None)
        accepted.append(entry)
    quarantined = copy.deepcopy(summary.get("quarantined", []))
    return {
        "batch": summary.get("id", summary.get("batch")),
        "source": source,
        "generation": current_id,
        "batch_generation": summary.get("generation"),
        "parent": None,
        "committed": False,
        "replayed": True,
        "accepted": accepted,
        "quarantined": quarantined,
        "counts": {
            "records": len(accepted) + len(quarantined),
            "accepted": 0,
            "unchanged": len(accepted),
            "quarantined": len(quarantined),
        },
    }
