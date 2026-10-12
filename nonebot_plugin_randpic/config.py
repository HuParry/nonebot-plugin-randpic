import json
import os
import re
from pathlib import Path
from pydantic import BaseModel, Extra
from typing import Dict, List, Optional, Set
from nonebot import require
from nonebot import get_driver
from nonebot.log import logger

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_data_dir


class Config(BaseModel, extra='ignore'):
    randpic_store_dir_path: str = get_data_dir("nonebot_plugin_randpic")  # 用户自定义图片存储文件夹
    randpic_banner_group: List[int] = []  # 禁用群组列表
    randpic_endpoint: Optional[str] = None               # 建议填写自定义域名，尾部不用加/
    randpic_bucket: Optional[str] = None            # 存储空间名称
    randpic_region: Optional[str] = None               # Bucket所在地域
    randpic_oss_access_key_id: Optional[str] = None      # 阿里云用户AccessKey ID
    randpic_oss_access_key_secret: Optional[str] = None  # 阿里云用户AccessKey Secret
    randpic_oss_no_upload_list: List[str] = [] # 不上传oss的指令数组


COMMANDS_FILENAME = 'randpic_commands.json'
DEFAULT_COMMANDS: List[str] = ["capoo"]

# 名称合法字符集：仅允许字母、数字、汉字。SQL 表名、文件夹名都会按原样使用，
# 必须收紧字符集，否则会出现 SQL 注入 / 语法错 / 文件系统报错。
VALID_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9一-龥]+$")

# JSON 文件健康状态。文件不存在不算损坏（首次启动场景）；
# 解析失败或顶层不是 list 视为损坏，运行时禁止写回以保护管理员的原始内容。
commands_file_corrupted: bool = False


def is_commands_file_writable() -> bool:
    return not commands_file_corrupted


def commands_file_path(store_dir: str) -> Path:
    return Path(store_dir) / COMMANDS_FILENAME


def load_commands_file(store_dir: str) -> Dict[str, Set[int]]:
    """读取指令配置 JSON。返回「指令名 -> 该指令被禁用的群号集合」映射。

    - 文件不存在：写入默认配置并返回。
    - 解析失败 / 顶层既不是 list 也不是 dict：置 corrupted 标记，返回空字典，运行时不再覆盖。
    - 旧格式兼容（顶层为 list of str）：等价为「指令名 -> 空集合」，并立即迁移写回为新版 dict 格式。
    - 新格式（顶层为 dict）：键是指令名，值是禁用群号的 list[int]。
    - 单条非法（指令名非 str/空/含特殊字符、群号非 int）：跳过并告警。
    """
    global commands_file_corrupted
    path = commands_file_path(store_dir)
    if not path.exists():
        logger.info(f"未找到 {path}，使用默认指令列表 {DEFAULT_COMMANDS} 并写入文件")
        default: Dict[str, Set[int]] = {name: set() for name in DEFAULT_COMMANDS}
        try:
            save_commands_file(store_dir, default)
        except OSError as e:
            logger.error(f"无法写入默认指令配置 {path}：{e}；本次运行将使用内存中的默认配置")
        return default
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        commands_file_corrupted = True
        logger.error(
            f"读取 {path} 失败（JSON 格式错误）：{e}；本次启动以空指令列表运行，"
            f"且运行时不会写回此文件以避免覆盖。请修复后重启 bot。"
        )
        return {}

    # 旧格式：顶层 list[str]，自动迁移为新版 dict 并立即回写
    if isinstance(data, list):
        logger.info(f"{path} 为旧版数组格式，将迁移为新版字典格式（每个指令带空的禁用群号列表）")
        migrated: Dict[str, Set[int]] = {}
        for x in data:
            if not isinstance(x, str):
                logger.warning(f"{path} 中忽略非字符串条目: {x!r}")
                continue
            name = x.strip()
            if not name:
                logger.warning(f"{path} 中忽略空字符串条目")
                continue
            if not VALID_COMMAND_PATTERN.fullmatch(name):
                logger.warning(f"{path} 中忽略非法名称 {name!r}（仅允许字母、汉字、数字）")
                continue
            migrated.setdefault(name, set())
        try:
            save_commands_file(store_dir, migrated)
        except OSError as e:
            logger.error(f"旧版指令配置迁移写入失败 {path}：{e}；本次运行继续使用迁移后的内存配置")
        return migrated

    if not isinstance(data, dict):
        commands_file_corrupted = True
        logger.error(
            f"{path} 顶层必须是 dict 或旧版的 list[str]，得到 {type(data).__name__}；本次启动以空指令列表运行，"
            f"且运行时不会写回此文件。"
        )
        return {}

    result: Dict[str, Set[int]] = {}
    for name, disabled in data.items():
        if not isinstance(name, str):
            logger.warning(f"{path} 中忽略非字符串键: {name!r}")
            continue
        name = name.strip()
        if not name:
            logger.warning(f"{path} 中忽略空字符串键")
            continue
        if not VALID_COMMAND_PATTERN.fullmatch(name):
            logger.warning(f"{path} 中忽略非法名称 {name!r}（仅允许字母、汉字、数字）")
            continue
        if not isinstance(disabled, list):
            logger.warning(f"{path} 中指令 {name!r} 的禁用群号必须是数组，得到 {type(disabled).__name__}；视为空")
            result.setdefault(name, set())
            continue
        gids: Set[int] = set()
        for g in disabled:
            # bool 是 int 的子类，要单独排除，避免 True/False 被当成群号
            if isinstance(g, bool) or not isinstance(g, int):
                logger.warning(f"{path} 中指令 {name!r} 忽略非整数群号: {g!r}")
                continue
            gids.add(g)
        result[name] = gids
    return result


def save_commands_file(store_dir: str, commands: Dict[str, Set[int]]) -> None:
    """全量覆盖写入指令配置 JSON。注意：调用方应先检查 is_commands_file_writable()。

    文件结构：`{ "指令名": [禁用群号, ...], ... }`。键按字母排序，群号按升序，保证可读性。
    """
    path = commands_file_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {name: sorted(gids) for name, gids in sorted(commands.items())}
    temporary_path = path.with_name(path.name + '.tmp')
    try:
        with temporary_path.open('w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


config_dict = Config.model_validate(get_driver().config.dict())
randpic_store_dir_path: str = config_dict.randpic_store_dir_path
randpic_banner_group = config_dict.randpic_banner_group

# 指令配置唯一来源：randpic_commands.json
# 结构：{ 指令名 -> 该指令被禁用的群号集合 }；运行时被插件直接读写，保存时整体回写。
randpic_commands_config: Dict[str, Set[int]] = load_commands_file(randpic_store_dir_path)
# 仅作快照，保留旧名以兼容外部引用；运行期请通过 randpic_commands_config 取最新键集合。
randpic_command_list: List[str] = list(randpic_commands_config.keys())

randpic_endpoint = config_dict.randpic_endpoint
randpic_bucket = config_dict.randpic_bucket
randpic_region = config_dict.randpic_region
randpic_oss_access_key_id = config_dict.randpic_oss_access_key_id
randpic_oss_access_key_secret = config_dict.randpic_oss_access_key_secret
randpic_oss_no_upload_list = config_dict.randpic_oss_no_upload_list
