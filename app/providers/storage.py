"""File storage for member photos (S3 with server-side encryption in production).

Photos of family members (often children) are never public: the API hands out
short-lived signed links, like S3 presigned URLs. The fake keeps files on local
disk under MEDIA_DIR and serves them through `GET /media/{key}` after checking
the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from app.core.logging import get_logger


@dataclass(frozen=True, slots=True)
class StoredFile:
    data: bytes
    content_type: str


class StorageProvider(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> StoredFile | None: ...

    async def delete(self, key: str) -> None: ...

    def signed_path(self, key: str) -> str:
        """Path + query of a link that is valid for a limited time."""
        ...


class FakeStorageProvider:
    """Local disk. Each file has a `.type` sidecar holding its content type."""

    name = "fake"

    def __init__(self, root: Path, url_secret: str, url_ttl_s: int) -> None:
        self._root = root
        self._secret = url_secret.encode()
        self._ttl = url_ttl_s
        self._log = get_logger("kaladhaga.providers.storage.fake")

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if self._root.resolve() not in path.parents:
            raise ValueError("storage key escapes the media directory")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.with_name(path.name + ".type").write_text(content_type)
        self._log.info("storage.put", provider=self.name, key=key, size=len(data))

    async def get(self, key: str) -> StoredFile | None:
        path = self._path(key)
        if not path.is_file():
            return None
        content_type = path.with_name(path.name + ".type").read_text()
        return StoredFile(data=path.read_bytes(), content_type=content_type)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".type").unlink(missing_ok=True)
        self._log.info("storage.deleted", provider=self.name, key=key)

    def _sig(self, key: str, exp: int) -> str:
        return hmac.new(self._secret, f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()

    def signed_path(self, key: str) -> str:
        exp = int(time.time()) + self._ttl
        return f"/media/{key}?{urlencode({'exp': exp, 'sig': self._sig(key, exp)})}"

    def check_signature(self, key: str, exp: int, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(self._sig(key, exp), sig)
