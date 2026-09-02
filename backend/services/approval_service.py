"""差旅审批状态流转服务。"""
from __future__ import annotations

from backend.infrastructure.repositories import approval_repository


class ApprovalService:
    def list(self, status: str | None = None):
        return [a.as_dict() for a in approval_repository.list_all(status)]

    def get(self, approval_id: str):
        record = approval_repository.get(approval_id)
        return record.as_dict() if record else None


approval_service = ApprovalService()
