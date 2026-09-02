"""基于 SQLAlchemy 的数据库引擎、会话和开发种子数据。

默认使用 SQLite 零依赖运行；设置 ``GOGO_DATABASE_URL`` 可切换 MySQL/Postgres。
首次启动自动建表并写入演示数据（等价于 schema.sql 的 INSERT 种子）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_settings
from backend.domain.models import (
    ApprovalRecord,
    Base,
    BookingRecord,
    TravelOrder,
    TravelPolicyRule,
    UserAccount,
    UserProfile,
)

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        connect_args = ({"check_same_thread": False, "timeout": 30}
                        if url.startswith("sqlite") else {})
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _configure_sqlite(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                finally:
                    cursor.close()
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_session() -> Session:
    get_engine()
    return _SessionLocal()


def init_db() -> None:
    """建表 + 写入种子数据（幂等）。"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    seed(engine)


def seed(engine) -> None:
    with get_session() as s:
        if s.execute(select(UserAccount.id).limit(1)).first():
            return  # 已初始化
        _seed_users(s)
        _seed_policy(s)
        _seed_orders(s)
        s.commit()


def _seed_users(s: Session) -> None:
    profiles = [
        ("u_001", "北京", "P7", "ZHANG SAN", "zhangsan@example.com", "张三", 0, "13800000001", "M"),
        ("u001", "上海", "P7", "LI SI", "lisi@example.com", "李四", 0, "13800000002", "M"),
        ("u002", "北京", "P6", "WANG WU", "wangwu@example.com", "王五", 0, "13800000003", "M"),
        ("u003", "成都", "P5", "ZHAO LIU", "zhaoliu@example.com", "赵六", 0, "13800000004", "F"),
        ("u004", "深圳", "P8", "CHEN QI", "chenqi@example.com", "陈七", 0, "13800000005", "M"),
    ]
    for user_id, base_city, level, pinyin, email, name, id_type, phone, gender in profiles:
        s.add(UserProfile(user_id=user_id, base_city=base_city, level=level,
                          name_pinyin=pinyin, email=email, chinese_name=name,
                          id_type=id_type, phone=phone, gender=gender))

    accounts = [
        ("u_001", "admin", "123456", "系统管理员", "ADMIN"),
        ("u001", "alice", "123456", "张三", "USER"),
        ("u002", "bob", "123456", "李四", "USER"),
        ("u003", "charlie", "123456", "王五", "USER"),
        ("u004", "david", "123456", "赵六", "USER"),
    ]
    for user_id, username, password, real_name, role in accounts:
        s.add(UserAccount(user_id=user_id, username=username, password=password,
                          real_name=real_name, role=role, created_time=datetime.now()))


def _seed_policy(s: Session) -> None:
    # 4 职级区间 × 3 城市等级 = 12 条
    rows = [
        (8, 99, "一线", "商务舱", "一等座", 700, 5, 200, 300, 8000, 3),
        (8, 99, "新一线", "商务舱", "一等座", 550, 5, 200, 300, 8000, 3),
        (8, 99, "其他", "商务舱", "一等座", 450, 5, 200, 300, 8000, 3),
        (7, 7, "一线", "经济舱/商务舱", "一等座", 600, 5, 200, 300, 8000, 3),
        (7, 7, "新一线", "经济舱/商务舱", "一等座", 500, 5, 200, 300, 8000, 3),
        (7, 7, "其他", "经济舱/商务舱", "一等座", 400, 5, 200, 300, 8000, 3),
        (6, 6, "一线", "经济舱", "一等座", 500, 4, 150, 200, 5000, 3),
        (6, 6, "新一线", "经济舱", "一等座", 400, 4, 150, 200, 5000, 3),
        (6, 6, "其他", "经济舱", "一等座", 350, 4, 150, 200, 5000, 3),
        (1, 5, "一线", "经济舱", "二等座", 400, 4, 150, 200, 5000, 3),
        (1, 5, "新一线", "经济舱", "二等座", 350, 4, 150, 200, 5000, 3),
        (1, 5, "其他", "经济舱", "二等座", 300, 4, 150, 200, 5000, 3),
    ]
    for (lmin, lmax, tier, fc, ts, hl, hsl, dml, dtl, at, abd) in rows:
        s.add(TravelPolicyRule(level_min=lmin, level_max=lmax, city_tier=tier,
                               flight_class=fc, train_seat_class=ts, hotel_limit=hl,
                               hotel_star_limit=hsl, daily_meal_limit=dml,
                               daily_transport_limit=dtl, approval_threshold=at,
                               advance_booking_days=abd))


def _seed_orders(s: Session) -> None:
    now = datetime.now()
    s.add(TravelOrder(order_id="to_20260701_001", user_id="u001", destination="上海",
                      departure_city="北京", departure_date="2026-07-10",
                      return_date="2026-07-12", purpose="客户拜访", status="APPROVED",
                      approval_id="ap_20260701_001", created_at=now, updated_at=now))
    s.add(TravelOrder(order_id="to_20260710_002", user_id="u001", destination="深圳",
                      departure_city="北京", departure_date="2026-07-20",
                      return_date="2026-07-22", purpose="产品交流会", status="SUBMITTED",
                      approval_id="ap_20260710_002", created_at=now, updated_at=now))
    s.add(TravelOrder(order_id="to_20260715_003", user_id="u002", destination="成都",
                      departure_city="上海", departure_date="2026-08-01",
                      return_date="2026-08-03", purpose="业务培训", status="DRAFT",
                      created_at=now, updated_at=now))

    s.add(ApprovalRecord(process_instance_id="ap_20260701_001", user_id="u001",
                         title="张三-北京→上海-2026/07/10~07/12", status="APPROVED",
                         approval_form='{"destination":"上海","budget":3000}',
                         remark="预算合理，同意", submit_time=now, update_time=now,
                         order_id="to_20260701_001"))
    s.add(ApprovalRecord(process_instance_id="ap_20260710_002", user_id="u001",
                         title="张三-北京→深圳-2026/07/20~07/22", status="PENDING",
                         approval_form='{"destination":"深圳","budget":4500}',
                         submit_time=now, update_time=now, order_id="to_20260710_002"))

    s.add(BookingRecord(booking_id="bk_20260702_001", user_id="u001",
                        travel_order_id="to_20260701_001", biz_type="FLIGHT",
                        platform="tuniu", external_order_no="TN2026070200001",
                        status="CONFIRMED", payment_status="PAID",
                        title="北京→上海 MU5101 07/10 09:00", total_amount=1280.00,
                        currency="CNY", contact_name="张三", contact_phone="13800000002",
                        start_time=datetime(2026, 7, 10, 9, 0),
                        end_time=datetime(2026, 7, 10, 11, 20), booked_at=now))
    s.add(BookingRecord(booking_id="bk_20260702_002", user_id="u001",
                        travel_order_id="to_20260701_001", biz_type="HOTEL",
                        platform="rolling-go-hotel", external_order_no="RGH20260702HTL88",
                        status="CONFIRMED", payment_status="PAID",
                        title="上海静安希尔顿酒店-高级大床房", total_amount=1580.00,
                        currency="CNY", contact_name="张三", contact_phone="13800000002",
                        start_time=datetime(2026, 7, 10, 15, 0),
                        end_time=datetime(2026, 7, 12, 12, 0), booked_at=now))
