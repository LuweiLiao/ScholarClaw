import React, { useRef, useEffect, useState, useCallback, memo } from 'react';
import type { ActivityEvent, ActivityType } from '../types';

interface Props {
  activities: ActivityEvent[];
  t: (key: string) => string;
  filterAgentId?: string;
  onCorrect?: (event: ActivityEvent) => void;
}

const LAYER_COLORS: Record<string, string> = {
  idea: '#f59e0b',
  experiment: '#3b82f6',
  coding: '#10b981',
  execution: '#ef4444',
  writing: '#a855f7',
};

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function classForType(t: ActivityType): string {
  switch (t) {
    case 'thinking': return 'flow-thinking';
    case 'tool_call': return 'flow-tool-call';
    case 'tool_result': return 'flow-tool-result';
    case 'file_read': return 'flow-file-read';
    case 'file_write': return 'flow-file-write';
    case 'llm_call': return 'flow-llm-call';
    case 'llm_response': return 'flow-llm-response';
    case 'stage_transition': return 'flow-stage';
    case 'human_feedback': return 'flow-human-feedback';
    case 'metaprompt_update': return 'flow-metaprompt';
    case 'error': return 'flow-error';
    default: return 'flow-generic';
  }
}

function iconForType(t: ActivityType): string {
  switch (t) {
    case 'thinking': return '💭';
    case 'tool_call': return '⚡';
    case 'tool_result': return '📋';
    case 'file_read': return '📖';
    case 'file_write': return '✏️';
    case 'llm_call': return '🤖';
    case 'llm_response': return '📥';
    case 'stage_transition': return '🔄';
    case 'human_feedback': return '🧭';
    case 'metaprompt_update': return '✍️';
    case 'error': return '❌';
    default: return '•';
  }
}

function labelForType(t: ActivityType): string {
  switch (t) {
    case 'thinking': return 'Thinking';
    case 'tool_call': return 'Tool Call';
    case 'tool_result': return 'Result';
    case 'file_read': return 'Read File';
    case 'file_write': return 'Write File';
    case 'llm_call': return 'LLM Call';
    case 'llm_response': return 'LLM Response';
    case 'stage_transition': return 'Stage';
    case 'human_feedback': return 'Human Feedback';
    case 'metaprompt_update': return 'Prompt Update';
    case 'error': return 'Error';
    default: return t;
  }
}

const FlowEvent = memo<{ event: ActivityEvent; expanded: boolean; onToggle: () => void; onCorrect?: (event: ActivityEvent) => void; t: (key: string) => string }>(
  ({ event, expanded, onToggle, onCorrect, t }) => {
    const hasDetail = !!event.detail;
    const cls = classForType(event.activityType);
    const isTerminal = event.activityType === 'tool_call' || event.activityType === 'tool_result';
    const isThinking = event.activityType === 'thinking';

    return (
      <div className={`flow-event ${cls}`} onClick={hasDetail ? onToggle : undefined}>
        <div className="flow-event-sidebar">
          <span className="flow-event-time">{formatTime(event.timestamp)}</span>
          <div className="flow-event-line" style={{ borderColor: LAYER_COLORS[event.layer] || '#555' }} />
        </div>
        <div className="flow-event-content">
          <div className="flow-event-badge">
            <span className="flow-badge-icon">{iconForType(event.activityType)}</span>
            <span className="flow-badge-label">{labelForType(event.activityType)}</span>
            <span className="flow-badge-agent" style={{ color: LAYER_COLORS[event.layer] || '#aaa' }}>
              {event.agentName}
            </span>
            {event.stage && <span className="flow-badge-stage">S{event.stage}</span>}
            {event.nodeId && <span className="flow-badge-node">{event.nodeId}</span>}
            {onCorrect && (
              <button
                type="button"
                className="flow-correct-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  onCorrect(event);
                }}
              >
                {t('supervisor.correct_step')}
              </button>
            )}
          </div>
          {isThinking ? (
            <div className="flow-thinking-bubble">
              <div className="flow-thinking-text">{event.summary}</div>
              {hasDetail && expanded && (
                <pre className="flow-thinking-detail">{event.detail}</pre>
              )}
              {hasDetail && !expanded && (
                <span className="flow-expand-hint">▶ 展开详情</span>
              )}
            </div>
          ) : isTerminal ? (
            <div className="flow-terminal-block">
              <div className="flow-terminal-header">
                <span className="flow-terminal-prompt">$</span>
                <span className="flow-terminal-cmd">{event.summary}</span>
              </div>
              {hasDetail && expanded && (
                <pre className="flow-terminal-output">{event.detail}</pre>
              )}
              {hasDetail && !expanded && (
                <span className="flow-expand-hint">▶ 展开输出</span>
              )}
            </div>
          ) : event.activityType === 'error' ? (
            <div className="flow-error-block">
              <div className="flow-error-text">{event.summary}</div>
              {hasDetail && expanded && (
                <pre className="flow-error-detail">{event.detail}</pre>
              )}
            </div>
          ) : event.activityType === 'stage_transition' ? (
            <div className="flow-stage-block">
              <span className="flow-stage-text">{event.summary}</span>
            </div>
          ) : event.activityType === 'llm_call' || event.activityType === 'llm_response' ? (
            <div className="flow-llm-block">
              <span className="flow-llm-text">{event.summary}</span>
              {hasDetail && expanded && (
                <pre className="flow-llm-detail">{event.detail}</pre>
              )}
              {hasDetail && !expanded && (
                <span className="flow-expand-hint">▶ 展开</span>
              )}
            </div>
          ) : (
            <div className="flow-generic-block">
              <span>{event.summary}</span>
              {hasDetail && expanded && <pre className="flow-generic-detail">{event.detail}</pre>}
            </div>
          )}
        </div>
      </div>
    );
  }
);

const ActivityTimeline: React.FC<Props> = ({ activities, t, filterAgentId, onCorrect }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const filtered = filterAgentId
    ? activities.filter(a => a.agentId === filterAgentId)
    : activities;

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [filtered.length, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 40;
    setAutoScroll(atBottom);
  }, []);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  if (filtered.length === 0) {
    return (
      <div className="timeline-empty">
        <span className="timeline-empty-icon">📋</span>
        <p>{t('timeline.empty')}</p>
      </div>
    );
  }

  return (
    <div className="activity-timeline flow-view">
      <div className="timeline-header">
        <span className="timeline-count">{filtered.length} events</span>
        <label className="timeline-autoscroll">
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={e => setAutoScroll(e.target.checked)}
          />
          {t('timeline.auto_scroll')}
        </label>
      </div>
      <div
        className="flow-list"
        ref={containerRef}
        onScroll={handleScroll}
      >
        {filtered.map(event => (
          <FlowEvent
            key={event.id}
            event={event}
            expanded={expandedIds.has(event.id)}
            onToggle={() => toggleExpand(event.id)}
            onCorrect={onCorrect}
            t={t}
          />
        ))}
      </div>
    </div>
  );
};

export default ActivityTimeline;
