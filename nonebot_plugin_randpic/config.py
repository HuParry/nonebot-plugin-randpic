from pydantic import BaseModel, Extra
from typing import List
from nonebot import require

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_data_dir


class Config(BaseModel, extra=Extra.ignore):
    randpic_command_list: List[str] = ["capoo"]  # 指令列表
    randpic_store_dir_path: str = get_data_dir("nonebot_plugin_randpic")  # 用户自定义图片存储文件夹
