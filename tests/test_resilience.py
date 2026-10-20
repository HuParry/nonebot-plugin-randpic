import asyncio
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import ServiceTest, FakeCloud, picture
from nonebot_plugin_randpic.ali_oss import OSSUploaderV2
from nonebot_plugin_randpic.config import Config
from nonebot_plugin_randpic.models import RandpicError


class ResilienceTests(ServiceTest):
    async def test_background_worker_retries_without_manual_command(self):
        import nonebot_plugin_randpic.service as service_module
        self.service.uploader = cloud = FakeCloud()
        cloud.fail = True
        image = await self.add_picture()
        with patch.object(service_module, 'RETRY_INTERVAL', .02):
            self.service._worker = asyncio.create_task(self.service._retry_worker())
            self.service._wake.set()
            for _ in range(100):
                if cloud.calls:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(cloud.calls)
            cloud.fail = False
            for _ in range(100):
                if image.key in cloud.objects and not self.service.repository.sync_jobs():
                    break
                await asyncio.sleep(.01)
            self.assertIn(image.key, cloud.objects)
            self.assertEqual(self.service.repository.sync_jobs(), [])
            await self.service.close()
            self.assertIsNone(self.service._worker)

    async def test_cancellation_keeps_lock_until_disk_operation_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.library.add
        def delayed(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test did not release worker')
            return original(*args)
        with patch.object(self.service.library, 'add', side_effect=delayed):
            task = asyncio.create_task(self.service.add('capoo', picture()))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            task.cancel()
            await asyncio.sleep(.01)
            self.assertTrue(self.service._mutation_lock.locked())
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.service.add('capoo', picture(2))
        self.assertEqual(len(self.service.repository.list_images()), 2)
        await self.service.sync()
        self.assertEqual(len(list((self.root / 'public/capoo').glob('*.png'))), 2)

    async def test_delete_permission_error_preserves_original_file(self):
        image = await self.add_picture()
        await self.record(image)
        with patch.object(Path, 'rename', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                await self.service.delete('90001', 100, 11)
        self.assertEqual((self.root / 'img' / image.key).read_bytes(), picture())
        self.assertEqual(len(self.service.repository.list_images()), 1)
        self.assertEqual(list(self.service.library.journal.directory.glob('*.json')), [])

    async def test_symlink_cannot_redirect_quoted_deletion(self):
        image = await self.add_picture()
        await self.record(image)
        path = self.root / 'img' / image.key
        path.unlink()
        outside = self.root / 'outside.png'
        outside.write_bytes(picture())
        path.symlink_to(outside)
        with self.assertRaises(RandpicError):
            await self.service.delete('90001', 100, 11)
        self.assertEqual(outside.read_bytes(), picture())

    async def test_real_sdk_requests_use_correct_fields_and_reuse_client(self):
        import nonebot_plugin_randpic.ali_oss as module
        uploader = OSSUploaderV2(Config(randpic_bucket='test-bucket', randpic_region='cn-beijing',
                                        randpic_oss_access_key_id='test', randpic_oss_access_key_secret='test'))
        client = SimpleNamespace(put_object=AsyncMock(return_value=SimpleNamespace(status_code=200)),
                                 delete_object=AsyncMock(return_value=SimpleNamespace(status_code=204)),
                                 close=AsyncMock())
        image = await self.add_picture()
        path = self.root / 'img' / image.key
        with patch.object(module.oss_aio, 'AsyncClient', return_value=client) as constructor:
            self.assertTrue(await uploader.upload_file(str(path), image.key, image.content_hash))
            request = client.put_object.await_args.args[0]
            self.assertEqual((request.bucket, request.key, request.content_type), ('test-bucket', image.key, 'image/png'))
            self.assertEqual(hashlib.sha256(request.body).hexdigest(), image.content_hash)
            self.assertIn('immutable', request.cache_control)
            self.assertTrue(await uploader.delete_file(image.key))
            constructor.assert_called_once()
            client.close.assert_not_awaited()
            self.assertFalse(await uploader.upload_file(str(path), image.key, 'stale hash'))
            self.assertEqual(client.put_object.await_count, 1)
            client.delete_object.side_effect = OSError('network failed')
            self.assertFalse(await uploader.delete_file(image.key))
            await uploader.close()
            client.close.assert_awaited_once()
