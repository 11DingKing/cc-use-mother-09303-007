"""领域错误类型。

所有业务规则违例都抛出 :class:`QuotaError` 的子类，服务层与 HTTP 层据此映射状态码。
"""
from __future__ import annotations


class QuotaError(Exception):
    """业务错误基类。"""

    code = "quota_error"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self)}


class ValidationError(QuotaError):
    """输入不满足领域约束。"""

    code = "invalid_input"
    status = 400


class NotFoundError(QuotaError):
    """批次、院校或资源不存在。"""

    code = "not_found"
    status = 404


class ConflictError(QuotaError):
    """状态冲突或并发冲突（含超发拦截）。"""

    code = "conflict"
    status = 409


class FrozenError(QuotaError):
    """方案已发布，输入冻结。"""

    code = "input_frozen"
    status = 409
