from backend.services.policy_service import travel_policy_service

from ._common import current_user_id, tool


@tool
def query_travel_policy(city: str, user_id: str | None = None) -> dict:
    """查询用户在指定城市的差旅政策，包括舱位、酒店、餐补、交通补贴和审批阈值。"""
    return travel_policy_service.get_policy(current_user_id(user_id), city)


@tool
def check_travel_policy(city: str, order_summary: str, user_id: str | None = None) -> dict:
    """检查机票、火车票或酒店订单是否符合当前用户和城市的差旅政策。"""
    policy = travel_policy_service.get_policy(current_user_id(user_id), city)
    return {"policy": policy, "check": travel_policy_service.check_compliance(order_summary, policy)}


def tools(): return [query_travel_policy, check_travel_policy]
