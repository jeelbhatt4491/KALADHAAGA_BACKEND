"""/media/{key}: stored files (member photos) behind short-lived signed links.
Only used with the fake (local disk) storage; S3 serves its own presigned URLs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.api.deps import ProvidersDep
from app.core.errors import AppError
from app.core.logging import get_logger
from app.providers.storage import FakeStorageProvider

log = get_logger("kaladhaga.media")
router = APIRouter(prefix="/media", tags=["media"], include_in_schema=False)


@router.get("/{key:path}")
async def get_media(
    key: Annotated[str, Path(max_length=255)],
    exp: Annotated[int, Query()],
    sig: Annotated[str, Query(max_length=128)],
    providers: ProvidersDep,
) -> Response:
    storage = providers.storage
    if not isinstance(storage, FakeStorageProvider) or not storage.check_signature(key, exp, sig):
        log.info("media.link_rejected", key=key)
        raise AppError("not_found", 404)
    try:
        stored = await storage.get(key)
    except ValueError:
        stored = None
    if stored is None:
        raise AppError("not_found", 404)
    return Response(
        stored.data,
        media_type=stored.content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )
