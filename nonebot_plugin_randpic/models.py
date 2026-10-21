"""图库内部的数据模型，不依赖聊天平台。"""

from dataclasses import dataclass
from typing import Optional


class RandpicError(Exception):
    """可以向用户展示的操作失败原因。"""


class InvalidImage(RandpicError):
    pass


class DuplicateImage(RandpicError):
    pass


@dataclass(frozen=True)
class ImageRecord:
    id: str
    command: str
    filename: str
    content_hash: str
    phash: str
    send_count: int = 0
    byte_size: int = 0
    deleted: bool = False

    @property
    def key(self) -> str:
        return f'{self.command}/{self.filename}'


@dataclass(frozen=True)
class ImageSnapshot:
    image: ImageRecord
    data: bytes


@dataclass(frozen=True)
class MutationResult:
    image: ImageRecord
    already_deleted: bool = False
    settings_saved: bool = True
    web_error: Optional[str] = None


@dataclass(frozen=True)
class PublishedFile:
    path: str
    content_hash: str
    etag: str
    size: int


@dataclass(frozen=True)
class RemoteObject:
    size: int
    etag: str


@dataclass(frozen=True)
class SyncResult:
    pending: int = 0
    succeeded: int = 0
    failed: int = 0
    web_error: Optional[str] = None
