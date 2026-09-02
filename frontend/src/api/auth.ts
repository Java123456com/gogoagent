const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8080';

export interface LoginResult {
  token: string;
  tokenName: string;
}

/** 登录，返回 Token */
export async function login(username: string, password: string): Promise<LoginResult> {
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json();
  if (!res.ok || data.code === 400 || data.code === 401) {
    throw new Error(data.message || '登录失败');
  }
  return data as LoginResult;
}

/** 退出登录 */
export async function logout(token: string): Promise<void> {
  await fetch(`${API_BASE}/api/auth/logout`, {
    method: 'POST',
    headers: { Authorization: token },
  }).catch(() => {
    // 忽略退出接口网络错误，本地状态照常清除
  });
}

/** 获取当前用户信息（可用于验证 token 有效性） */
export async function fetchUserInfo(token: string): Promise<{ userId: string; admin?: boolean }> {
  const res = await fetch(`${API_BASE}/api/auth/info`, {
    headers: { Authorization: token },
  });
  if (!res.ok) throw new Error('未登录');
  return res.json();
}
