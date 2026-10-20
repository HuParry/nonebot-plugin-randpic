"""测试仅操作临时图库，QQ 和 OSS 请求使用模拟对象。"""

import atexit
import hashlib
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nonebot
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply
from PIL import Image


_boot = tempfile.TemporaryDirectory()
atexit.register(_boot.cleanup)
nonebot.init(driver='~fastapi', log_level='ERROR', randpic_store_dir_path=_boot.name, localstore_data_dir=_boot.name)
nonebot.get_driver().register_adapter(Adapter)
loaded = nonebot.load_plugin('nonebot_plugin_randpic')
if loaded is None:
    raise RuntimeError('插件未能加载')
from nonebot_plugin_randpic import handlers
from nonebot_plugin_randpic.config import Config
from nonebot_plugin_randpic.service import RandpicService


def picture(seed=1, *, size=(32, 32), format='PNG'):
    data = random.Random(seed).randbytes(size[0] * size[1] * 3)
    image = Image.frombytes('RGB', size, data)
    output = io.BytesIO()
    image.save(output, format=format)
    return output.getvalue()


def event(text='删除', *, quoted_id=11, role='admin', group_id=100, sender_id=90001):
    message = text if isinstance(text, Message) else Message(text)
    reply = None if quoted_id is None else Reply(
        time=1, message_type='group', message_id=quoted_id, real_id=quoted_id,
        sender={'user_id': sender_id}, message=Message(MessageSegment.image('https://example.test/image.png')),
    )
    return GroupMessageEvent(
        time=1, self_id=90001, post_type='message', message_type='group', sub_type='normal',
        message_id=99, user_id=123, group_id=group_id, message=message, raw_message=str(message),
        font=0, sender={'user_id': 123, 'role': role}, reply=reply,
    )


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.services = []
        self.service = await self.open_service(self.root)
        self.original = handlers._service
        handlers.setup(self.service)
        self.addCleanup(handlers.setup, self.original)
        self.addAsyncCleanup(self.close_services)
        self.bot = Bot(Adapter(nonebot.get_driver()), '90001')

    async def open_service(self, root, **kwargs):
        service = RandpicService(Config(randpic_store_dir_path=root, **kwargs), root)
        self.services.append(service)
        await service.start(background=False)
        return service

    async def close_services(self):
        for service in reversed(self.services):
            await service.close()

    async def reopen(self):
        await self.service.close()
        self.service = await self.open_service(self.root)
        handlers.setup(self.service)
        return self.service

    async def add_picture(self, seed=1, command='capoo', **kwargs):
        return (await self.service.add(command, picture(seed, **kwargs))).image

    async def record(self, image, message_id=11, group_id=100, *, count_send=False):
        await self.service.record_sent(image, '90001', group_id, {'message_id': message_id}, count_send=count_send)

    def rows(self, sql, parameters=()):
        with self.service.repository.connection() as db:
            return [tuple(row) for row in db.execute(sql, parameters)]


class FakeCloud:
    def __init__(self):
        self.objects = {}
        self.fail = False
        self.calls = []

    async def open(self):
        pass

    async def close(self):
        pass

    async def upload_file(self, path, key, expected_hash=None):
        self.calls.append(('upload', key))
        if self.fail:
            return False
        data = Path(path).read_bytes()
        if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
            return False
        self.objects[key] = data
        return True

    async def delete_file(self, key):
        self.calls.append(('delete', key))
        if self.fail:
            return False
        self.objects.pop(key, None)
        return True
