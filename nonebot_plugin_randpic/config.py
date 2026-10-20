"""插件设置与向后兼容的指令配置文件。"""

import json
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from nonebot.log import logger
from pydantic import BaseModel, Field

from .utils import atomic_write, valid_command


class Config(BaseModel):
    randpic_store_dir_path: Optional[Path] = None
    randpic_banner_group: List[int] = Field(default_factory=list)
    randpic_endpoint: Optional[str] = None
    randpic_bucket: Optional[str] = None
    randpic_region: Optional[str] = None
    randpic_oss_access_key_id: Optional[str] = None
    randpic_oss_access_key_secret: Optional[str] = None
    randpic_oss_no_upload_list: List[str] = Field(default_factory=list)
    randpic_upload_concurrency: int = Field(default=16, ge=1, le=64)
    randpic_download_max_mb: int = Field(default=32, ge=1, le=256)
    randpic_download_timeout: float = Field(default=30.0, gt=0, le=300)
    randpic_web_page_size: int = Field(default=100, ge=1, le=500)

    class Config:
        extra = 'ignore'

    @property
    def oss_enabled(self) -> bool:
        return all((self.randpic_endpoint, self.randpic_bucket, self.randpic_region,
                    self.randpic_oss_access_key_id, self.randpic_oss_access_key_secret))


class CommandSettings:
    """保留旧版 list[str] 和 dict[str, list[int]] 格式；损坏的文件绝不覆盖。"""

    def __init__(self, root: Path):
        self.path = root / 'randpic_commands.json'
        self.commands: Dict[str, FrozenSet[int]] = {}
        self.writable = True

    def load(self) -> None:
        if not self.path.exists():
            self.commands = {'capoo': frozenset()}
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, list):
                entries = [(name, []) for name in data]
            elif isinstance(data, dict):
                entries = list(data.items())
            else:
                raise ValueError('顶层必须是对象或旧版指令数组')
        except (OSError, ValueError) as exc:
            self.writable = False
            self.commands = {}
            logger.error(f'读取指令配置失败，保留原文件并以空指令列表运行：{self.path} ({exc})')
            return
        commands: Dict[str, FrozenSet[int]] = {}
        for name, groups in entries:
            if not isinstance(name, str) or not valid_command(name.strip()):
                logger.warning(f'指令配置中忽略非法名称：{name!r}')
                continue
            name = name.strip()
            if not isinstance(groups, list):
                logger.warning(f'指令「{name}」的禁用群列表无效，视为空列表。')
                groups = []
            valid_groups = frozenset(g for g in groups if type(g) is int and g > 0)
            commands[name] = commands.get(name, frozenset()) | valid_groups
        self.commands = commands
        if isinstance(data, list):
            self.save()

    def save(self) -> bool:
        if not self.writable:
            return False
        try:
            data = {name: sorted(groups) for name, groups in sorted(self.commands.items())}
            atomic_write(self.path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
            return True
        except OSError as exc:
            logger.error(f'保存指令配置失败，本次修改仅在当前运行中生效：{exc}')
            return False

    def add(self, command: str) -> bool:
        if not valid_command(command):
            raise ValueError('名称仅支持字母、汉字、数字，且长度不能超过 120 字节。')
        if command in self.commands:
            return True
        self.commands = {**self.commands, command: frozenset()}
        return self.save()

    def set_disabled(self, command: str, group_id: int, disabled: bool) -> bool:
        groups = self.commands[command]
        groups = groups | {group_id} if disabled else groups - {group_id}
        self.commands = {**self.commands, command: frozenset(groups)}
        return self.save()
