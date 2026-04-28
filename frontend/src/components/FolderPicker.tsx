import { useState, useEffect, useRef, useCallback } from 'react';

interface BrowseEntry {
  name: string;
  path: string;
  type: 'dir' | 'file';
}

interface BrowseResult {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
  error: string | null;
}

interface Props {
  ws: WebSocket | null;
  onSelect: (path: string) => void;
  onClose: () => void;
  title?: string;
  /** 'folder' = 选文件夹（默认）; 'file' = 选文件 */
  mode?: 'folder' | 'file';
  /** mode=file 时只显示这些扩展名，如 ['.tex'] */
  filterExts?: string[];
  /** 打开时默认跳到这个路径 */
  initialPath?: string;
}

export default function FolderPicker({
  ws,
  onSelect,
  onClose,
  title,
  mode = 'folder',
  filterExts,
  initialPath,
}: Props) {
  const defaultTitle = mode === 'file' ? '选择文件' : '选择文件夹';
  const [result, setResult] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [inputPath, setInputPath] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const browse = useCallback((path: string) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    setLoading(true);
    ws.send(JSON.stringify({ command: 'browse_path', path }));
  }, [ws]);

  useEffect(() => {
    if (!ws) return;
    const handler = (e: MessageEvent) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'browse_result') {
          setResult(msg.payload);
          setInputPath(msg.payload.path || '');
          setLoading(false);
        }
      } catch {
        // Ignore messages for other panels sharing the same WebSocket.
      }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws]);

  useEffect(() => { browse(initialPath || ''); }, [browse, initialPath]);

  const visibleEntries = result?.entries.filter(e => {
    if (e.type === 'dir') return true;
    if (mode === 'folder') return false; // 选文件夹时不显示文件
    if (filterExts && filterExts.length > 0) {
      return filterExts.some(ext => e.name.toLowerCase().endsWith(ext));
    }
    return true;
  }) ?? [];

  const handleEntryClick = (entry: BrowseEntry) => {
    if (entry.type === 'dir') {
      browse(entry.path);
    } else if (mode === 'file') {
      onSelect(entry.name); // 返回文件名（不含路径）
      onClose();
    }
  };

  const handleInputSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputPath.trim()) browse(inputPath.trim());
  };

  return (
    <div className="folder-picker-overlay" onClick={onClose}>
      <div className="folder-picker-modal" onClick={e => e.stopPropagation()}>
        <div className="folder-picker-header">
          <span className="folder-picker-title">
            {mode === 'file' ? '📄' : '📁'} {title ?? defaultTitle}
          </span>
          <button className="folder-picker-close" onClick={onClose}>✕</button>
        </div>

        <form className="folder-picker-bar" onSubmit={handleInputSubmit}>
          <input
            ref={inputRef}
            className="folder-picker-input"
            value={inputPath}
            onChange={e => setInputPath(e.target.value)}
            placeholder="输入路径后按 Enter 跳转..."
            spellCheck={false}
          />
          <button type="submit" className="folder-picker-go">→</button>
        </form>

        {result?.parent != null && (
          <button className="folder-picker-back" onClick={() => browse(result.parent!)}>
            ↑ 上一级
          </button>
        )}

        {result?.error && (
          <div className="folder-picker-error">{result.error}</div>
        )}

        <div className="folder-picker-list">
          {loading && <div className="folder-picker-loading">加载中...</div>}
          {!loading && visibleEntries.length === 0 && (
            <div className="folder-picker-empty">
              {mode === 'file' ? '没有找到匹配的文件' : '空文件夹'}
            </div>
          )}
          {!loading && visibleEntries.map(entry => (
            <div
              key={entry.path}
              className={`folder-picker-entry ${entry.type} ${mode === 'file' && entry.type === 'file' ? 'selectable-file' : ''}`}
              onClick={() => handleEntryClick(entry)}
              title={entry.path}
            >
              <span className="folder-picker-entry-icon">
                {entry.type === 'dir' ? '📁' : '📄'}
              </span>
              <span className="folder-picker-entry-name">{entry.name}</span>
            </div>
          ))}
        </div>

        <div className="folder-picker-footer">
          <span className="folder-picker-current" title={result?.path}>
            {result?.path || '—'}
          </span>
          {mode === 'folder' && (
            <button
              className="folder-picker-select-btn"
              disabled={!result?.path}
              onClick={() => { if (result?.path) { onSelect(result.path); onClose(); } }}
            >
              选择此文件夹
            </button>
          )}
          {mode === 'file' && (
            <span className="folder-picker-hint">点击文件即可选择</span>
          )}
        </div>
      </div>
    </div>
  );
}
