import React, { memo, useState } from 'react';
import type { ApprovalRequest, ApprovalActionType } from '../types';

interface Props {
  requests: ApprovalRequest[];
  t: (key: string) => string;
  onApprove: (requestId: string) => void;
  onReject: (requestId: string, comment?: string) => void;
  onApproveAll: (actionType: ApprovalActionType) => void;
  onAlwaysAllow?: (requestId: string, toolName: string) => void;
}

const TOOL_ICONS: Record<string, string> = {
  write_file: '✏️',
  edit_file: '✏️',
  bash: '⚡',
  latex_compile: '📄',
  file_write: '✏️',
  run_script: '▶️',
  api_call: '🌐',
  file_delete: '🗑️',
};

export default memo(function ApprovalDialog({
  requests, t, onApprove, onReject, onApproveAll, onAlwaysAllow,
}: Props) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  if (requests.length === 0) return null;

  return (
    <div className="approval-overlay">
      <div className="approval-queue-header">
        <span className="approval-queue-count">{requests.length}</span>
        <span>{t('approval.pending_count')}</span>
      </div>
      {requests.map(req => {
        const toolName = req.toolName || req.actionType;
        const args = req.args;
        const icon = TOOL_ICONS[toolName] || TOOL_ICONS[req.actionType] || '⚠️';

        return (
          <div key={req.requestId} className="approval-card">
            <div className="approval-header">
              <span className="approval-icon">{icon}</span>
              <span className="approval-title">
                {t('approval.title')} — <code>{toolName}</code>
              </span>
              <span className="approval-agent">{req.agentName}</span>
            </div>

            <div className="approval-description">{req.description}</div>

            {args && (
              <div className="approval-args">
                {typeof args.path === 'string' && args.path && (
                  <div className="approval-arg-row">
                    <span className="approval-arg-label">Path:</span>
                    <code className="approval-arg-value">{args.path}</code>
                  </div>
                )}
                {typeof args.command === 'string' && args.command && (
                  <div className="approval-arg-row">
                    <span className="approval-arg-label">Command:</span>
                    <code className="approval-arg-value">{args.command}</code>
                  </div>
                )}
              </div>
            )}

            {req.detail && (
              <>
                {expandedId === req.requestId ? (
                  <pre
                    className="approval-detail"
                    onClick={() => setExpandedId(null)}
                  >
                    {req.detail}
                  </pre>
                ) : (
                  <button
                    className="approval-expand-btn"
                    onClick={() => setExpandedId(req.requestId)}
                  >
                    ▶ {t('approval.show_detail')}
                  </button>
                )}
              </>
            )}

            <div className="approval-actions">
              <button
                className="approval-btn approval-btn--reject"
                onClick={() => onReject(req.requestId)}
                title={t('approval.reject')}
              >
                ✕ {t('approval.reject')}
              </button>
              <button
                className="approval-btn approval-btn--approve"
                onClick={() => onApprove(req.requestId)}
                title={t('approval.approve')}
              >
                ✓ {t('approval.approve_once')}
              </button>
              {onAlwaysAllow && (
                <button
                  className="approval-btn approval-btn--always"
                  onClick={() => onAlwaysAllow(req.requestId, toolName)}
                  title={t('approval.always_allow')}
                >
                  ✓✓ {t('approval.always_allow')}
                </button>
              )}
              <button
                className="approval-btn approval-btn--approve-all"
                onClick={() => onApproveAll(req.actionType)}
                title={t('approval.approve_all')}
              >
                ✓* {t('approval.approve_all')}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
});
