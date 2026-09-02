from ._common import current_user_id, tool
from backend.infrastructure.repositories import user_repository


@tool
def query_user_contact_info(user_id: str | None = None) -> dict:
    """查询当前用户用于预订和报销的姓名、手机号、邮箱、证件信息。"""
    user_id = current_user_id(user_id)
    profile = user_repository.get_profile(user_id)
    if profile is None: return {"found": False}
    return {"found": True, "user_id": profile.user_id, "name": profile.chinese_name, "phone": profile.phone, "email": profile.email, "id_type": profile.id_type, "id_number": profile.id_number}


@tool
def query_user_base_location(user_id: str | None = None) -> dict:
    """查询用户常驻城市。"""
    user_id = current_user_id(user_id)
    profile = user_repository.get_profile(user_id)
    return {"user_id": user_id, "base_city": profile.base_city if profile else None}


@tool
def update_user_contact_info(user_id: str | None = None, chinese_name: str | None = None, phone: str | None = None, email: str | None = None) -> dict:
    """更新用户姓名、手机号或邮箱。"""
    user_id = current_user_id(user_id)
    values = {k: v for k, v in {"chinese_name": chinese_name, "phone": phone, "email": email}.items() if v is not None}
    return user_repository.upsert_profile(user_id, values).as_dict()


@tool
def update_user_base_location(base_city: str, user_id: str | None = None) -> dict:
    """更新用户常驻城市。"""
    return user_repository.upsert_profile(current_user_id(user_id), {"base_city": base_city}).as_dict()


def tools(): return [query_user_contact_info, query_user_base_location, update_user_contact_info, update_user_base_location]
