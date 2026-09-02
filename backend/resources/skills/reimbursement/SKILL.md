---
name: reimbursement
description: 发票识别与报销单生成
---

# 报销 Skill

## 工具
- ocr_invoice：识别发票图片，返回发票金额、类型、日期、发票代码等信息
- generate_expense_report：根据识别结果和历史行程生成报销单
- submit_reimbursement：提交报销审批

## 流程
1. 引导用户上传发票图片。
2. ocr_invoice 识别。
3. 展示结果供确认。
4. generate_expense_report 生成报销单。
5. submit_reimbursement 提交审批。
