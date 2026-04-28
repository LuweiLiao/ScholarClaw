import React, { useEffect, useState } from 'react';
import type { ArtifactPreviewInfo } from '../types';

interface Props {
  projectId: string;
  stage: number;
  filename: string;
  dir?: string;
  ws: WebSocket | null;
  onClose: () => void;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderMarkdown(text: string): React.ReactNode {
  const lines = text.split('\n');
  const elements: React.ReactNode[] = [];
  let inCodeBlock = false;
  let codeLines: string[] = [];
  let codeLang = '';

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('```')) {
      if (inCodeBlock) {
        elements.push(
          <pre key={`code-${i}`} className="md-code-block" data-lang={codeLang}>
            <code>{codeLines.join('\n')}</code>
          </pre>
        );
        codeLines = [];
        codeLang = '';
        inCodeBlock = false;
      } else {
        codeLang = line.slice(3).trim();
        inCodeBlock = true;
      }
      continue;
    }
    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }
    if (line.startsWith('# ')) {
      elements.push(<h1 key={i} className="md-h1">{line.slice(2)}</h1>);
    } else if (line.startsWith('## ')) {
      elements.push(<h2 key={i} className="md-h2">{line.slice(3)}</h2>);
    } else if (line.startsWith('### ')) {
      elements.push(<h3 key={i} className="md-h3">{line.slice(4)}</h3>);
    } else if (line.startsWith('#### ')) {
      elements.push(<h4 key={i} className="md-h4">{line.slice(5)}</h4>);
    } else if (line.startsWith('- ') || line.startsWith('* ')) {
      elements.push(<li key={i} className="md-li">{renderInline(line.slice(2))}</li>);
    } else if (/^\d+\.\s/.test(line)) {
      elements.push(<li key={i} className="md-li md-ol">{renderInline(line.replace(/^\d+\.\s/, ''))}</li>);
    } else if (line.startsWith('> ')) {
      elements.push(<blockquote key={i} className="md-quote">{renderInline(line.slice(2))}</blockquote>);
    } else if (line.startsWith('---') || line.startsWith('***')) {
      elements.push(<hr key={i} className="md-hr" />);
    } else if (line.trim() === '') {
      elements.push(<br key={i} />);
    } else {
      elements.push(<p key={i} className="md-p">{renderInline(line)}</p>);
    }
  }
  return <>{elements}</>;
}

function renderInline(text: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  let remaining = text;
  let key = 0;
  const inlinePattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/;
  while (remaining) {
    const match = inlinePattern.exec(remaining);
    if (!match) {
      parts.push(remaining);
      break;
    }
    if (match.index > 0) {
      parts.push(remaining.slice(0, match.index));
    }
    const token = match[0];
    if (token.startsWith('`')) {
      parts.push(<code key={key++} className="md-inline-code">{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**')) {
      parts.push(<strong key={key++}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('*')) {
      parts.push(<em key={key++}>{token.slice(1, -1)}</em>);
    }
    remaining = remaining.slice(match.index + token.length);
  }
  return <>{parts}</>;
}

function formatJson(text: string): string {
  try {
    const parsed = JSON.parse(text);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return text;
  }
}

const ArtifactViewer: React.FC<Props> = ({ projectId, stage, filename, dir, ws, onClose }) => {
  const [preview, setPreview] = useState<ArtifactPreviewInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        command: 'get_artifact_preview',
        projectId, stage, filename, dir: dir || '',
      }));
      setLoading(true);
    }
  }, [ws, projectId, stage, filename, dir]);

  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'artifact_preview' && msg.payload?.filename === filename && msg.payload?.stage === stage) {
          setPreview(msg.payload);
          setLoading(false);
        }
      } catch { /* ignore */ }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws, filename, stage]);

  const renderContent = () => {
    if (!preview) return null;
    switch (preview.contentType) {
      case 'image':
        return <img src={preview.content} alt={filename} style={{ maxWidth: '100%', borderRadius: 8 }} />;
      case 'markdown':
        return <div className="artifact-md-render">{renderMarkdown(preview.content)}</div>;
      case 'json':
        return <pre className="artifact-code artifact-json">{formatJson(preview.content)}</pre>;
      case 'yaml':
        return <pre className="artifact-code artifact-yaml">{preview.content}</pre>;
      default:
        return <pre className="artifact-code">{preview.content}</pre>;
    }
  };

  return (
    <div className="artifact-viewer-overlay" onClick={onClose}>
      <div className="artifact-viewer-panel" onClick={e => e.stopPropagation()}>
        <div className="artifact-viewer-header">
          <h3>📄 {filename}</h3>
          <span className="artifact-viewer-meta">
            S{stage} · {preview?.contentType || '...'} · {preview ? formatSize(preview.size) : '...'}
          </span>
          <button className="artifact-viewer-close" onClick={onClose}>✕</button>
        </div>
        <div className="artifact-viewer-body">
          {loading && <div className="artifact-viewer-loading">加载中...</div>}
          {!loading && renderContent()}
        </div>
      </div>
    </div>
  );
};

export default ArtifactViewer;
