"""异步应用服务：业务串行提交，网页及时发布，按远端实际状态同步。"""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

from .ali_oss import OSSUploaderV2
from .config import Config
from .library import ImageLibrary
from .models import ImageRecord, ImageSnapshot, MutationResult, PublishedFile, RandpicError, SyncResult
from .progress import SyncProgress
from .utils import run_blocking
from .web import GalleryPublisher


RETRY_INTERVAL = 30


class RandpicService:
    def __init__(self, config: Config, root: Path):
        self.config = config
        self.library = ImageLibrary(root)
        self.repository = self.library.repository
        self.publisher = GalleryPublisher(self.library.root, config.randpic_web_page_size)
        self.client: Optional[httpx.AsyncClient] = None
        self.uploader = OSSUploaderV2(config) if config.oss_enabled else None
        self._mutation_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self.ready = False
        self._web_dirty = True
        self._published_revision = -1
        self._published_remote_enabled = False
        self._manifest: Dict[str, PublishedFile] = {}
        self._remote_dirty = True
        self._closed = False

    @property
    def commands(self):
        return self.library.settings.commands

    def is_enabled(self, command: str, group_id: int) -> bool:
        groups = self.commands.get(command)
        return self.ready and groups is not None and group_id not in groups and group_id not in self.config.randpic_banner_group

    def _require_ready(self) -> None:
        if not self.ready or self._closed:
            raise RandpicError('图库尚未就绪，请稍后重试。')

    async def start(self, *, background: bool = True) -> None:
        async with self._mutation_lock:
            if self.ready:
                return
            await run_blocking(self.library.start)
            try:
                self.client = httpx.AsyncClient(
                    timeout=self.config.randpic_download_timeout, follow_redirects=True,
                    headers={'User-Agent': 'Mozilla/5.0 Randpic/2.0', 'Referer': 'https://gchat.qpic.cn/'},
                )
                if self.uploader is not None:
                    await self.uploader.open()
                await self._refresh_locked(force=True)
                self.ready = True
                self._closed = False
            except BaseException:
                if self.client is not None:
                    await self.client.aclose()
                if self.uploader is not None:
                    await self.uploader.close()
                await run_blocking(self.library.close)
                raise
        if background:
            self._worker = asyncio.create_task(self._retry_worker(), name='randpic-sync')
            self._wake.set()

    async def close(self) -> None:
        self._closed = True
        self.ready = False
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        async with self._sync_lock:
            async with self._mutation_lock:
                try:
                    if self.uploader is not None:
                        await self.uploader.close()
                    if self.client is not None:
                        await self.client.aclose()
                        self.client = None
                finally:
                    await run_blocking(self.library.close)

    async def _refresh_locked(self, *, force: bool = False) -> Optional[str]:
        try:
            revision = self.library.revision
            remote_enabled = self.uploader is not None
            if (not force and not self._web_dirty and revision == self._published_revision
                    and remote_enabled == self._published_remote_enabled):
                return None
            images = await run_blocking(self.repository.list_images)
            manifest = await run_blocking(
                self.publisher.publish, images, list(self.commands),
                set(self.config.randpic_oss_no_upload_list), remote_enabled=remote_enabled,
            )
            self._manifest = manifest
            self._published_revision = revision
            self._published_remote_enabled = remote_enabled
            self._web_dirty = False
            self._remote_dirty = remote_enabled
            return None
        except Exception as exc:
            self._web_dirty = True
            logger.error(f'图库已保存，但网页同步失败，将自动重试：{exc}')
            return '网页同步失败，将自动重试'

    async def download(self, url: str) -> bytes:
        self._require_ready()
        if urlsplit(url).scheme not in {'http', 'https'}:
            raise RandpicError('图片下载地址无效。')
        limit = self.config.randpic_download_max_mb * 1024 * 1024
        try:
            async with self.client.stream('GET', url) as response:
                response.raise_for_status()
                length = response.headers.get('Content-Length')
                if length and length.isdigit() and int(length) > limit:
                    raise RandpicError(f'原图超过 {self.config.randpic_download_max_mb}MB，请先压缩后再添加。')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise RandpicError(f'原图超过 {self.config.randpic_download_max_mb}MB，请先压缩后再添加。')
                    data.extend(chunk)
                return bytes(data)
        except RandpicError:
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning(f'下载图片失败：{exc}')
            raise RandpicError('图片下载失败，请稍后重新发送。') from exc

    async def add(self, command: str, data: bytes) -> MutationResult:
        async with self._mutation_lock:
            self._require_ready()
            result = await run_blocking(self.library.add, command, data)
            error = await self._refresh_locked()
        self._wake.set()
        return replace(result, web_error=error)

    async def delete(self, bot_id: str, group_id: int, message_id: int) -> MutationResult:
        async with self._mutation_lock:
            self._require_ready()
            result = await run_blocking(self.library.delete, bot_id, group_id, message_id)
            error = await self._refresh_locked()
        self._wake.set()
        return replace(result, web_error=error)

    async def pick(self, command: str) -> Optional[ImageSnapshot]:
        async with self._mutation_lock:
            self._require_ready()
            snapshot = await run_blocking(self.library.pick, command)
            error = await self._refresh_locked()
        if error or self._remote_dirty:
            self._wake.set()
        return snapshot

    async def record_sent(self, image: ImageRecord, bot_id: str, group_id: int, result: Any, *, count_send: bool) -> None:
        message_id = None
        try:
            message_id = int(result['message_id'])
        except (TypeError, ValueError, KeyError):
            logger.warning('发图接口未返回 message_id，本条消息将无法引用删除。')
        try:
            async with self._mutation_lock:
                self._require_ready()
                await run_blocking(self.repository.record_message, image.id, str(bot_id), group_id, message_id, count_send)
        except Exception as exc:
            logger.error(f'图片已发送，但记录发送信息失败：{exc}')

    async def set_disabled(self, command: str, group_id: int, disabled: bool) -> bool:
        async with self._mutation_lock:
            self._require_ready()
            return await run_blocking(self.library.settings.set_disabled, command, group_id, disabled)

    async def sync(self, *, force: bool = False) -> SyncResult:
        async with self._sync_lock:
            if self._closed:
                return SyncResult()
            async with self._mutation_lock:
                self._require_ready()
                error = await self._refresh_locked(force=force)
                manifest = dict(self._manifest)
            if error:
                return SyncResult(pending=1, web_error=error)
            if self.uploader is None or (not force and not self._remote_dirty):
                return SyncResult()
            self._remote_dirty = True
            started = asyncio.get_running_loop().time()
            try:
                remote = await self.uploader.list_files()
            except Exception as exc:
                logger.error(f'列举 OSS 文件失败，本轮未执行上传或删除，将自动重试：{exc}')
                return SyncResult(failed=1, pending=1)

            # 普通 PutObject 的 ETag 与 MD5 相同；大多数对象只需分页列表即可确认。
            jobs = []
            comparisons = []
            failed_keys = set()
            for key, file in manifest.items():
                old = remote.get(key)
                if force or old is None or old.size != file.size:
                    jobs.append((key, file))
                elif old.etag != file.etag:
                    comparisons.append((key, file, old))
            candidates = iter(comparisons)

            async def compare_worker():
                for key, file, old in candidates:
                    try:
                        if not await self.uploader.matches_file(key, file, old):
                            jobs.append((key, file))
                    except Exception as exc:
                        failed_keys.add(key)
                        logger.error(f'核对 OSS 文件失败，将自动重试：{key} ({exc})')

            limit = self.config.randpic_upload_concurrency
            await asyncio.gather(*(compare_worker() for _ in range(min(limit, len(comparisons)))))
            jobs.extend((key, None) for key in remote.keys() - manifest.keys())
            succeeded = completed = 0

            if jobs:
                with SyncProgress(len(jobs)) as progress:
                    async def phase(items):
                        remaining = iter(items)

                        async def worker():
                            nonlocal succeeded, completed
                            for key, file in remaining:
                                try:
                                    async with self._mutation_lock:
                                        # 跳过被同步期间新操作替代的上传或删除。
                                        if self._manifest.get(key) != file:
                                            continue
                                    if file is None:
                                        ok = await self.uploader.delete_file(key)
                                    else:
                                        ok = await self.uploader.upload_file(file.path, key, file.content_hash)
                                    if ok:
                                        succeeded += 1
                                    else:
                                        failed_keys.add(key)
                                except Exception as exc:
                                    failed_keys.add(key)
                                    logger.error(f'同步 OSS 文件失败，将自动重试：{key} ({exc})')
                                finally:
                                    completed += 1
                                    progress.update(completed, succeeded, len(failed_keys))

                        await asyncio.gather(*(worker() for _ in range(min(limit, len(items)))))

                    # 先处理图片，再更新网页；网络期间不占用图库修改锁。
                    await phase([job for job in jobs if not job[0].endswith('.html')])
                    await phase([job for job in jobs if job[0].endswith('.html')])

            async with self._mutation_lock:
                pending_keys = failed_keys | {
                    key for key in manifest.keys() | self._manifest.keys()
                    if manifest.get(key) != self._manifest.get(key)
                }
                if self._web_dirty or self.library.revision != self._published_revision:
                    pending_keys.add('<网页待发布>')
                self._remote_dirty = bool(pending_keys)
                changed = manifest != self._manifest
            if changed:
                self._wake.set()
            if jobs or failed_keys:
                elapsed = asyncio.get_running_loop().time() - started
                summary = (f'OSS 同步完成：成功 {succeeded}，失败 {len(failed_keys)}，'
                           f'待处理 {len(pending_keys)}，用时 {elapsed:.1f} 秒')
                if failed_keys:
                    logger.warning(summary + '；将重新比较远端内容后重试')
                else:
                    logger.info(summary)
            return SyncResult(succeeded=succeeded, failed=len(failed_keys), pending=len(pending_keys))

    async def _retry_worker(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=RETRY_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._closed:
                return
            if (not self._web_dirty and self.library.revision == self._published_revision
                    and (self.uploader is None or not self._remote_dirty)):
                continue
            try:
                await self.sync()
            except Exception as exc:
                logger.error(f'图库后台同步失败，将稍后重试：{exc}')
