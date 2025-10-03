from pydantic import BaseModel, Extra
from typing import List
from nonebot import require
from nonebot import get_driver

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_data_dir


class Config(BaseModel, extra='ignore'):
    randpic_command_list: List[str] = ["capoo"]  # 指令列表
    randpic_store_dir_path: str = get_data_dir("nonebot_plugin_randpic")  # 用户自定义图片存储文件夹
    randpic_banner_group: List[int] = []  # 禁用群组列表
    randpic_endpoint: str = None               # 建议填写自定义域名，尾部不用加/
    randpic_bucket: str = None            # 存储空间名称
    randpic_region: str = None                 # Bucket所在地域
    randpic_oss_access_key_id: str = None      # 阿里云用户AccessKey ID
    randpic_oss_access_key_secret: str = None  # 阿里云用户AccessKey Secret


config_dict = Config.model_validate(get_driver().config.dict())
randpic_command_list: List[str] = config_dict.randpic_command_list
randpic_store_dir_path: str = config_dict.randpic_store_dir_path
randpic_banner_group = config_dict.randpic_banner_group

randpic_endpoint = config_dict.randpic_endpoint
randpic_bucket = config_dict.randpic_bucket
randpic_region = config_dict.randpic_region
randpic_oss_access_key_id = config_dict.randpic_oss_access_key_id
randpic_oss_access_key_secret = config_dict.randpic_oss_access_key_secret
