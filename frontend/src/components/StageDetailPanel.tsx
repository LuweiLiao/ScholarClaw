import React, { useEffect, useState, useCallback } from 'react';
import type { StageDetailInfo, ArtifactPreviewInfo } from '../types';
import { STAGE_META } from '../types';

interface Props {
  projectId: string;
  stage: number;
  ws: WebSocket | null;
  onClose: () => void;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const StageDetailPanel: React.FC<Props> = ({ projectId, stage, ws, onClose }) => {
  const [detail, setDetail] = useState<StageDetailInfo | null>(null);
  const [preview, setPreview] = useState<ArtifactPreviewInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const stageMeta = STAGE_META[stage as keyof typeof STAGE_META];

  const fetchDetail = useCallback(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ command: 'get_stage_detail', projectId, stage }));
      setLoading(true);
    }
  }, [ws, projectId, stage]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'stage_detail' && msg.payload?.stage === stage) {
          setDetail(msg.payload);
          setLoading(false);
        }
        if (msg.type === 'artifact_preview' && msg.payload?.stage === stage) {
          setPreview(msg.payload);
        }
      } catch { /* ignore */ }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws, stage]);

  const openPreview = (filename: string, dir?: string) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        command: 'get_artifact_preview',
        projectId, stage, filename, dir: dir || '',
      }));
    }
  };

  const statusLabel = {
    pending: '⏳ 未开始',
    completed: '✅ 已完成',
    incomplete: '⚠️ 部分完成',
  };

  return (
    <div className="stage-detail-overlay" onClick={onClose}>
      <div className="stage-detail-panel" onClick={e => e.stopPropagation()}>
        <div className="stage-detail-header">
          <h3>S{stage} · {stageMeta?.name || `Stage ${stage}`}</h3>
          <span className={`stage-detail-status status-${detail?.status || 'pending'}`}>
            {statusLabel[detail?.status || 'pending']}
          </span>
          <button className="stage-detail-close" onClick={onClose}>✕</button>
        </div>

        {loading && <div className="stage-detail-loading">加载中...</div>}

        {detail && (
          <>
            <div className="stage-detail-expected">
              <h4>期望产出</h4>
              <div className="expected-list">
                {detail.expectedOutputs.map(out => {
                  const found = detail.files.some(f =>
                    out.endsWith('/') ? f.name.startsWith(out.replace(/\/$/, '')) : f.name === out
                  );
                  return (
                    <span key={out} className={`expected-item ${found ? 'found' : 'missing'}`}>
                      {found ? '✓' : '✗'} {out}
                    </span>
                  );
                })}
              </div>
            </div>

            <div className="stage-detail-files">
              <h4>文件列表 ({detail.files.length})</h4>
              {detail.files.length === 0 ? (
                <div className="no-files">暂无文件</div>
              ) : (
                <table className="files-table">
                  <thead>
                    <tr><th>文件名</th><th>大小</th><th>操作</th></tr>
                  </thead>
                  <tbody>
                    {detail.files.map(f => (
                      <tr key={f.name}>
                        <td className="file-name">{f.name}</td>
                        <td className="file-size">{formatSize(f.size)}</td>
                        <td>
                          <button
                            className="preview-btn"
                            onClick={() => openPreview(f.name, f.dir)}
                          >
                            预览
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}

        {preview && (
          <div className="artifact-preview-section">
            <div className="preview-header">
              <h4>📄 {preview.filename}</h4>
              <span className="preview-meta">
                {preview.contentType} · {formatSize(preview.size)}
              </span>
              <button className="preview-close" onClick={() => setPreview(null)}>✕</button>
            </div>
            <div className="preview-content">
              {preview.contentType === 'image' ? (
                <img src={preview.content} alt={preview.filename} style={{ maxWidth: '100%' }} />
              ) : (
                <pre className={`preview-code preview-${preview.contentType}`}>
                  {preview.content}
                </pre>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default StageDetailPanel;
