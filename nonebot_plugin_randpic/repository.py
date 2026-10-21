"""SQLite 存储。连接只在线程内使用，事务不跨聊天请求或网络请求。"""

import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from typing import Iterator, List, Optional

from .models import ImageRecord, RandpicError


IMAGE_COLUMNS = ', '.join(field.name for field in fields(ImageRecord))
SCHEMA = '''
CREATE TABLE IF NOT EXISTS randpic_images (
    id TEXT PRIMARY KEY, command TEXT NOT NULL, filename TEXT NOT NULL,
    content_hash TEXT NOT NULL, phash TEXT NOT NULL,
    send_count INTEGER NOT NULL DEFAULT 0 CHECK(send_count >= 0),
    byte_size INTEGER NOT NULL DEFAULT 0 CHECK(byte_size >= 0),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0, 1))
) WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS randpic_image_path
    ON randpic_images(command, filename) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS randpic_image_command
    ON randpic_images(command, id) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS randpic_image_hash
    ON randpic_images(command, content_hash) WHERE deleted = 0;
CREATE TABLE IF NOT EXISTS randpic_messages (
    bot_id TEXT NOT NULL, group_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    image_id TEXT NOT NULL REFERENCES randpic_images(id),
    PRIMARY KEY(bot_id, group_id, message_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS randpic_sequences (
    command TEXT PRIMARY KEY, last_index INTEGER NOT NULL CHECK(last_index >= 0)
) WITHOUT ROWID;
'''


def _image(row: sqlite3.Row) -> ImageRecord:
    values = dict(row)
    values['deleted'] = bool(values['deleted'])
    return ImageRecord(**values)


def _hamming(left: str, right: str) -> int:
    try:
        if len(left) != 16 or len(right) != 16:
            return 65
        return bin(int(left, 16) ^ int(right, 16)).count('1')
    except (ValueError, TypeError):
        return 65


class Repository:
    def __init__(self, path: Path):
        self.path = path

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

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode = WAL')
            db.execute('PRAGMA synchronous = FULL')
            db.executescript(SCHEMA)

    @staticmethod
    def mark_deleted(db: sqlite3.Connection, image_id: str) -> None:
        db.execute('UPDATE randpic_images SET deleted = 1 WHERE id = ?', (image_id,))

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
        exact = db.execute('''SELECT 1 FROM randpic_images
            WHERE command = ? AND deleted = 0 AND content_hash = ? LIMIT 1''',
                           (command, content_hash)).fetchone()
        if exact is not None:
            return True
        return db.execute('''SELECT 1 FROM randpic_images
            WHERE command = ? AND deleted = 0 AND hamming_distance(phash, ?) <= 5 LIMIT 1''',
                          (command, phash)).fetchone() is not None

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
