import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from tests.support import ServiceTest, picture
from nonebot_plugin_randpic.models import DuplicateImage, RandpicError


class LibraryTests(ServiceTest):
    async def test_add_random_and_persistent_counter(self):
        first = await self.add_picture()
        snapshot = await self.service.pick('capoo')
        self.assertEqual(snapshot.image.id, first.id)
        self.assertEqual(hashlib.sha256(snapshot.data).hexdigest(), first.content_hash)
        await self.record(first, count_send=True)
        await self.reopen()
        current = self.service.repository.list_images()[0]
        self.assertEqual((current.id, current.send_count), (first.id, 1))

    async def test_all_suffixes_share_one_sequence_even_after_deletion(self):
        first = await self.add_picture(1, format='JPEG')
        second = await self.add_picture(2, format='PNG')
        self.assertNotEqual(Path(first.filename).stem, Path(second.filename).stem)
        await self.record(second)
        await self.service.delete('90001', 100, 11)
        third = await self.add_picture(3, format='GIF')
        self.assertIn('0000000003', third.filename)
        self.assertNotEqual(third.id, second.id)
        await self.reopen()
        fourth = await self.add_picture(4)
        self.assertIn('0000000004', fourth.filename)

    async def test_duplicate_is_rejected_but_can_be_readded_after_deletion(self):
        first = await self.add_picture()
        with self.assertRaises(DuplicateImage):
            await self.add_picture()
        await self.record(first)
        await self.service.delete('90001', 100, 11)
        replacement = await self.add_picture()
        self.assertNotEqual(replacement.id, first.id)
        result = await self.service.delete('90001', 100, 11)
        self.assertTrue(result.already_deleted)
        self.assertTrue((self.service.library.image_root / replacement.key).exists())

    async def test_offline_addition_renaming_and_duplicates_are_reconciled(self):
        first = await self.add_picture()
        await self.record(first, count_send=True)
        await self.service.close()
        directory = self.root / 'img/capoo'
        original = directory / first.filename
        original.rename(directory / 'renamed-without-extension')
        (directory / 'manual.jpg').write_bytes(picture(2, format='PNG'))
        (directory / 'randpic_capoo_0000000008.jpg').write_bytes(picture(3, format='JPEG'))
        (directory / 'randpic_capoo_0000000008.png').write_bytes(picture(4))
        (directory / 'notes.txt').write_text('keep me')
        (directory / 'broken.png').write_bytes(b'broken')
        await self.reopen()
        images = self.service.repository.list_images()
        self.assertEqual(len(images), 4)
        self.assertEqual(len({Path(image.filename).stem for image in images}), 4)
        self.assertTrue(all(image.validated for image in images))
        retained = next(image for image in images if image.content_hash == first.content_hash)
        self.assertEqual((retained.id, retained.send_count), (first.id, 1))
        await self.service.delete('90001', 100, 11)
        self.assertFalse((directory / retained.filename).exists())
        self.assertEqual((directory / 'notes.txt').read_text(), 'keep me')
        self.assertEqual((directory / 'broken.png').read_bytes(), b'broken')

    async def test_offline_inplace_change_does_not_redirect_old_quote_to_new_image(self):
        old = await self.add_picture()
        await self.record(old)
        await self.service.close()
        (self.root / 'img' / old.key).write_bytes(picture(5))
        await self.reopen()
        current = self.service.repository.list_images()[0]
        self.assertNotEqual(current.id, old.id)
        result = await self.service.delete('90001', 100, 11)
        self.assertTrue(result.already_deleted)
        self.assertTrue((self.root / 'img' / current.key).exists())

    async def test_offline_name_swap_keeps_identity_and_counts(self):
        first, second = await self.add_picture(1), await self.add_picture(2)
        await self.record(first, 11, count_send=True)
        await self.record(second, 12)
        await self.service.close()
        a, b = self.root / 'img' / first.key, self.root / 'img' / second.key
        temporary = a.with_name('swap.tmp')
        a.rename(temporary)
        b.rename(a)
        temporary.rename(b)
        await self.reopen()
        active = {image.id: image for image in self.service.repository.list_images()}
        self.assertEqual(active[first.id].filename, second.filename)
        self.assertEqual(active[first.id].send_count, 1)
        await self.service.delete('90001', 100, 11)
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())

    async def test_offline_new_directory_is_indexed_when_registered_in_config(self):
        await self.service.close()
        directory = self.root / 'img/新图库'
        directory.mkdir()
        (directory / '随便的名字.png').write_bytes(picture())
        (self.root / 'randpic_commands.json').write_text(json.dumps({'capoo': [], '新图库': []}, ensure_ascii=False))
        await self.reopen()
        self.assertIn('新图库', self.service.commands)
        self.assertIn('新图库', json.loads((self.root / 'randpic_commands.json').read_text()))
        self.assertIsNotNone(await self.service.pick('新图库'))

    async def test_normal_restart_does_not_rename_or_rewrite_images(self):
        image = await self.add_picture()
        path = self.root / 'img' / image.key
        before = path.stat().st_mtime_ns
        await self.reopen()
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(self.service.repository.list_images()[0].id, image.id)

    async def test_missing_file_is_removed_without_failing_other_random_images(self):
        first, second = await self.add_picture(1), await self.add_picture(2)
        (self.root / 'img' / first.key).unlink()
        with patch.object(self.service.repository, 'choose', side_effect=[first, second]):
            snapshot = await self.service.pick('capoo')
        self.assertEqual(snapshot.image.id, second.id)
        self.assertEqual(len(self.service.repository.list_images()), 1)

    async def test_changed_live_file_is_never_sent_or_deleted(self):
        image = await self.add_picture()
        await self.record(image)
        (self.root / 'img' / image.key).write_bytes(picture(6))
        with self.assertRaises(RandpicError):
            await self.service.pick('capoo')
        with self.assertRaises(RandpicError):
            await self.service.delete('90001', 100, 11)
        self.assertEqual(len(self.service.repository.list_images()), 1)

    async def test_same_image_all_historical_references_show_deleted(self):
        image = await self.add_picture()
        await self.record(image, 11)
        await self.record(image, 12, group_id=200)
        await self.service.delete('90001', 100, 11)
        result = await self.service.delete('90001', 200, 12)
        self.assertTrue(result.already_deleted)
        with self.assertRaises(RandpicError):
            await self.service.delete('other-bot', 100, 11)
        with self.assertRaises(RandpicError):
            await self.service.delete('90001', 200, 11)

    async def test_add_failure_rolls_back_file_and_database(self):
        with patch.object(self.service.repository, 'upsert_image', side_effect=sqlite3.IntegrityError('failure')):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.add_picture()
        self.assertEqual(self.service.repository.list_images(), [])
        self.assertEqual(list((self.root / 'img/capoo').glob('*.png')), [])
        self.assertEqual(list(self.service.library.journal.directory.glob('*.json')), [])

    async def test_delete_failure_restores_original_and_reference(self):
        image = await self.add_picture()
        await self.record(image)
        with patch.object(self.service.repository, 'mark_dirty', side_effect=sqlite3.IntegrityError('failure')):
            with self.assertRaises(sqlite3.IntegrityError):
                await self.service.delete('90001', 100, 11)
        self.assertEqual((self.root / 'img' / image.key).read_bytes(), picture())
        self.assertFalse(self.service.repository.list_images()[0].deleted)
        self.assertFalse((await self.service.delete('90001', 100, 11)).already_deleted)

    async def test_file_collision_never_removes_the_preexisting_target(self):
        import nonebot_plugin_randpic.journal as journal
        def collision(source, destination):
            destination.write_bytes(b'another program wrote this')
            raise FileExistsError('simulated race')
        with patch.object(journal, 'install_file', side_effect=collision):
            with self.assertRaises(FileExistsError):
                await self.add_picture()
        path = self.root / 'img/capoo/randpic_capoo_0000000001.png'
        self.assertEqual(path.read_bytes(), b'another program wrote this')
        self.assertEqual(self.service.repository.list_images(), [])

    async def test_interrupted_delete_is_recovered_before_indexing(self):
        image = await self.add_picture()
        await self.record(image)
        self.service.library.journal.delete(image)
        await self.reopen()
        self.assertTrue((self.root / 'img' / image.key).exists())
        self.assertEqual(self.service.repository.list_images()[0].id, image.id)
        self.service.library.journal.delete(image)
        with self.service.repository.connection(write=True) as db:
            db.execute('UPDATE randpic_images SET deleted = 1 WHERE id = ?', (image.id,))
        await self.reopen()
        self.assertFalse((self.root / 'img' / image.key).exists())
        self.assertEqual(self.service.repository.list_images(), [])

    async def test_concurrent_adds_allocate_unique_stems(self):
        results = await asyncio.gather(*(self.service.add('capoo', picture(seed)) for seed in range(1, 7)))
        self.assertEqual(len({Path(result.image.filename).stem for result in results}), 6)
        self.assertEqual(len(self.service.repository.list_images()), 6)

    async def test_concurrent_delete_only_mutates_once(self):
        image = await self.add_picture()
        await self.record(image)
        results = await asyncio.gather(self.service.delete('90001', 100, 11), self.service.delete('90001', 100, 11))
        self.assertEqual(sorted(result.already_deleted for result in results), [False, True])

    async def test_stale_send_can_be_recorded_after_deletion(self):
        image = await self.add_picture()
        await self.record(image)
        await self.service.delete('90001', 100, 11)
        await self.record(image, 12, count_send=True)
        self.assertTrue((await self.service.delete('90001', 100, 12)).already_deleted)
        self.assertEqual(self.service.repository.list_images(include_deleted=True)[0].send_count, 0)

    async def test_only_one_process_can_use_a_directory(self):
        from nonebot_plugin_randpic.service import RandpicService
        second = RandpicService(self.service.config, self.root)
        with self.assertRaises(RandpicError):
            await second.start(background=False)
        await second.close()
        self.assertIsNotNone(await self.service.add('capoo', picture()))
