"""Randpic 2.0：启动、关闭及 NoneBot 插件注册。"""

from nonebot import get_driver, get_plugin_config, require
from nonebot.plugin import PluginMetadata

from .config import Config

require('nonebot_plugin_localstore')
from nonebot_plugin_localstore import get_data_dir

from . import handlers
from .drivers.fastapi import register_route
from .service import RandpicService


__plugin_meta__ = PluginMetadata(
    name='随机发送图片',
    description='自定义图库、按发送次数加权随机发图、引用删除及网页同步',
    usage='发送图库指令随机发图；管理员可添加图片、引用图片发送“删除”，以及 @bot 启用/禁用指令。',
    type='application',
    homepage='https://github.com/HuParry/nonebot-plugin-randpic',
    config=Config,
    supported_adapters={'nonebot.adapters.onebot.v11'},
)

plugin_config = get_plugin_config(Config)
root = plugin_config.randpic_store_dir_path if plugin_config.randpic_store_dir_path is not None else get_data_dir('nonebot_plugin_randpic')
runtime = RandpicService(plugin_config, root)
handlers.setup(runtime)
driver = get_driver()
register_route(driver, runtime.publisher.local_directory)


@driver.on_startup
async def startup() -> None:
    await runtime.start()


@driver.on_shutdown
async def shutdown() -> None:
    await runtime.close()
