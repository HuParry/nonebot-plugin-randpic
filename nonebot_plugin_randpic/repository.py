"""SQLite 存储。连接只在线程内使用，事务不跨聊天请求或网络请求。"""

import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Tuple

from nonebot.log import logger

from .models import ImageRecord, RandpicError, SyncJob
from .utils import digest_file, parse_index, safe_path, valid_command


IMAGE_COLUMNS = ', '.join(field.name for field in fields(ImageRecord))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS randpic_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS randpic_images (
    id TEXT PRIMARY KEY, command TEXT NOT NULL, filename TEXT NOT NULL,
    content_hash TEXT NOT NULL, phash TEXT NOT NULL, send_count INTEGER NOT NULL DEFAULT 0 CHECK(send_count >= 0),
    byte_size INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0, 1)),
    validated INTEGER NOT NULL DEFAULT 1 CHECK(validated IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS randpic_image_path ON randpic_images(command, filename) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS randpic_image_command ON randpic_images(command, deleted, id);
CREATE TABLE IF NOT EXISTS randpic_messages (
    bot_id TEXT NOT NULL, group_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    image_id TEXT NOT NULL REFERENCES randpic_images(id), PRIMARY KEY(bot_id, group_id, message_id)
);
CREATE TABLE IF NOT EXISTS randpic_sequences (command TEXT PRIMARY KEY, last_index INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS randpic_remote (key TEXT PRIMARY KEY, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS randpic_sync_jobs (
    key TEXT PRIMARY KEY, action TEXT NOT NULL CHECK(action IN ('upload', 'delete')),
    local_path TEXT, content_hash TEXT NOT NULL, revision INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
'''


def _image(row: sqlite3.Row) -> ImageRecord:
    values = dict(row)
    values['deleted'] = bool(values['deleted'])
    values['validated'] = bool(values['validated'])
    return ImageRecord(**values)


def _hamming(left: str, right: str) -> int:
    try:
        if len(left) != 16 or len(right) != 16:
            return 65
        return bin(int(left, 16) ^ int(right, 16)).count('1')
    except (ValueError, TypeError):
        return 65


class Repository:
    def __init__(self, path: Path, image_root: Path):
        self.path, self.image_root = path, image_root

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.create_function('hamming_distance', 2, _hamming, deterministic=True)
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def meta(db: sqlite3.Connection, key: str, default: str = '0') -> str:
        row = db.execute('SELECT value FROM randpic_meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute('INSERT OR REPLACE INTO randpic_meta VALUES (?, ?)', (key, value))

    def mark_dirty(self, db: sqlite3.Connection) -> None:
        self.set_meta(db, 'catalog_revision', str(int(self.meta(db, 'catalog_revision')) + 1))

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = int(self.meta(db, 'schema_version')) if 'randpic_meta' in names else 0
            if version > 2:
                raise RandpicError('数据库来自更新版本的 Randpic，不能用 2.0.0 覆盖。')
            legacy = any(name.startswith('Pic_of_') for name in names) or 'randpic_sent_images' in names
            backup = self.path.with_name(self.path.stem + '.pre-v2.db')
            if not version and legacy and not backup.exists():
                temporary = backup.with_name(backup.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    with closing(sqlite3.connect(temporary)) as destination:
                        db.backup(destination)
                    temporary.replace(backup)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            db.execute('PRAGMA journal_mode = WAL')
            db.execute('PRAGMA synchronous = FULL')
            db.executescript(SCHEMA)
        with self.connection(write=True) as db:
            if int(self.meta(db, 'schema_version')) == 0:
                self._migrate_legacy(db, names)
                self.set_meta(db, 'schema_version', '2')
                self.set_meta(db, 'published_revision', '-1')
                self.mark_dirty(db)

    def _migrate_legacy(self, db: sqlite3.Connection, names: set) -> None:
        current: Dict[Tuple[str, str], ImageRecord] = {}
        for name in sorted(names):
            if not name.startswith('Pic_of_') or not valid_command(name[7:]):
                continue
            command = name[7:]
            self.set_sequence(db, command, 0)
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')}
            if 'img_url' not in columns:
                continue
            for row in db.execute(f'SELECT * FROM "{name}"'):
                key = str(row['img_url']).replace('\\', '/')
                try:
                    path = safe_path(self.image_root, key)
                    if key.split('/')[0] != command:
                        raise RandpicError('分类与图片路径不一致')
                    self.set_sequence(db, command, parse_index(path.stem, command) or 0)
                    exists = path.is_file()
                    content_hash = digest_file(path) if exists else ''
                    stat = path.stat() if exists else None
                    count = max(0, int(row['send_count'] or 0)) if 'send_count' in columns else 0
                except (OSError, ValueError, RandpicError) as exc:
                    logger.warning(f'迁移旧图片记录失败，保留原表：{key} ({exc})')
                    continue
                previous = current.get((command, path.name))
                if previous is not None:
                    db.execute('UPDATE randpic_images SET send_count = send_count + ? WHERE id = ?', (count, previous.id))
                    continue
                image = ImageRecord(
                    uuid.uuid4().hex, command, path.name, content_hash,
                    str(row['phash'] or '') if 'phash' in columns else '', count,
                    stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0, not exists, False,
                )
                self.upsert_image(db, image)
                current[(command, path.name)] = image
                db.execute('INSERT OR IGNORE INTO randpic_remote VALUES (?, ?)', (key, ''))
            db.execute('INSERT OR IGNORE INTO randpic_remote VALUES (?, ?)', (f'{command}/index.html', ''))
        if current:
            db.execute('INSERT OR IGNORE INTO randpic_remote VALUES (?, ?)', ('index.html', ''))
        if 'randpic_sent_images' not in names:
            return
        historical: Dict[Tuple[str, str, str], str] = {}
        for row in db.execute('SELECT * FROM randpic_sent_images'):
            try:
                command, key, content_hash = str(row['command']), str(row['img_url']).replace('\\', '/'), str(row['content_hash'])
                path = safe_path(self.image_root, key)
                if path.parent.name != command:
                    raise RandpicError('分类与图片路径不一致')
                self.set_sequence(db, command, parse_index(path.stem, command) or 0)
                image = current.get((command, path.name))
                if row['deleted'] or image is None or image.deleted or image.content_hash != content_hash:
                    identity = (command, path.name, content_hash)
                    image_id = historical.get(identity)
                    if image_id is None:
                        image_id = uuid.uuid4().hex
                        historical[identity] = image_id
                        self.upsert_image(db, ImageRecord(image_id, command, path.name, content_hash, '', deleted=True, validated=False))
                    if row['sync_pending']:
                        db.execute('INSERT OR IGNORE INTO randpic_remote VALUES (?, ?)', (key, ''))
                else:
                    image_id = image.id
                db.execute('INSERT OR REPLACE INTO randpic_messages VALUES (?, ?, ?, ?)',
                           (str(row['bot_id']), int(row['group_id']), int(row['message_id']), image_id))
            except (KeyError, IndexError, ValueError, RandpicError) as exc:
                logger.warning(f'迁移历史发图消息失败，保留原记录：{exc}')

    @staticmethod
    def upsert_image(db: sqlite3.Connection, image: ImageRecord) -> None:
        names = [field.name for field in fields(ImageRecord)]
        values = [getattr(image, name) for name in names]
        updates = ', '.join(f'{name} = excluded.{name}' for name in names if name != 'id')
        db.execute(f'INSERT INTO randpic_images ({IMAGE_COLUMNS}) VALUES ({", ".join("?" for _ in names)}) '
                   f'ON CONFLICT(id) DO UPDATE SET {updates}', values)

    @staticmethod
    def get_image(db: sqlite3.Connection, image_id: str) -> Optional[ImageRecord]:
        row = db.execute(f'SELECT {IMAGE_COLUMNS} FROM randpic_images WHERE id = ?', (image_id,)).fetchone()
        return _image(row) if row else None

    def list_images(self, command: Optional[str] = None, *, include_deleted: bool = False) -> List[ImageRecord]:
        conditions, parameters = [], []
        if command is not None:
            conditions.append('command = ?')
            parameters.append(command)
        if not include_deleted:
            conditions.append('deleted = 0')
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.connection() as db:
            return [_image(row) for row in db.execute(f'SELECT {IMAGE_COLUMNS} FROM randpic_images{where}', parameters)]

    def choose(self, command: str) -> Optional[ImageRecord]:
        # ticket 含聚合，随机数只产生一次；53 位整数转换后严格在 [0, 1) 内，避免 abs(INT64_MIN)。
        with self.connection() as db:
            row = db.execute(f'''
                WITH weighted AS (
                    SELECT id, SUM(1.0 / (send_count + 1.0)) OVER (ORDER BY id) AS cumulative
                    FROM randpic_images WHERE command = ? AND deleted = 0
                ), ticket AS (
                    SELECT ((random() & 9007199254740991) / 9007199254740992.0) * MAX(cumulative) AS value
                    FROM weighted
                )
                SELECT {', '.join('images.' + name.strip() for name in IMAGE_COLUMNS.split(','))}
                FROM weighted CROSS JOIN ticket JOIN randpic_images AS images ON images.id = weighted.id
                WHERE weighted.cumulative >= ticket.value ORDER BY weighted.cumulative LIMIT 1
            ''', (command,)).fetchone()
            return _image(row) if row else None

    @staticmethod
    def duplicate(db: sqlite3.Connection, command: str, content_hash: str, phash: str) -> bool:
        return db.execute('''SELECT 1 FROM randpic_images WHERE command = ? AND deleted = 0
            AND (content_hash = ? OR hamming_distance(phash, ?) <= 5) LIMIT 1''',
                          (command, content_hash, phash)).fetchone() is not None

    @staticmethod
    def sequence(db: sqlite3.Connection, command: str) -> int:
        row = db.execute('SELECT last_index FROM randpic_sequences WHERE command = ?', (command,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def set_sequence(db: sqlite3.Connection, command: str, index: int) -> None:
        db.execute('''INSERT INTO randpic_sequences VALUES (?, ?)
            ON CONFLICT(command) DO UPDATE SET last_index = MAX(last_index, excluded.last_index)''', (command, index))

    def record_message(self, image_id: str, bot_id: str, group_id: int, message_id: Optional[int], count_send: bool) -> None:
        with self.connection(write=True) as db:
            image = self.get_image(db, image_id)
            if image is None:
                raise RandpicError('发送的图片记录不存在。')
            if count_send and not image.deleted:
                db.execute('UPDATE randpic_images SET send_count = send_count + 1 WHERE id = ?', (image_id,))
            if message_id is not None:
                db.execute('INSERT OR REPLACE INTO randpic_messages VALUES (?, ?, ?, ?)',
                           (str(bot_id), group_id, message_id, image_id))

    @staticmethod
    def referenced_image(db: sqlite3.Connection, bot_id: str, group_id: int, message_id: int) -> ImageRecord:
        row = db.execute(f'''SELECT {', '.join('i.' + name.strip() for name in IMAGE_COLUMNS.split(','))}
            FROM randpic_messages AS m JOIN randpic_images AS i ON i.id = m.image_id
            WHERE m.bot_id = ? AND m.group_id = ? AND m.message_id = ?''',
                         (str(bot_id), group_id, message_id)).fetchone()
        if row is None:
            raise RandpicError('找不到这条消息对应的图库图片，请引用本插件发送的图片。')
        return _image(row)

    def revision(self) -> Tuple[int, int]:
        with self.connection() as db:
            return int(self.meta(db, 'catalog_revision')), int(self.meta(db, 'published_revision', '-1'))

    def published(self, revision: int) -> None:
        with self.connection(write=True) as db:
            self.set_meta(db, 'published_revision', str(revision))

    def reconcile_remote(self, manifest: Mapping[str, Tuple[str, str]], *, force: bool = False) -> None:
        with self.connection(write=True) as db:
            known = {row['key']: row['content_hash'] for row in db.execute('SELECT * FROM randpic_remote')}
            jobs = {row['key']: row for row in db.execute('SELECT * FROM randpic_sync_jobs')}
            revision = int(self.meta(db, 'sync_revision'))

            def enqueue(key: str, action: str, path: Optional[str], content_hash: str) -> None:
                nonlocal revision
                old = jobs.get(key)
                if old and (old['action'], old['local_path'], old['content_hash']) == (action, path, content_hash) and not force:
                    return
                revision += 1
                db.execute('''INSERT OR REPLACE INTO randpic_sync_jobs
                    (key, action, local_path, content_hash, revision) VALUES (?, ?, ?, ?, ?)''',
                           (key, action, path, content_hash, revision))

            for key, (path, content_hash) in manifest.items():
                if force or known.get(key) != content_hash or key in jobs:
                    enqueue(key, 'upload', path, content_hash)
            for key in (set(known) | set(jobs)) - set(manifest):
                enqueue(key, 'delete', None, '')
            self.set_meta(db, 'sync_revision', str(revision))

    def sync_jobs(self) -> List[SyncJob]:
        with self.connection() as db:
            return [SyncJob(row['key'], row['action'], row['local_path'], row['content_hash'], row['revision'])
                    for row in db.execute('SELECT * FROM randpic_sync_jobs ORDER BY revision')]

    def complete_job(self, job: SyncJob) -> None:
        with self.connection(write=True) as db:
            if job.action == 'delete':
                db.execute('DELETE FROM randpic_remote WHERE key = ?', (job.key,))
            else:
                db.execute('INSERT OR REPLACE INTO randpic_remote VALUES (?, ?)', (job.key, job.content_hash))
            # 老请求完成时，只能确认自己的版本，不能覆盖期间新加入的删除/上传任务。
            db.execute('DELETE FROM randpic_sync_jobs WHERE key = ? AND revision = ?', (job.key, job.revision))

    def fail_job(self, job: SyncJob, error: str) -> None:
        with self.connection(write=True) as db:
            db.execute('''UPDATE randpic_sync_jobs SET attempts = attempts + 1, last_error = ?
                WHERE key = ? AND revision = ?''', (error[:500], job.key, job.revision))
