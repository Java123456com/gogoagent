import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  fetchMyTravelOrders,
  fetchPlanHtml,
  type MyTravelBooking,
  type MyTravelOrder,
} from '../api/travel';
import { useAuthStore } from '../store/authStore';
import { useChatStore } from '../store/chatStore';
import { useUiStore } from '../store/uiStore';

/** 差旅单状态的颜色映射，复用 admin-status 的样式类 */
const ORDER_STATUS_CLASS: Record<string, string> = {
  DRAFT: 'pending',
  SUBMITTED: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
  COMPLETED: 'approved',
  CANCELLED: 'cancelled',
};

const APPROVAL_STATUS_CLASS: Record<string, string> = {
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
  CANCELLED: 'cancelled',
};

function formatDate(v: string | null): string {
  return v && v.length > 0 ? v : '-';
}

function formatTime(ts: number | null): string {
  if (!ts) return '-';
  return new Date(ts).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDateTime(ts: number | null): string {
  if (!ts) return '-';
  return new Date(ts).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/**
 * 是否允许发起报销：仅当审批已通过 且 出发日期 <= 今天时展示报销按钮。
 * <ul>
 *   <li>审批未通过（PENDING / REJECTED / CANCELLED / 无审批） → 不展示；</li>
 *   <li>出发日期解析失败 或 在未来 → 不展示；</li>
 *   <li>返程日期已在过去 视为「已完成」，仍展示。</li>
 * </ul>
 */
function canReimburse(order: MyTravelOrder): boolean {
  if (order.approvalStatus !== 'APPROVED') return false;
  const dep = order.departureDate;
  if (!dep) return false;
  const depTs = Date.parse(dep);
  if (Number.isNaN(depTs)) return false;
  // 用「今天 00:00」比较，避免时区/时刻造成的边界抖动
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return depTs <= today.getTime();
}

/** 是否允许取消行程：草稿、审批中、已通过状态可取消 */
const CANCELLABLE_STATUSES = new Set(['DRAFT', 'SUBMITTED', 'APPROVED']);
function canCancel(order: MyTravelOrder): boolean {
  return !!order.status && CANCELLABLE_STATUSES.has(order.status);
}

/** 生成快捷动作对应的对话提问，尽量把差旅单上下文塞给智能体 */
function buildPrompts(order: MyTravelOrder) {
  const dep = order.departureCity || '出发地';
  const dest = order.destination || '目的地';
  const dDate = order.departureDate || '';
  const rDate = order.returnDate || '';
  const purpose = order.purpose || '';
  const range = dDate && rDate ? `${dDate} 至 ${rDate}` : dDate || rDate || '出差期间';
  return {
    reimburse:
      `我要为差旅单 ${order.orderId}（${dep} → ${dest}` +
      `${dDate ? `，${dDate}` : ''}${rDate ? `~${rDate}` : ''}` +
      `${purpose ? `，事由：${purpose}` : ''}）发起报销，请帮我处理`,
    cancel:
      `我要取消差旅单 ${order.orderId}（${dep} → ${dest}` +
      `${dDate ? `，${dDate}` : ''}${rDate ? `~${rDate}` : ''}` +
      `${purpose ? `，事由：${purpose}` : ''}），请帮我处理取消`,
    plan:
      `请帮我规划${dDate ? ` ${dDate}` : ''}从 ${dep} 到 ${dest} 的出差行程` +
      `${rDate ? `，${rDate} 返程` : ''}${purpose ? `，出差事由：${purpose}` : ''}，` +
      `包含交通与酒店的方案对比。差旅单号：${order.orderId}`,
    weather: `请查询 ${dest} 在 ${range} 期间的天气情况，给我出差穿搭建议`,
    info: `请查询 ${dest} 最新的差旅出行资讯、注意事项与目的地实用信息`,
    visa: `我计划${dDate ? ` ${dDate}` : ''}前往 ${dest} 出差${
      purpose ? `（${purpose}）` : ''
    }，请介绍目前该目的地对中国公民的签证政策与最新办理流程`,
  };
}

function StatusBadge({
  code,
  label,
  classMap,
}: {
  code: string | null;
  label: string | null;
  classMap: Record<string, string>;
}) {
  if (!code) return null;
  const cls = classMap[code] || 'pending';
  return <span className={`admin-status admin-status-${cls}`}>{label || code}</span>;
}

function OrderCard({
  order,
  active,
  onClick,
}: {
  order: MyTravelOrder;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <div className={`travel-card${active ? ' active' : ''}`} onClick={onClick}>
      <div className="travel-card-head">
        <span className="travel-card-route">
          {order.departureCity || '-'} <span className="travel-card-arrow">→</span>{' '}
          {order.destination || '-'}
        </span>
        {order.international && <span className="travel-card-tag">国际</span>}
      </div>
      <div className="travel-card-dates">
        {formatDate(order.departureDate)} ~ {formatDate(order.returnDate)}
      </div>
      {order.purpose && <div className="travel-card-purpose">事由：{order.purpose}</div>}
      <div className="travel-card-meta">
        <StatusBadge
          code={order.status}
          label={order.statusLabel}
          classMap={ORDER_STATUS_CLASS}
        />
        {order.approvalStatus && (
          <StatusBadge
            code={order.approvalStatus}
            label={`审批：${order.approvalStatusLabel || order.approvalStatus}`}
            classMap={APPROVAL_STATUS_CLASS}
          />
        )}
        {order.bookingCount > 0 && (
          <span className="travel-card-count">预订 {order.bookingCount} 项</span>
        )}
      </div>
      <div className="travel-card-time">创建：{formatTime(order.createdAt)}</div>
    </div>
  );
}

function BookingItem({ b }: { b: MyTravelBooking }) {
  return (
    <div className="booking-item">
      <div className="booking-item-head">
        <span className="booking-item-type">{b.bizTypeLabel || b.bizType || '预订'}</span>
        <span className="booking-item-title">{b.title || b.externalOrderNo || b.bookingId}</span>
        <StatusBadge
          code={b.status}
          label={b.statusLabel}
          classMap={{
            CREATED: 'pending',
            PENDING_PAYMENT: 'pending',
            PAID: 'approved',
            CONFIRMED: 'approved',
            COMPLETED: 'approved',
            CANCELLED: 'cancelled',
            REFUNDED: 'cancelled',
            FAILED: 'rejected',
          }}
        />
      </div>
      <div className="booking-item-meta">
        {b.platform && <span>平台：{b.platform}</span>}
        {b.externalOrderNo && <span>单号：{b.externalOrderNo}</span>}
        {b.totalAmount && (
          <span>
            金额：{b.currency || '¥'} {b.totalAmount}
          </span>
        )}
        {b.startTime && (
          <span>
            起：{formatDateTime(b.startTime)}
            {b.endTime ? ` → ${formatDateTime(b.endTime)}` : ''}
          </span>
        )}
        {b.bookedAt && <span>下单：{formatTime(b.bookedAt)}</span>}
      </div>
    </div>
  );
}

function OrderDetail({
  order,
  onAction,
}: {
  order: MyTravelOrder;
  onAction: (prompt: string) => void;
}) {
  const prompts = useMemo(() => buildPrompts(order), [order]);

  const handleViewPlan = async () => {
    try {
      const html = await fetchPlanHtml(order.orderId);
      const blob = new Blob([html], { type: 'text/html' });
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (err: any) {
      if (err?.message === 'UNAUTHORIZED') {
        alert('登录已过期，请重新登录');
        return;
      }
      alert('查看行程方案失败，可能方案尚未生成');
    }
  };
  const bookingsByType = useMemo(() => {
    const groups: Record<string, MyTravelBooking[]> = {};
    for (const b of order.bookings) {
      const key = b.bizTypeLabel || b.bizType || '其他';
      (groups[key] ||= []).push(b);
    }
    return groups;
  }, [order.bookings]);

  return (
    <div className="travel-detail">
      <div className="travel-detail-head">
        <div>
          <div className="travel-detail-title">
            {order.departureCity || '-'} → {order.destination || '-'}
            {order.international && <span className="travel-card-tag">国际</span>}
          </div>
          <div className="travel-detail-sub">
            差旅单号：{order.orderId}
            <span className="travel-detail-sep">·</span>
            {formatDate(order.departureDate)} ~ {formatDate(order.returnDate)}
          </div>
        </div>
        <div className="travel-detail-status">
          <StatusBadge
            code={order.status}
            label={order.statusLabel}
            classMap={ORDER_STATUS_CLASS}
          />
        </div>
      </div>

      {order.purpose && (
        <div className="travel-detail-block">
          <div className="travel-detail-label">出差事由</div>
          <div className="travel-detail-value">{order.purpose}</div>
        </div>
      )}

      <div className="travel-detail-block">
        <div className="travel-detail-label">审批状态</div>
        {order.approvalId ? (
          <div className="travel-detail-approval">
            <StatusBadge
              code={order.approvalStatus}
              label={order.approvalStatusLabel}
              classMap={APPROVAL_STATUS_CLASS}
            />
            <span className="travel-detail-approval-id">审批单号：{order.approvalId}</span>
            {order.approvalUpdateTime && (
              <span className="travel-detail-approval-time">
                更新：{formatTime(order.approvalUpdateTime)}
              </span>
            )}
            {order.approvalRemark && (
              <div className="travel-detail-approval-remark">备注：{order.approvalRemark}</div>
            )}
          </div>
        ) : (
          <div className="travel-detail-empty">尚未发起审批</div>
        )}
      </div>

      <div className="travel-detail-block">
        <div className="travel-detail-label">预订信息（{order.bookingCount}）</div>
        {order.bookingCount === 0 ? (
          <div className="travel-detail-empty">暂无预订记录</div>
        ) : (
          <div className="booking-groups">
            {Object.entries(bookingsByType).map(([type, list]) => (
              <div className="booking-group" key={type}>
                <div className="booking-group-title">
                  {type}
                  <span className="booking-group-count">{list.length}</span>
                </div>
                <div className="booking-group-list">
                  {list.map((b) => (
                    <BookingItem key={b.bookingId} b={b} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="travel-actions">
        {canReimburse(order) && (
          <button className="travel-action-btn primary" onClick={() => onAction(prompts.reimburse)}>
            💰 发起报销
          </button>
        )}
        {canCancel(order) && (
          <button className="travel-action-btn danger" onClick={() => onAction(prompts.cancel)}>
            ✕ 取消行程
          </button>
        )}
        {order.bookingCount === 0 && order.status !== 'CANCELLED' && (
          <button className="travel-action-btn" onClick={() => onAction(prompts.plan)}>
            🗺 规划行程
          </button>
        )}
        {order.planHtmlUrl && (
          <button className="travel-action-btn" onClick={handleViewPlan}>
            📋 查看行程
          </button>
        )}
        <button className="travel-action-btn" onClick={() => onAction(prompts.weather)}>
          ☁ 目的地天气
        </button>
        <button className="travel-action-btn" onClick={() => onAction(prompts.info)}>
          📰 目的地资讯
        </button>
        {order.international && (
          <button className="travel-action-btn" onClick={() => onAction(prompts.visa)}>
            🛂 签证政策
          </button>
        )}
      </div>
    </div>
  );
}

export default function MyTravelPage() {
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const setView = useUiStore((s) => s.setView);
  const setPendingUserMessage = useUiStore((s) => s.setPendingUserMessage);
  const createConversation = useChatStore((s) => s.createConversation);

  const [orders, setOrders] = useState<MyTravelOrder[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await fetchMyTravelOrders();
      setOrders(data);
      setSelectedId((prev) => {
        if (prev && data.some((o) => o.orderId === prev)) return prev;
        return data.length > 0 ? data[0].orderId : null;
      });
    } catch (err: any) {
      if (err?.message === 'UNAUTHORIZED') {
        clearAuth();
        return;
      }
      setError('加载出差申请失败');
      setOrders([]);
      setSelectedId(null);
    } finally {
      setLoading(false);
    }
  }, [clearAuth]);

  useEffect(() => {
    load();
  }, [load]);

  const selected = selectedId ? orders.find((o) => o.orderId === selectedId) : undefined;

  /** 快捷动作：新建对话 → 切到对话页 → 预填 message，由 ChatWindow 自动发送 */
  const handleAction = (prompt: string) => {
    createConversation();
    setPendingUserMessage(prompt);
    setView('chat');
  };

  return (
    <main className="admin-page travel-page">
      <div className="admin-header">
        <div>
          <h1 className="admin-title">我的差旅</h1>
          <p className="admin-subtitle">查看出差申请、审批状态与预订信息，快捷发起对应对话</p>
        </div>
        <button className="admin-refresh" onClick={load} disabled={loading}>
          刷新
        </button>
      </div>

      {error && <div className="admin-error">{error}</div>}

      {loading ? (
        <div className="admin-empty">加载中...</div>
      ) : orders.length === 0 ? (
        <div className="admin-empty">您还没有出差申请，去对话页发起一个吧</div>
      ) : (
        <div className="travel-layout">
          <div className="travel-list">
            {orders.map((o) => (
              <OrderCard
                key={o.orderId}
                order={o}
                active={selectedId === o.orderId}
                onClick={() => setSelectedId(o.orderId)}
              />
            ))}
          </div>
          <div className="travel-main">
            {selected ? (
              <OrderDetail order={selected} onAction={handleAction} />
            ) : (
              <div className="admin-empty">请选择左侧的出差申请查看详情</div>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
