import { useState, useRef, useEffect } from 'react';
import type { PlannerStatus } from '../types';

const LAYER_KEYS = ['idea', 'experiment', 'coding', 'execution', 'writing'] as const;

/** Strip <think>…</think> blocks (model reasoning) from displayed text. */
function stripThinkTags(text: string): string {
  return text
    .replace(/<think>[\s\S]*?<\/think>/g, '')
    .replace(/<think>[\s\S]*$/, '')  // partial unclosed tag during streaming
    .trim();
}

interface Props {
  ws: WebSocket | null;
  projectId: string;
  plannerStatus: PlannerStatus | null;
  t: (k: string) => string;
  onClose: () => void;
  onConfirmed: () => void;
}

export default function PlannerChat({ ws, projectId, plannerStatus, t, onClose, onConfirmed }: Props) {
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [selectedProposals, setSelectedProposals] = useState<Set<string>>(new Set());
  const [agentCounts, setAgentCounts] = useState<Record<string, number>>({
    idea: 2, experiment: 1, coding: 2, execution: 2, writing: 2,
  });
  const [view, setView] = useState<'chat' | 'proposals' | 'confirm'>('chat');
  const [localMessages, setLocalMessages] = useState<{ role: string; content: string }[]>([]);
  const [streamingText, setStreamingText] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);
  const prevServerLen = useRef(0);

  const status = plannerStatus?.status ?? 'chatting';
  const proposals = plannerStatus?.proposals ?? null;

  const serverHistory = plannerStatus?.chatHistory ?? [];
  const displayMessages = serverHistory.length >= localMessages.length
    ? serverHistory
    : localMessages;

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [displayMessages.length, streamingText]);

  // Sync local messages only when server history actually grows (new assistant reply arrived)
  useEffect(() => {
    if (serverHistory.length > prevServerLen.current) {
      prevServerLen.current = serverHistory.length;
      setLocalMessages(serverHistory);
      if (sending) {
        setSending(false);
        setStreamingText('');
      }
    }
  }, [serverHistory.length]);

  // Listen for streaming chunks
  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'planner_chunk' && msg.payload?.projectId === projectId) {
          setStreamingText(prev => prev + (msg.payload.text || ''));
        }
      } catch { /* ignore */ }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws, projectId]);

  useEffect(() => {
    if (proposals && proposals.length > 0 && view === 'chat') {
      setView('proposals');
    }
  }, [proposals]);

  useEffect(() => {
    if (status === 'confirmed') {
      onConfirmed();
    }
  }, [status]);

  const send = () => {
    const text = input.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    setSending(true);
    setStreamingText('');
    setLocalMessages(prev => [...prev, { role: 'user', content: text }]);
    ws.send(JSON.stringify({ command: 'planner_chat', projectId, message: text }));
    setInput('');
  };

  const toggleProposal = (id: string) => {
    setSelectedProposals(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const confirmSelection = () => {
    if (selectedProposals.size === 0) return;
    setView('confirm');
  };

  const confirmPlan = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      command: 'planner_select',
      projectId,
      proposalIds: Array.from(selectedProposals),
      layerAgentCounts: agentCounts,
    }));
    setTimeout(() => {
      ws.send(JSON.stringify({ command: 'planner_confirm', projectId }));
    }, 300);
  };

  return (
    <div className="planner-overlay">
      <div className="planner-container">
        <div className="planner-header">
          <h3>{t('planner.title')}</h3>
          <div className="planner-status-badge" data-status={status}>
            {t(`planner.${status}`)}
          </div>
          <button className="planner-close" onClick={onClose}>&times;</button>
        </div>

        {view === 'chat' && (
          <div className="planner-chat">
            <div className="planner-messages">
              {displayMessages.map((msg, i) => {
                const display = msg.role === 'assistant' ? stripThinkTags(msg.content) : msg.content;
                if (msg.role === 'assistant' && !display) return null;
                return (
                  <div key={i} className={`planner-msg planner-msg-${msg.role}`}>
                    <div className="planner-msg-role">
                      {msg.role === 'user' ? '你' : 'AI'}
                    </div>
                    <div className="planner-msg-content">{display}</div>
                  </div>
                );
              })}
              {streamingText && (() => {
                const display = stripThinkTags(streamingText);
                if (!display) return (
                  <div className="planner-msg planner-msg-assistant">
                    <div className="planner-msg-role">AI</div>
                    <div className="planner-msg-content planner-typing">{t('planner.thinking')}</div>
                  </div>
                );
                return (
                  <div className="planner-msg planner-msg-assistant">
                    <div className="planner-msg-role">AI</div>
                    <div className="planner-msg-content">{display}<span className="streaming-cursor">▌</span></div>
                  </div>
                );
              })()}
              {sending && !streamingText && (
                <div className="planner-msg planner-msg-assistant">
                  <div className="planner-msg-role">AI</div>
                  <div className="planner-msg-content planner-typing">{t('planner.thinking')}</div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
            <div className="planner-input-row">
              <textarea
                className="planner-input"
                placeholder={t('planner.input_placeholder')}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
                disabled={sending}
              />
              <button
                className="planner-send-btn"
                onClick={send}
                disabled={sending || !input.trim()}
              >
                {t('planner.send')}
              </button>
            </div>
          </div>
        )}

        {view === 'proposals' && proposals && (
          <div className="planner-proposals">
            <h4>{t('planner.proposals_title')}</h4>
            <div className="proposal-cards">
              {proposals.map(p => (
                <div
                  key={p.id}
                  className={`proposal-card ${selectedProposals.has(p.id) ? 'selected' : ''}`}
                  onClick={() => toggleProposal(p.id)}
                >
                  <div className="proposal-header">
                    <span className="proposal-check">
                      {selectedProposals.has(p.id) ? '✓' : '○'}
                    </span>
                    <h5>{p.title}</h5>
                  </div>
                  <p className="proposal-summary">{p.summary}</p>
                  <p className="proposal-approach">{p.approach}</p>
                  <div className="proposal-effort">
                    <span className="proposal-effort-label">{t('planner.effort')}:</span>
                    {LAYER_KEYS.map(l => (
                      <span key={l} className="proposal-effort-item">
                        {t(`planner.layer_${l}`)}: {p.estimated_effort[l] ?? 0}
                      </span>
                    ))}
                  </div>
                  <div className="proposal-tasks">
                    <span className="proposal-tasks-label">{t('planner.tasks')}:</span>
                    {p.task_breakdown.map(tk => (
                      <span key={tk.id} className="proposal-task-chip" data-layer={tk.layer}>
                        {tk.title}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="proposal-actions">
              <button className="planner-back-btn" onClick={() => setView('chat')}>
                {t('planner.back_to_chat')}
              </button>
              <button
                className="planner-select-btn"
                onClick={confirmSelection}
                disabled={selectedProposals.size === 0}
              >
                {t('planner.select_proposals')} ({selectedProposals.size})
              </button>
            </div>
          </div>
        )}

        {view === 'confirm' && (
          <div className="planner-confirm">
            <h4>{t('planner.agent_count')}</h4>
            <div className="agent-count-grid">
              {LAYER_KEYS.map(layer => (
                <div key={layer} className="agent-count-row">
                  <label>{t(`planner.layer_${layer}`)}</label>
                  <input
                    type="range"
                    min={1}
                    max={5}
                    value={agentCounts[layer] ?? 1}
                    onChange={e => setAgentCounts(prev => ({ ...prev, [layer]: parseInt(e.target.value) }))}
                  />
                  <span className="agent-count-val">{agentCounts[layer] ?? 1}</span>
                </div>
              ))}
            </div>
            <div className="confirm-actions">
              <button className="planner-back-btn" onClick={() => setView('proposals')}>
                {t('planner.back_to_chat')}
              </button>
              <button className="planner-confirm-btn" onClick={confirmPlan}>
                {t('planner.confirm_plan')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
