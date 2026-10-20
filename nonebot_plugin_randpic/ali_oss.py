"""复用异步 OSS 客户端；不持有图库或数据库的锁。"""

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Optional

import alibabacloud_oss_v2 as oss
import alibabacloud_oss_v2.aio as oss_aio
from nonebot.log import logger

from .config import Config
from .utils import run_blocking


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

    async def upload_file(self, local_file_path: str, oss_key: str, expected_hash: Optional[str] = None) -> bool:
        try:
            await self.open()
            data = await run_blocking(Path(local_file_path).read_bytes)
            if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
                return False  # 任务已过期，后续版本会重新上传，不发送旧内容。
            result = await asyncio.wait_for(self.client.put_object(oss.PutObjectRequest(
                bucket=self.bucket, key=oss_key, body=data,
                content_type=mimetypes.guess_type(oss_key)[0] or 'application/octet-stream',
                cache_control='no-cache' if oss_key.endswith(('.html', '.json')) else 'public, max-age=31536000, immutable',
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
