"""NoneBot 命令适配层；图片和存储操作全部交给 RandpicService。"""

import time
from typing import Optional
from urllib.parse import quote

from nonebot.adapters.onebot.v11 import Bot, GROUP, GROUP_ADMIN, GROUP_OWNER, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import Arg, CommandArg
from nonebot.plugin import on_command, on_fullmatch, on_message, on_regex
from nonebot.rule import Rule, to_me
from nonebot.typing import T_State

from .models import MutationResult, RandpicError
from .service import RandpicService
from .utils import valid_command


_service: RandpicService


def setup(service: RandpicService) -> None:
    global _service
    _service = service


async def _known_command(event: GroupMessageEvent) -> bool:
    return all(segment.type == 'text' for segment in event.get_message()) and _service.is_enabled(
        event.get_plaintext().strip(), event.group_id
    )


def _reply_id(event: GroupMessageEvent) -> Optional[int]:
    if event.reply is not None:
        return event.reply.message_id
    for segment in event.original_message:
        if segment.type == 'reply':
            try:
                return int(segment.data['id'])
            except (KeyError, TypeError, ValueError):
                return None
    return None


async def _delete_request(event: GroupMessageEvent) -> bool:
    return _reply_id(event) is not None and event.get_plaintext().strip() == '删除' and all(
        segment.type in {'text', 'reply'}
        or (segment.type == 'at' and str(segment.data.get('qq')) == str(event.self_id))
        for segment in event.get_message()
    )


picture = on_message(rule=Rule(_known_command), permission=GROUP, priority=3, block=True)
add = on_regex(r'^添加(.+)', permission=GROUP_ADMIN | GROUP_OWNER, priority=2, block=True)
delete_picture = on_message(rule=Rule(_delete_request), permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True)
OSS = on_fullmatch('上传oss', ignorecase=True, permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True)
disable = on_command('禁用', rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True)
enable = on_command('启用', rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER, priority=1, block=True)


async def _sync_warning(matcher) -> None:
    try:
        result = await _service.sync()
        if result.web_error:
            await matcher.send(result.web_error + '，也可发送“上传oss”手动重试。')
        elif result.failed:
            await matcher.send('图库操作已完成，但部分 OSS 同步失败，将自动重试，也可发送“上传oss”重试。')
    except Exception as exc:
        logger.exception(f'图库同步失败：{exc}')
        await matcher.send('图库操作已完成，但网页或 OSS 同步暂未完成，将自动重试。')


def _result_notes(result: MutationResult) -> str:
    notes = ''
    if not result.settings_saved:
        notes += '\n指令配置写入失败，本次新增指令仅在当前运行中有效，请检查 JSON 文件和目录权限。'
    if result.web_error:
        notes += '\n' + result.web_error + '。'
    if _service.config.oss_enabled:
        notes += '\nOSS 正在自动同步。'
    return notes


@picture.handle()
async def send_picture(event: GroupMessageEvent) -> None:
    try:
        snapshot = await _service.pick(event.get_plaintext().strip())
    except RandpicError as exc:
        await picture.send(str(exc))
        return
    except Exception as exc:
        logger.exception(f'读取随机图片失败：{exc}')
        await picture.send('暂时无法读取图片，请稍后再试。')
        return
    if snapshot is None:
        await picture.send('当前还没有图片！')
        return
    try:
        result = await picture.send(MessageSegment.image(snapshot.data))
    except Exception as exc:
        logger.warning(f'发送图片失败：{exc}')
        await picture.send('图片发送失败，请稍后再试。')
        return
    await _service.record_sent(snapshot.image, str(event.self_id), event.group_id, result, count_send=True)


@add.handle()
async def prepare_add(event: GroupMessageEvent, state: T_State) -> None:
    command = event.get_plaintext().strip()[len('添加'):].strip()
    if not valid_command(command):
        await add.finish('名称仅支持字母、汉字、数字，且长度不能超过 120 字节。')
    if not _service.ready:
        await add.finish('图库尚未就绪，请稍后重试。')
    state['randpic_command'] = command
    if event.get_message()['image']:
        state['pic'] = event.get_message()


@add.got('pic', prompt='请发送图片，可一次发送多张。')
async def add_images(bot: Bot, event: GroupMessageEvent, state: T_State, pic_list: Message = Arg('pic')) -> None:
    images = pic_list['image']
    if not images:
        await add.finish('没有收到图片，请重新发送“添加<指令>”后上传图片。')
    command = state['randpic_command']
    changed = False
    for number, segment in enumerate(images, 1):
        try:
            url = segment.data.get('url') or segment.data.get('file', '')
            if not str(url).startswith(('http://', 'https://')):
                try:
                    info = await bot.get_image(file=segment.data.get('file', ''))
                    url = info.get('url', '')
                except Exception:
                    url = ''
            data = await _service.download(str(url))
            result = await _service.add(command, data)
            changed = True
        except RandpicError as exc:
            await add.send(f'第 {number} 张图片：{exc}', at_sender=True)
            continue
        except Exception as exc:
            logger.exception(f'添加图片失败：{exc}')
            await add.send(f'第 {number} 张图片保存失败，请稍后重试。', at_sender=True)
            continue
        message = f'\n已保存到「{command}」图库。' + _result_notes(result)
        endpoint = _service.config.randpic_endpoint
        if endpoint and _service.config.oss_enabled and command not in _service.config.randpic_oss_no_upload_list:
            message += f'\n{endpoint.rstrip("/")}/{quote(command, safe="")}/'
        try:
            sent = await add.send(segment + Message(message), at_sender=True)
            await _service.record_sent(result.image, str(event.self_id), event.group_id, sent, count_send=False)
        except Exception as exc:
            logger.warning(f'图片已入库，但发送添加回执失败：{exc}')
            await add.send(f'第 {number} 张图片已保存，但图片回执发送失败。', at_sender=True)
    if changed:
        await _sync_warning(add)
    await add.finish()


@delete_picture.handle()
async def delete_image(event: GroupMessageEvent) -> None:
    if event.reply is not None and event.reply.sender.user_id is not None and str(event.reply.sender.user_id) != str(event.self_id):
        await delete_picture.finish('请引用本 Bot 发送的图库图片，再发送“删除”。', at_sender=True)
    try:
        result = await _service.delete(str(event.self_id), event.group_id, _reply_id(event))
    except RandpicError as exc:
        await delete_picture.finish(str(exc), at_sender=True)
    except Exception as exc:
        logger.exception(f'删除图片失败：{exc}')
        await delete_picture.finish('删除失败，已保留原图，请稍后重试或查看日志。', at_sender=True)
    message = '这张图片已经从图库删除。' if result.already_deleted else f'已从「{result.image.command}」图库删除这张图片。'
    await delete_picture.send(message + _result_notes(result), at_sender=True)
    await _sync_warning(delete_picture)
    await delete_picture.finish()


@OSS.handle()
async def upload_oss() -> None:
    if not _service.ready:
        await OSS.finish('图库尚未就绪，请稍后重试。')
    await OSS.send('正在刷新网页并同步至 OSS…' if _service.config.oss_enabled else '正在刷新本地网页…')
    start = time.perf_counter()
    try:
        result = await _service.sync(force=True)
    except Exception as exc:
        logger.exception(f'手动同步图库失败：{exc}')
        await OSS.finish('同步失败，将自动重试，请查看 Bot 日志。')
    if result.web_error or result.failed or result.pending:
        await OSS.finish(f'本次同步成功 {result.succeeded} 项，失败 {result.failed} 项，未完成 {result.pending} 项；将自动重试。' + (result.web_error or ''))
    await OSS.finish(f'同步完成，用时 {time.perf_counter() - start:.2f} 秒。')


async def _set_disabled(event: GroupMessageEvent, args: Message, value: bool, matcher) -> None:
    command = args.extract_plain_text().strip()
    if command not in _service.commands:
        await matcher.finish('请指定存在的指令名，例如：@bot 禁用 capoo')
    if (event.group_id in _service.commands[command]) == value:
        await matcher.finish(f'指令「{command}」在本群已经是{"禁用" if value else "启用"}状态。')
    try:
        saved = await _service.set_disabled(command, event.group_id, value)
    except RandpicError as exc:
        await matcher.finish(str(exc))
    message = f'已在本群{"禁用" if value else "启用"}指令「{command}」。'
    if not saved:
        message += '\n配置写入失败，此次修改仅在当前运行中有效，请检查 JSON 文件和目录权限。'
    await matcher.finish(message)


@disable.handle()
async def disable_command(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    await _set_disabled(event, args, True, disable)


@enable.handle()
async def enable_command(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    await _set_disabled(event, args, False, enable)
