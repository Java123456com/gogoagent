import { useAuthStore } from '../store/authStore';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8080';

/** 从 authStore 读取 token，构造带鉴权的请求头 */
function authHeaders(): Record<string, string> {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: token } : {};
}

/** 审批表单（差旅要素），字段可能缺失 */
export interface ApprovalForm {
  orderId?: string;
  userId?: string;
  purpose?: string;
  destination?: string;
  departureCity?: string;
  departureDate?: string;
  returnDate?: string;
  [key: string]: unknown;
}

/** 后台审批单视图 */
export interface AdminApproval {
  processInstanceId: string;
  userId: string;
  userName: string | null;
  title: string;
  status: string;
  statusLabel: string;
  orderId: string | null;
  remark: string | null;
  submitTime: number | null;
  updateTime: number | null;
  form: ApprovalForm | string | null;
}

export type ApprovalStatusFilter = 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED' | '';

/** 查询审批单列表，status 为空则查询全部 */
export async function fetchApprovals(status: ApprovalStatusFilter = ''): Promise<AdminApproval[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : '';
  const res = await fetch(`${API_BASE}/api/admin/approvals${query}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (res.status === 403) throw new Error('FORBIDDEN');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** 对审批单做出决策，decision: agree=通过, refuse=拒绝 */
export async function decideApproval(
  processInstanceId: string,
  decision: 'agree' | 'refuse',
  remark?: string,
): Promise<AdminApproval> {
  const res = await fetch(
    `${API_BASE}/api/admin/approvals/${encodeURIComponent(processInstanceId)}/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ decision, remark }),
    },
  );
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (res.status === 403) throw new Error('FORBIDDEN');
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || `HTTP ${res.status}`);
  }
  return res.json();
}
