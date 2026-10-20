from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.exception import FinishedException

from tests.support import ServiceTest, event, handlers, picture


def matcher(*, result=None):
    return SimpleNamespace(send=AsyncMock(return_value=result or {'message_id': 31}),
                           finish=AsyncMock(side_effect=FinishedException))


class HandlerTests(ServiceTest):
    async def test_admin_permissions_match_add_and_delete(self):
        for role, allowed in [('member', False), ('admin', True), ('owner', True)]:
            self.assertEqual(await handlers.add.permission(self.bot, event(role=role)), allowed)
            self.assertEqual(await handlers.delete_picture.permission(self.bot, event(role=role)), allowed)

    async def test_only_exact_quoted_delete_matches(self):
        self.assertTrue(await handlers._delete_request(event()))
        self.assertFalse(await handlers._delete_request(event('删除', quoted_id=None)))
        self.assertFalse(await handlers._delete_request(event('删除图片')))
        self.assertFalse(await handlers._delete_request(event(Message('删除') + MessageSegment.image('url'))))
        self.assertTrue(await handlers._delete_request(event(MessageSegment.at('90001') + Message(' 删除'))))
        self.assertFalse(await handlers._delete_request(event(MessageSegment.at('123') + Message(' 删除'))))

    async def test_send_success_counts_but_api_failure_does_not(self):
        image = await self.add_picture()
        fake = matcher()
        with patch.object(handlers, 'picture', fake):
            await handlers.send_picture(event('capoo', quoted_id=None))
        self.assertEqual(self.service.repository.list_images()[0].send_count, 1)
        self.assertEqual(self.rows('SELECT message_id FROM randpic_messages'), [(31,)])
        fake.send.side_effect = [OSError('offline'), None]
        with patch.object(handlers, 'picture', fake):
            await handlers.send_picture(event('capoo', quoted_id=None))
        self.assertEqual(self.service.repository.list_images()[0].send_count, 1)
        await self.service.delete('90001', 100, 31)
        self.assertFalse((self.root / 'img' / image.key).exists())

    async def test_add_in_one_message_supports_quote_delete_and_no_send_count(self):
        segment = MessageSegment('image', {'file': 'image-id', 'url': 'https://test/image'})
        request = event(Message('添加 新图库') + segment, quoted_id=None)
        state = {}
        await handlers.prepare_add(request, state)
        self.assertEqual(state['randpic_command'], '新图库')
        self.assertEqual(len(state['pic']['image']), 1)
        fake = matcher()
        with patch.object(handlers, 'add', fake), patch.object(self.service, 'download', AsyncMock(return_value=picture())):
            with self.assertRaises(FinishedException):
                await handlers.add_images(self.bot, request, state, state['pic'])
        image = self.service.repository.list_images()[0]
        self.assertEqual(image.send_count, 0)
        fake = matcher()
        with patch.object(handlers, 'delete_picture', fake):
            with self.assertRaises(FinishedException):
                await handlers.delete_image(event(quoted_id=31))
        self.assertEqual(self.service.repository.list_images(), [])
        self.assertIn('已从', fake.send.await_args.args[0])

    async def test_wrong_sender_or_unknown_quote_is_not_deleted(self):
        image = await self.add_picture()
        await self.record(image)
        for request in (event(sender_id=123), event(quoted_id=1234), event(group_id=200)):
            with patch.object(handlers, 'delete_picture', matcher()):
                with self.assertRaises(FinishedException):
                    await handlers.delete_image(request)
            self.assertTrue((self.root / 'img' / image.key).exists())

    async def test_enable_disable_banned_groups_and_exact_commands(self):
        self.assertTrue(await handlers._known_command(event(' capoo ', quoted_id=None)))
        self.assertFalse(await handlers._known_command(event('capoo please', quoted_id=None)))
        self.assertFalse(await handlers._known_command(event(Message('capoo') + MessageSegment.image('url'))))
        with patch.object(handlers, 'disable', matcher()):
            with self.assertRaises(FinishedException):
                await handlers.disable_command(event(), Message('capoo'))
        self.assertFalse(await handlers._known_command(event('capoo')))
        with patch.object(handlers, 'enable', matcher()):
            with self.assertRaises(FinishedException):
                await handlers.enable_command(event(), Message('capoo'))
        self.assertTrue(await handlers._known_command(event('capoo')))
        self.service.config.randpic_banner_group = [100]
        self.assertFalse(await handlers._known_command(event('capoo')))

    async def test_missing_message_id_still_counts_success(self):
        image = await self.add_picture()
        await self.service.record_sent(image, '90001', 100, {}, count_send=True)
        self.assertEqual(self.service.repository.list_images()[0].send_count, 1)
        self.assertEqual(self.rows('SELECT * FROM randpic_messages'), [])
