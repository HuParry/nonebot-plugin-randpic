import time
import asyncio
import os
from pathlib import Path
from httpx import AsyncClient
import ssl
from typing import Any, Dict, List, Optional, Set, Tuple
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
from .image_store import DeletedImage, ImageDeletionError, SentImageStore

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


def _parse_image_index(stem: str, command: str) -> Optional[int]:
    """读取本分类文件名中的数字编号，不要求补齐前导零。"""
    prefix = randpic_filename.format(command=command, index='')
    if not stem.startswith(prefix):
        return None
    number = stem[len(prefix):]
    if number.isascii() and number.isdigit():
        return int(number)
    return None


def _next_image_index(command_dir: Path, command: str) -> int:
    """忽略后缀，使用现有最大图片编号加一；扫描时只保留最大值。"""
    max_index = 0
    with os.scandir(command_dir) as entries:
        for entry in entries:
            index = _parse_image_index(Path(entry.name).stem, command)
            if index is not None:
                max_index = max(max_index, index)
    return max_index + 1


def _normalize_image_names(command_dir: Path, command: str) -> List[Path]:
    """保留规范且主文件名唯一的图片，只重命名其余图片；重复执行不改名。"""
    supported_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff'}
    next_index = _next_image_index(command_dir, command)
    used_stems: Set[str] = set()
    image_paths: List[Path] = []
    for image_path in sorted(command_dir.iterdir()):
        if not image_path.is_file():
            continue
        extension = image_path.suffix
        if extension and extension.lower() not in supported_extensions:
            continue
        if not extension:
            try:
                extension = get_image_extension(image_path.read_bytes())
            except Exception as e:
                logger.warning(f"无法识别无扩展名文件 {image_path}，跳过：{e}")
                continue

        stem = image_path.stem
        index = _parse_image_index(stem, command)
        is_standard = (
            bool(image_path.suffix)
            and index is not None
            and index > 0
            and stem == randpic_filename.format(command=command, index=str(index).zfill(10))
        )
        if not is_standard or stem in used_stems:
            # 从现有最大编号之后分配，避免目标文件与原有文件冲突。
            new_stem = randpic_filename.format(command=command, index=str(next_index).zfill(10))
            next_index += 1
            target = command_dir / (new_stem + extension)
            image_path = image_path.rename(target)
        used_stems.add(image_path.stem)
        image_paths.append(image_path)
    return image_paths


connection: aiosqlite.Connection
sent_image_store: SentImageStore
_image_lock = asyncio.Lock()
_gallery_lock = asyncio.Lock()

# 激活驱动器
driver = get_driver()


@driver.on_startup
async def _():
    await connect()
    await create_dir()


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
    global sent_image_store
    sent_image_store = SentImageStore(randpic_database_path / "data.db", randpic_img_path)
    await sent_image_store.initialize()

async def _is_folder_changed(command: str, folder_path: Path) -> bool:
    async with connection.execute(
        "SELECT folder_mtime FROM folder_snapshot WHERE command = ?", (command,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return True
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

    # 先创建文件夹
    for command in command_list:
        path = randpic_img_path / command
        if not path.exists():
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
        path = randpic_img_path / command
        image_paths = _normalize_image_names(path, command)
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

        # 只索引本次检查通过的图片，不再重新遍历无关文件。
        for image_path in image_paths:
            filename = image_path.name

            try:
                with (path / filename).open('rb') as f:
                    data = f.read()
                data = compress_image_from_bytes(data, log_progress=False)
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
    command = str(event.get_message()).strip()
    try:
        async with _image_lock:
            cursor = await connection.cursor()
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
            await cursor.close()
            if data is None:
                await picture.send('当前还没有图片!')
                return
            image_id, file_name = data
            img = randpic_img_path / file_name
            image_bytes = img.read_bytes()
        result = await picture.send(MessageSegment.image(image_bytes))
    except Exception as e:
        logger.warning(f'图片发送失败：{e}')
        await picture.send(f'{command}出不来了，稍后再试试吧~')
        return
    await _remember_image_message(
        event, result, command, image_id, file_name, hashlib.sha256(image_bytes).hexdigest()
    )


async def _remember_image_message(
    event: GroupMessageEvent, result: Any, command: str, image_id: int,
    img_url: str, content_hash: str, *, count_send: bool = True,
) -> None:
    message_id = None
    try:
        message_id = int(result['message_id'])
    except (KeyError, TypeError, ValueError):
        logger.warning('发图接口未返回有效的 message_id，这条消息将无法引用删除。')
    try:
        async with _image_lock:
            await sent_image_store.record_sent(
                str(event.self_id), event.group_id, message_id, command,
                image_id, img_url, content_hash, count_send=count_send,
            )
    except Exception as e:
        logger.warning(f'图片已发送，但保存消息对应关系或发送次数失败：{e}')


def _reply_message_id(event: GroupMessageEvent) -> Optional[int]:
    if event.reply is not None:
        return event.reply.message_id
    # get_msg 不可用时，仍可利用原始引用 ID 查询本插件自己的发图记录。
    for segment in event.original_message:
        if segment.type == 'reply':
            try:
                return int(segment.data['id'])
            except (KeyError, TypeError, ValueError):
                return None
    return None


async def _is_delete_request(event: GroupMessageEvent) -> bool:
    if _reply_message_id(event) is None:
        return False
    message = event.get_message()
    if message.extract_plain_text().strip() != '删除':
        return False
    return all(
        segment.type in {'text', 'reply'}
        or (segment.type == 'at' and str(segment.data.get('qq')) == str(event.self_id))
        for segment in message
    )


async def _sync_deleted_image(image: DeletedImage) -> List[str]:
    """调用方持有图库及网页锁，避免并发上传重新发布已删除的图片。"""
    if await sent_image_store.has_current_image(image):
        # 同名位置已有新图时，旧消息的同步重试不得删除新图。
        await sent_image_store.mark_synced(image)
        return []
    errors = []
    pages_ready = False
    try:
        StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').remove_command_image(
            image.command, Path(image.img_url).name, randpic_oss_no_upload_list
        )
        pages_ready = True
    except Exception as e:
        logger.warning(f'图片已删除，但刷新本地网页失败：{image.img_url} ({e})')
        errors.append('本地网页更新失败')
    if isOss:
        try:
            uploader = OSSUploaderV2()
            if not await uploader.delete_file(image.img_url):
                errors.append('OSS 图片删除失败')
            if pages_ready:
                page_keys = ['index.html']
                if image.command not in randpic_oss_no_upload_list:
                    page_keys.append(f'{image.command}/index.html')
                results = [
                    await uploader.upload_file(str(randpic_path / 'public' / key), key)
                    for key in page_keys
                ]
                if not all(results):
                    errors.append('OSS 页面更新失败')
        except Exception as e:
            logger.warning(f'图片已删除，但 OSS 同步失败：{image.img_url} ({e})')
            errors.append('OSS 同步失败')
    if not errors:
        try:
            await sent_image_store.mark_synced(image)
        except Exception as e:
            logger.warning(f'保存图片删除同步状态失败：{image.img_url} ({e})')
            errors.append('同步状态保存失败')
    return errors


delete_picture = on_message(
    rule=Rule(_is_delete_request), permission=GROUP_ADMIN | GROUP_OWNER,
    priority=1, block=True,
)


@delete_picture.handle()
async def handle_delete_picture(event: GroupMessageEvent) -> None:
    if event.reply is not None:
        sender_id = event.reply.sender.user_id
        if sender_id is not None and str(sender_id) != str(event.self_id):
            await delete_picture.finish('请引用本 Bot 发送的图库图片，再发送“删除”。', at_sender=True)
    async with _gallery_lock:
        async with _image_lock:
            try:
                image = await sent_image_store.delete(
                    str(event.self_id), event.group_id, _reply_message_id(event)
                )
            except ImageDeletionError as e:
                await delete_picture.finish(str(e), at_sender=True)
            except Exception as e:
                logger.exception(f'删除图库图片失败：{e}')
                await delete_picture.finish('删除失败，请稍后重试或查看 Bot 日志。', at_sender=True)
            errors = []
            if image.sync_pending:
                try:
                    errors = await _sync_deleted_image(image)
                except Exception as e:
                    logger.exception(f'图片已删除，但同步失败：{e}')
                    errors.append('网页或 OSS 同步失败')
    message = '这张图片已经从图库删除。' if image.already_deleted else f'已从「{image.command}」图库删除这张图片。'
    if errors:
        message += '\n' + '；'.join(errors) + '，可再次引用原消息发送“删除”重试。'
    await delete_picture.finish(message, at_sender=True)


add = on_regex(r"^添加(.+)$", permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)


async def _ensure_new_command(command: str) -> bool:
    """为新指令建文件夹、建表，并加入运行时配置 + 写回 JSON。"""
    async with _image_lock:
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
async def add_pic(event: GroupMessageEvent, matched: Tuple[Any, ...] = RegexGroup(), pic_list: Message = Arg('pic')):
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

        async with _image_lock:
            async with connection.execute(f'SELECT phash FROM Pic_of_{command}') as hash_cursor:
                SIMILARITY_THRESHOLD = 5  # 汉明距离 ≤5 认为相似
                is_duplicate = False
                while True:
                    row = await hash_cursor.fetchone()
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
                async with connection.execute(
                    f'INSERT INTO Pic_of_{command}(img_url, phash) VALUES (?, ?)',
                    (f"{command}/{file_name}", new_phash_str),
                ) as insert_cursor:
                    saved_image_id = insert_cursor.lastrowid
                await _save_snapshot(command, command_dir)
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
                async with _gallery_lock:
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
        result = await add.send(pic_name + Message(msg), at_sender=True)
        await _remember_image_message(
            event, result, command, saved_image_id, f"{command}/{file_name}",
            hashlib.sha256(data).hexdigest(), count_send=False,
        )

    await add.finish()

OSS = on_fullmatch('上传oss', ignorecase=True, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True, )
@OSS.handle()
async def handle_oss(event: GroupMessageEvent) -> None:
    if not isOss:
        return
    await OSS.send('正在上传至OSS...')
    start_time = time.perf_counter()
    try:
        async with _gallery_lock:
            failed_deletions = 0
            async with _image_lock:
                for image in await sent_image_store.pending_deletions():
                    if await _sync_deleted_image(image):
                        failed_deletions += 1
            StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_static_site(
                randpic_oss_no_upload_list
            )
            succeeded, failed = await OSSUploaderV2().upload_folder(str(randpic_path / 'public'))
    except Exception as e:
        logger.exception(f"生成或上传 OSS 静态页面失败：{e}")
        await OSS.finish('上传失败，请查看 Bot 日志。')
    elapsed_time = time.perf_counter() - start_time
    if failed or failed_deletions:
        message = f'上传结束，用时: {elapsed_time:.2f}秒，成功 {succeeded} 个，失败 {failed} 个。'
        if failed_deletions:
            message += f'另有 {failed_deletions} 张图片的删除同步未完成，请重试。'
        await OSS.finish(message)
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
