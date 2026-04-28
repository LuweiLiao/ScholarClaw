import React, { useRef, useEffect, useState, useCallback, useMemo, memo } from 'react';
import type { ActivityEvent } from '../types';

interface Props {
  activities: ActivityEvent[];
  t: (key: string) => string;
  filterAgentId?: string;
  agentNames?: Record<string, string>;
}

const LAYER_COLORS: Record<string, string> = {
  idea: '#f59e0b',
  experiment: '#3b82f6',
  coding: '#10b981',
  execution: '#ef4444',
  writing: '#a855f7',
};

interface ConversationTurn {
  id: string;
  turnNumber: number;
  agentId: string;
  agentName: string;
  layer: string;
  startTime: number;
  endTime: number;
  llmRequest?: ActivityEvent;
  llmCall?: ActivityEvent;
  llmResponse?: ActivityEvent;
  thinking: ActivityEvent[];
  toolCalls: ToolCallPair[];
  errors: ActivityEvent[];
  stageTransitions: ActivityEvent[];
  fileOps: ActivityEvent[];
  userMessages: ActivityEvent[];
}

interface ToolCallPair {
  call: ActivityEvent;
  result?: ActivityEvent;
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

function groupIntoTurns(events: ActivityEvent[]): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  let current: ConversationTurn | null = null;
  let turnNum = 0;

  for (const evt of events) {
    // llm_call after llm_request belongs to the same turn (legacy stats event).
    if (
      evt.activityType === 'llm_call' &&
      current &&
      current.llmRequest &&
      !current.llmCall &&
      current.agentId === evt.agentId
    ) {
      current.llmCall = evt;
      current.endTime = evt.timestamp;
      continue;
    }

    if (
      evt.activityType === 'llm_request' ||
      evt.activityType === 'llm_call' ||
      evt.activityType === 'stage_transition'
    ) {
      if (
        current &&
        (current.llmRequest ||
          current.llmCall ||
          current.toolCalls.length > 0 ||
          current.thinking.length > 0)
      ) {
        current.endTime = evt.timestamp;
        turns.push(current);
      }
      turnNum++;
      current = {
        id: `turn-${turnNum}-${evt.id}`,
        turnNumber: turnNum,
        agentId: evt.agentId,
        agentName: evt.agentName,
        layer: evt.layer,
        startTime: evt.timestamp,
        endTime: evt.timestamp,
        thinking: [],
        toolCalls: [],
        errors: [],
        stageTransitions: [],
        fileOps: [],
        userMessages: [],
      };
      if (evt.activityType === 'llm_request') {
        current.llmRequest = evt;
      } else if (evt.activityType === 'llm_call') {
        current.llmCall = evt;
      } else {
        current.stageTransitions.push(evt);
      }
      continue;
    }

    if (!current) {
      turnNum++;
      current = {
        id: `turn-${turnNum}-${evt.id}`,
        turnNumber: turnNum,
        agentId: evt.agentId,
        agentName: evt.agentName,
        layer: evt.layer,
        startTime: evt.timestamp,
        endTime: evt.timestamp,
        thinking: [],
        toolCalls: [],
        errors: [],
        stageTransitions: [],
        fileOps: [],
        userMessages: [],
      };
    }

    current.endTime = evt.timestamp;

    switch (evt.activityType) {
      case 'llm_response':
        current.llmResponse = evt;
        break;
      case 'thinking':
        current.thinking.push(evt);
        break;
      case 'tool_call':
        current.toolCalls.push({ call: evt });
        break;
      case 'tool_result':
        if (current.toolCalls.length > 0) {
          const lastUnresolved = [...current.toolCalls].reverse().find(tc => !tc.result);
          if (lastUnresolved) lastUnresolved.result = evt;
          else current.toolCalls.push({ call: evt, result: evt });
        }
        break;
      case 'error':
        current.errors.push(evt);
        break;
      case 'file_read':
      case 'file_write':
        current.fileOps.push(evt);
        break;
      case 'user_message':
      case 'human_feedback':
      case 'metaprompt_update':
        current.userMessages.push(evt);
        break;
      default:
        break;
    }
  }

  if (
    current &&
    (current.llmRequest ||
      current.llmCall ||
      current.llmResponse ||
      current.toolCalls.length > 0 ||
      current.thinking.length > 0 ||
      current.stageTransitions.length > 0 ||
      current.userMessages.length > 0)
  ) {
    turns.push(current);
  }

  return turns;
}

/** Render a chat-style message bubble with collapsible long content + copy. */
const ChatBubble: React.FC<{
  role: 'user' | 'assistant' | 'system' | 'tool';
  agentName: string;
  meta?: string;
  body: string;
  expanded: boolean;
  onToggle: () => void;
  layerColor?: string;
  pending?: boolean;
  t: (key: string) => string;
}> = ({ role, agentName, meta, body, expanded, onToggle, layerColor, pending, t }) => {
  const trimmed = body || '';
  const PREVIEW_CHARS = 1200;
  const isLong = trimmed.length > PREVIEW_CHARS;
  const preview = isLong && !expanded ? `${trimmed.slice(0, PREVIEW_CHARS)}…` : trimmed;
  const icons: Record<string, string> = { user: '🧑', assistant: '🤖', system: '⚙️', tool: '🛠' };
  const labels: Record<string, string> = {
    user: t('conversation.role_user'),
    assistant: t('conversation.role_assistant'),
    system: t('conversation.role_system'),
    tool: t('conversation.role_tool'),
  };

  const onCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(trimmed).catch(() => undefined);
    }
  };

  return (
    <div className={`cv-bubble cv-bubble--${role}`} style={layerColor ? { borderLeftColor: layerColor } : undefined}>
      <div className="cv-bubble-header">
        <span className="cv-bubble-icon">{icons[role] || '•'}</span>
        <span className="cv-bubble-role">{labels[role] || role}</span>
        <span className="cv-bubble-agent" style={{ color: layerColor }}>{agentName}</span>
        {meta && <span className="cv-bubble-meta">{meta}</span>}
        <div className="cv-bubble-actions">
          {pending && <span className="cv-bubble-pending">{t('conversation.waiting_response')}</span>}
          {trimmed && (
            <button type="button" className="cv-bubble-btn" onClick={onCopy}>
              {t('conversation.copy')}
            </button>
          )}
          {isLong && (
            <button type="button" className="cv-bubble-btn" onClick={(e) => { e.stopPropagation(); onToggle(); }}>
              {expanded ? t('conversation.collapse') : t('conversation.expand')}
            </button>
          )}
        </div>
      </div>
      {trimmed ? (
        <pre className="cv-bubble-body">{preview}</pre>
      ) : pending ? (
        <div className="cv-bubble-pending-body">…</div>
      ) : (
        <div className="cv-bubble-empty">{t('conversation.no_content')}</div>
      )}
    </div>
  );
};

const ThinkingBlock = memo<{ events: ActivityEvent[]; expanded: boolean; onToggle: () => void }>(
  ({ events, expanded, onToggle }) => {
    if (events.length === 0) return null;
    const combined = events.map(e => {
      const text = e.detail || e.summary;
      return text.replace(/^💭\s*/, '');
    }).join('\n\n');

    return (
      <div className="cv-thinking" onClick={onToggle}>
        <div className="cv-thinking-indicator">
          <span className="cv-thinking-dots">
            <span className="cv-dot" />
            <span className="cv-dot" />
            <span className="cv-dot" />
          </span>
          <span className="cv-thinking-label">Thinking</span>
        </div>
        {expanded ? (
          <pre className="cv-thinking-text">{combined}</pre>
        ) : (
          <div className="cv-thinking-preview">
            {combined.slice(0, 120)}
            {combined.length > 120 ? '...' : ''}
          </div>
        )}
      </div>
    );
  }
);

const ToolCallBlock = memo<{ pair: ToolCallPair; expanded: boolean; onToggle: () => void }>(
  ({ pair, expanded, onToggle }) => {
    const { call, result } = pair;
    const isError = result?.activityType === 'error' || result?.summary?.includes('失败') || result?.summary?.includes('ERROR');
    const statusCls = isError ? 'cv-tool--error' : result ? 'cv-tool--success' : 'cv-tool--pending';

    return (
      <div className={`cv-tool-block ${statusCls}`} onClick={onToggle}>
        <div className="cv-tool-header">
          <span className="cv-tool-status-dot" />
          <span className="cv-tool-name">{call.summary.replace(/^[^\s]+\s*/, '')}</span>
          {result && <span className="cv-tool-duration">{formatTime(result.timestamp)}</span>}
        </div>
        {expanded && (
          <div className="cv-tool-details">
            {call.detail && (
              <div className="cv-tool-args">
                <span className="cv-tool-section-label">Input:</span>
                <pre>{call.detail}</pre>
              </div>
            )}
            {result && (
              <div className={`cv-tool-result ${isError ? 'cv-tool-result--error' : ''}`}>
                <span className="cv-tool-section-label">Output:</span>
                <pre>{result.detail || result.summary}</pre>
              </div>
            )}
          </div>
        )}
        {!expanded && result && (
          <div className="cv-tool-summary">
            {result.summary.replace(/^[^\s]+\s*/, '').slice(0, 80)}
          </div>
        )}
      </div>
    );
  }
);

const CollapsedFileOps = memo<{ events: ActivityEvent[] }>(
  ({ events }) => {
    if (events.length === 0) return null;
    const reads = events.filter(e => e.activityType === 'file_read');
    const writes = events.filter(e => e.activityType === 'file_write');

    return (
      <div className="cv-file-ops">
        {reads.length > 0 && (
          <span className="cv-file-badge cv-file-read-badge">
            📖 {reads.length} file{reads.length > 1 ? 's' : ''} read
          </span>
        )}
        {writes.length > 0 && (
          <span className="cv-file-badge cv-file-write-badge">
            ✏️ {writes.length} file{writes.length > 1 ? 's' : ''} written
          </span>
        )}
      </div>
    );
  }
);

const TurnCard = memo<{
  turn: ConversationTurn;
  expandedIds: Set<string>;
  onToggle: (id: string) => void;
  t: (key: string) => string;
}>(
  ({ turn, expandedIds, onToggle, t }) => {
    const layerColor = LAYER_COLORS[turn.layer] || '#555';
    const duration = turn.endTime - turn.startTime;
    const hasToolCalls = turn.toolCalls.length > 0;
    const isCollapsed = !expandedIds.has(turn.id);

    const tokens = turn.llmResponse?.tokens
      || turn.llmCall?.tokens
      || (turn.llmResponse?.summary.match(/(\d+)\s*tokens/i)?.[1] ? Number(turn.llmResponse?.summary.match(/(\d+)\s*tokens/i)?.[1]) : 0);
    const elapsed = turn.llmResponse?.elapsedMs || turn.llmCall?.elapsedMs || 0;
    const requestBody = turn.llmRequest?.detail || turn.llmRequest?.summary || '';
    const responseBody = turn.llmResponse?.detail || '';
    const requestMeta = [
      turn.stageTransitions[0]?.stage ? `S${turn.stageTransitions[0].stage}` : turn.llmRequest?.stage ? `S${turn.llmRequest.stage}` : '',
      formatTime(turn.startTime),
      duration > 0 ? formatDuration(duration) : '',
    ].filter(Boolean).join(' · ');
    const responseMeta = [
      turn.llmResponse?.model || turn.llmCall?.model || '',
      tokens ? `${tokens} tokens` : '',
      elapsed ? formatDuration(elapsed) : '',
    ].filter(Boolean).join(' · ');

    return (
      <div className="cv-turn" style={{ borderLeftColor: layerColor }}>
        <div className="cv-turn-header" onClick={() => onToggle(turn.id)}>
          <div className="cv-turn-meta">
            <span className="cv-turn-number">Turn {turn.turnNumber}</span>
            <span className="cv-turn-agent" style={{ color: layerColor }}>{turn.agentName}</span>
            <span className="cv-turn-time">{formatTime(turn.startTime)}</span>
            {duration > 0 && <span className="cv-turn-duration">{formatDuration(duration)}</span>}
          </div>
          <div className="cv-turn-summary">
            {tokens > 0 && <span className="cv-turn-tokens">{tokens} tokens</span>}
            {hasToolCalls && (
              <span className="cv-turn-tools">
                {turn.toolCalls.length} tool{turn.toolCalls.length > 1 ? 's' : ''}
              </span>
            )}
            {turn.errors.length > 0 && (
              <span className="cv-turn-errors">
                {turn.errors.length} error{turn.errors.length > 1 ? 's' : ''}
              </span>
            )}
            <span className="cv-turn-chevron">{isCollapsed ? '▸' : '▾'}</span>
          </div>
        </div>

        {turn.stageTransitions.map(st => (
          <div key={st.id} className="cv-stage-divider">
            <span className="cv-stage-line" />
            <span className="cv-stage-label">{st.summary}</span>
            <span className="cv-stage-line" />
          </div>
        ))}

        {!isCollapsed && (
          <div className="cv-turn-body">
            {turn.userMessages
              .filter(msg => msg.activityType === 'user_message' || msg.activityType === 'agent_chat')
              .map(msg => (
                <ChatBubble
                  key={msg.id}
                  role="user"
                  agentName={msg.agentName || 'You'}
                  meta={formatTime(msg.timestamp)}
                  body={msg.detail || msg.summary}
                  expanded={expandedIds.has(`${msg.id}-chat`)}
                  onToggle={() => onToggle(`${msg.id}-chat`)}
                  layerColor={layerColor}
                  t={t}
                />
              ))}

            {turn.userMessages
              .filter(msg => msg.activityType === 'human_feedback')
              .map(msg => (
                <ChatBubble
                  key={msg.id}
                  role="system"
                  agentName={t('conversation.human_feedback')}
                  meta={formatTime(msg.timestamp)}
                  body={msg.detail || msg.summary}
                  expanded={expandedIds.has(`${msg.id}-fb`)}
                  onToggle={() => onToggle(`${msg.id}-fb`)}
                  layerColor={layerColor}
                  t={t}
                />
              ))}

            {turn.userMessages
              .filter(msg => msg.activityType === 'metaprompt_update')
              .map(msg => (
                <ChatBubble
                  key={msg.id}
                  role="system"
                  agentName={t('conversation.metaprompt_update')}
                  meta={formatTime(msg.timestamp)}
                  body={msg.detail || msg.summary}
                  expanded={expandedIds.has(`${msg.id}-mp`)}
                  onToggle={() => onToggle(`${msg.id}-mp`)}
                  layerColor={layerColor}
                  t={t}
                />
              ))}

            {(turn.llmRequest || turn.llmCall) && (
              <ChatBubble
                role="user"
                agentName={turn.agentName}
                meta={requestMeta}
                body={requestBody}
                expanded={expandedIds.has(`${turn.id}-req`)}
                onToggle={() => onToggle(`${turn.id}-req`)}
                layerColor={layerColor}
                t={t}
              />
            )}

            <ThinkingBlock
              events={turn.thinking}
              expanded={expandedIds.has(`${turn.id}-thinking`)}
              onToggle={() => onToggle(`${turn.id}-thinking`)}
            />

            {turn.toolCalls.map((pair, i) => (
              <ToolCallBlock
                key={`${turn.id}-tool-${i}`}
                pair={pair}
                expanded={expandedIds.has(`${turn.id}-tool-${i}`)}
                onToggle={() => onToggle(`${turn.id}-tool-${i}`)}
              />
            ))}

            <CollapsedFileOps events={turn.fileOps} />

            {turn.errors.map(err => (
              <div key={err.id} className="cv-error-block">
                <span className="cv-error-icon">❌</span>
                <span className="cv-error-text">{err.summary}</span>
                {err.detail && <pre className="cv-error-detail">{err.detail}</pre>}
              </div>
            ))}

            {(turn.llmResponse || turn.llmRequest) && (
              <ChatBubble
                role="assistant"
                agentName={turn.agentName}
                meta={responseMeta || (turn.llmResponse ? formatTime(turn.llmResponse.timestamp) : '')}
                body={responseBody}
                expanded={expandedIds.has(`${turn.id}-resp`)}
                onToggle={() => onToggle(`${turn.id}-resp`)}
                layerColor={layerColor}
                pending={!turn.llmResponse && !!turn.llmRequest}
                t={t}
              />
            )}
          </div>
        )}
      </div>
    );
  }
);

const ConversationView: React.FC<Props> = ({ activities, t, filterAgentId }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<'conversation' | 'flat'>('conversation');
  const [agentFilter, setAgentFilter] = useState<string>(filterAgentId || '');

  const filtered = useMemo(() => {
    if (agentFilter) return activities.filter(a => a.agentId === agentFilter);
    return activities;
  }, [activities, agentFilter]);

  const turns = useMemo(() => groupIntoTurns(filtered), [filtered]);

  const uniqueAgents = useMemo(() => {
    const agents = new Map<string, string>();
    for (const a of activities) {
      if (!agents.has(a.agentId)) agents.set(a.agentId, a.agentName);
    }
    return agents;
  }, [activities]);

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [turns.length, filtered.length, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
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
        <span className="timeline-empty-icon">💬</span>
        <p>{t('timeline.empty')}</p>
      </div>
    );
  }

  return (
    <div className="conversation-view">
      <div className="cv-toolbar">
        <div className="cv-agent-tabs">
          <button
            className={`cv-tab ${!agentFilter ? 'cv-tab--active' : ''}`}
            onClick={() => setAgentFilter('')}
          >
            All
          </button>
          {Array.from(uniqueAgents.entries()).map(([id, name]) => (
            <button
              key={id}
              className={`cv-tab ${agentFilter === id ? 'cv-tab--active' : ''}`}
              onClick={() => setAgentFilter(id)}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="cv-controls">
          <button
            className={`cv-mode-btn ${viewMode === 'conversation' ? 'cv-mode-btn--active' : ''}`}
            onClick={() => setViewMode('conversation')}
            title="Conversation View"
          >
            💬
          </button>
          <button
            className={`cv-mode-btn ${viewMode === 'flat' ? 'cv-mode-btn--active' : ''}`}
            onClick={() => setViewMode('flat')}
            title="Flat Timeline"
          >
            📋
          </button>
          <label className="cv-autoscroll">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={e => setAutoScroll(e.target.checked)}
            />
            Auto-scroll
          </label>
          <span className="cv-count">
            {viewMode === 'conversation'
              ? `${turns.length} turns`
              : `${filtered.length} events`}
          </span>
        </div>
      </div>

      <div className="cv-body" ref={containerRef} onScroll={handleScroll}>
        {viewMode === 'conversation' ? (
          turns.map((turn, idx) => {
            const isDefaultOpen = idx >= turns.length - 3;
            const turnExpanded = new Set(expandedIds);
            if (isDefaultOpen) turnExpanded.add(turn.id);
            return (
              <TurnCard
                key={turn.id}
                turn={turn}
                expandedIds={turnExpanded}
                onToggle={toggleExpand}
                t={t}
              />
            );
          })
        ) : (
          filtered.map(event => (
            <FlatEvent
              key={event.id}
              event={event}
              expanded={expandedIds.has(event.id)}
              onToggle={() => toggleExpand(event.id)}
            />
          ))
        )}
      </div>
    </div>
  );
};

const FlatEvent = memo<{ event: ActivityEvent; expanded: boolean; onToggle: () => void }>(
  ({ event, expanded, onToggle }) => {
    const layerColor = LAYER_COLORS[event.layer] || '#555';
    const typeIcons: Record<string, string> = {
      thinking: '💭', tool_call: '⚡', tool_result: '📋',
      file_read: '📖', file_write: '✏️', llm_call: '🤖',
      llm_response: '📥', stage_transition: '🔄', error: '❌',
    };

    return (
      <div
        className={`cv-flat-event cv-flat-${event.activityType}`}
        onClick={event.detail ? onToggle : undefined}
      >
        <span className="cv-flat-time">{formatTime(event.timestamp)}</span>
        <span className="cv-flat-icon">{typeIcons[event.activityType] || '•'}</span>
        <span className="cv-flat-agent" style={{ color: layerColor }}>{event.agentName}</span>
        <span className="cv-flat-summary">{event.summary}</span>
        {expanded && event.detail && (
          <pre className="cv-flat-detail">{event.detail}</pre>
        )}
      </div>
    );
  }
);

export default ConversationView;
