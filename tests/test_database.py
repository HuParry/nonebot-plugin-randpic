import json

from tests.support import ServiceTest, picture


class DatabaseTests(ServiceTest):
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

    async def test_command_settings_and_disabled_groups_survive_restart(self):
        await self.service.close()
        path = self.root / 'randpic_commands.json'
        path.write_text('{"capoo": [], "马哥": []}')
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
