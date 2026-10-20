"""持久化发图消息与图片的对应关系，并执行可回滚的本地删除。"""

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional

import aiosqlite
from nonebot.log import logger


class ImageDeletionError(ValueError):
    """可以直接向用户说明的删除失败原因。"""


@dataclass(frozen=True)
class DeletedImage:
    command: str
    img_url: str
    content_hash: str
    already_deleted: bool
    sync_pending: bool


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as image_file:
        for chunk in iter(lambda: image_file.read(128 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class SentImageStore:
    def __init__(self, database_path: Path, image_root: Path):
        self.database_path = database_path
        self.image_root = image_root

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS randpic_sent_images (
                    bot_id TEXT NOT NULL,
                    group_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    img_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    sync_pending INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (bot_id, group_id, message_id)
                )
            ''')
            await db.execute('''
                CREATE INDEX IF NOT EXISTS randpic_sent_images_picture
                ON randpic_sent_images (img_url, content_hash)
            ''')
            await db.commit()
            await self._recover_staged_files(db)

    def image_path(self, command: str, img_url: str) -> Path:
        parts = PurePosixPath(img_url).parts
        if (
            not re.fullmatch(r'[A-Za-z0-9一-龥]+', command)
            or len(parts) != 2
            or parts[0] != command
            or parts[1] in {'.', '..'}
            or '\\' in img_url
        ):
            raise ImageDeletionError('图片路径记录异常，未执行删除，请管理员检查日志。')
        path = self.image_root.joinpath(*parts)
        if path.is_symlink():
            raise ImageDeletionError('图片路径为符号链接，未执行删除。')
        try:
            path.resolve().relative_to(self.image_root.resolve())
        except ValueError as e:
            raise ImageDeletionError('图片路径超出图库范围，未执行删除。') from e
        return path

    async def record_sent(
        self, bot_id: str, group_id: int, message_id: Optional[int], command: str,
        image_id: int, img_url: str, content_hash: str, *, count_send: bool = True,
    ) -> None:
        self.image_path(command, img_url)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('BEGIN IMMEDIATE')
            async with db.execute(
                f'SELECT id FROM Pic_of_{command} WHERE id = ? AND img_url = ?',
                (image_id, img_url),
            ) as cursor:
                exists = await cursor.fetchone() is not None
            if exists and count_send:
                await db.execute(
                    f'UPDATE Pic_of_{command} SET send_count = send_count + 1 WHERE id = ?',
                    (image_id,),
                )
            if message_id is None:
                await db.commit()
                return
            # 发图等待期间图片可能已被管理员删除；此时仍记录消息，但不恢复图片。
            async with db.execute(
                'SELECT COALESCE(MAX(sync_pending), 0) FROM randpic_sent_images '
                'WHERE img_url = ? AND content_hash = ? AND deleted = 1',
                (img_url, content_hash),
            ) as cursor:
                pending = (await cursor.fetchone())[0] if not exists else 0
            await db.execute('''
                INSERT OR REPLACE INTO randpic_sent_images
                    (bot_id, group_id, message_id, command, img_url, content_hash, deleted, sync_pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (str(bot_id), group_id, message_id, command, img_url,
                  content_hash, int(not exists), pending))
            await db.commit()

    async def delete(self, bot_id: str, group_id: int, message_id: int) -> DeletedImage:
        """调用方持有图库操作锁；文件先暂存，事务失败时恢复，成功后清理。"""
        staged = None
        image_path = None
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('BEGIN IMMEDIATE')
            try:
                async with db.execute('''
                    SELECT command, img_url, content_hash, deleted, sync_pending
                    FROM randpic_sent_images
                    WHERE bot_id = ? AND group_id = ? AND message_id = ?
                ''', (str(bot_id), group_id, message_id)) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ImageDeletionError(
                        '无法找到这条消息对应的图片，请引用本插件升级后发送的图库图片。'
                    )
                command, img_url, content_hash, deleted, pending = row
                image_path = self.image_path(command, img_url)
                if deleted:
                    await db.rollback()
                    return DeletedImage(command, img_url, content_hash, True, bool(pending))
                if image_path.exists():
                    if not image_path.is_file() or file_digest(image_path) != content_hash:
                        raise ImageDeletionError('这条消息对应的文件已被替换，未删除当前文件。')
                    staged = image_path.with_name(
                        f'.randpic-delete-{image_path.name}.{uuid.uuid4().hex}.tmp'
                    )
                    image_path.rename(staged)
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (f'Pic_of_{command}',),
                ) as cursor:
                    table_exists = await cursor.fetchone() is not None
                if table_exists:
                    await db.execute(f'DELETE FROM Pic_of_{command} WHERE img_url = ?', (img_url,))
                # 保留删除标记，其他历史引用能够提示已删除，也能重试失败的网页/OSS同步。
                await db.execute('''
                    UPDATE randpic_sent_images SET deleted = 1, sync_pending = 1
                    WHERE img_url = ? AND content_hash = ?
                ''', (img_url, content_hash))
                await db.execute(
                    'UPDATE folder_snapshot SET folder_mtime = ?, checked_at = ? WHERE command = ?',
                    (image_path.parent.stat().st_mtime, time.time(), command),
                )
                await db.commit()
            except BaseException:
                await db.rollback()
                if staged is not None and staged.exists():
                    if image_path.exists():
                        logger.error(f'删除回滚时目标已存在，保留暂存文件以免覆盖：{staged}')
                    else:
                        staged.rename(image_path)
                raise
        if staged is not None:
            try:
                staged.unlink()
            except OSError as e:
                logger.warning(f'图片已从图库删除，但清理暂存文件失败，将在启动时重试：{staged} ({e})')
        # 删除暂存文件也会改变目录时间，刷新快照以免重启时无谓重建索引。
        try:
            async with aiosqlite.connect(self.database_path) as db:
                await db.execute(
                    'UPDATE folder_snapshot SET folder_mtime = ?, checked_at = ? WHERE command = ?',
                    (image_path.parent.stat().st_mtime, time.time(), command),
                )
                await db.commit()
        except Exception as e:
            logger.warning(f'图片已删除，但刷新目录快照失败：{command} ({e})')
        return DeletedImage(command, img_url, content_hash, False, True)

    async def has_current_image(self, image: DeletedImage) -> bool:
        """失败重试时文件名可能已分配给新图，此时不能再删除同名网页/OSS资源。"""
        return self.image_path(image.command, image.img_url).exists()

    async def mark_synced(self, image: DeletedImage) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('''
                UPDATE randpic_sent_images SET sync_pending = 0
                WHERE img_url = ? AND content_hash = ? AND deleted = 1
            ''', (image.img_url, image.content_hash))
            await db.commit()

    async def pending_deletions(self) -> List[DeletedImage]:
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('''
                SELECT DISTINCT command, img_url, content_hash FROM randpic_sent_images
                WHERE deleted = 1 AND sync_pending = 1
            ''') as cursor:
                return [DeletedImage(*row, True, True) for row in await cursor.fetchall()]

    async def _recover_staged_files(self, db: aiosqlite.Connection) -> None:
        """恢复删除事务中断的原图，清理已提交删除留下的暂存文件。"""
        for staged in self.image_root.glob('*/.randpic-delete-*.tmp'):
            match = re.fullmatch(r'\.randpic-delete-(.+)\.[0-9a-f]{32}\.tmp', staged.name)
            if match is None:
                continue
            try:
                command = staged.parent.name
                img_url = f'{command}/{match[1]}'
                original = self.image_path(command, img_url)
                async with db.execute(
                    'SELECT MIN(deleted) FROM randpic_sent_images WHERE img_url = ? AND content_hash = ?',
                    (img_url, file_digest(staged)),
                ) as cursor:
                    deleted = (await cursor.fetchone())[0]
                if deleted == 1:
                    staged.unlink()
                elif not original.exists():
                    staged.rename(original)
                else:
                    logger.error(f'恢复删除事务时目标已存在，保留暂存文件：{staged}')
            except Exception as e:
                logger.error(f'恢复图片删除暂存文件失败：{staged} ({e})')
