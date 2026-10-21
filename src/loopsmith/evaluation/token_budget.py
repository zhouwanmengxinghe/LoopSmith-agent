from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class TokenBudget:
    # 串行评测共用账本，保留未返回 usage 请求的预留量，防止失败消耗被当成零
    def __init__(self, path: Path, limit: int = 10_000_000) -> None:
        self.path, self.limit = path, limit
        path.parent.mkdir(parents=True, exist_ok=True)

    # 在独占锁下预留或结算；拒绝并发账本写入而不冒险超预算
    def _update(self, operation: str, amount: int, reservation: int = 0) -> None:
        lock = self.path.with_suffix(".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "limit": self.limit,
                    "reported_tokens": 0,
                    "reserved_tokens": 0,
                    "calls_reserved": 0,
                    "calls_with_usage": 0,
                }
            )
            if state["limit"] != self.limit:
                raise ValueError("Budget limit differs from the existing ledger")
            if operation == "reserve":
                if state["reported_tokens"] + state["reserved_tokens"] + amount > self.limit:
                    raise RuntimeError("API token budget exhausted; request not sent")
                state["reserved_tokens"] += amount
                state["calls_reserved"] += 1
            else:
                state["reserved_tokens"] -= reservation
                state["reported_tokens"] += amount
                state["calls_with_usage"] += 1
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            os.replace(temporary, self.path)
        finally:
            os.close(descriptor)
            lock.unlink()

    # 以完整请求的 UTF-8 字节数和额外协议余量保守预留，不调用额外的 tokenizer API
    def reserve(self, request: dict[str, Any], max_output: int) -> int:
        payload = {key: request.get(key) for key in ("messages", "tool_schemas", "system")}
        amount = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + max_output + 8192
        self._update("reserve", amount)
        return amount

    # 记录供应商报告的完整输入（含缓存）和输出，未返回 usage 的请求不释放预留
    def settle(self, reservation: int, tokens: int) -> None:
        self._update("settle", tokens, reservation)
