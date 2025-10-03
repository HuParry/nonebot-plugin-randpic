# -*- coding: utf-8 -*-

from .config import *
from nonebot.log import logger
import asyncio
import alibabacloud_oss_v2 as oss

# 建议填写自定义域名，例如：https://oss.huparry.cn 。
endpoint = randpic_endpoint
# 存储空间名称。
bucket = randpic_bucket
# 填写Bucket所在地域。以华东1（杭州）为例，Region填写为cn-hangzhou。
region = randpic_region
# 填写RAM用户的Access Key ID和Access Key Secret
access_key_id = randpic_oss_access_key_id
access_key_secret = randpic_oss_access_key_secret

isOss: bool = False

if endpoint and bucket and region and access_key_id and access_key_secret:
    isOss = True
    logger.info("已配置OSS对象存储，静态页面将生效...")

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple


class OSSUploaderV2:
    def __init__(self):
        """
        初始化OSS上传器V2

        参数:
        access_key_id: 阿里云AccessKey ID
        access_key_secret: 阿里云AccessKey Secret
        bucket_name: 存储空间名称
        """
        # 创建凭证
        credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
        )

        # 加载SDK的默认配置，并设置凭证提供者
        cfg = oss.config.load_default()
        cfg.credentials_provider = credentials_provider
        # 填写Bucket所在地域。以华东1（杭州）为例，Region填写为cn-hangzhou
        cfg.region = region
        # 使用配置好的信息创建OSS客户端

        # 创建客户端
        self.client = oss.Client(cfg)
        self.bucket_name = bucket
        # self.uploaded_count = 0
        # self.failed_count = 0
        # self.lock = threading.Lock()

    async def upload_file(self, local_file_path: str, oss_key: str) -> bool | None:
        """
        上传单个文件到OSS

        参数:
        local_file_path: 本地文件路径
        oss_key: OSS对象键名

        返回:
        bool: 上传是否成功
        """
        loop = asyncio.get_event_loop()

        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda bucket_name=self.bucket_name, key=oss_key:
                self.client.put_object_from_file(oss.PutObjectRequest(
                    bucket=bucket_name,
                    key=key
                ), local_file_path),
            ),
            timeout=10.0
        )

        # 检查上传是否成功
        if result.status_code != 200:
            logger.waring(f"✗ 上传失败: {local_file_path} (状态码: {result.status_code})")

    async def upload_folder(
            self,
            local_folder_path: str,
            oss_prefix: str = "",
            include_hidden: bool = False,
            overwrite: bool = True
    ) -> None:
        """
        上传整个文件夹到OSS

        参数:
        local_folder_path: 本地文件夹路径
        oss_prefix: OSS中的前缀路径（可选）
        include_hidden: 是否包含隐藏文件
        overwrite: 是否覆盖已存在的文件
        """
        loop = asyncio.get_event_loop()
        local_folder_path = Path(local_folder_path)
        if not local_folder_path.exists():
            print(f"错误: 本地文件夹不存在 - {local_folder_path}")
            return
        # 收集所有需要上传的文件
        files_to_upload: List[Tuple[str, str]] = []
        for root, dirs, files in os.walk(local_folder_path):
            # 如果不包含隐藏文件，过滤掉隐藏文件夹
            if not include_hidden:
                dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                # 如果不包含隐藏文件，跳过隐藏文件
                if not include_hidden and file.startswith('.'):
                    continue
                local_file_path = Path(root) / file
                # 计算相对路径
                relative_path = local_file_path.relative_to(local_folder_path)
                # 构建OSS对象键名
                if oss_prefix:
                    oss_key = f"{oss_prefix.rstrip('/')}/{relative_path}".replace('\\', '/')
                else:
                    oss_key = str(relative_path).replace('\\', '/')
                if not overwrite:
                    # 如果不覆盖，检查文件是否已存在
                    if self.check_file_exists(oss_key):
                        print(f"跳过已存在文件: {oss_key}")
                        continue
                files_to_upload.append((str(local_file_path), oss_key))
        print(f"找到 {len(files_to_upload)} 个文件需要上传")
        # 并发上传文件
        max_workers = min(32, ((os.cpu_count() or 1) + 1) // 2)
        for local_file, oss_key in files_to_upload:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    ThreadPoolExecutor(max_workers=max_workers),
                    lambda bucket=self.bucket_name, key=oss_key, data=open(local_file, 'rb'):
                    self.client.put_object(oss.PutObjectRequest(
                        bucket=bucket,
                        key=key,
                        body=data
                    )),
                ),
                timeout=10.0
            )
            if result.status_code != 200:
                logger.waring(f"✗ 上传失败: {local_file} (状态码: {result.status_code})")

    def check_file_exists(self, oss_key: str) -> bool:
        """
        检查OSS中是否存在指定文件

        参数:
        oss_key: OSS对象键名

        返回:
        bool: 文件是否存在
        """
        try:
            head_request = oss.HeadObjectRequest(
                bucket=self.bucket_name,
                key=oss_key
            )
            result = self.client.head_object(head_request)
            return result.status_code == 200
        except oss.exceptions.ServiceError as e:
            if e.code == 'NoSuchKey':
                return False
            else:
                print(f"检查文件存在性时出错: {oss_key} (错误: {e.message})")
                return False
        except Exception as e:
            print(f"检查文件存在性时出错: {oss_key} (错误: {str(e)})")
            return False
