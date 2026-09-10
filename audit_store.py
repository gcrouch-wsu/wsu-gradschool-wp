from __future__ import annotations

import gzip
import json
import os
import time
from collections import OrderedDict
from threading import RLock
from typing import Any

CHUNK_TTL_SECONDS = 60 * 60


class RevisionConflict(Exception):
    def __init__(self, current_rev: int):
        super().__init__("The stored audit changed before this write finished.")
        self.current_rev = current_rev


class AuditStore:
    """Storage adapter for local memory or private Vercel Blob objects."""

    def __init__(self) -> None:
        self.blob_enabled = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
        self._chunks: dict[tuple[str, int], bytes] = {}
        self._chunk_times: dict[tuple[str, int], float] = {}
        self._reports: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._decisions: dict[str, dict[str, str]] = {}
        self._live: dict[str, dict[str, dict]] = {}
        self._decision_revs: dict[str, int] = {}
        self._live_revs: dict[str, int] = {}
        self._lock = RLock()

    @property
    def mode(self) -> str:
        return "blob" if self.blob_enabled else "memory"

    @staticmethod
    def _chunk_path(audit_id: str, index: int) -> str:
        return f"audits/{audit_id}/chunks/{index:04d}.bin"

    @staticmethod
    def _report_path(audit_id: str) -> str:
        return f"audits/{audit_id}/report.json.gz"

    @staticmethod
    def _decisions_path(audit_id: str) -> str:
        return f"audits/{audit_id}/decisions.json"

    @staticmethod
    def _live_path(audit_id: str) -> str:
        return f"audits/{audit_id}/live.json"

    @staticmethod
    def _chunk_index_path() -> str:
        return "audits/chunk-index.json"

    @staticmethod
    def _client():
        from vercel.blob import BlobClient

        return BlobClient()

    def _cache_report(self, audit_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._reports[audit_id] = state
            self._reports.move_to_end(audit_id)
            while len(self._reports) > 4:
                self._reports.popitem(last=False)

    def expire_stale_chunks(self) -> None:
        """Drop chunks that were never completed."""
        cutoff = time.time() - CHUNK_TTL_SECONDS
        if self.blob_enabled:
            index = self._load_chunk_index()
            stale = [audit_id for audit_id, info in index.items() if float(info.get("t") or 0) < cutoff]
            for audit_id in stale:
                self.delete_chunks(audit_id, int(index[audit_id].get("n") or 0))
            return
        with self._lock:
            stale_ids = {audit_id for (audit_id, _index), stamped in self._chunk_times.items() if stamped < cutoff}
            stale_keys = [key for key in self._chunks if key[0] in stale_ids]
            for key in stale_keys:
                self._chunks.pop(key, None)
                self._chunk_times.pop(key, None)

    def put_chunk(self, audit_id: str, index: int, payload: bytes) -> None:
        self.expire_stale_chunks()
        if self.blob_enabled:
            with self._client() as client:
                client.put(
                    self._chunk_path(audit_id, index), payload,
                    access="private", content_type="application/octet-stream", overwrite=True,
                )
            chunk_index = self._load_chunk_index()
            prior = chunk_index.get(audit_id) or {}
            chunk_index[audit_id] = {"t": time.time(), "n": max(int(prior.get("n") or 0), index + 1)}
            self._save_chunk_index(chunk_index)
            return
        with self._lock:
            key = (audit_id, index)
            self._chunks[key] = payload
            self._chunk_times[key] = time.time()

    def get_chunk(self, audit_id: str, index: int) -> bytes | None:
        if not self.blob_enabled:
            self.expire_stale_chunks()
        if self.blob_enabled:
            from vercel.blob.errors import BlobNotFoundError
            try:
                with self._client() as client:
                    result = client.get(self._chunk_path(audit_id, index), access="private", use_cache=False)
            except BlobNotFoundError:
                return None
            return result.content if result is not None else None
        with self._lock:
            return self._chunks.get((audit_id, index))

    def delete_chunks(self, audit_id: str, total_chunks: int) -> None:
        if self.blob_enabled:
            paths = [self._chunk_path(audit_id, index) for index in range(total_chunks)]
            if paths:
                with self._client() as client:
                    client.delete(paths)
            index = self._load_chunk_index()
            if audit_id in index:
                index.pop(audit_id, None)
                self._save_chunk_index(index)
            return
        with self._lock:
            for index in range(total_chunks):
                key = (audit_id, index)
                self._chunks.pop(key, None)
                self._chunk_times.pop(key, None)

    def save_report(self, audit_id: str, report: dict, filename: str) -> None:
        state = {"report": report, "filename": filename}
        self._cache_report(audit_id, state)
        if self.blob_enabled:
            raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            body = gzip.compress(raw)
            with self._client() as client:
                client.put(
                    self._report_path(audit_id), body,
                    access="private", content_type="application/gzip", overwrite=True,
                    multipart=len(body) > 4 * 1024 * 1024,
                )
        self.save_decisions(audit_id, {})
        self.save_live(audit_id, {})

    def load_report(self, audit_id: str) -> dict[str, Any] | None:
        with self._lock:
            cached = self._reports.get(audit_id)
            if cached is not None:
                self._reports.move_to_end(audit_id)
                return cached
        if not self.blob_enabled:
            return None
        from vercel.blob.errors import BlobNotFoundError
        try:
            with self._client() as client:
                result = client.get(self._report_path(audit_id), access="private", use_cache=True)
        except BlobNotFoundError:
            return None
        if result is None:
            return None
        state = json.loads(gzip.decompress(result.content).decode("utf-8"))
        self._cache_report(audit_id, state)
        return state

    def _load_chunk_index(self) -> dict[str, dict]:
        from vercel.blob.errors import BlobNotFoundError
        try:
            with self._client() as client:
                result = client.get(self._chunk_index_path(), access="private", use_cache=False)
        except BlobNotFoundError:
            return {}
        if result is None:
            return {}
        parsed = json.loads(result.content.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _save_chunk_index(self, index: dict[str, dict]) -> None:
        with self._client() as client:
            client.put(
                self._chunk_index_path(),
                json.dumps(index, separators=(",", ":")).encode("utf-8"),
                access="private", content_type="application/json", overwrite=True,
            )

    @staticmethod
    def _unwrap_revisioned(raw: Any, empty):
        if isinstance(raw, dict) and isinstance(raw.get("items"), dict) and "rev" in raw:
            try:
                return dict(raw["items"]), int(raw["rev"])
            except (TypeError, ValueError):
                return dict(empty), 0
        if isinstance(raw, dict):
            return dict(raw), 0
        return dict(empty), 0

    def _load_revisioned(self, audit_id: str, memory: dict, revs: dict, path_fn, empty):
        if not self.blob_enabled:
            with self._lock:
                return dict(memory.get(audit_id, empty)), int(revs.get(audit_id, 0))
        from vercel.blob.errors import BlobNotFoundError
        try:
            with self._client() as client:
                result = client.get(path_fn(audit_id), access="private", use_cache=False)
        except BlobNotFoundError:
            return dict(empty), 0
        raw = json.loads(result.content.decode("utf-8")) if result is not None else empty
        items, rev = self._unwrap_revisioned(raw, empty)
        with self._lock:
            revs[audit_id] = rev
        return items, rev

    def _save_revisioned(self, audit_id: str, items: dict, expected_rev: int | None, memory: dict, revs: dict, path_fn):
        current, current_rev = self._load_revisioned(audit_id, memory, revs, path_fn, {})
        if expected_rev is not None and expected_rev != current_rev:
            raise RevisionConflict(current_rev)
        new_rev = current_rev + 1
        payload = {"rev": new_rev, "items": items}
        if self.blob_enabled:
            with self._client() as client:
                client.put(
                    path_fn(audit_id),
                    json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    access="private", content_type="application/json", overwrite=True,
                    cache_control_max_age=60,
                )
        with self._lock:
            memory[audit_id] = dict(items)
            revs[audit_id] = new_rev
        return new_rev

    def load_decisions(self, audit_id: str) -> dict[str, str]:
        items, _rev = self._load_revisioned(
            audit_id, self._decisions, self._decision_revs, self._decisions_path, {}
        )
        return items

    def save_decisions(self, audit_id: str, decisions: dict[str, str], expected_rev: int | None = None) -> int:
        return self._save_revisioned(
            audit_id, decisions, expected_rev, self._decisions, self._decision_revs, self._decisions_path
        )

    def load_live(self, audit_id: str) -> dict[str, dict]:
        items, _rev = self._load_revisioned(
            audit_id, self._live, self._live_revs, self._live_path, {}
        )
        return items

    def save_live(self, audit_id: str, live: dict[str, dict], expected_rev: int | None = None) -> int:
        return self._save_revisioned(
            audit_id, live, expected_rev, self._live, self._live_revs, self._live_path
        )

    def load_state(self, audit_id: str | None) -> dict[str, Any] | None:
        if not audit_id:
            return None
        state = self.load_report(audit_id)
        if state is None:
            return None
        decisions, decision_rev = self._load_revisioned(
            audit_id, self._decisions, self._decision_revs, self._decisions_path, {}
        )
        live, live_rev = self._load_revisioned(
            audit_id, self._live, self._live_revs, self._live_path, {}
        )
        return {
            **state,
            "decisions": decisions,
            "decision_rev": decision_rev,
            "live": live,
            "live_rev": live_rev,
        }

    def delete_audit(self, audit_id: str | None, total_chunks: int = 0) -> None:
        if not audit_id:
            return
        if self.blob_enabled:
            paths = [self._report_path(audit_id), self._decisions_path(audit_id), self._live_path(audit_id)]
            paths.extend(self._chunk_path(audit_id, index) for index in range(total_chunks))
            with self._client() as client:
                client.delete(paths)
        with self._lock:
            self._reports.pop(audit_id, None)
            self._decisions.pop(audit_id, None)
            self._live.pop(audit_id, None)
            self._decision_revs.pop(audit_id, None)
            self._live_revs.pop(audit_id, None)
            for key in [key for key in self._chunks if key[0] == audit_id]:
                self._chunks.pop(key, None)
                self._chunk_times.pop(key, None)
