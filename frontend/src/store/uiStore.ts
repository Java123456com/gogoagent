import { create } from 'zustand';

export type AppView = 'chat' | 'admin' | 'travel' | 'preference';

interface UiState {
  view: AppView;
  setView: (view: AppView) => void;
  /**
   * 跨页面预填的一次性对话内容。
   * <p>「我的差旅」页面点击"发起报销 / 规划行程"等按钮时写入，
   * ChatWindow 检测到非空即自动发送并调用 {@link consumePendingUserMessage} 清空。</p>
   */
  pendingUserMessage: string | null;
  setPendingUserMessage: (msg: string | null) => void;
  consumePendingUserMessage: () => string | null;
}

/** 全局界面视图状态：对话 / 我的差旅 / 后台审批管理 */
export const useUiStore = create<UiState>((set, get) => ({
  view: 'chat',
  setView: (view) => set({ view }),
  pendingUserMessage: null,
  setPendingUserMessage: (msg) => set({ pendingUserMessage: msg }),
  consumePendingUserMessage: () => {
    const msg = get().pendingUserMessage;
    if (msg != null) set({ pendingUserMessage: null });
    return msg;
  },
}));
