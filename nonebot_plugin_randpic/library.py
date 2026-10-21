"""图库业务：启动整理、添加、引用删除、发送快照，均在工作线程中执行。"""

import hashlib
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from nonebot.log import logger

from .compress import process_image
from .config import CommandSettings
from .journal import FileJournal
from .models import DuplicateImage, ImageRecord, ImageSnapshot, MutationResult, RandpicError
from .repository import Repository
from .utils import SUPPORTED_EXTENSIONS, StoreLock, digest_file, image_stem, parse_index, safe_path


@dataclass
class _Candidate:
    source: Path
    content_hash: str
    original_hash: str
    phash: str
    extension: str
    payload: Optional[Path]
    previous: Optional[ImageRecord] = None
    filename: str = ''


class ImageLibrary:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.image_root = self.root / 'img'
        self.repository = Repository(self.root / 'database' / 'data.db')
        self.settings = CommandSettings(self.root)
        self.journal = FileJournal(self.root, self.repository)
        self.store_lock = StoreLock(self.root / 'database' / 'randpic.lock')
        self.revision = 0

    def start(self) -> None:
        for directory in (self.root, self.image_root, self.root / 'database'):
            if directory.is_symlink():
                raise RandpicError('图库内部目录不能是符号链接。')
            directory.mkdir(parents=True, exist_ok=True)
        self.store_lock.acquire()
        try:
            self.repository.initialize()
            self.journal.initialize()
            self.journal.recover()
            self.settings.load()
            self.reconcile()
        except BaseException:
            self.store_lock.release()
            raise

    def close(self) -> None:
        self.store_lock.release()

    def _candidates(self, command: str, existing: List[ImageRecord]) -> List[_Candidate]:
        directory = safe_path(self.image_root, f'{command}/placeholder').parent
        directory.mkdir(parents=True, exist_ok=True)
        cached = {image.content_hash: image for image in existing}
        candidates = []
        for source in sorted(directory.iterdir()):
            if source.is_symlink() or not source.is_file() or source.name.startswith('.randpic-'):
                continue
            if source.suffix and source.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                before = source.stat()
                if before.st_size > 128 * 1024 * 1024:
                    raise RandpicError('单张图片大于 128MB，请先压缩')
                data = source.read_bytes()
                original_hash = hashlib.sha256(data).hexdigest()
                known = cached.get(original_hash)
                payload = None
                if known is not None:
                    content_hash, phash, extension = original_hash, known.phash, Path(known.filename).suffix.lower()
                else:
                    image = process_image(data)
                    content_hash, phash, extension = image.content_hash, image.phash, image.extension
                    if image.data != data:
                        payload = self.journal.stage(image.data)
                after = source.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise RandpicError('检查时文件被其他程序修改，请在 Bot 停止后操作')
                suffix = source.suffix.lower()
                if suffix == extension or (extension == '.jpg' and suffix == '.jpeg') or (extension == '.tiff' and suffix == '.tif'):
                    extension = source.suffix
                candidates.append(_Candidate(source, content_hash, original_hash, phash, extension, payload))
            except Exception as exc:
                logger.warning(f'无法索引图片，保留原文件并跳过：{source} ({exc})')
        return candidates

    @staticmethod
    def _match(candidates: List[_Candidate], existing: List[ImageRecord]) -> None:
        by_path = {image.filename: image for image in existing}
        used = set()
        # 先保留所有原路径和内容一致的图片，避免新加入的重复文件抢占旧图身份。
        for candidate in candidates:
            old = by_path.get(candidate.source.name)
            if old is not None and old.content_hash == candidate.original_hash:
                candidate.previous = old
                used.add(old.id)
        by_hash: Dict[str, List[ImageRecord]] = defaultdict(list)
        for image in existing:
            if image.id not in used:
                by_hash[image.content_hash].append(image)
        for candidate in candidates:
            if candidate.previous is None and by_hash[candidate.original_hash]:
                candidate.previous = by_hash[candidate.original_hash].pop()

    def _plan_names(self, db: sqlite3.Connection, command: str, candidates: List[_Candidate]) -> int:
        directory = self.image_root / command
        maximum = self.repository.sequence(db, command)
        for path in directory.iterdir():
            index = parse_index(path.stem, command)
            if index is not None:
                maximum = max(maximum, index)
        used = set()

        def standard(candidate: _Candidate) -> bool:
            index = parse_index(candidate.source.stem, command)
            return index is not None and index > 0 and candidate.source.stem == image_stem(command, index)

        # 已经正确命名且扩展名准确的图片优先保留原名。
        candidates.sort(key=lambda c: (not (standard(c) and c.source.suffix == c.extension), c.source.name))
        for candidate in candidates:
            stem = candidate.source.stem
            destination = directory / (stem + candidate.extension)
            if not standard(candidate) or stem in used or (destination != candidate.source and destination.exists()):
                maximum += 1
                stem = image_stem(command, maximum)
            candidate.filename = stem + candidate.extension
            used.add(stem)
        return maximum

    def reconcile(self) -> None:
        planned: Dict[str, Tuple[List[_Candidate], List[ImageRecord]]] = {}
        for command in self.settings.commands:
            existing = self.repository.list_images(command)
            candidates = self._candidates(command, existing)
            self._match(candidates, existing)
            planned[command] = (candidates, existing)
        try:
            with self.repository.connection(write=True) as db:
                changed = False
                for command, (candidates, existing) in planned.items():
                    maximum = self._plan_names(db, command, candidates)
                    db.execute('UPDATE randpic_images SET deleted = 1 WHERE command = ? AND deleted = 0', (command,))
                    records = []
                    for candidate in candidates:
                        previous = candidate.previous
                        image = ImageRecord(
                            previous.id if previous else uuid.uuid4().hex, command, candidate.filename,
                            candidate.content_hash, candidate.phash, previous.send_count if previous else 0,
                        )
                        if candidate.payload is not None or candidate.filename != candidate.source.name:
                            self.journal.write(image, candidate.source, candidate.payload)
                        destination = self.image_root / image.key
                        stat = destination.stat()
                        image = replace(image, byte_size=stat.st_size)
                        self.repository.upsert_image(db, image)
                        records.append(image)
                    self.repository.set_sequence(db, command, maximum)
                    old_state = {(i.id, i.filename, i.content_hash) for i in existing}
                    new_state = {(i.id, i.filename, i.content_hash) for i in records}
                    changed = changed or old_state != new_state
            if changed:
                self.revision += 1
        finally:
            # 事务已经提交时清理备份，事务失败时还原文件。
            self.journal.recover()

    def add(self, command: str, data: bytes) -> MutationResult:
        from .utils import valid_command
        if not valid_command(command):
            raise RandpicError('名称仅支持字母、汉字、数字，且不能超过 120 字节。')
        processed = process_image(data)
        directory = safe_path(self.image_root, f'{command}/placeholder').parent
        directory.mkdir(parents=True, exist_ok=True)
        image = None
        try:
            with self.repository.connection(write=True) as db:
                if self.repository.duplicate(db, command, processed.content_hash, processed.phash):
                    raise DuplicateImage('这张图片已经存在相似图，不能重复添加。')
                maximum = self.repository.sequence(db, command)
                for path in directory.iterdir():
                    index = parse_index(path.stem, command)
                    if index is not None:
                        maximum = max(maximum, index)
                index = maximum + 1
                image = ImageRecord(uuid.uuid4().hex, command, image_stem(command, index) + processed.extension,
                                    processed.content_hash, processed.phash)
                payload = self.journal.stage(processed.data)
                self.journal.write(image, None, payload)
                stat = (directory / image.filename).stat()
                image = replace(image, byte_size=stat.st_size)
                self.repository.upsert_image(db, image)
                self.repository.set_sequence(db, command, index)
        finally:
            self.journal.recover()
        self.revision += 1
        saved = self.settings.add(command)
        return MutationResult(image, settings_saved=saved)

    def delete(self, bot_id: str, group_id: int, message_id: int) -> MutationResult:
        try:
            with self.repository.connection(write=True) as db:
                image = self.repository.referenced_image(db, bot_id, group_id, message_id)
                if image.deleted:
                    return MutationResult(image, already_deleted=True)
                path = safe_path(self.image_root, image.key)
                if path.exists() and (not path.is_file() or digest_file(path) != image.content_hash):
                    raise RandpicError('这条消息对应的文件已被替换，未执行删除。请重新发送当前图片后再操作。')
                self.journal.delete(image)
                self.repository.mark_deleted(db, image.id)
                result = MutationResult(replace(image, deleted=True))
        finally:
            self.journal.recover()
        self.revision += 1
        return result

    def pick(self, command: str) -> Optional[ImageSnapshot]:
        while True:
            image = self.repository.choose(command)
            if image is None:
                return None
            path = safe_path(self.image_root, image.key)
            try:
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != image.content_hash:
                    raise RandpicError('图片在运行时被修改，请重启 Bot 重新索引。')
                return ImageSnapshot(image, data)
            except FileNotFoundError:
                # 清理在运行中意外消失的文件，并继续挑选其他图片。
                with self.repository.connection(write=True) as db:
                    self.repository.mark_deleted(db, image.id)
                self.revision += 1
                logger.warning(f'图片文件已丢失，已从图库索引移除：{path}')
