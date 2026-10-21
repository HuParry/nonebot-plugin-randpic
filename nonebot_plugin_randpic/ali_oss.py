"""复用异步 OSS 客户端；不持有图库或数据库的锁。"""

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Dict, Optional

import alibabacloud_oss_v2 as oss
import alibabacloud_oss_v2.aio as oss_aio
from nonebot.log import logger

from .config import Config
from .models import PublishedFile, RandpicError, RemoteObject
from .utils import SUPPORTED_EXTENSIONS, parse_index, run_blocking, valid_command


def is_gallery_key(key: str) -> bool:
    """只处理插件自己的网页和规范命名图片，不清理 Bucket 内任意文件。"""
    if key == 'index.html':
        return True
    parts = PurePosixPath(key).parts
    if len(parts) != 2 or key != '/'.join(parts) or not valid_command(parts[0]):
        return False
    command, filename = parts
    if filename == 'index.html' or re.fullmatch(r'page-[1-9][0-9]*\.html', filename):
        return True
    path = PurePosixPath(filename)
    return path.suffix.lower() in SUPPORTED_EXTENSIONS and parse_index(path.stem, command) is not None


class OSSUploaderV2:
    def __init__(self, config: Config):
        credentials = oss.credentials.StaticCredentialsProvider(
            access_key_id=config.randpic_oss_access_key_id,
            access_key_secret=config.randpic_oss_access_key_secret,
        )
        self.cfg = oss.config.load_default()
        self.cfg.credentials_provider = credentials
        self.cfg.region = config.randpic_region
        self.bucket = config.randpic_bucket
        self.client = None

    async def open(self) -> None:
        if self.client is None:
            self.client = oss_aio.AsyncClient(self.cfg)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def list_files(self) -> Dict[str, RemoteObject]:
        """完整分页列举成功后才交给同步器，避免部分列表造成错误删除。"""
        await self.open()
        objects: Dict[str, RemoteObject] = {}
        token = None
        seen_tokens = set()
        while True:
            result = await asyncio.wait_for(self.client.list_objects_v2(oss.ListObjectsV2Request(
                bucket=self.bucket, max_keys=1000, continuation_token=token,
            )), timeout=60)
            if not 200 <= result.status_code < 300:
                raise RandpicError(f'列举 OSS 对象失败：HTTP {result.status_code}')
            for item in result.contents or []:
                if item.key is not None and is_gallery_key(item.key):
                    objects[item.key] = RemoteObject(int(item.size), str(item.etag or '').strip('"').lower())
            if not result.is_truncated:
                return objects
            next_token = result.next_continuation_token
            if not next_token or next_token in seen_tokens:
                raise RandpicError('OSS 分页标记无效，本轮停止同步。')
            seen_tokens.add(next_token)
            token = next_token

    async def matches_file(self, key: str, file: PublishedFile, remote: RemoteObject) -> bool:
        if remote.size != file.size:
            return False
        if remote.etag == file.etag:
            return True
        # 分片上传或加密对象的 ETag 不保证为 MD5，按本插件的 SHA-256 元数据核对。
        await self.open()
        try:
            result = await asyncio.wait_for(self.client.head_object(oss.HeadObjectRequest(
                bucket=self.bucket, key=key,
            )), timeout=60)
        except oss.exceptions.ServiceError as exc:
            if exc.code == 'NoSuchKey':
                return False
            raise
        if not 200 <= result.status_code < 300:
            raise RandpicError(f'读取 OSS 对象属性失败：{key} (HTTP {result.status_code})')
        return (result.content_length == file.size
                and (result.metadata or {}).get('randpic-sha256') == file.content_hash)

    async def upload_file(self, local_file_path: str, oss_key: str, expected_hash: Optional[str] = None) -> bool:
        try:
            await self.open()
            data = await run_blocking(Path(local_file_path).read_bytes)
            content_hash = hashlib.sha256(data).hexdigest()
            if expected_hash is not None and content_hash != expected_hash:
                return False  # 任务已过期，后续版本会重新上传，不发送旧内容。
            result = await asyncio.wait_for(self.client.put_object(oss.PutObjectRequest(
                bucket=self.bucket, key=oss_key, body=data,
                metadata={'randpic-sha256': content_hash},
                content_type=mimetypes.guess_type(oss_key)[0] or 'application/octet-stream',
                cache_control='no-cache' if oss_key.endswith('.html') else 'public, max-age=31536000, immutable',
            )), timeout=60)
            if 200 <= result.status_code < 300:
                return True
            logger.error(f'OSS 上传失败：{oss_key} (HTTP {result.status_code})')
        except Exception as exc:
            logger.error(f'OSS 上传失败：{oss_key} ({exc})')
        return False

    async def delete_file(self, oss_key: str) -> bool:
        try:
            await self.open()
            result = await asyncio.wait_for(self.client.delete_object(
                oss.DeleteObjectRequest(bucket=self.bucket, key=oss_key)
            ), timeout=60)
            if 200 <= result.status_code < 300:
                return True
            logger.error(f'OSS 删除失败：{oss_key} (HTTP {result.status_code})')
        except oss.exceptions.ServiceError as exc:
            if exc.code == 'NoSuchKey':
                return True
            logger.error(f'OSS 删除失败：{oss_key} ({exc})')
        except Exception as exc:
            logger.error(f'OSS 删除失败：{oss_key} ({exc})')
        return False
