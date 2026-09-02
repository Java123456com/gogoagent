from ._common import tool


@tool
def ocr_invoice(file_path: str) -> dict:
    """识别发票；真实 OCR/MCP 未配置时返回待处理状态。"""
    return {"status": "PENDING_OCR", "file_path": file_path, "message": "请配置 OCR 服务"}


@tool
def generate_expense_report(invoice_items: str, travel_order_id: str | None = None) -> dict:
    """根据发票条目生成报销单草稿。"""
    return {"status": "DRAFT", "travel_order_id": travel_order_id, "invoice_items": invoice_items}


@tool
def submit_reimbursement(report: str, user_id: str = "u_001") -> dict:
    """提交报销单；保留 Java 当前 A2A 未实现语义。"""
    return {"status": "NOT_IMPLEMENTED", "user_id": user_id, "report": report, "message": "ReimbursementAgent 在 Java 版本中仍为占位能力"}


def tools(): return [ocr_invoice, generate_expense_report, submit_reimbursement]
