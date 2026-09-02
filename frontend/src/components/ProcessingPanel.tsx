import { useState, useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import ProgressCard, { PlanTaskList } from './ProgressCard';
import TravelResultCard from './TravelResultCard';
import type { PlanProgress, PlanTask, TravelData } from '../types';

type TabKey = 'progress' | 'plan' | 'results';

interface TabDef {
  key: TabKey;
  label: string;
  icon: ReactNode;
  badge?: number;
}

interface Props {
  progress: PlanProgress;
  thinkingByAgent: Record<string, string[]>;
  planTasks: PlanTask[];
  travelData: TravelData[];
}

export default function ProcessingPanel({
  progress,
  thinkingByAgent,
  planTasks,
  travelData,
}: Props) {
  const hasPlan = planTasks.length > 0;
  const hasResults = travelData.length > 0;

  const [activeTab, setActiveTab] = useState<TabKey>('progress');

  // 自动切换到新出现的 tab
  const prevPlanCount = useRef(0);
  const prevResultCount = useRef(0);

  useEffect(() => {
    if (planTasks.length > 0 && prevPlanCount.current === 0) {
      setActiveTab('plan');
    }
    prevPlanCount.current = planTasks.length;
  }, [planTasks.length]);

  useEffect(() => {
    if (travelData.length > 0 && prevResultCount.current === 0) {
      setActiveTab('results');
    }
    prevResultCount.current = travelData.length;
  }, [travelData.length]);

  const tabs: TabDef[] = [
    {
      key: 'progress',
      label: '执行进度',
      icon: (
        <svg viewBox="0 0 16 16" fill="none" width="12" height="12">
          <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
          <path d="M8 5v3l2 1.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      ),
    },
    {
      key: 'plan',
      label: '计划任务',
      icon: (
        <svg viewBox="0 0 16 16" fill="none" width="12" height="12">
          <path d="M3 4h10M3 8h10M3 12h6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      ),
      badge: hasPlan ? planTasks.filter((t) => t.state === 'in_progress' || t.state === 'todo').length : undefined,
    },
    {
      key: 'results',
      label: '搜索结果',
      icon: (
        <svg viewBox="0 0 16 16" fill="none" width="12" height="12">
          <rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M5 7h6M5 10h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
      ),
      badge: hasResults ? travelData.length : undefined,
    },
  ];

  // 过滤掉没有内容的 tabs（plan/results 在无数据时不显示）
  const visibleTabs = tabs.filter((tab) => {
    if (tab.key === 'progress') return true;
    if (tab.key === 'plan') return hasPlan;
    if (tab.key === 'results') return hasResults;
    return true;
  });

  // 如果当前 activeTab 不在 visibleTabs 中，回退到 progress
  const resolvedTab = visibleTabs.find((t) => t.key === activeTab) ? activeTab : 'progress';

  return (
    <div className="processing-panel">
      {/* Tab 栏 */}
      <div className="processing-panel-tabs">
        {visibleTabs.map((tab) => (
          <button
            key={tab.key}
            className={`processing-tab${resolvedTab === tab.key ? ' active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
            type="button"
          >
            {tab.icon}
            <span>{tab.label}</span>
            {tab.badge !== undefined && tab.badge > 0 && (
              <span className="processing-tab-badge">{tab.badge}</span>
            )}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      <div className="processing-panel-content">
        {resolvedTab === 'progress' && (
          <ProgressCard
            progress={progress}
            thinkingByAgent={thinkingByAgent}
            planTasks={planTasks}
          />
        )}
        {resolvedTab === 'plan' && (
          <div className="processing-plan-section">
            <PlanTaskList tasks={planTasks} />
          </div>
        )}
        {resolvedTab === 'results' && (
          <div className="processing-results-section">
            {travelData.map((td, idx) => (
              <TravelResultCard key={`travel-${idx}`} data={td} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
