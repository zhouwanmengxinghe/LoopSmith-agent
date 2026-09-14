from __future__ import annotations

import asyncio

from loopsmith.core.app import _install_signal_handlers


# 功能：验证 Windows 不支持 asyncio signal handler 时 daemon 不再因 NotImplementedError 崩溃
# 设计：用最小假事件循环稳定复现 Windows 行为，不依赖当前测试平台
def test_signal_handler_fallback_on_unsupported_platform() -> None:
    class UnsupportedLoop:
        def add_signal_handler(self, *args: object) -> None:
            raise NotImplementedError

    installed = _install_signal_handlers(
        UnsupportedLoop(),  # type: ignore[arg-type]
        asyncio.Event(),
    )

    assert installed is False
