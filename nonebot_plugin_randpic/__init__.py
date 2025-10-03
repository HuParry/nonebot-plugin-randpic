import time
import uuid
from httpx import AsyncClient
import ssl
from nonebot.adapters.onebot.v11 import MessageSegment, Message, Event, GroupMessageEvent
from nonebot.adapters.onebot.v11 import GROUP, GROUP_ADMIN, GROUP_OWNER
from nonebot.plugin import on_fullmatch
import os
from nonebot.plugin import PluginMetadata
from nonebot.params import Arg
from nonebot import get_driver
from nonebot.log import logger
import hashlib
import aiosqlite
from nonebot.params import Fullmatch

from .config import *
from .ali_oss import *
from .compress import compress_image_from_bytes
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

randpic_command_tuple: Tuple[str, ...] = tuple(set(randpic_command_list))  # 形成指令元组
randpic_command_add_tuple = tuple("添加" + tup for tup in randpic_command_tuple)  # 形成添加指令元组
randpic_path = Path(randpic_store_dir_path)
randpic_img_path = randpic_path / 'img'
randpic_database_path = randpic_path / 'database'
randpic_command_path_tuple = tuple(randpic_img_path / command for command in randpic_command_tuple)  # 形成指令文件夹路径元组


randpic_filename: str = 'randpic_{command}_{index}'

connection: aiosqlite.Connection

# 激活驱动器
driver = get_driver()


@driver.on_startup
async def _():
    logger.info("正在检查文件...")
    await create_file()
    logger.info("文件检查完成，欢迎使用！")


# @driver.on_shutdown
# async def close_connection():
#     logger.info("正在关闭数据库")
#     await connection.close()


# 创建所需文件夹和数据库
async def create_file():
    # 先创建文件夹
    for path in randpic_command_path_tuple:
        if not path.exists():
            logger.warning('未找到{path}文件夹，准备创建{path}文件夹...'.format(path=path))
            path.mkdir(parents=True, exist_ok=True)
    # 创建数据库，没有数据表创建数据表

    global connection
    if not randpic_database_path.exists():
        randpic_database_path.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(randpic_database_path / "data.db")
    cursor = await connection.cursor()

    # 创建表
    for command in randpic_command_tuple:
        await cursor.execute('DROP table if exists Pic_of_{command};'.format(command=command))
        await cursor.execute('''
            CREATE TABLE IF NOT EXISTS Pic_of_{command} (
                md5 TEXT PRIMARY KEY,
                img_url TEXT
            )
            '''.format(command=command))
        await connection.commit()

    # 读取所有文件夹文件，调整文件夹内图片，并写入数据库
    for index in range(len(randpic_command_path_tuple)):
        global randpic_filename
        path: Path = randpic_command_path_tuple[index]
        command = randpic_command_tuple[index]
        randpic_file_list = os.listdir(path)

        # 文件名哈希化
        def get_uuid(command: str):
            return uuid.uuid5(uuid.uuid4(), command).hex
        for i in range(len(randpic_file_list)):
            filename = randpic_file_list[i]
            filename_without_extension, filename_extension = os.path.splitext(filename)
            hash_new_filename = get_uuid(command) + (filename_extension if filename_extension else '.jpg')
            os.rename(path / filename, path / hash_new_filename)
            print(filename, ' -> ', hash_new_filename)

        # 将哈希化的文件名订正为规范名
        randpic_file_list = os.listdir(path)
        for i in range(len(randpic_file_list)):
            hash_filename = randpic_file_list[i]
            filename_without_extension, filename_extension = os.path.splitext(hash_filename)
            new_filename = randpic_filename.format( command=randpic_command_tuple[index],
                        index=str(i + 1).zfill(10) ) + (filename_extension if filename_extension else '.jpg')
            os.rename(path / hash_filename, path / new_filename)

        # 将图片信息写入数据库
        randpic_file_list = sorted( os.listdir(path) )
        for i in range(len(randpic_file_list)):
            filename = randpic_file_list[i]

            with (path / filename).open('rb') as f:
                data = f.read()
            data = compress_image_from_bytes(data)
            with (path / filename).open('wb') as f:
                f.write(data)

            fmd5 = hashlib.md5(data).hexdigest()
            cursor = await connection.cursor()
            command: str = randpic_command_tuple[index]
            await cursor.execute(
                'INSERT or REPLACE INTO Pic_of_{command}(md5, img_url) VALUES (?, ?)'.format(command=command),
                (fmd5, str(Path() / command / filename)))
            await connection.commit()


picture = on_fullmatch(randpic_command_tuple, permission=GROUP, priority=1, block=True)


@picture.handle()
async def pic(event: GroupMessageEvent):
    if event.group_id in randpic_banner_group:
        return
    global connection
    cursor = await connection.cursor()
    command = str(event.get_message()).strip()
    await cursor.execute(f'SELECT img_url FROM Pic_of_{command} ORDER BY RANDOM() limit 1')
    data = await cursor.fetchone()
    if data is None:
        await picture.finish('当前还没有图片!')
    file_name = data[0]
    img = randpic_img_path / file_name
    try:
        await picture.send(MessageSegment.image(img))
    except Exception as e:
        logger.info(e)
        await picture.send(f'{command}出不来了，稍后再试试吧~')


add = on_fullmatch(randpic_command_add_tuple, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True)


@add.got("pic", prompt="请发送图片！")
async def add_pic(args: str = Fullmatch(), pic_list: Message = Arg('pic')):
    global connection
    cursor = await connection.cursor()
    command = args.replace('添加', '')

    for pic_name in pic_list:
        if pic_name.type != 'image':
            await add.send(pic_name + MessageSegment.text("\n输入格式有误，请重新触发指令！"), at_sender=True)
            continue
        pic_url = pic_name.data['url']

        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers("DEFAULT")
        async with AsyncClient(verify=ssl_context) as client:
            resp = await client.get(pic_url, timeout=5.0)

        try:
            resp.raise_for_status()
        except Exception as e:
            logger.warning(e)
            await add.send(
                pic_name +
                MessageSegment.text('\n保存出错了，这张请重试')
            )
            continue

        data = resp.content
        fmd5 = hashlib.md5(data).hexdigest()

        await cursor.execute(f'SELECT img_url FROM Pic_of_{command} where md5=?', (fmd5,))
        status = await cursor.fetchone()

        if status is not None:
            await add.send(pic_name + Message('\n这张已经有了，不能重复添加！'))
        else:
            without_extension, extension = os.path.splitext(pic_url)
            randpic_cur_picnum = len(os.listdir(randpic_img_path / command))
            file_name = (randpic_filename.format(command=command, index=str(randpic_cur_picnum + 1).zfill(10))
                         + (extension if extension != '' else '.jpg'))
            file_path = randpic_img_path / command / file_name

            try:
                data = compress_image_from_bytes(data)   # 若图片超规格，压缩图片
                with file_path.open("wb") as f:
                    f.write(data)
                await cursor.execute('insert into Pic_of_{command}(md5, img_url) values (?, ?)'.format(command=command),
                                     (fmd5, str(Path() / command / file_name)))
            except Exception as e:
                logger.warning(e)
                await add.finish(pic_name + Message("\n导入失败！"), at_sender=True)

            msg = "\n导入成功！"
            if isOss:
                msg += f'可去 {endpoint}/{command}/ 查看'
                # StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_static_site()
                StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_command_html(command, file_name)
                await OSSUploaderV2().upload_file(str(randpic_path/ 'public' / command / 'index.html'), f'{command}/index.html') # 修改index.html文件
                await OSSUploaderV2().upload_file(str(randpic_path / 'public' / 'index.html'), 'index.html')
                await OSSUploaderV2().upload_file(file_path, f'{command}/{file_name}') # 上传新增的图片到OSS
            await add.finish(pic_name + Message(msg), at_sender=True)

oss = on_fullmatch('上传oss', ignorecase=True, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True, )
@oss.handle()
async def handle_oss(event: GroupMessageEvent) -> None:
    if not isOss:
        return
    await oss.send('正在上传至OSS...')
    start_time = time.time()

    StaticImageGalleryGenerator(randpic_img_path, randpic_path / 'public').generate_static_site()
    await OSSUploaderV2().upload_folder(str(randpic_path / 'public'))

    end_time = time.time()
    elapsed_time = end_time - start_time
    await oss.finish(f'上传完成，用时: {elapsed_time:.2f}秒')
