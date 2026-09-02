import { useCallback, useEffect, useState } from 'react';
import {
  fetchPreferenceOptions,
  fetchUserPreferences,
  saveUserPreferences,
  type PreferenceCategory,
  type UserPreferences,
} from '../api/preference';
import { useAuthStore } from '../store/authStore';

export default function PreferencePage() {
  const { clearAuth } = useAuthStore();
  const [categories, setCategories] = useState<PreferenceCategory[]>([]);
  const [preferences, setPreferences] = useState<UserPreferences>({});
  const [memorySummary, setMemorySummary] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [opts, prefData] = await Promise.all([
        fetchPreferenceOptions(),
        fetchUserPreferences(),
      ]);
      setCategories(opts);
      if (prefData.preferences) {
        setPreferences(prefData.preferences);
      }
      setMemorySummary(prefData.memorySummary || null);
    } catch (err: any) {
      if (err?.message === 'UNAUTHORIZED') {
        clearAuth();
        return;
      }
      setError('加载偏好设置失败');
    } finally {
      setLoading(false);
    }
  }, [clearAuth]);

  useEffect(() => {
    load();
  }, [load]);

  const handleSingleSelect = (key: string, value: string) => {
    setPreferences((prev) => ({
      ...prev,
      [key]: prev[key] === value ? '' : value,
    }));
    setSaved(false);
  };

  const handleMultiSelect = (key: string, value: string) => {
    setPreferences((prev) => {
      const current = Array.isArray(prev[key]) ? (prev[key] as string[]) : [];
      const next = current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
      return { ...prev, [key]: next };
    });
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await saveUserPreferences(preferences);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err: any) {
      if (err?.message === 'UNAUTHORIZED') {
        clearAuth();
        return;
      }
      setError('保存失败，请重试');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="admin-page pref-page">
        <div className="admin-empty">加载中...</div>
      </div>
    );
  }

  return (
    <div className="admin-page pref-page">
      <div className="admin-header">
        <div>
          <h1 className="admin-title">差旅偏好设置</h1>
          <p className="admin-subtitle">
            设置您的差旅偏好，智能助手将根据您的偏好推荐最合适的方案
          </p>
        </div>
        <button
          className="pref-save-btn"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? '保存中...' : saved ? '✓ 已保存' : '保存偏好'}
        </button>
      </div>

      {error && <div className="admin-error">{error}</div>}

      {memorySummary && Object.keys(preferences).length === 0 && (
        <div className="pref-memory-hint">
          <div className="pref-memory-hint-icon">💡</div>
          <div className="pref-memory-hint-body">
            <div className="pref-memory-hint-title">智能助手已记录的偏好</div>
            <div className="pref-memory-hint-text">{memorySummary}</div>
            <div className="pref-memory-hint-tip">
              自动解析失败，请在下方手动勾选确认您的偏好
            </div>
          </div>
        </div>
      )}

      <div className="pref-grid">
        {categories.map((cat) => (
          <div key={cat.category} className="pref-category">
            <div className="pref-category-header">
              <span className="pref-category-icon">{cat.icon}</span>
              <span className="pref-category-label">{cat.label}</span>
            </div>
            <div className="pref-items">
              {cat.items.map((item) => (
                <div key={item.key} className="pref-item">
                  <div className="pref-item-label">{item.label}</div>
                  <div className="pref-item-options">
                    {item.options.map((opt) => {
                      const isSelected =
                        item.type === 'single'
                          ? preferences[item.key] === opt
                          : Array.isArray(preferences[item.key]) &&
                            (preferences[item.key] as string[]).includes(opt);
                      return (
                        <button
                          key={opt}
                          className={`pref-option${isSelected ? ' selected' : ''}${item.type === 'multi' ? ' multi' : ''}`}
                          onClick={() =>
                            item.type === 'single'
                              ? handleSingleSelect(item.key, opt)
                              : handleMultiSelect(item.key, opt)
                          }
                        >
                          {item.type === 'multi' && (
                            <span className="pref-option-check">
                              {isSelected ? '✓' : ''}
                            </span>
                          )}
                          {opt}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
