import { useAuthStore } from '../store/authStore';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8080';

function authHeaders(): Record<string, string> {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: token } : {};
}

/** 单个偏好项定义 */
export interface PreferenceItem {
  key: string;
  label: string;
  type: 'single' | 'multi';
  options: string[];
}

/** 偏好分类 */
export interface PreferenceCategory {
  category: string;
  label: string;
  icon: string;
  items: PreferenceItem[];
}

/** 用户已保存的偏好值（key -> 单值字符串 或 多值数组） */
export type UserPreferences = Record<string, string | string[]>;

/** 获取可选的差旅偏好选项列表 */
export async function fetchPreferenceOptions(): Promise<PreferenceCategory[]> {
  const res = await fetch(`${API_BASE}/api/preferences/options`, {
    headers: authHeaders(),
  });
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** 获取当前用户已保存的偏好（从长期记忆召回 + LLM 解析） */
export async function fetchUserPreferences(): Promise<{
  preferences?: UserPreferences;
  memorySummary?: string;
}> {
  const res = await fetch(`${API_BASE}/api/preferences`, {
    headers: authHeaders(),
  });
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return {
    preferences: data.preferences || undefined,
    memorySummary: data.memorySummary || undefined,
  };
}

/** 保存用户偏好设置 */
export async function saveUserPreferences(preferences: UserPreferences): Promise<void> {
  const res = await fetch(`${API_BASE}/api/preferences`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(preferences),
  });
  if (res.status === 401) throw new Error('UNAUTHORIZED');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}
