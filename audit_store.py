from __future__ import annotations

import gzip
import json
import os
from collections import OrderedDict
from threading import RLock
from typing import Any


class AuditStore:
    """Storage adapter for local memory or private Vercel Blob objects."""

    def __init__(self) -> None:
        self.blob_enabled = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
        self._chunks: dict[tuple[str, int], bytes] = {}
        self._reports: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._decisions: dict[str, dict[str, str]] = {}
        self._live: dict[str, dict[str, dict]] = {}
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
    def _client():
        from vercel.blob import BlobClient

        return BlobClient()

    def _cache_report(self, audit_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._reports[audit_id] = state
            self._reports.move_to_end(audit_id)
            while len(self._reports) > 4:
                self._reports.popitem(last=False)

    def put_chunk(self, audit_id: str, index: int, payload: bytes) -> None:
        if self.blob_enabled:
            with self._client() as client:
                client.put(
                    self._chunk_path(audit_id, index), payload,
                    access="private", content_type="application/octet-stream", overwrite=True,
                )
            return
        with self._lock:
            self._chunks[(audit_id, index)] = payload

    def get_chunk(self, audit_id: str, index: int) -> bytes | None:
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
            return
        with self._lock:
            for index in range(total_chunks):
                self._chunks.pop((audit_id, index), None)

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

    def load_decisions(self, audit_id: str) -> dict[str, str]:
        if not self.blob_enabled:
            with self._lock:
                return dict(self._decisions.get(audit_id, {}))
        from vercel.blob.errors import BlobNotFoundError
        try:
            with self._client() as client:
                result = client.get(self._decisions_path(audit_id), access="private", use_cache=False)
        except BlobNotFoundError:
            return {}
        return json.loads(result.content.decode("utf-8")) if result is not None else {}

    def save_decisions(self, audit_id: str, decisions: dict[str, str]) -> None:
        if self.blob_enabled:
            with self._client() as client:
                client.put(
                    self._decisions_path(audit_id),
                    json.dumps(decisions, separators=(",", ":")).encode("utf-8"),
                    access="private", content_type="application/json", overwrite=True,
                    cache_control_max_age=60,
                )
            return
        with self._lock:
            self._decisions[audit_id] = dict(decisions)

    def load_live(self, audit_id: str) -> dict[str, dict]:
        if not self.blob_enabled:
            with self._lock:
                return dict(self._live.get(audit_id, {}))
        from vercel.blob.errors import BlobNotFoundError
        try:
            with self._client() as client:
                result = client.get(self._live_path(audit_id), access="private", use_cache=False)
        except BlobNotFoundError:
            return {}
        return json.loads(result.content.decode("utf-8")) if result is not None else {}

    def save_live(self, audit_id: str, live: dict[str, dict]) -> None:
        if self.blob_enabled:
            with self._client() as client:
                client.put(
                    self._live_path(audit_id),
                    json.dumps(live, separators=(",", ":")).encode("utf-8"),
                    access="private", content_type="application/json", overwrite=True,
                    cache_control_max_age=60,
                )
            return
        with self._lock:
            self._live[audit_id] = dict(live)

    def load_state(self, audit_id: str | None) -> dict[str, Any] | None:
        if not audit_id:
            return None
        state = self.load_report(audit_id)
        if state is None:
            return None
        return {
            **state,
            "decisions": self.load_decisions(audit_id),
            "live": self.load_live(audit_id),
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
            for key in [key for key in self._chunks if key[0] == audit_id]:
                self._chunks.pop(key, None)
