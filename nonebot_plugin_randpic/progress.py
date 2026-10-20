"""终端单行进度，与 NoneBot 默认控制台日志共用 Rich Console。"""

import sys
import threading
import time
from typing import Optional

from nonebot import log as nonebot_log
from rich.console import Console
from rich.live import Live
from rich.text import Text


class SyncProgress:
    # 同时维护多个图库时也只显示一个动态行，不等待其他同步结束。
    _display_lock = threading.Lock()

    def __init__(self, total: int):
        self.total = total
        self.stream = sys.stdout
        self.console: Optional[Console] = None
        self.live: Optional[Live] = None
        self._handler_id: Optional[int] = None
        self._owns_display = False
        self._last_refresh = 0.0

    def _text(self, completed: int, succeeded: int, failed: int) -> Text:
        percent = completed * 100 / self.total if self.total else 100
        return Text(
            f'OSS 同步 {completed}/{self.total} ({percent:.1f}%) '
            f'成功 {succeeded}，失败 {failed}',
            no_wrap=True, overflow='ellipsis',
        )

    def _write_log(self, message) -> None:
        # 保留 NoneBot 格式和 ANSI 颜色；日志由 Live 自动排到进度行上方。
        self.console.print(Text.from_ansi(str(message).rstrip('\n')), soft_wrap=True)

    def __enter__(self):
        self.console = Console(file=self.stream)
        if not self.console.is_terminal or self.console.is_dumb_terminal or self.console.is_jupyter:
            return self
        if not self._display_lock.acquire(blocking=False):
            return self
        self._owns_display = True
        try:
            # 只临时接管 NoneBot 自带的控制台输出，文件和其他自定义 sink 保持原样。
            # 若默认 sink 已被用户移除，则尊重其日志配置，只输出最终汇总。
            try:
                nonebot_log.logger.remove(nonebot_log.logger_id)
            except ValueError:
                self._release_display()
                return self
            try:
                self._handler_id = nonebot_log.logger.add(
                    self._write_log, level=0, diagnose=False,
                    filter=nonebot_log.default_filter, format=nonebot_log.default_format,
                    colorize=True,
                )
            except Exception:
                nonebot_log.logger_id = self._add_default_handler()
                raise
            nonebot_log.logger_id = self._handler_id
            self.live = Live(
                self._text(0, 0, 0), console=self.console, transient=True,
                auto_refresh=False, redirect_stdout=True, redirect_stderr=True,
            )
            self.live.start(refresh=True)
            self._last_refresh = time.monotonic()
        except Exception:
            # 显示不可用不能中断上传；退出时同时恢复日志和标准输出。
            self.__exit__(None, None, None)
        return self

    def update(self, completed: int, succeeded: int, failed: int) -> None:
        if self.live is None:
            return
        now = time.monotonic()
        if completed != self.total and now - self._last_refresh < 0.2:
            return
        try:
            self.live.update(self._text(completed, succeeded, failed), refresh=True)
            self._last_refresh = now
        except Exception:
            self.__exit__(None, None, None)

    def _add_default_handler(self) -> int:
        return nonebot_log.logger.add(
            self.stream, level=0, diagnose=False,
            filter=nonebot_log.default_filter, format=nonebot_log.default_format,
        )

    def _release_display(self) -> None:
        if self._owns_display:
            self._owns_display = False
            self._display_lock.release()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.live is not None:
                try:
                    self.live.stop()
                except Exception:
                    pass  # 终端关闭等显示错误不应把成功的上传变成失败。
                finally:
                    self.live = None
        finally:
            try:
                if self._handler_id is not None:
                    handler_id, self._handler_id = self._handler_id, None
                    try:
                        nonebot_log.logger.remove(handler_id)
                    except ValueError:
                        pass  # 同步期间用户重新配置了日志，不重新添加默认控制台。
                    else:
                        nonebot_log.logger_id = self._add_default_handler()
            finally:
                self._release_display()
