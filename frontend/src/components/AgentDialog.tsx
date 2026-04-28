import { useEffect, useRef, useState, useCallback } from 'react';
import type { LobsterAgent, LogEntry, ActivityEvent } from '../types';
import ConversationView from './ConversationView';

interface Props {
  agent: LobsterAgent;
  logs: LogEntry[];
  ws: WebSocket | null;
  activities?: ActivityEvent[];
  t?: (key: string) => string;
  onClose: () => void;
  onStopAgent?: (agentId: string) => void;
}

export default function AgentDialog({ agent, logs, ws, activities = [], t, onClose, onStopAgent }: Props) {
  const [rawLines, setRawLines] = useState<string[]>([]);
  const [tab, setTab] = useState<'conversation' | 'raw'>('conversation');
  const bottomRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [chatInput, setChatInput] = useState('');
  const [chatHistory, setChatHistory] = useState<Array<{ role: 'user' | 'agent'; text: string; ts: number }>>([]);
  const [dialogHeight, setDialogHeight] = useState(() => {
    try {
      const saved = localStorage.getItem('agent-dialog-height');
      return saved ? parseInt(saved) : 520;
    } catch { return 520; }
  });
  const resizingRef = useRef(false);
  const resizeStartRef = useRef({ y: 0, h: 0 });
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const agentLogs = logs.filter((l) => l.agentId === agent.id);
  const tFn = t || ((k: string) => k);

  const fetchRawLog = useCallback(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ command: 'tail_agent_log', agentId: agent.id, maxLines: 500 }));
  }, [ws, agent.id]);

  useEffect(() => {
    fetchRawLog();

    const handler = (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'agent_log_tail' && msg.payload?.agentId === agent.id) {
          setRawLines(msg.payload.lines || []);
        }
        if (msg.type === 'agent_activity' && msg.payload?.agentId === agent.id && msg.payload?.activityType === 'user_message') {
          setChatHistory(prev => [...prev, { role: 'user', text: msg.payload.summary, ts: msg.payload.timestamp * 1000 }]);
        }
      } catch { /* ignore */ }
    };
    ws?.addEventListener('message', handler);

    pollRef.current = setInterval(fetchRawLog, 3000);

    return () => {
      ws?.removeEventListener('message', handler);
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [ws, agent.id, fetchRawLog]);

  useEffect(() => {
    if (tab === 'raw') {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [rawLines.length, tab]);

  const sendChat = useCallback(() => {
    const text = chatInput.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ command: 'agent_chat', agentId: agent.id, message: text }));
    setChatHistory(prev => [...prev, { role: 'user', text, ts: Date.now() }]);
    setChatInput('');
    inputRef.current?.focus();
  }, [chatInput, ws, agent.id]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  };

  // Resize from top edge
  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    resizingRef.current = true;
    resizeStartRef.current = { y: e.clientY, h: dialogHeight };

    const onMove = (ev: MouseEvent) => {
      if (!resizingRef.current) return;
      const dy = resizeStartRef.current.y - ev.clientY;
      const newH = Math.max(300, Math.min(window.innerHeight - 60, resizeStartRef.current.h + dy));
      setDialogHeight(newH);
    };
    const onUp = () => {
      resizingRef.current = false;
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      try { localStorage.setItem('agent-dialog-height', String(dialogHeight)); } catch { /* ignore */ }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [dialogHeight]);

  const levelIcon = (level: string) => {
    switch (level) {
      case 'success': return '✅';
      case 'error': return '❌';
      case 'warning': return '⚠️';
      default: return '▸';
    }
  };

  const isActive = ['working', 'waiting_discussion', 'discussing', 'awaiting_approval'].includes(agent.status);

  // Merge chat history with activities for the conversation view
  const agentActivities = activities.filter(a => a.agentId === agent.id);
  const existingUserMsgTexts = new Set(
    agentActivities.filter(a => a.activityType === 'user_message').map(a => a.summary)
  );
  const localOnly = chatHistory.filter(msg => !existingUserMsgTexts.has(msg.text));
  const allActivities: ActivityEvent[] = [
    ...agentActivities,
    ...localOnly.map((msg, i) => ({
      id: `chat-${i}-${msg.ts}`,
      agentId: agent.id,
      agentName: msg.role === 'user' ? 'You' : agent.name,
      projectId: agent.projectId || '',
      layer: agent.layer as ActivityEvent['layer'],
      activityType: 'user_message' as const,
      summary: msg.text,
      timestamp: msg.ts / 1000,
    })),
  ].sort((a, b) => a.timestamp - b.timestamp);

  return (
    <div className="agent-dialog-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="agent-dialog agent-dialog-bottom" style={{ height: dialogHeight }}>
        {/* Resize handle at top */}
        <div
          className="agent-dialog-resize-handle"
          onMouseDown={onResizeStart}
          title={tFn('agent.resize')}
        />

        <div className="agent-dialog-header">
          <div className="agent-dialog-title">
            <span className="agent-dialog-icon">🦞</span>
            <span className="agent-dialog-name">{agent.name}</span>
            <span className={`agent-dialog-status status-${agent.status}`}>{agent.status}</span>
            {agent.projectId && <span className="agent-dialog-project">{agent.projectId}</span>}
            {isActive && onStopAgent && (
              confirmStop ? (
                <span style={{ display: 'inline-flex', gap: 4, marginLeft: 8 }}>
                  <button
                    className="agent-stop-btn"
                    onClick={() => { onStopAgent(agent.id); setConfirmStop(false); }}
                  >
                    ✓ {tFn('agent.stop')}
                  </button>
                  <button
                    style={{ background: 'transparent', border: '1px solid #30363d', color: '#8b949e', borderRadius: 4, padding: '2px 6px', fontSize: 10, cursor: 'pointer' }}
                    onClick={() => setConfirmStop(false)}
                  >
                    ✕
                  </button>
                </span>
              ) : (
                <button
                  className="agent-stop-btn"
                  style={{ marginLeft: 8 }}
                  onClick={() => setConfirmStop(true)}
                  title={tFn('agent.stop_confirm')}
                >
                  ⏹ {tFn('agent.stop')}
                </button>
              )
            )}
          </div>
          <div className="agent-dialog-tabs">
            <button className={tab === 'conversation' ? 'active' : ''} onClick={() => setTab('conversation')}>
              💬 {tFn('agent.conversation')}
              {allActivities.length > 0 && (
                <span style={{ marginLeft: 4, background: '#388bfd', color: '#fff', borderRadius: 8, padding: '0 5px', fontSize: 10 }}>
                  {allActivities.length}
                </span>
              )}
            </button>
            <button className={tab === 'raw' ? 'active' : ''} onClick={() => setTab('raw')}>
              🖥️ Raw
            </button>
          </div>
          <button className="agent-dialog-close" onClick={onClose}>✕</button>
        </div>

        <div className="agent-dialog-body">
          {tab === 'conversation' ? (
            <div className="agent-dialog-conversation">
              {allActivities.length === 0 && agentLogs.length === 0 && (
                <div className="agent-dialog-empty">{tFn('agent.no_activity')}</div>
              )}
              {/* Show structured logs when no activities yet */}
              {allActivities.length === 0 && agentLogs.length > 0 && (
                <div className="agent-dialog-logs">
                  {agentLogs.map((log) => (
                    <div key={log.id} className={`agent-dialog-log-item level-${log.level}`}>
                      <span className="adl-time">{new Date(log.timestamp).toLocaleTimeString('en-GB')}</span>
                      <span className="adl-icon">{levelIcon(log.level)}</span>
                      {log.stage && <span className="adl-stage">S{log.stage}</span>}
                      <span className="adl-msg">{log.message}</span>
                    </div>
                  ))}
                </div>
              )}
              {allActivities.length > 0 && (
                <ConversationView
                  activities={allActivities}
                  t={tFn}
                  filterAgentId={agent.id}
                />
              )}
            </div>
          ) : (
            <div className="agent-dialog-terminal">
              {rawLines.length === 0 && <div className="agent-dialog-empty">No process output</div>}
              {rawLines.map((line, i) => (
                <div key={i} className="agent-dialog-term-line">{line || '\u00A0'}</div>
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {/* Chat input area */}
        <div className="agent-dialog-input-area">
          <textarea
            ref={inputRef}
            className="agent-dialog-input"
            placeholder={tFn('agent.chat_placeholder')}
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            disabled={!isActive}
          />
          <button
            className="agent-dialog-send-btn"
            onClick={sendChat}
            disabled={!chatInput.trim() || !isActive}
            title={tFn('agent.send')}
          >
            ↵
          </button>
        </div>
      </div>
    </div>
  );
}
