import type { CoordinationSessionInfo } from '../types';

interface Props {
  sessions: CoordinationSessionInfo[];
  t: (k: string) => string;
}

const LAYER_NUM: Record<string, string> = {
  idea: 'L1', experiment: 'L2', coding: 'L3', execution: 'L4', writing: 'L5',
};

export default function CoordinationPanel({ sessions, t }: Props) {
  if (!sessions || sessions.length === 0) return null;

  return (
    <div className="coordination-panel">
      <h4 className="coordination-title">{t('coordination.title')}</h4>
      {sessions.map(s => (
        <div key={`${s.projectId}:${s.layer}`} className="coord-session">
          <div className="coord-session-header">
            <span className="coord-layer-badge">{LAYER_NUM[s.layer] ?? s.layer}</span>
            <span className={`coord-phase coord-phase-${s.phase}`}>
              {t(`coordination.${s.phase}`)}
            </span>
            <span className="coord-agents">{s.agentIds.length} agents</span>
          </div>

          {s.messages.length > 0 && (
            <details className="coord-messages">
              <summary>
                {s.phase === 'reviewing' || s.phase === 'done'
                  ? t('coordination.review')
                  : t('coordination.plan')}
                {' '}({s.messages.length})
              </summary>
              <div className="coord-msg-list">
                {s.messages.map((m, i) => (
                  <div key={i} className={`coord-msg coord-msg-${m.phase}`}>
                    <span className="coord-msg-name">{m.agentName}</span>
                    <span className="coord-msg-text">{m.content}</span>
                  </div>
                ))}
              </div>
            </details>
          )}

          {s.coordinationPlan && (
            <details className="coord-plan-detail">
              <summary>{t('coordination.plan')}</summary>
              <pre className="coord-plan-text">{s.coordinationPlan}</pre>
            </details>
          )}

          {s.reviewSummary && (
            <details className="coord-review-detail">
              <summary>{t('coordination.review')}</summary>
              <pre className="coord-review-text">{s.reviewSummary}</pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
