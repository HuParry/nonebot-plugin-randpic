"""文件操作日志：数据库提交前保留原图，启动时恢复未完成的操作。"""

import errno
import json
import os
import shutil
import uuid
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional

from nonebot.log import logger

from .models import ImageRecord, RandpicError
from .repository import Repository
from .utils import atomic_write, digest_file


def install_file(source: Path, destination: Path) -> None:
    """目标必须不存在，不能静默覆盖目录中已有的文件。"""
    try:
        os.link(source, destination)
        return
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise
    created = False
    try:
        with source.open('rb') as original, destination.open('xb') as output:
            created = True
            shutil.copyfileobj(original, output, 128 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            with suppress(OSError):
                destination.unlink()
        raise


class FileJournal:
    def __init__(self, root: Path, repository: Repository):
        self.root = root
        self.repository = repository
        self.directory = root / 'database' / 'transactions'
        self.prepared = root / 'database' / 'prepared'

    def initialize(self) -> None:
        for directory in (self.directory, self.prepared):
            if directory.is_symlink():
                raise RandpicError('数据库内部目录不能是符号链接。')
            directory.mkdir(parents=True, exist_ok=True)

    def _key(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _path(self, key: Optional[str]) -> Optional[Path]:
        if key is None:
            return None
        parts = PurePosixPath(key).parts
        if not parts or '..' in parts or PurePosixPath(key).is_absolute() or '\\' in key:
            raise RandpicError('文件操作日志中的路径不合法。')
        path = self.root.joinpath(*parts)
        if path.is_symlink():
            raise RandpicError('文件操作日志不能操作符号链接。')
        path.resolve().relative_to(self.root.resolve())
        return path

    def stage(self, data: bytes) -> Path:
        path = self.prepared / (uuid.uuid4().hex + '.bin')
        atomic_write(path, data)
        return path

    def _begin(self, image: ImageRecord, source: Optional[Path], destination: Optional[Path], payload: Optional[Path]) -> Dict[str, Any]:
        operation_id = uuid.uuid4().hex
        backup = source.with_name(f'.randpic-{operation_id}.bak') if source is not None else None
        entry = {
            'image_id': image.id, 'command': image.command, 'filename': image.filename,
            'content_hash': image.content_hash, 'kind': 'write' if destination is not None else 'delete',
            'source': self._key(source) if source is not None else None,
            'destination': self._key(destination) if destination is not None else None,
            'backup': self._key(backup) if backup is not None else None,
            'payload': self._key(payload) if payload is not None else None,
            'installed': False,
        }
        entry_path = self.directory / (operation_id + '.json')
        atomic_write(entry_path, json.dumps(entry, ensure_ascii=False).encode())
        try:
            if source is not None:
                source.rename(backup)
            if destination is not None:
                install_file(payload if payload is not None else backup, destination)
                entry['installed'] = True
                atomic_write(entry_path, json.dumps(entry, ensure_ascii=False).encode())
        except BaseException:
            if entry['installed']:
                with suppress(OSError):
                    destination.unlink()
            raise
        return entry

    def write(self, image: ImageRecord, source: Optional[Path], payload: Optional[Path]) -> None:
        destination = self.root / 'img' / image.command / image.filename
        self._begin(image, source, destination, payload)

    def delete(self, image: ImageRecord) -> None:
        source = self.root / 'img' / image.command / image.filename
        if source.exists():
            self._begin(image, source, None, None)

    def recover(self) -> None:
        for entry_path in sorted(self.directory.glob('*.json')):
            try:
                entry = json.loads(entry_path.read_text(encoding='utf-8'))
                with self.repository.connection() as db:
                    image = self.repository.get_image(db, entry['image_id'])
                source, destination, backup, payload = (self._path(entry[name]) for name in ('source', 'destination', 'backup', 'payload'))
                committed = image is not None and (
                    image.deleted if entry['kind'] == 'delete' else
                    not image.deleted and image.command == entry['command']
                    and image.filename == entry['filename'] and image.content_hash == entry['content_hash']
                )
                if not committed:
                    if destination is not None and destination.exists() and (entry.get('installed') or (backup is not None and backup.exists())):
                        if digest_file(destination) == entry['content_hash']:
                            destination.unlink()
                        elif entry.get('installed') or source == destination:
                            raise RandpicError('目标被修改，保留操作日志和备份，避免覆盖用户文件')
                    if backup is not None and backup.exists():
                        if source.exists() or source.is_symlink():
                            raise RandpicError('恢复位置已存在，保留原图备份，避免覆盖用户文件')
                        backup.rename(source)
                for temporary in (backup, payload):
                    if temporary is not None:
                        with suppress(FileNotFoundError):
                            temporary.unlink()
                entry_path.unlink()
            except Exception as exc:
                logger.error(f'恢复图库文件操作失败，已保留备份：{entry_path} ({exc})')
        # 只清理未被日志引用的准备文件；原始图片从未放入这个目录。
        referenced = set()
        for entry_path in self.directory.glob('*.json'):
            try:
                referenced.add(json.loads(entry_path.read_text(encoding='utf-8')).get('payload'))
            except (OSError, ValueError):
                return
        for path in self.prepared.glob('*.bin'):
            if self._key(path) not in referenced:
                with suppress(OSError):
                    path.unlink()
