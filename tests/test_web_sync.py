import asyncio
import hashlib
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from tests.support import ServiceTest, FakeCloud, picture
from nonebot_plugin_randpic.drivers.fastapi import GalleryFiles
from nonebot_plugin_randpic.models import RandpicError


class WebSyncTests(ServiceTest):
    async def test_local_gallery_updates_without_oss_and_is_http_accessible(self):
        image = await self.add_picture(command='马哥')
        app = FastAPI()
        app.mount('/randpic', GalleryFiles(directory=str(self.root / 'public'), html=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            home = await client.get('/randpic/')
            self.assertEqual(home.status_code, 200)
            self.assertIn('马哥', home.text)
            gallery = await client.get('/randpic/马哥/')
            self.assertIn('loading="lazy"', gallery.text)
            self.assertEqual(gallery.headers['cache-control'], 'no-cache')
            response = await client.get('/randpic/' + image.key)
            self.assertEqual(hashlib.sha256(response.content).hexdigest(), image.content_hash)
            await self.record(image)
            await self.service.delete('90001', 100, 11)
            self.assertEqual((await client.get('/randpic/' + image.key)).status_code, 404)
            self.assertIn('暂时还没有图片', (await client.get('/randpic/马哥/')).text)

    async def test_pagination_is_pruned_after_deletion_and_html_ignores_counts(self):
        self.service.publisher.page_size = 2
        images = [await self.add_picture(seed) for seed in range(1, 6)]
        public = self.root / 'public'
        self.assertTrue((public / 'capoo/page-3.html').exists())
        before = (public / 'capoo/index.html').read_bytes()
        await self.record(images[0], count_send=True)
        await self.service.sync()
        self.assertEqual((public / 'capoo/index.html').read_bytes(), before)
        for n, image in enumerate(images[2:]):
            await self.record(image, 100 + n)
            await self.service.delete('90001', 100, 100 + n)
        self.assertFalse((public / 'capoo/page-2.html').exists())
        self.assertFalse((public / 'capoo/page-3.html').exists())
        self.assertIn('第 1 / 1 页', (public / 'capoo/index.html').read_text())

    async def test_oss_exclusions_do_not_hide_local_gallery(self):
        self.service.uploader = cloud = FakeCloud()
        self.service.config.randpic_oss_no_upload_list = ['capoo']
        image = await self.add_picture()
        visible = await self.add_picture(2, '马哥')
        result = await self.service.sync()
        self.assertEqual(result.failed, 0)
        self.assertTrue((self.root / 'public' / image.key).exists())
        self.assertNotIn(image.key, cloud.objects)
        self.assertIn(visible.key, cloud.objects)
        self.assertNotIn('capoo/index.html', cloud.objects)

    async def test_cloud_differences_are_rediscovered_after_restart_and_delete_failure(self):
        self.service.uploader = cloud = FakeCloud()
        cloud.fail = True
        image = await self.add_picture()
        await self.record(image)
        result = await self.service.sync()
        self.assertGreater(result.failed, 0)
        self.assertGreater(result.pending, 0)
        await self.reopen()
        self.service.uploader = cloud
        cloud.fail = False
        result = await self.service.sync()
        self.assertEqual(result.pending, 0)
        self.assertIn(image.key, cloud.objects)
        cloud.fail = True
        await self.service.delete('90001', 100, 11)
        result = await self.service.sync()
        self.assertGreater(result.pending, 0)
        self.assertFalse((self.root / 'public' / image.key).exists())
        cloud.fail = False
        self.assertEqual((await self.service.sync()).pending, 0)
        self.assertNotIn(image.key, cloud.objects)

    async def test_publication_failure_retries_without_losing_image(self):
        with patch.object(self.service.publisher, 'publish', side_effect=OSError('disk failure')):
            result = await self.service.add('capoo', picture())
        self.assertIsNotNone(result.web_error)
        self.assertTrue((self.root / 'img' / result.image.key).exists())
        self.assertIsNone((await self.service.sync()).web_error)
        self.assertTrue((self.root / 'public' / result.image.key).exists())

    async def test_inflight_upload_is_deleted_on_next_comparison_without_blocking_mutation(self):
        started, release = asyncio.Event(), asyncio.Event()
        class SlowCloud(FakeCloud):
            async def upload_file(inner, path, key, expected_hash=None):
                if key.endswith('.png'):
                    data = Path(path).read_bytes()
                    started.set()
                    await release.wait()
                    inner.objects[key] = data
                    return True
                return await super().upload_file(path, key, expected_hash)
        self.service.uploader = cloud = SlowCloud()
        image = await self.add_picture()
        await self.record(image)
        task = asyncio.create_task(self.service.sync())
        self.addAsyncCleanup(lambda: asyncio.gather(task, return_exceptions=True))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(self.service.delete('90001', 100, 11), 2)
        finally:
            release.set()
        await task
        self.assertTrue(self.service._remote_dirty)
        self.assertEqual((await self.service.sync()).pending, 0)
        self.assertNotIn(image.key, cloud.objects)

    async def test_upload_concurrency_is_bounded_and_parallel(self):
        class CountingCloud(FakeCloud):
            active = maximum = 0
            async def upload_file(inner, *args):
                inner.active += 1
                inner.maximum = max(inner.maximum, inner.active)
                try:
                    await asyncio.sleep(.01)
                    return await super().upload_file(*args)
                finally:
                    inner.active -= 1
        self.service.uploader = cloud = CountingCloud()
        self.service.config.randpic_upload_concurrency = 3
        for seed in range(1, 8):
            await self.add_picture(seed)
        self.assertEqual((await self.service.sync()).pending, 0)
        self.assertEqual(cloud.maximum, 3)

    async def test_download_handles_http_errors_and_actual_stream_size(self):
        await self.service.client.aclose()
        self.service.config.randpic_download_max_mb = 1
        async def respond(request):
            if request.url.path == '/error':
                return httpx.Response(503)
            if request.url.path == '/huge':
                return httpx.Response(200, content=b'x' * (1024 * 1024 + 1))
            return httpx.Response(200, content=b'hello')
        self.service.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.assertEqual(await self.service.download('https://test/ok'), b'hello')
        for url in ('file:///tmp/image', 'https://test/error', 'https://test/huge'):
            with self.assertRaises(RandpicError):
                await self.service.download(url)
