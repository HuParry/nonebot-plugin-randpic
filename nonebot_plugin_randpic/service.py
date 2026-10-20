"""异步应用服务：业务串行提交，网页及时发布，远端任务独立重试。"""

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from nonebot.log import logger

from .ali_oss import OSSUploaderV2
from .config import Config
from .library import ImageLibrary
from .models import ImageRecord, ImageSnapshot, MutationResult, RandpicError, SyncResult
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

    async def _refresh_locked(self, *, force: bool = False, force_upload: bool = False) -> Optional[str]:
        try:
            revision, published = await run_blocking(self.repository.revision)
            if not force and not force_upload and not self._web_dirty and revision == published:
                return None
            images = await run_blocking(self.repository.list_images)
            manifest = await run_blocking(
                self.publisher.publish, images, list(self.commands), set(self.config.randpic_oss_no_upload_list)
            )
            if self.uploader is not None:
                await run_blocking(self.repository.reconcile_remote, manifest, force=force_upload)
            await run_blocking(self.repository.published, revision)
            self._web_dirty = False
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
        if error:
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
                error = await self._refresh_locked(force_upload=force)
            if error:
                return SyncResult(web_error=error)
            if self.uploader is None:
                return SyncResult()
            jobs = await run_blocking(self.repository.sync_jobs)
            if not jobs:
                return SyncResult()
            succeeded = failed = completed = 0
            started = asyncio.get_running_loop().time()

            with SyncProgress(len(jobs)) as progress:
                async def phase(items):
                    queue = asyncio.Queue()
                    for job in items:
                        queue.put_nowait(job)

                    async def worker():
                        nonlocal succeeded, failed, completed
                        while not queue.empty():
                            job = queue.get_nowait()
                            try:
                                if job.action == 'delete':
                                    ok = await self.uploader.delete_file(job.key)
                                else:
                                    ok = await self.uploader.upload_file(job.local_path, job.key, job.content_hash)
                                if ok:
                                    await run_blocking(self.repository.complete_job, job)
                                    succeeded += 1
                                else:
                                    await run_blocking(self.repository.fail_job, job, '远端请求失败或任务已过期')
                                    failed += 1
                            except Exception as exc:
                                logger.error(f'同步 OSS 任务失败，已保留以便重试：{job.key} ({exc})')
                                failed += 1
                            finally:
                                queue.task_done()
                                completed += 1
                                progress.update(completed, succeeded, failed)

                    await asyncio.gather(*(worker() for _ in range(min(self.config.randpic_upload_concurrency, len(items)))))

                # 先同步图片，再同步页面。
                await phase([job for job in jobs if not job.key.endswith('.html')])
                await phase([job for job in jobs if job.key.endswith('.html')])
            pending = len(await run_blocking(self.repository.sync_jobs))
            elapsed = asyncio.get_running_loop().time() - started
            summary = (f'OSS 同步完成：成功 {succeeded}，失败 {failed}，待处理 {pending}，'
                       f'用时 {elapsed:.1f} 秒')
            if failed:
                logger.warning(summary + '；失败任务将自动重试')
            else:
                logger.info(summary)
            return SyncResult(succeeded=succeeded, failed=failed, pending=pending)

    async def _retry_worker(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=RETRY_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._closed:
                return
            try:
                await self.sync()
            except Exception as exc:
                logger.error(f'图库后台同步失败，将稍后重试：{exc}')
