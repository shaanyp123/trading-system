"""QuantConnect ObjectStore REST client.

Thin async wrapper over QC's public ObjectStore endpoints at
``https://www.quantconnect.com/api/v2/object-store/{list,get,set}``. Used
by ``services/qc_adapter/orchestrator.py`` to pull JSONL events emitted
by the QC algorithm and to push Phase-1 defensive-trim instructions (PR-C
wires the write path; ``set_object`` is included now to lock the shape).

Auth model (QC docs § "Authentication"):

* HTTP Basic with the username = the numeric QC ``user_id`` and the
  password = ``sha256(<api_token>:<timestamp>)`` hex-digested.
* ``Timestamp: <epoch_seconds>`` request header carrying the same
  timestamp that was hashed into the password.
* Both must arrive together — QC rejects with 401 if the hash is stale
  or the timestamp is more than a few minutes off wall-clock.

Operator's ``secrets/<env>.enc.yaml`` carries the credential pair under
``quantconnect.organization_id`` (the user_id) and
``quantconnect.api_token``; the entrypoint maps them to
``QC_ADAPTER_QC_USER_ID`` and ``QC_ADAPTER_QC_API_TOKEN``.

Retry contract: transient errors (HTTP 5xx, connection errors,
``httpx.TransportError``) retry up to 3 attempts with exponential
backoff capped at 8s; 401/403/404 raise immediately. Anything else past
the retry budget raises :class:`QCObjectStoreError`. The orchestrator
treats that exception as a "failed poll" via
:func:`services.qc_adapter.cursor.plan_cursor_advance_failure`.

§6.8 / A27: this module DOES talk to QC's REST API; the smoke-test
obligation lands on the operator runbook at ``deploy/qc_adapter/README.md``
per dev-guide §6.8 alternative (b) — an unmocked CI fixture is
infeasible because QC requires a real account + token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Final

import httpx
import structlog

log = structlog.get_logger()

#: User-Agent set on every QC API call. QC has been observed to 403 a
#: bare default-httpx UA in the past; a stable explicit UA also helps
#: when the operator reads QC's request logs.
USER_AGENT: Final[str] = "trading-qc-adapter/0.1"

#: Retry budget for transient failures (5xx, transport errors).
_MAX_RETRIES: Final[int] = 3
#: Exponential backoff base; sleep = min(2 ** attempt, _MAX_BACKOFF).
_BACKOFF_BASE: Final[float] = 1.0
_MAX_BACKOFF: Final[float] = 8.0


class QCObjectStoreError(Exception):
    """Raised on terminal QC ObjectStore failures (post-retry or 4xx)."""


class QCObjectStoreAuthError(QCObjectStoreError):
    """401/403 from QC — credentials wrong or expired."""


class QCObjectStoreNotFoundError(QCObjectStoreError):
    """404 from QC — key/path does not exist."""


@dataclass(frozen=True, slots=True)
class _AuthHeaders:
    """The 3 headers QC requires on every request."""

    authorization: str
    timestamp: str
    user_agent: str = USER_AGENT


def _build_auth_headers(
    user_id: str, api_token: str, *, now_epoch: int | None = None
) -> _AuthHeaders:
    """Compute the QC Basic-auth + Timestamp header pair.

    QC's contract: password = sha256("{api_token}:{timestamp}").hexdigest();
    Authorization: Basic base64("{user_id}:{password}").
    Pure function so tests can stub ``now_epoch`` deterministically.
    """
    ts = str(now_epoch if now_epoch is not None else int(time.time()))
    password = hashlib.sha256(f"{api_token}:{ts}".encode()).hexdigest()
    raw = f"{user_id}:{password}".encode()
    return _AuthHeaders(
        authorization=f"Basic {base64.b64encode(raw).decode('ascii')}",
        timestamp=ts,
    )


class QCObjectStoreClient:
    """Async HTTP client for QC ObjectStore REST endpoints.

    Construction stores the (user_id, api_token) pair and a shared
    :class:`httpx.AsyncClient`. The client is async-context-managed by
    the caller (orchestrator opens once at startup, closes at shutdown).
    """

    def __init__(
        self,
        *,
        user_id: str,
        api_token: str,
        base_url: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not user_id:
            raise ValueError("user_id must be a non-empty string")
        if not api_token:
            raise ValueError("api_token must be a non-empty string")
        self._user_id = user_id
        self._api_token = api_token
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": USER_AGENT},
        )

    async def __aenter__(self) -> QCObjectStoreClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_keys(self, prefix: str) -> list[str]:
        """List ObjectStore keys under ``prefix``.

        QC's ``POST /object-store/list`` returns ``{ "objects": [
        {"key": "...", "size": N, "modified": "..."} ], "success": true }``.
        Returns just the keys, sorted ascending so downstream callers
        get a stable iteration order.
        """
        bound = log.bind(service_name="qc_adapter", op="list_keys", prefix=prefix)
        body = await self._post_json("/object-store/list", {"path": prefix})
        objects = body.get("objects") or []
        if not isinstance(objects, list):
            raise QCObjectStoreError(
                f"QC list response 'objects' has unexpected type {type(objects).__name__}"
            )
        keys: list[str] = []
        for entry in objects:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                keys.append(entry["key"])
        keys.sort()
        bound.info("qc_objectstore_listed", count=len(keys))
        return keys

    async def get_object(self, key: str) -> bytes:
        """Fetch the object body for ``key``.

        QC's ``POST /object-store/get`` is a 2-step flow:

        1. Request hits the v2 endpoint; response carries ``url``
           pointing at a presigned S3 URL (short-lived).
        2. GET the presigned URL with no QC headers attached (the URL
           is self-authenticating).

        Raises :class:`QCObjectStoreNotFoundError` on 404; other
        non-2xx → :class:`QCObjectStoreError`.
        """
        bound = log.bind(service_name="qc_adapter", op="get_object", key=key)
        body = await self._post_json("/object-store/get", {"key": key})
        presigned = body.get("url")
        if not isinstance(presigned, str) or not presigned:
            # QC ObjectStore can also inline the body when the object is
            # tiny — surface that path if/when it shows up.
            inline = body.get("data")
            if isinstance(inline, str):
                bound.info("qc_objectstore_inline_body", bytes=len(inline))
                return inline.encode("utf-8")
            raise QCObjectStoreError(
                f"QC get response missing presigned 'url' or inline 'data' for key={key!r}"
            )
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.get(presigned)
            except httpx.TransportError as exc:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_compute_backoff(attempt))
                    continue
                bound.warning("qc_objectstore_presigned_transport_failed", error=str(exc))
                raise QCObjectStoreError(
                    f"presigned GET transport failure after {_MAX_RETRIES} attempts: {exc}"
                ) from exc
            if resp.status_code == 404:
                raise QCObjectStoreNotFoundError(f"key not found: {key!r}")
            if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES - 1:
                bound.warning(
                    "qc_objectstore_presigned_5xx_retry",
                    status=resp.status_code,
                    attempt=attempt,
                )
                await asyncio.sleep(_compute_backoff(attempt))
                continue
            if resp.status_code != 200:
                raise QCObjectStoreError(
                    f"presigned GET non-200 for key={key!r}: {resp.status_code}"
                )
            bound.info("qc_objectstore_fetched", bytes=len(resp.content))
            return resp.content
        # Unreachable — every branch above either returns or raises; satisfy mypy.
        raise QCObjectStoreError(f"presigned GET retry budget exhausted for key={key!r}")

    async def set_object(self, key: str, body: bytes) -> None:
        """Write ``body`` to ObjectStore at ``key``.

        Locked here for PR-A's shape contract; the actual writer is
        PR-C's instruction-protocol path. Calling this from PR-A is a
        contract violation — guarded only by the orchestrator's lack of
        a call site.
        """
        bound = log.bind(service_name="qc_adapter", op="set_object", key=key, bytes=len(body))
        # QC's set endpoint accepts multipart/form-data with the key
        # as a query param and the body as a "objectData" file part.
        headers = self._build_request_headers()
        url = f"{self._base_url}/object-store/set"
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(
                    url,
                    params={"key": key},
                    files={"objectData": (key.rsplit("/", 1)[-1], body)},
                    headers=headers,
                )
            except httpx.TransportError as exc:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_compute_backoff(attempt))
                    continue
                bound.warning("qc_objectstore_set_transport_failed", error=str(exc))
                raise QCObjectStoreError(
                    f"set_object transport failure after {_MAX_RETRIES} attempts: {exc}"
                ) from exc
            if resp.status_code in (401, 403):
                raise QCObjectStoreAuthError(f"QC set 4xx: {resp.status_code} for key={key!r}")
            if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES - 1:
                bound.warning(
                    "qc_objectstore_set_5xx_retry",
                    status=resp.status_code,
                    attempt=attempt,
                )
                await asyncio.sleep(_compute_backoff(attempt))
                continue
            if not resp.is_success:
                raise QCObjectStoreError(
                    f"QC set non-2xx for key={key!r}: {resp.status_code} body={resp.text[:200]!r}"
                )
            bound.info("qc_objectstore_set")
            return
        raise QCObjectStoreError(f"set_object retry budget exhausted for key={key!r}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_request_headers(self) -> dict[str, str]:
        h = _build_auth_headers(self._user_id, self._api_token)
        return {
            "Authorization": h.authorization,
            "Timestamp": h.timestamp,
            "User-Agent": h.user_agent,
        }

    async def _post_json(self, path: str, payload: dict[str, str]) -> dict[str, object]:
        url = f"{self._base_url}{path}"
        bound = log.bind(service_name="qc_adapter", op="post_json", url=path)
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(
                    url,
                    data=payload,
                    headers=self._build_request_headers(),
                )
            except httpx.TransportError as exc:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_compute_backoff(attempt))
                    continue
                bound.warning("qc_objectstore_transport_failed", error=str(exc))
                raise QCObjectStoreError(
                    f"transport failure after {_MAX_RETRIES} attempts: {exc}"
                ) from exc
            if resp.status_code in (401, 403):
                raise QCObjectStoreAuthError(
                    f"QC auth rejected ({resp.status_code}): {resp.text[:200]!r}"
                )
            if resp.status_code == 404:
                raise QCObjectStoreNotFoundError(f"404 for {path!r}")
            if 500 <= resp.status_code < 600 and attempt < _MAX_RETRIES - 1:
                bound.warning(
                    "qc_objectstore_5xx_retry",
                    status=resp.status_code,
                    attempt=attempt,
                )
                await asyncio.sleep(_compute_backoff(attempt))
                continue
            if not resp.is_success:
                raise QCObjectStoreError(
                    f"QC non-2xx ({resp.status_code}) for {path!r}: {resp.text[:200]!r}"
                )
            try:
                body = resp.json()
            except ValueError as exc:
                raise QCObjectStoreError(f"QC response not JSON for {path!r}: {exc}") from exc
            if not isinstance(body, dict):
                raise QCObjectStoreError(
                    f"QC response top-level not object for {path!r}: {type(body).__name__}"
                )
            # QC convention: success=true on happy path; success=false +
            # errors=[...] on application-layer rejections (e.g. invalid
            # key shape). Surface as terminal error — the JSON envelope
            # is a 200 response so the retry layer can't see it.
            if body.get("success") is False:
                errors = body.get("errors") or []
                raise QCObjectStoreError(f"QC success=false for {path!r}: {errors}")
            return body
        raise QCObjectStoreError(f"retry budget exhausted for {path!r}")


def _compute_backoff(attempt: int) -> float:
    """Exponential backoff capped at :data:`_MAX_BACKOFF`."""
    return float(min(_BACKOFF_BASE * (2**attempt), _MAX_BACKOFF))


__all__ = [
    "USER_AGENT",
    "QCObjectStoreAuthError",
    "QCObjectStoreClient",
    "QCObjectStoreError",
    "QCObjectStoreNotFoundError",
]
