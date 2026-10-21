"""文件命名、原子写入以及异步线程调用。"""

import asyncio
import hashlib
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from .models import RandpicError


COMMAND_PATTERN = re.compile(r'[A-Za-z0-9一-龥]+')
SUPPORTED_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff'})


def valid_command(command: str) -> bool:
    return bool(command and COMMAND_PATTERN.fullmatch(command) and len(command.encode('utf-8')) <= 120)


def image_stem(command: str, index: int) -> str:
    return f'randpic_{command}_{index:010d}'


def parse_index(stem: str, command: str) -> Optional[int]:
    prefix = f'randpic_{command}_'
    if not stem.startswith(prefix):
        return None
    number = stem[len(prefix):]
    return int(number) if number.isascii() and number.isdigit() else None


def safe_path(root: Path, key: str) -> Path:
    parts = PurePosixPath(key).parts
    if (
        len(parts) != 2 or not valid_command(parts[0]) or parts[1] in {'.', '..'}
        or '\\' in key or PurePosixPath(key).is_absolute()
    ):
        raise RandpicError('图片路径不合法。')
    path = root.joinpath(*parts)
    if path.is_symlink() or path.parent.is_symlink():
        raise RandpicError('图库不支持符号链接，请使用普通图片文件和目录。')
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RandpicError('图片路径超出图库目录。') from exc
    return path


def digest_file(path: Path, algorithm: str = 'sha256') -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open('rb') as file:
        for block in iter(lambda: file.read(128 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """取消协程时等待文件/数据库操作结束，避免后台线程越过操作锁继续写入。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


class StoreLock:
    """多个进程不得同时整理同一个图库；同一 Bot 内的多个账号不受影响。"""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    file.write(b'0')
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            file.close()
            raise RandpicError('这个数据目录正在被另一个 Bot 进程使用。') from exc
        self.file = file

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
