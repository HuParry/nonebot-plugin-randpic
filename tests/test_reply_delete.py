"""运行方式：python -m unittest discover -s tests -v；不访问 QQ 或真实 OSS。"""

import asyncio
import atexit
import hashlib
import importlib
import io
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import nonebot
from PIL import Image
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply
from nonebot.exception import FinishedException


_boot_directory = tempfile.TemporaryDirectory()
atexit.register(_boot_directory.cleanup)
nonebot.init(
    driver='~fastapi', log_level='ERROR',
    randpic_store_dir_path=_boot_directory.name,
    localstore_data_dir=_boot_directory.name,
)
nonebot.get_driver().register_adapter(Adapter)
with redirect_stdout(io.StringIO()):
    _loaded = nonebot.load_plugin('nonebot_plugin_randpic')
if _loaded is None:
    raise RuntimeError('测试插件加载失败')
plugin = _loaded.module
from nonebot_plugin_randpic.image_store import ImageDeletionError, SentImageStore


class ReplyDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image_root = self.root / 'img'
        self.image_root.mkdir()
        self.database = self.root / 'data.db'
        self.store = SentImageStore(self.database, self.image_root)
        await self.store.initialize()
        self.db = await aiosqlite.connect(self.database)
        self.addAsyncCleanup(self.db.close)
        await self.db.execute('''CREATE TABLE folder_snapshot (
            command TEXT PRIMARY KEY, folder_mtime REAL NOT NULL, checked_at REAL NOT NULL
        )''')
        await self.db.commit()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            plugin, connection=self.db, sent_image_store=self.store,
            randpic_path=self.root, randpic_img_path=self.image_root,
            current_commands_config={'capoo': set()}, randpic_banner_group=[],
            randpic_oss_no_upload_list=[], isOss=False,
            _image_lock=asyncio.Lock(), _gallery_lock=asyncio.Lock(),
            create=True,
        ))
        self.bot = Bot(Adapter(nonebot.get_driver()), '90001')
        self.responses = []

    async def asyncTearDown(self):
        self.stack.close()
        await self.db.close()
        self.temporary.cleanup()

    def event(self, *, text='删除', quoted_id=11, role='admin', group_id=100, sender_id=90001):
        message = Message(text)
        reply = None if quoted_id is None else Reply(
            time=1, message_type='group', message_id=quoted_id, real_id=quoted_id,
            sender={'user_id': sender_id}, message=Message(MessageSegment.image('https://example.test/a.png')),
        )
        return GroupMessageEvent(
            time=1, self_id=90001, post_type='message', message_type='group', sub_type='normal',
            message_id=99, user_id=123, group_id=group_id, message=message,
            raw_message=str(message), font=0, sender={'user_id': 123, 'role': role}, reply=reply,
        )

    async def add_image(self, *, name='randpic_capoo_0000000001.png', content=b'original', command='capoo'):
        folder = self.image_root / command
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        url = f'{command}/{name}'
        await self.db.execute(f'''CREATE TABLE IF NOT EXISTS Pic_of_{command} (
            id INTEGER PRIMARY KEY AUTOINCREMENT, img_url TEXT NOT NULL, phash TEXT NOT NULL,
            send_count INTEGER NOT NULL DEFAULT 0
        )''')
        async with self.db.execute(
            f'INSERT INTO Pic_of_{command} (img_url, phash) VALUES (?, ?)', (url, '0' * 16)
        ) as cursor:
            image_id = cursor.lastrowid
        await self.db.execute(
            'INSERT OR REPLACE INTO folder_snapshot VALUES (?, ?, ?)',
            (command, folder.stat().st_mtime, time.time()),
        )
        await self.db.commit()
        return image_id, path, url, hashlib.sha256(content).hexdigest()

    async def record(self, image, message_id=11, *, group_id=100):
        image_id, path, url, digest = image
        await self.store.record_sent(
            '90001', group_id, message_id, path.parent.name, image_id, url, digest, count_send=False
        )

    async def rows(self, sql, parameters=()):
        async with self.db.execute(sql, parameters) as cursor:
            return await cursor.fetchall()

    async def handle(self, event):
        async def finish(message, **kwargs):
            self.responses.append(message)
            raise FinishedException

        with patch.object(plugin, 'delete_picture', SimpleNamespace(finish=finish)):
            with self.assertRaises(FinishedException):
                await plugin.handle_delete_picture(event)
        return self.responses[-1]

    async def test_permission_is_the_same_as_add(self):
        for role, expected in [('member', False), ('admin', True), ('owner', True)]:
            event = self.event(role=role)
            self.assertEqual(await plugin.delete_picture.permission(self.bot, event), expected)
            self.assertEqual(
                await plugin.delete_picture.permission(self.bot, event),
                await plugin.add.permission(self.bot, event),
            )

    async def test_rule_requires_quote_and_exact_delete_text(self):
        self.assertTrue(await plugin._is_delete_request(self.event()))
        self.assertTrue(await plugin._is_delete_request(self.event(text='  删除\n')))
        self.assertFalse(await plugin._is_delete_request(self.event(quoted_id=None)))
        self.assertFalse(await plugin._is_delete_request(self.event(text='删除capoo')))
        event = self.event()
        event.message += MessageSegment.at(12345)
        self.assertFalse(await plugin._is_delete_request(event))
        event = self.event()
        event.message += MessageSegment.at(90001)
        self.assertTrue(await plugin._is_delete_request(event))
        event.message += MessageSegment.image('https://example.test/extra.png')
        self.assertFalse(await plugin._is_delete_request(event))

    async def test_original_reply_id_works_without_get_msg(self):
        image = await self.add_image()
        await self.record(image)
        event = self.event(quoted_id=None)
        event.original_message = MessageSegment.reply(11) + Message('删除')
        self.assertTrue(await plugin._is_delete_request(event))
        self.assertIn('已从', await self.handle(event))
        self.assertFalse(image[1].exists())

    async def test_sent_picture_is_recorded_and_counted(self):
        image = await self.add_image()
        with patch.object(plugin.picture, 'send', AsyncMock(return_value={'message_id': 11})):
            await plugin.pic(self.event(text='capoo', quoted_id=None))
        self.assertEqual(await self.rows('SELECT send_count FROM Pic_of_capoo'), [(1,)])
        self.assertEqual(await self.rows('SELECT message_id, content_hash FROM randpic_sent_images'), [(11, image[3])])
        self.assertIn('已从', await self.handle(self.event()))

    async def test_missing_send_message_id_still_counts(self):
        await self.add_image()
        with patch.object(plugin.picture, 'send', AsyncMock(return_value=None)):
            await plugin.pic(self.event(text='capoo', quoted_id=None))
        self.assertEqual(await self.rows('SELECT send_count FROM Pic_of_capoo'), [(1,)])
        self.assertEqual(await self.rows('SELECT * FROM randpic_sent_images'), [])

    async def test_add_confirmation_can_be_quoted_without_counting_as_random_send(self):
        initial = await self.add_image()
        initial[1].unlink()
        await self.db.execute('DELETE FROM Pic_of_capoo')
        await self.db.commit()
        image_data = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(image_data, format='PNG')
        response = SimpleNamespace(content=image_data.getvalue(), raise_for_status=lambda: None)
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        with patch.object(plugin, 'AsyncClient', return_value=client), \
                patch.object(plugin.add, 'send', AsyncMock(return_value={'message_id': 55})), \
                patch.object(plugin.add, 'finish', AsyncMock(side_effect=FinishedException)):
            with self.assertRaises(FinishedException):
                await plugin.add_pic(
                    self.event(text='添加capoo', quoted_id=None), matched=('capoo',),
                    pic_list=Message(MessageSegment('image', {
                        'file': 'new.png', 'url': 'https://example.test/new.png',
                    })),
                )
        self.assertEqual(await self.rows('SELECT send_count FROM Pic_of_capoo'), [(0,)])
        self.assertEqual(await self.rows('SELECT message_id FROM randpic_sent_images'), [(55,)])
        self.assertIn('已从', await self.handle(self.event(quoted_id=55)))

    async def test_in_flight_send_does_not_restore_deleted_image(self):
        image = await self.add_image()
        await self.record(image)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_send(message):
            started.set()
            await release.wait()
            return {'message_id': 12}

        with patch.object(plugin.picture, 'send', slow_send):
            task = asyncio.create_task(plugin.pic(self.event(text='capoo', quoted_id=None)))
            await started.wait()
            try:
                await self.handle(self.event())
            finally:
                release.set()
                await task
        self.assertFalse(image[1].exists())
        self.assertEqual(await self.rows('SELECT deleted FROM randpic_sent_images WHERE message_id = 12'), [(1,)])
        self.assertIn('已经', await self.handle(self.event(quoted_id=12)))

    async def test_empty_collection_is_reported_normally(self):
        await self.add_image()
        await self.db.execute('DELETE FROM Pic_of_capoo')
        await self.db.commit()
        with patch.object(plugin.picture, 'send', AsyncMock()) as send:
            await plugin.pic(self.event(text='capoo', quoted_id=None))
        send.assert_awaited_once_with('当前还没有图片!')

    async def test_deletes_source_database_and_last_image_web_pages(self):
        image = await self.add_image()
        await self.record(image)
        gallery = plugin.StaticImageGalleryGenerator(self.image_root, self.root / 'public')
        gallery.generate_command_html('capoo', image[1].name, [])
        self.assertTrue((self.root / 'public' / image[2]).exists())
        self.assertIn('已从', await self.handle(self.event()))
        self.assertFalse(image[1].exists())
        self.assertFalse((self.root / 'public' / image[2]).exists())
        self.assertEqual(await self.rows('SELECT * FROM Pic_of_capoo'), [])
        self.assertIn('暂时还没有图片', (self.root / 'public/capoo/index.html').read_text())
        self.assertIn('0 个分类', (self.root / 'public/index.html').read_text())
        self.assertEqual(await self.rows('SELECT deleted, sync_pending FROM randpic_sent_images'), [(1, 0)])

    async def test_repeat_and_other_group_reference_share_deletion_status(self):
        image = await self.add_image()
        await self.record(image)
        await self.record(image, message_id=12, group_id=200)
        await self.handle(self.event())
        self.assertIn('已经', await self.handle(self.event(quoted_id=12, group_id=200)))
        self.assertEqual(await self.rows('SELECT deleted FROM randpic_sent_images'), [(1,), (1,)])

    async def test_untracked_wrong_group_and_wrong_sender_cannot_delete(self):
        image = await self.add_image()
        await self.record(image)
        for event in (self.event(quoted_id=999), self.event(group_id=200), self.event(sender_id=123)):
            await self.handle(event)
            self.assertTrue(image[1].exists())
        with self.assertRaises(ImageDeletionError):
            await self.store.delete('other-bot', 100, 11)
        self.assertTrue(image[1].exists())

    async def test_replaced_file_is_not_deleted(self):
        image = await self.add_image()
        await self.record(image)
        image[1].write_bytes(b'replacement')
        self.assertIn('已被替换', await self.handle(self.event()))
        self.assertEqual(image[1].read_bytes(), b'replacement')
        self.assertEqual(await self.rows('SELECT deleted FROM randpic_sent_images'), [(0,)])

    async def test_reused_filename_is_safe_from_old_quotes_and_sync_retry(self):
        image = await self.add_image()
        await self.record(image)
        await self.store.delete('90001', 100, 11)
        replacement = await self.add_image(content=b'new file')
        uploader = SimpleNamespace(delete_file=AsyncMock(return_value=True), upload_file=AsyncMock(return_value=True))
        with patch.object(plugin, 'isOss', True), patch.object(plugin, 'OSSUploaderV2', return_value=uploader):
            self.assertIn('已经', await self.handle(self.event()))
        self.assertEqual(replacement[1].read_bytes(), b'new file')
        uploader.delete_file.assert_not_awaited()

    async def test_missing_source_still_cleans_database(self):
        image = await self.add_image()
        await self.record(image)
        image[1].unlink()
        self.assertIn('已从', await self.handle(self.event()))
        self.assertEqual(await self.rows('SELECT * FROM Pic_of_capoo'), [])

    async def test_database_failure_restores_file_and_record(self):
        image = await self.add_image()
        await self.record(image)
        await self.db.execute('''CREATE TRIGGER reject_delete BEFORE DELETE ON Pic_of_capoo
            BEGIN SELECT RAISE(ABORT, 'simulated database failure'); END''')
        await self.db.commit()
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.store.delete('90001', 100, 11)
        self.assertEqual(image[1].read_bytes(), b'original')
        self.assertEqual(await self.rows('SELECT deleted FROM randpic_sent_images'), [(0,)])
        self.assertEqual(list(image[1].parent.glob('.randpic-delete-*')), [])

    async def test_file_failure_does_not_remove_database_record(self):
        image = await self.add_image()
        await self.record(image)
        with patch.object(Path, 'rename', side_effect=PermissionError('simulated file failure')):
            with self.assertRaises(PermissionError):
                await self.store.delete('90001', 100, 11)
        self.assertTrue(image[1].exists())
        self.assertEqual(len(await self.rows('SELECT * FROM Pic_of_capoo')), 1)

    async def test_mapping_survives_restart(self):
        image = await self.add_image()
        await self.record(image)
        restarted = SentImageStore(self.database, self.image_root)
        await restarted.initialize()
        await restarted.delete('90001', 100, 11)
        self.assertFalse(image[1].exists())

    async def test_interrupted_delete_recovery(self):
        image = await self.add_image()
        await self.record(image)
        staged = image[1].with_name(f'.randpic-delete-{image[1].name}.{"a" * 32}.tmp')
        image[1].rename(staged)
        await self.store.initialize()
        self.assertEqual(image[1].read_bytes(), b'original')
        image[1].rename(staged)
        await self.db.execute('UPDATE randpic_sent_images SET deleted = 1')
        await self.db.commit()
        await self.store.initialize()
        self.assertFalse(staged.exists())
        self.assertFalse(image[1].exists())

    async def test_path_traversal_and_symlink_are_rejected(self):
        for command, url in [('capoo', '../escape.png'), ('bad;SQL', 'bad;SQL/a.png'), ('capoo', 'capoo/../a.png')]:
            with self.assertRaises(ImageDeletionError):
                self.store.image_path(command, url)
        image = await self.add_image()
        await self.record(image)
        image[1].unlink()
        outside = self.root / 'outside.png'
        outside.write_bytes(b'original')
        image[1].symlink_to(outside)
        with self.assertRaises(ImageDeletionError):
            await self.store.delete('90001', 100, 11)
        self.assertEqual(outside.read_bytes(), b'original')

    async def test_cloud_failure_is_persistent_and_retryable(self):
        image = await self.add_image()
        await self.record(image)
        uploader = SimpleNamespace(
            delete_file=AsyncMock(side_effect=[False, True]), upload_file=AsyncMock(return_value=True)
        )
        with patch.object(plugin, 'isOss', True), patch.object(plugin, 'OSSUploaderV2', return_value=uploader):
            self.assertIn('OSS 图片删除失败', await self.handle(self.event()))
            self.assertEqual(len(await self.store.pending_deletions()), 1)
            self.assertIn('已经', await self.handle(self.event()))
        self.assertEqual(await self.store.pending_deletions(), [])
        self.assertEqual(uploader.delete_file.await_count, 2)
        self.assertEqual(uploader.upload_file.await_count, 4)

    async def test_full_upload_retries_pending_cloud_deletions(self):
        image = await self.add_image()
        await self.record(image)
        await self.store.delete('90001', 100, 11)
        uploader = SimpleNamespace(
            delete_file=AsyncMock(return_value=True), upload_file=AsyncMock(return_value=True),
            upload_folder=AsyncMock(return_value=(2, 0)),
        )

        async def finish(message, **kwargs):
            self.responses.append(message)
            raise FinishedException

        matcher = SimpleNamespace(send=AsyncMock(), finish=finish)
        with patch.object(plugin, 'isOss', True), patch.object(plugin, 'OSSUploaderV2', return_value=uploader), \
                patch.object(plugin, 'OSS', matcher), redirect_stdout(io.StringIO()):
            with self.assertRaises(FinishedException):
                await plugin.handle_oss(self.event())
        self.assertEqual(await self.store.pending_deletions(), [])
        uploader.delete_file.assert_awaited_once_with(image[2])
        self.assertIn('上传完成', self.responses[-1])

    async def test_excluded_collection_still_cleans_old_cloud_object(self):
        image = await self.add_image()
        await self.record(image)
        uploader = SimpleNamespace(delete_file=AsyncMock(return_value=True), upload_file=AsyncMock(return_value=True))
        with patch.object(plugin, 'isOss', True), patch.object(plugin, 'OSSUploaderV2', return_value=uploader), \
                patch.object(plugin, 'randpic_oss_no_upload_list', ['capoo']):
            await self.handle(self.event())
        uploader.delete_file.assert_awaited_once_with(image[2])
        self.assertEqual([call.args[1] for call in uploader.upload_file.await_args_list], ['index.html'])

    async def test_concurrent_requests_delete_once(self):
        image = await self.add_image()
        await self.record(image)

        async def finish(message, **kwargs):
            self.responses.append(message)
            raise FinishedException

        async def invoke():
            with self.assertRaises(FinishedException):
                await plugin.handle_delete_picture(self.event())

        with patch.object(plugin, 'delete_picture', SimpleNamespace(finish=finish)):
            await asyncio.gather(invoke(), invoke())
        self.assertTrue(any('已从' in result for result in self.responses))
        self.assertTrue(any('已经' in result for result in self.responses))
        self.assertFalse(image[1].exists())

    async def test_delete_keeps_other_images_and_snapshot_consistent(self):
        first = await self.add_image()
        second = await self.add_image(name='randpic_capoo_0000000002.jpg', content=b'second')
        await self.record(first)
        await self.handle(self.event())
        self.assertTrue(second[1].exists())
        self.assertFalse(await plugin._is_folder_changed('capoo', second[1].parent))
        self.assertEqual(await self.rows('SELECT img_url FROM Pic_of_capoo'), [(second[2],)])


class OSSDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_sdk_delete_and_close(self):
        oss_module = importlib.import_module('nonebot_plugin_randpic.ali_oss')
        uploader = object.__new__(oss_module.OSSUploaderV2)
        uploader.cfg = object()
        uploader.bucket_name = 'test-bucket'
        client = SimpleNamespace(
            delete_object=AsyncMock(return_value=SimpleNamespace(status_code=204)), close=AsyncMock()
        )
        with patch.object(oss_module.oss_aio, 'AsyncClient', return_value=client):
            self.assertTrue(await uploader.delete_file('capoo/image.png'))
        request = client.delete_object.await_args.args[0]
        self.assertEqual(request.bucket, 'test-bucket')
        self.assertEqual(request.key, 'capoo/image.png')
        client.close.assert_awaited_once()

    async def test_sdk_failure_still_closes_client(self):
        oss_module = importlib.import_module('nonebot_plugin_randpic.ali_oss')
        uploader = object.__new__(oss_module.OSSUploaderV2)
        uploader.cfg = object()
        uploader.bucket_name = 'test-bucket'
        client = SimpleNamespace(delete_object=AsyncMock(side_effect=OSError('network failure')), close=AsyncMock())
        with patch.object(oss_module.oss_aio, 'AsyncClient', return_value=client):
            self.assertFalse(await uploader.delete_file('capoo/image.png'))
        client.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
