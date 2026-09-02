import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AuthState {
  token: string | null;
  userId: string | null;
  username: string | null;
  isAdmin: boolean;

  setAuth: (token: string, userId: string, username: string, isAdmin?: boolean) => void;
  setIsAdmin: (isAdmin: boolean) => void;
  clearAuth: () => void;
  isLoggedIn: () => boolean;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      userId: null,
      username: null,
      isAdmin: false,

      setAuth: (token, userId, username, isAdmin = false) => set({ token, userId, username, isAdmin }),

      setIsAdmin: (isAdmin) => set({ isAdmin }),

      clearAuth: () => set({ token: null, userId: null, username: null, isAdmin: false }),

      isLoggedIn: () => !!get().token,
    }),
    {
      name: 'gogo-auth', // localStorage key
    },
  ),
);
