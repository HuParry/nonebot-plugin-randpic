from pydantic import BaseModel, Extra
from typing import Set


class Config(BaseModel, extra=Extra.ignore):
    randpic_command_set: Set[str] = {"capoo"}  # 指令集合


