import hashlib
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from tests.support import ServiceTest, picture


class MigrationTests(ServiceTest):
    async def legacy(self, *, count=True, deleted=False, missing=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'database').mkdir()
        (root / 'img/capoo').mkdir(parents=True)
        key = 'capoo/randpic_capoo_0000000042.png'
        data = picture()
        if not missing:
            (root / 'img' / key).write_bytes(data)
        (root / 'randpic_commands.json').write_text('{"capoo": []}')
        with closing(sqlite3.connect(root / 'database/data.db')) as db:
            db.execute('CREATE TABLE Pic_of_capoo (id INTEGER PRIMARY KEY, img_url TEXT, phash TEXT' + (', send_count INTEGER' if count else '') + ')')
            db.execute('INSERT INTO Pic_of_capoo VALUES (' + ','.join('?' for _ in range(4 if count else 3)) + ')',
                       (1, key, '', 12) if count else (1, key, ''))
            db.execute('CREATE TABLE randpic_sent_images (bot_id TEXT, group_id INTEGER, message_id INTEGER, command TEXT, img_url TEXT, content_hash TEXT, deleted INTEGER, sync_pending INTEGER)')
            db.execute('INSERT INTO randpic_sent_images VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       ('90001', 100, 11, 'capoo', key, 'old hash' if deleted else hashlib.sha256(data).hexdigest(), int(deleted), 1))
            db.commit()
        return root, key

    async def test_migration_preserves_counts_quotes_and_original_tables(self):
        root, key = await self.legacy()
        service = await self.open_service(root)
        image = service.repository.list_images()[0]
        self.assertEqual((image.send_count, image.key), (12, key))
        backup = root / 'database/data.pre-v2.db'
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT send_count FROM Pic_of_capoo').fetchone()[0], 12)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='randpic_images'").fetchone())
        await service.close()
        service = await self.open_service(root)
        self.assertEqual(service.repository.list_images()[0].id, image.id)
        self.assertEqual(service.repository.list_images()[0].send_count, 12)
        await service.delete('90001', 100, 11)
        self.assertFalse((root / 'img' / key).exists())

    async def test_pre_counter_schema_and_missing_high_number(self):
        root, key = await self.legacy(count=False, missing=True)
        service = await self.open_service(root)
        self.assertEqual(service.repository.list_images(), [])
        result = await service.add('capoo', picture(2))
        self.assertIn('0000000043', result.image.filename)
        self.assertTrue((await service.delete('90001', 100, 11)).already_deleted)

    async def test_deleted_legacy_quote_does_not_delete_reused_filename(self):
        root, key = await self.legacy(deleted=True)
        service = await self.open_service(root)
        self.assertTrue((await service.delete('90001', 100, 11)).already_deleted)
        self.assertTrue((root / 'img' / key).exists())
        self.assertEqual(len(service.repository.list_images()), 1)

    async def test_broken_config_is_never_overwritten(self):
        await self.service.close()
        path = self.root / 'randpic_commands.json'
        path.write_text('{broken!')
        await self.reopen()
        self.assertEqual(self.service.commands, {})
        result = await self.service.add('新图库', picture())
        self.assertFalse(result.settings_saved)
        self.assertEqual(path.read_text(), '{broken!')
        self.assertIsNotNone(await self.service.pick('新图库'))

    async def test_legacy_command_list_and_disabled_groups_survive_restart(self):
        await self.service.close()
        path = self.root / 'randpic_commands.json'
        path.write_text('["capoo", "马哥"]')
        await self.reopen()
        self.assertEqual(json.loads(path.read_text()), {'capoo': [], '马哥': []})
        self.assertTrue(await self.service.set_disabled('马哥', 100, True))
        self.assertFalse(self.service.is_enabled('马哥', 100))
        self.assertTrue(self.service.is_enabled('马哥', 200))
        await self.reopen()
        self.assertFalse(self.service.is_enabled('马哥', 100))
        await self.service.set_disabled('马哥', 100, False)
        self.assertTrue(self.service.is_enabled('马哥', 100))

    async def test_sql_weight_favors_low_send_counts(self):
        first, second = await self.add_picture(1), await self.add_picture(2)
        with self.service.repository.connection(write=True) as db:
            db.execute('UPDATE randpic_images SET send_count = 9 WHERE id = ?', (second.id,))
        picks = [self.service.repository.choose('capoo').id for _ in range(600)]
        self.assertGreater(picks.count(first.id), 450)
        self.assertGreater(picks.count(second.id), 0)
        self.assertIsNone(self.service.repository.choose('不存在'))

    async def test_removed_command_and_unregistered_directory_stay_disabled(self):
        await self.add_picture()
        await self.service.close()
        (self.root / 'randpic_commands.json').write_text('{}')
        directory = self.root / 'img/未配置'
        directory.mkdir()
        (directory / 'manual.png').write_bytes(picture())
        await self.reopen()
        self.assertEqual(self.service.commands, {})
        self.assertFalse(self.service.is_enabled('capoo', 100))
        self.assertFalse((self.root / 'public/capoo/index.html').exists())
