"""Uploads: large inputs go straight to object storage through short-lived presigned URLs.

The API never streams a 50 MB tarball through itself: it records the upload, hands out a URL
that accepts exactly one PUT for a limited time, and the task later refers to the upload id.
With the local object store (development) the URL points back at `/v1/blobs/...` here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, Response

from fixloop_api.auth import Principal, get_state, require
from fixloop_api.schemas import UploadOut, UploadRequest
from fixloop_api.state import ApiState
from fixloop_core.errors import Forbidden, NotFound, TooLarge
from fixloop_core.objectstore import LocalObjectStore
from fixloop_core.storage import repo

router = APIRouter(tags=["uploads"])
UPLOAD_TTL_S = 3600


@router.post("/v1/uploads", status_code=201, response_model=UploadOut, summary="Start an upload")
async def start_upload(
    body: UploadRequest,
    principal: Principal = Depends(require("uploads:write")),
    state: ApiState = Depends(get_state),
) -> UploadOut:
    limits = state.config.limits.intake
    cap = limits.sql_seed_max_bytes if body.kind in ("seed", "parquet") else limits.upload_max_bytes
    if body.size_bytes > cap:
        raise TooLarge(f"{body.kind} uploads are limited to {cap // (1024 * 1024)} MB")
    async with state.db.transaction() as s:
        upload = await repo.create_upload(
            s,
            tenant_id=principal.tenant.id,
            kind=body.kind,
            max_bytes=body.size_bytes,
            content_type=body.content_type,
            ttl_seconds=UPLOAD_TTL_S,
        )
    url = await state.blobs.presign_put(upload.key, body.content_type, ttl=UPLOAD_TTL_S)
    return UploadOut(
        upload_id=upload.id,
        upload_url=url,
        headers={"Content-Type": body.content_type},
        expires_at=datetime.now(UTC) + timedelta(seconds=UPLOAD_TTL_S),
    )


def _local_store(state: ApiState) -> LocalObjectStore:
    if not isinstance(state.blobs, LocalObjectStore):
        raise NotFound("blob endpoints exist only with the local object store")
    return state.blobs


@router.put("/v1/blobs/{key:path}", status_code=204, include_in_schema=False)
async def put_blob(
    key: str,
    request: Request,
    expires: int = Query(...),
    signature: str = Query(...),
    state: ApiState = Depends(get_state),
) -> Response:
    store = _local_store(state)
    if not store.verify("PUT", key, expires, signature):
        raise Forbidden("the upload URL is invalid or expired")
    limit = state.config.limits.intake.sql_seed_max_bytes
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise TooLarge("the upload is larger than allowed")
        chunks.append(chunk)
    await store.put_bytes(
        key, b"".join(chunks), request.headers.get("content-type", "application/octet-stream")
    )
    return Response(status_code=204)


@router.get("/v1/blobs/{key:path}", include_in_schema=False)
async def get_blob(
    key: str, expires: int = Query(...), signature: str = Query(...), state: ApiState = Depends(get_state)
) -> Response:
    store = _local_store(state)
    if not store.verify("GET", key, expires, signature):
        raise Forbidden("the download URL is invalid or expired")
    return Response(await store.get_bytes(key), media_type="application/octet-stream")
