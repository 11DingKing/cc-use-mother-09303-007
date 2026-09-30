"""国际培训名额配置服务端。

领域模型见 ``models``，分配规则见 ``allocator``，
额度调整见 ``ledger``，对外用例见 ``service.QuotaService``。
"""
from .service import QuotaService
from .models import (
    Application,
    Dimension,
    GuaranteeRule,
    LedgerEntryKind,
    QuotaError,
    BatchState,
)

__all__ = [
    "QuotaService",
    "Application",
    "Dimension",
    "GuaranteeRule",
    "LedgerEntryKind",
    "BatchState",
    "QuotaError",
]
