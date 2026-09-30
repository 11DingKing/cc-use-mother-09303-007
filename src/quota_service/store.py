"""JSON 文件持久化与并发控制。

两道锁共同保证“先校验余额/容量、再写分录”的序列不会交错：

- 进程内 ``threading.RLock``：串行化同进程的并发请求；
- 事务期间对锁文件持有 ``flock`` 排他锁：串行化多进程/多 worker 部署。

落盘采用临时文件 + 原子替换，崩溃不会留下半写状态。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # 跨进程锁（POSIX 部署环境）
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows 等环境退化为进程内锁
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

from .errors import NotFoundError
from .models import Batch


class JsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = threading.RLock()
        self._data: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._data is None:
            self._data = self._read_disk()
        return self._data

    def _read_disk(self) -> dict[str, dict]:
        if self.path.exists() and self.path.stat().st_size > 0:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw.get("batches", {})
        return {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"batches": self._data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, dict]]:
        """持锁取得原始字典，事务体内完成“校验 + 修改”，退出时原子落盘。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.lock_path, "w") as lock_file:
            if _HAS_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                # 持锁后以磁盘为准，丢弃可能被其他进程写旧的缓存
                self._data = self._read_disk()
                data = self._data
                yield data
            except Exception:
                # 强制下次从磁盘重读，避免异常半成品驻留内存
                self._data = None
                raise
            else:
                self._flush()
            finally:
                if _HAS_FCNTL:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def get_batch_raw(self, data: dict[str, dict], batch_id: str) -> dict:
        batch = data.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        return batch

    def get_batch(self, batch_id: str) -> Batch:
        with self._lock, open(self.lock_path, "w") as lock_file:
            if _HAS_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            self._data = self._read_disk()
            raw = self._data.get(batch_id)
            if raw is None:
                raise NotFoundError(f"批次 {batch_id} 不存在")
            return Batch.from_dict(raw)
