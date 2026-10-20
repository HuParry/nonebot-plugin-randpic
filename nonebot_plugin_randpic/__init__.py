import time
import uuid
import os
from pathlib import Path
from httpx import AsyncClient
import ssl
from typing import Any, Dict, Set, Tuple
from nonebot.adapters.onebot.v11 import MessageSegment, Message, GroupMessageEvent
from nonebot.adapters.onebot.v11 import GROUP, GROUP_ADMIN, GROUP_OWNER
from nonebot.plugin import on_command, on_message, on_regex, on_fullmatch
from nonebot.plugin import PluginMetadata
from nonebot.params import Arg, CommandArg, RegexGroup
from nonebot.rule import Rule, to_me
from nonebot import get_driver, Driver
from nonebot.log import logger
import hashlib
import aiosqlite
from urllib import parse
from urllib.parse import urlparse
import importlib
import imagehash
from .config import *
from .config import save_commands_file, is_commands_file_writable, VALID_COMMAND_PATTERN
from . import config as _config
from .ali_oss import *
from .compress import compress_image_from_bytes, get_image_extension, compute_phash
from .web import StaticImageGalleryGenerator

__plugin_meta__ = PluginMetadata(
    name="随机发送图片",
    description="发送自定义指令后bot会随机发出一张你所存储的图片",
    usage="使用命令：<你设置的指令>",
    type="application",
    homepage="https://github.com/HuParry/nonebot-plugin-randpic",
    config=Config,
    supported_adapters={"nonebot.adapters.onebot.v11"},
)

# 当前生效的指令配置（运行时可变）：{ 指令名 -> 在该群号集合中被禁用 }。
# 直接引用 config 模块的字典对象，保证插件内的所有修改都能在持久化时被一并写回。
current_commands_config: Dict[str, Set[int]] = _config.randpic_commands_config
randpic_path = Path(randpic_store_dir_path)
randpic_img_path = randpic_path / 'img'
randpic_database_path = randpic_path / 'database'


def _persist_commands() -> bool:
    """把当前指令配置全量写回 JSON。
    若启动时检测到 JSON 损坏，则跳过写入，避免覆盖管理员的原始内容。
    返回是否真的写入。
    """
    if not is_commands_file_writable():
        logger.warning(
            f"{_config.COMMANDS_FILENAME} 处于损坏状态，跳过本次持久化。"
            f"新增的指令仅在本次会话内生效，重启后会丢失。"
        )
        return False
    try:
        save_commands_file(randpic_store_dir_path, current_commands_config)
        return True
    except OSError as e:
        logger.error(f"保存 {_config.COMMANDS_FILENAME} 失败：{e}")
        return False


randpic_filename: str = 'randpic_{command}_{index}'


def _next_image_index(command_dir: Path, command: str) -> int:
    """忽略后缀，使用现有最大图片编号加一；扫描时只保留最大值。"""
    prefix = randpic_filename.format(command=command, index='')
    max_index = 0
    with os.scandir(command_dir) as entries:
        for entry in entries:
            stem = Path(entry.name).stem
            if not stem.startswith(prefix):
                continue
            number = stem[len(prefix):]
            if number.isascii() and number.isdigit():
                max_index = max(max_index, int(number))
    return max_index + 1

connection: aiosqlite.Connection

# 激活驱动器
driver = get_driver()


@driver.on_startup
async def _():
    logger.info("正在检查文件...")
    await connect()
    await create_dir()
    logger.info("文件检查完成，欢迎使用！")


@driver.on_shutdown
async def _shutdown():
    db_connection = globals().get("connection")
    if db_connection is not None:
        await db_connection.close()

# 连接数据库
async def connect():
    # 创建数据库
    global connection
    if not randpic_database_path.exists():
        randpic_database_path.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(randpic_database_path / "data.db")

async def _is_folder_changed(command: str, folder_path: Path) -> bool:
    cursor = await connection.cursor()
    await cursor.execute(
        "SELECT folder_mtime FROM folder_snapshot WHERE command = ?",
        (command,)
    )
    row = await cursor.fetchone()
    if row is None:
        return True  # 新命令，无记录
    return folder_path.stat().st_mtime != float(row[0])

async def _save_snapshot(command: str, folder_path: Path):
    await connection.execute(
        """INSERT OR REPLACE INTO folder_snapshot (command, folder_mtime, checked_at)
           VALUES (?, ?, ?)""",
        (command, folder_path.stat().st_mtime, time.time())
    )
    await connection.commit()


# 创建所需文件夹和数据库
async def create_dir():
    command_list = sorted(current_commands_config)
    supported_image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff'}

    # 先创建文件夹
    for command in command_list:
        path = randpic_img_path / command
        if not path.exists():
            logger.warning('未找到{path}文件夹，准备创建{path}文件夹...'.format(path=path))
            path.mkdir(parents=True, exist_ok=True)

    cursor = await connection.cursor()
    await cursor.execute('''
        CREATE TABLE IF NOT EXISTS folder_snapshot (
            command      TEXT PRIMARY KEY,
            folder_mtime REAL NOT NULL,
            checked_at   REAL NOT NULL
        );
    ''')
    # 创建表
    for command in command_list:
        folder_changed = await _is_folder_changed(command, randpic_img_path / command)
        table_name = f"Pic_of_{command}"
        await cursor.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1] for row in await cursor.fetchall()}
        has_send_count = "send_count" in columns
        if not folder_changed and has_send_count:
            continue
        await cursor.execute(f'DROP TABLE IF EXISTS {table_name}')
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS Pic_of_{command} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                img_url TEXT NOT NULL,
                phash TEXT NOT NULL,
                send_count INTEGER NOT NULL DEFAULT 0
            )
            '''.format(command=command))
        await connection.commit()

    # 读取所有文件夹文件，调整文件夹内图片，并写入数据库
        global randpic_filename
        path: Path = randpic_img_path / command
        randpic_file_list = []
        for filename in sorted(os.listdir(path)):
            image_path = path / filename
            extension = image_path.suffix.lower()
            if not image_path.is_file():
                logger.warning(f"跳过非文件路径：{image_path}")
                continue
            if extension and extension not in supported_image_extensions:
                logger.warning(f"跳过不支持的图片格式：{image_path}")
                continue
            randpic_file_list.append(filename)

        # 文件名哈希化
        def get_uuid(command: str):
            return uuid.uuid5(uuid.uuid4(), command).hex
        hash_str = get_uuid(command)
        for i in range(len(randpic_file_list)):
            filename = randpic_file_list[i]
            filename_without_extension, filename_extension = os.path.splitext(filename)
            format_str = randpic_filename.format( command=command, index=str(i + 1).zfill(10) )
            if not filename_extension:
                try:
                    with (path / filename).open('rb') as f:
                        data = f.read()
                    filename_extension = get_image_extension(data)
                except Exception as e:
                    logger.warning(f"无法识别无扩展名文件 {path / filename}，跳过：{e}")
                    continue
            hash_new_filename =  f"{format_str}_{hash_str}{filename_extension}"
            os.rename(path / filename, path / hash_new_filename)

        # 将哈希化的文件名订正为规范名
        randpic_file_list = sorted(os.listdir(path))
        for i in range(len(randpic_file_list)):
            hash_filename = randpic_file_list[i]
            if f"_{hash_str}" not in hash_filename:
                continue
            new_filename = hash_filename.replace(f"_{hash_str}", '')
            os.rename(path / hash_filename, path / new_filename)

        # 将图片信息写入数据库
        randpic_file_list = sorted(os.listdir(path))
        for i in range(len(randpic_file_list)):
            filename = randpic_file_list[i]

            try:
                with (path / filename).open('rb') as f:
                    data = f.read()
                data = compress_image_from_bytes(data)
                with (path / filename).open('wb') as f:
                    f.write(data)

                new_phash_str = compute_phash(data)
                await cursor.execute(
                    'INSERT INTO Pic_of_{command}(img_url, phash) VALUES (?, ?)'.format(command=command),
                    (f"{command}/{filename}", new_phash_str))
            except Exception as e:
                logger.warning(f"索引图片失败，跳过 {path / filename}: {e}")

        await connection.commit()
        await _save_snapshot(command, path)

def web_app_init(web_driver: Driver):
    try:
        _module = importlib.import_module(
            f"nonebot_plugin_randpic.drivers.{driver.type.split('+')[0]}"
        )
    except ImportError:
        logger.warning(f"Driver {driver.type} not supported")
        return
    
    StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_static_site(randpic_oss_no_upload_list)
    if not os.path.exists(randpic_path / 'public'):
        return
    register_route = getattr(_module, "register_route")
    register_route(web_driver, randpic_path / 'public')
    host = str(web_driver.config.host)
    port = web_driver.config.port
    if host in {"0.0.0.0", "127.0.0.1"}:
        host = "localhost"
    logger.opt(colors=True).info(
        f"Nonebot docs will be running at: <b><u>http://{host}:{port}/randpic/</u></b>"
    )
web_app_init(driver)


async def _is_known_command(event: GroupMessageEvent) -> bool:
    if event.group_id in randpic_banner_group:
        return False
    msg = str(event.get_message()).strip()
    disabled = current_commands_config.get(msg)
    if disabled is None:
        return False
    # 在本群被禁用：rule 直接不匹配，避免 block=True 拦下其它插件
    return event.group_id not in disabled


picture = on_message(rule=Rule(_is_known_command), permission=GROUP, priority=2, block=True)


@picture.handle()
async def pic(event: GroupMessageEvent):
    if event.group_id in randpic_banner_group:
        return
    global connection
    cursor = await connection.cursor()
    command = str(event.get_message()).strip()
    await cursor.execute(f'''
        WITH weighted AS (
            SELECT
                id,
                img_url,
                SUM(1.0 / (send_count + 1)) OVER (
                    ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) AS cumulative_weight
            FROM Pic_of_{command}
        ),
        ticket AS (
            SELECT
                abs(random()) / 9223372036854775808.0
                * MAX(cumulative_weight) AS value
            FROM weighted
        )
        SELECT weighted.id, weighted.img_url
        FROM weighted CROSS JOIN ticket
        WHERE weighted.cumulative_weight >= ticket.value
        ORDER BY weighted.id
        LIMIT 1
    ''')
    data = await cursor.fetchone()
    if data is None:
        await picture.finish('当前还没有图片!')
    image_id, file_name = data
    img = randpic_img_path / file_name
    try:
        await picture.send(MessageSegment.image(img))
    except Exception as e:
        logger.info(e)
        await picture.send(f'{command}出不来了，稍后再试试吧~')
        return

    try:
        await cursor.execute(
            f'UPDATE Pic_of_{command} SET send_count = send_count + 1 WHERE id = ?',
            (image_id,)
        )
        await connection.commit()
    except Exception as e:
        logger.warning(f"图片已发送，但更新发送次数失败（分类：{command}，图片ID：{image_id}）：{e}")


add = on_regex(r"^添加(.+)$", permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


async def _ensure_new_command(command: str) -> bool:
    """为新指令建文件夹、建表，并加入运行时配置 + 写回 JSON。"""
    global connection
    path = randpic_img_path / command
    path.mkdir(parents=True, exist_ok=True)
    cursor = await connection.cursor()
    await cursor.execute(
        f'''CREATE TABLE IF NOT EXISTS Pic_of_{command} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            img_url TEXT NOT NULL,
            phash TEXT NOT NULL,
            send_count INTEGER NOT NULL DEFAULT 0
        )'''
    )
    await connection.commit()
    current_commands_config.setdefault(command, set())
    persisted = _persist_commands()
    logger.info(f"已动态创建新指令分类：{command}")
    return persisted


@add.got("pic", prompt="请发送图片！")
async def add_pic(matched: Tuple[Any, ...] = RegexGroup(), pic_list: Message = Arg('pic')):
    global connection, persisted
    command = matched[0].strip()

    # 名称校验：仅允许字母、汉字、数字
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await add.finish("名称仅支持字母、汉字和数字，且不能为空！")

    # 若是新指令，先建文件夹与数据表，再续接添加流程
    if command not in current_commands_config:
        try:
            persisted = await _ensure_new_command(command)
        except Exception as e:
            logger.warning(e)
            await add.finish(f"创建新分类「{command}」失败！")
        msg = f"已新建分类「{command}」，本张图片将作为首张保存。"
        if not persisted:
            msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 写入失败，本次新增指令仅当前会话有效；请检查文件权限或修复 JSON。"
        await add.send(msg)

    cursor = await connection.cursor()

    for pic_name in pic_list:
        if pic_name.type != 'image':
            await add.send(pic_name + MessageSegment.text("\n输入格式有误，请重新触发指令！"), at_sender=True)
            continue
        pic_url = pic_name.data.get('url')
        if not pic_url:
            await add.send(pic_name + MessageSegment.text("\n无法读取图片地址，请重新发送图片！"), at_sender=True)
            continue
        logger.info(f"正在下载图片：{pic_url}")

        try:
            ssl_context = ssl.create_default_context()
            ssl_context.set_ciphers("DEFAULT")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Referer": "https://gchat.qpic.cn/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Dest": "image",
            }
            async with AsyncClient(headers=headers, verify=ssl_context, timeout=10.0) as client:
                resp = await client.get(pic_url)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"下载图片失败 {pic_url}: {e}")
            await add.send(
                pic_name +
                MessageSegment.text('\n下载失败了，请稍后重试')
            )
            continue

        try:
            data = compress_image_from_bytes(resp.content)
            new_phash_str = compute_phash(data)
            image_extension = get_image_extension(data)
            new_phash = imagehash.hex_to_hash(new_phash_str)
        except Exception as e:
            logger.warning(f"图片解码或处理失败 {pic_url}: {e}")
            await add.send(pic_name + MessageSegment.text('\n图片格式无法处理，请换一张图片重试'))
            continue

        await cursor.execute(f'SELECT phash FROM Pic_of_{command}')
        SIMILARITY_THRESHOLD = 5  # 汉明距离 ≤5 认为相似
        is_duplicate = False
        while True:
            row = await cursor.fetchone()
            if row is None:
                break
            ex_phash_str = row[0]
            try:
                ex_phash = imagehash.hex_to_hash(ex_phash_str)
            except (TypeError, ValueError) as e:
                logger.warning(f"忽略损坏的图片感知哈希记录：{ex_phash_str!r} ({e})")
                continue
            if (new_phash - ex_phash) <= SIMILARITY_THRESHOLD:
                is_duplicate = True
                break
        if is_duplicate:
            await add.send(pic_name + Message('\n这张已经有相似图，不能重复添加！'), at_sender=True)
            continue

        command_dir = randpic_img_path / command
        file_path = None
        file_created = False
        try:
            # 编号扫描与文件创建之间没有 await，避免并发消息分配相同编号。
            index = _next_image_index(command_dir, command)
            while True:
                file_name = (randpic_filename.format(command=command, index=str(index).zfill(10))
                             + image_extension)
                file_path = command_dir / file_name
                try:
                    with file_path.open("xb") as f:
                        file_created = True
                        f.write(data)
                    break
                except FileExistsError:
                    index += 1
            await cursor.execute('insert into Pic_of_{command}(img_url, phash) values (?, ?)'.format(command=command),
                                 (f"{command}/{file_name}", new_phash_str))
            await connection.commit()
        except Exception as e:
            logger.warning(f"图片入库失败 {file_path}: {e}")
            await connection.rollback()
            if file_created:
                try:
                    file_path.unlink()
                except OSError:
                    logger.warning(f"无法清理未入库图片文件：{file_path}")
            await add.send(pic_name + Message("\n导入失败！"), at_sender=True)
            continue

        msg = "\n导入成功！"
        if isOss and command not in randpic_oss_no_upload_list:
            msg += f'可去 {endpoint}/{parse.quote(command)}/ 查看'
            try:
                StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_command_html(
                    command, file_name, randpic_oss_no_upload_list
                )
                uploader = OSSUploaderV2()
                uploaded = [
                    await uploader.upload_file(str(randpic_path / 'public' / command / 'index.html'), f'{command}/index.html'),
                    await uploader.upload_file(str(randpic_path / 'public' / 'index.html'), 'index.html'),
                    await uploader.upload_file(str(file_path), f'{command}/{file_name}'),
                ]
                if not all(uploaded):
                    msg += '\n但 OSS 上传未全部成功，请稍后重试“上传oss”。'
            except Exception as e:
                logger.warning(f"图片已保存，但更新 OSS 页面失败：{e}")
                msg += '\n但 OSS 页面更新失败，请稍后重试“上传oss”。'
        await add.send(pic_name + Message(msg), at_sender=True)

    await add.finish()

OSS = on_fullmatch('上传oss', ignorecase=True, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True, )
@OSS.handle()
async def handle_oss(event: GroupMessageEvent) -> None:
    if not isOss:
        return
    await OSS.send('正在上传至OSS...')
    start_time = time.perf_counter()
    try:
        StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_static_site(
            randpic_oss_no_upload_list
        )
        succeeded, failed = await OSSUploaderV2().upload_folder(str(randpic_path / 'public'))
    except Exception as e:
        logger.exception(f"生成或上传 OSS 静态页面失败：{e}")
        await OSS.finish('上传失败，请查看 Bot 日志。')
    elapsed_time = time.perf_counter() - start_time
    if failed:
        await OSS.finish(
            f'上传结束，用时: {elapsed_time:.2f}秒，成功 {succeeded} 个，失败 {failed} 个。'
        )
    await OSS.finish(f'上传完成，用时: {elapsed_time:.2f}秒，地址：{endpoint}/')


# 分群管理：@bot 禁用<指令> / @bot 启用<指令>
disable = on_command("禁用", rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


@disable.handle()
async def handle_disable(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    command = args.extract_plain_text().strip()
    if not command:
        await disable.finish("请指定要禁用的指令名！例如：@bot 禁用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await disable.finish("指令名称仅支持字母、汉字和数字！")
    if command not in current_commands_config:
        await disable.finish(f"指令「{command}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid in disabled:
        await disable.finish(f"指令「{command}」在本群已经是禁用状态。")
    disabled.add(gid)
    msg = f"已在本群禁用指令「{command}」。"
    if not _persist_commands():
        msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 写入失败，本次修改仅当前会话有效；请检查文件权限或修复 JSON。"
    await disable.finish(msg)


enable = on_command("启用", rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


@enable.handle()
async def handle_enable(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    command = args.extract_plain_text().strip()
    if not command:
        await enable.finish("请指定要启用的指令名！例如：@bot 启用 capoo")
    if not VALID_COMMAND_PATTERN.fullmatch(command):
        await enable.finish("指令名称仅支持字母、汉字和数字！")
    if command not in current_commands_config:
        await enable.finish(f"指令「{command}」不存在！")
    gid = event.group_id
    disabled = current_commands_config[command]
    if gid not in disabled:
        await enable.finish(f"指令「{command}」在本群本来就没有被禁用。")
    disabled.discard(gid)
    msg = f"已在本群重新启用指令「{command}」。"
    if not _persist_commands():
        msg += f"\n⚠️ {_config.COMMANDS_FILENAME} 写入失败，本次修改仅当前会话有效；请检查文件权限或修复 JSON。"
    await enable.finish(msg)
