import React, { memo, useMemo, useState, useCallback } from 'react';
import type { TaskNodeDetailPayload, TaskNodeInfo } from '../types';

export type NodeDetailTab = 'overview' | 'logs' | 'prompt';

interface Props {
  payload: TaskNodeDetailPayload | null;
  graphNode: TaskNodeInfo | null;
  loading: boolean;
  ws: WebSocket | null;
  onClose: () => void;
  /** Parent sets loading before `get_node_detail` when refreshing from the panel */
  onRefreshStart?: () => void;
  t: (key: string) => string;
}

function mergeNode(graphNode: TaskNodeInfo | null, partial?: Partial<TaskNodeInfo> | TaskNodeInfo | null): TaskNodeInfo | null {
  if (!graphNode && !partial) return null;
  if (!graphNode) return partial as TaskNodeInfo;
  if (!partial) return graphNode;
  return { ...graphNode, ...partial };
}

function formatIo(io: Record<string, unknown> | Array<unknown> | undefined): string {
  if (io === undefined) return '';
  if (Array.isArray(io) && io.every(item => typeof item === 'string')) return io.join('\n');
  try {
    return JSON.stringify(io, null, 2);
  } catch {
    return String(io);
  }
}

export default memo(function NodeDetailPanel({
  payload,
  graphNode,
  loading,
  ws,
  onClose,
  onRefreshStart,
  t,
}: Props) {
  const [tab, setTab] = useState<NodeDetailTab>('overview');

  const effectiveNode = useMemo(
    () => mergeNode(graphNode, payload?.node ?? undefined),
    [graphNode, payload?.node],
  );

  const taskId = payload?.taskId ?? graphNode?.id ?? '';

  const refreshDetail = useCallback(() => {
    const p = payload?.projectId;
    if (!p || !taskId || !ws || ws.readyState !== WebSocket.OPEN) return;
    onRefreshStart?.();
    ws.send(JSON.stringify({ command: 'get_node_detail', taskId, projectId: p }));
  }, [payload?.projectId, taskId, ws, onRefreshStart]);

  const requestPrompt = useCallback(() => {
    const p = payload?.projectId;
    if (!p || !taskId || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ command: 'get_metaprompt', projectId: p, nodeId: taskId }));
  }, [payload?.projectId, taskId, ws]);

  if (!payload && !graphNode) return null;

  const logsText = payload?.logs ?? '';
  const promptText = payload?.promptDraft ?? '';

  return (
    <div className="stage-detail-overlay" onClick={onClose}>
      <div className="stage-detail-panel node-detail-panel" onClick={e => e.stopPropagation()}>
        <div className="stage-detail-header">
          <h3>
            {effectiveNode?.title ?? (taskId || t('nodeDetail.title'))}
          </h3>
          {effectiveNode && (
            <span className={`taskgraph-node-status taskgraph-node-status--${effectiveNode.status}`} style={{ fontSize: 12 }}>
              {t(`taskgraph.${effectiveNode.status}`)}
            </span>
          )}
          <button type="button" className="stage-detail-close" onClick={onClose} aria-label="close">✕</button>
        </div>

        <div className="node-detail-tabs">
          {(['overview', 'logs', 'prompt'] as const).map(k => (
            <button
              key={k}
              type="button"
              className={`node-detail-tab ${tab === k ? 'node-detail-tab--active' : ''}`}
              onClick={() => setTab(k)}
            >
              {t(`nodeDetail.tab_${k}`)}
            </button>
          ))}
        </div>

        {loading && <div className="stage-detail-loading">{t('nodeDetail.loading')}</div>}

        {payload?.error && (
          <div className="node-detail-error">{payload.error}</div>
        )}

        {!loading && tab === 'overview' && (
          <div className="node-detail-body">
            <dl className="node-detail-dl">
              <dt>{t('nodeDetail.id')}</dt>
              <dd className="node-detail-mono">{taskId}</dd>
              {effectiveNode && (
                <>
                  <dt>{t('nodeDetail.layer')}</dt>
                  <dd>{effectiveNode.layer}</dd>
                  <dt>{t('taskgraph.stages')}</dt>
                  <dd>S{effectiveNode.stage_from}–S{effectiveNode.stage_to}</dd>
                  <dt>{t('taskgraph.dependencies')}</dt>
                  <dd className="node-detail-mono">
                    {effectiveNode.dependencies?.length ? effectiveNode.dependencies.join(', ') : '—'}
                  </dd>
                  {effectiveNode.assigned_agent && (
                    <>
                      <dt>{t('taskgraph.assigned')}</dt>
                      <dd className="node-detail-mono">{effectiveNode.assigned_agent}</dd>
                    </>
                  )}
                </>
              )}
            </dl>
            {effectiveNode?.description && (
              <div className="node-detail-section">
                <h4>{t('nodeDetail.description')}</h4>
                <p className="node-detail-desc">{effectiveNode.description}</p>
              </div>
            )}
            <div className="node-detail-section">
              <h4>{t('nodeDetail.inputs')}</h4>
              <pre className="node-detail-pre">{formatIo(payload?.inputs) || t('nodeDetail.placeholder_io')}</pre>
            </div>
            <div className="node-detail-section">
              <h4>{t('nodeDetail.outputs')}</h4>
              <pre className="node-detail-pre">{formatIo(payload?.outputs) || t('nodeDetail.placeholder_io')}</pre>
            </div>
            <div className="node-detail-toolbar">
              <button type="button" className="node-detail-refresh" onClick={refreshDetail}>
                {t('nodeDetail.refresh_detail')}
              </button>
            </div>
          </div>
        )}

        {!loading && tab === 'logs' && (
          <div className="node-detail-body">
            <pre className="node-detail-pre node-detail-pre--grow">{logsText || t('nodeDetail.logs_placeholder')}</pre>
            <div className="node-detail-toolbar">
              <button type="button" className="node-detail-refresh" onClick={refreshDetail}>
                {t('nodeDetail.refresh_detail')}
              </button>
            </div>
          </div>
        )}

        {!loading && tab === 'prompt' && (
          <div className="node-detail-body">
            <p className="node-detail-hint">{t('nodeDetail.prompt_hint')}</p>
            <pre className="node-detail-pre node-detail-pre--grow">{promptText || t('nodeDetail.prompt_placeholder')}</pre>
            <div className="node-detail-toolbar">
              <button type="button" className="node-detail-refresh" onClick={requestPrompt}>
                {t('nodeDetail.request_prompt')}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
});
