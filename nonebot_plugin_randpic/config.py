from pydantic import BaseModel, Extra
from typing import List


class Config(BaseModel, extra=Extra.ignore):
    randpic_command_list: List[str] = ["capoo"]  # 指令列表
