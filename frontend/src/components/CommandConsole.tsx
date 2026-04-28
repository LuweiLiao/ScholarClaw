import React, { memo, useEffect, useRef, useState, useCallback } from 'react';
import type { ChatMessage } from '../types';

interface Props {
  messages: ChatMessage[];
  connected: boolean;
  t: (key: string) => string;
  onSend: (content: string, targetLayer?: string) => void;
}

const SLASH_COMMANDS = [
  { cmd: '/help', desc: 'console.cmd.help' },
  { cmd: '/stop', desc: 'console.cmd.stop' },
  { cmd: '/skip', desc: 'console.cmd.skip' },
  { cmd: '/retry', desc: 'console.cmd.retry' },
  { cmd: '/focus', desc: 'console.cmd.focus' },
  { cmd: '/status', desc: 'console.cmd.status' },
  { cmd: '/pause', desc: 'console.cmd.pause' },
  { cmd: '/resume', desc: 'console.cmd.resume' },
];

export default memo(function CommandConsole({ messages, connected, t, onSend }: Props) {
  const [input, setInput] = useState('');
  const [showHints, setShowHints] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (listRef.current && expanded) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [messages.length, expanded]);

  const filteredHints = input.startsWith('/')
    ? SLASH_COMMANDS.filter(c => c.cmd.startsWith(input.split(' ')[0]))
    : [];

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setInput(val);
    setShowHints(val.startsWith('/') && val.split(' ').length <= 1);
  }, []);

  const handleSend = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setInput('');
    setShowHints(false);
    if (!expanded) setExpanded(true);
  }, [input, onSend, expanded]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSend();
    } else if (e.key === 'Escape') {
      setShowHints(false);
    }
  }, [handleSend]);

  const selectHint = useCallback((cmd: string) => {
    setInput(cmd + ' ');
    setShowHints(false);
    inputRef.current?.focus();
  }, []);

  const recentMessages = messages.slice(-20);

  return (
    <div className="command-console">
      {expanded && recentMessages.length > 0 && (
        <div className="console-messages" ref={listRef}>
          {recentMessages.map(msg => (
            <div key={msg.id} className={`console-msg console-msg--${msg.role}`}>
              <span className="console-msg-role">
                {msg.role === 'user' ? '▶ YOU' : '◀ SYS'}
              </span>
              <span className="console-msg-content">{msg.content}</span>
            </div>
          ))}
        </div>
      )}

      {showHints && filteredHints.length > 0 && (
        <div className="console-hint-panel">
          {filteredHints.map(h => (
            <div
              key={h.cmd}
              className="console-hint-item"
              onClick={() => selectHint(h.cmd)}
            >
              <span className="console-hint-cmd">{h.cmd}</span>
              <span className="console-hint-desc">{t(h.desc)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="console-input-row">
        <span
          className="console-prompt"
          onClick={() => setExpanded(!expanded)}
          style={{ cursor: 'pointer' }}
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? '▼' : '▶'} $
        </span>
        <input
          ref={inputRef}
          className="console-input"
          value={input}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            if (input.startsWith('/') && input.split(' ').length <= 1) setShowHints(true);
          }}
          placeholder={t('console.placeholder')}
          disabled={!connected}
        />
        <button
          className="console-send-btn"
          onClick={handleSend}
          disabled={!input.trim() || !connected}
        >
          ↵
        </button>
        <span style={{ fontSize: 10, color: connected ? '#3fb950' : '#f85149', marginLeft: 4 }}>
          {connected ? '●' : '○'}
        </span>
      </div>
    </div>
  );
});
