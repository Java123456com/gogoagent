class ReimbursementAgent:
    """Java build() currently returns null (A2A TODO); preserve that contract."""
    def build(self): return None

    def invoke(self, state):
        request = state.get("request", "") if isinstance(state, dict) else ""
        return {
            "final": "报销能力当前仍为占位实现，尚未接入发票识别与报销单提交流程。",
            "trace": [{"agent": "ReimbursementAgent", "output": request or "placeholder"}],
        }


reimbursement_agent = ReimbursementAgent()
