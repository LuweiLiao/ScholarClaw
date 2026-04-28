import React, { memo, useState, useCallback, useEffect, useRef } from 'react';
import type { TaskGraphInfo, TaskNodeInfo } from '../types';

interface Props {
  taskGraph: TaskGraphInfo | null;
  t: (key: string) => string;
  ws: WebSocket | null;
  selectedProjectId: string | null;
}

const LAYER_ORDER = ['idea', 'experiment', 'coding', 'execution', 'writing'];
const LAYER_COLORS: Record<string, string> = {
  idea: '#f59e0b',
  experiment: '#3b82f6',
  coding: '#10b981',
  execution: '#ef4444',
  writing: '#a855f7',
};

interface ContextMenu {
  x: number;
  y: number;
  nodeId: string;
  status: string;
}

export default memo(function TaskGraphView({ taskGraph, t, ws, selectedProjectId }: Props) {
  const [contextMenu, setContextMenu] = useState<ContextMenu | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = () => setContextMenu(null);
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, []);

  const handleContextMenu = useCallback((e: React.MouseEvent, node: TaskNodeInfo) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, nodeId: node.id, status: node.status });
  }, []);

  const sendCommand = useCallback((command: string, taskId: string) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const projectId = selectedProjectId || taskGraph?.projectId || taskGraph?.project_id || '';
    ws.send(JSON.stringify({ command, taskId, projectId }));
    setContextMenu(null);
  }, [ws, selectedProjectId, taskGraph]);

  if (!taskGraph || !taskGraph.nodes || Object.keys(taskGraph.nodes).length === 0) {
    return (
      <div className="taskgraph-empty">
        <span className="taskgraph-empty-icon">📋</span>
        <p>{t('taskgraph.empty')}</p>
      </div>
    );
  }

  const nodes = taskGraph.nodes;
  const nodesByLayer: Record<string, TaskNodeInfo[]> = {};
  for (const node of Object.values(nodes)) {
    const layer = node.layer || 'idea';
    if (!nodesByLayer[layer]) nodesByLayer[layer] = [];
    nodesByLayer[layer].push(node);
  }

  const stats = {
    total: Object.keys(nodes).length,
    done: Object.values(nodes).filter(n => n.status === 'done').length,
    running: Object.values(nodes).filter(n => n.status === 'running').length,
    failed: Object.values(nodes).filter(n => n.status === 'failed').length,
  };

  return (
    <div className="taskgraph-view">
      <div className="taskgraph-header">
        <span>{t('taskgraph.title')} — {stats.done}/{stats.total} tasks</span>
        <span>
          {stats.running > 0 && <span style={{ color: '#f0883e', marginRight: 8 }}>⚡ {stats.running} running</span>}
          {stats.failed > 0 && <span style={{ color: '#f85149' }}>❌ {stats.failed} failed</span>}
        </span>
      </div>
      <div className="taskgraph-body">
        {LAYER_ORDER.map(layer => {
          const layerNodes = nodesByLayer[layer];
          if (!layerNodes || layerNodes.length === 0) return null;
          return (
            <div key={layer} className="taskgraph-layer">
              <div className="taskgraph-layer-title" style={{ color: LAYER_COLORS[layer] || '#8b949e' }}>
                {layer}
              </div>
              <div className="taskgraph-nodes">
                {layerNodes.map(node => (
                  <div
                    key={node.id}
                    className={`taskgraph-node taskgraph-node--${node.status}`}
                    onClick={() => setSelectedNode(selectedNode === node.id ? null : node.id)}
                    onContextMenu={(e) => handleContextMenu(e, node)}
                  >
                    <div className="taskgraph-node-title">{node.title}</div>
                    <div className="taskgraph-node-meta">
                      <span className={`taskgraph-node-status taskgraph-node-status--${node.status}`}>
                        {t(`taskgraph.${node.status}`)}
                      </span>
                      <span>S{node.stage_from}–S{node.stage_to}</span>
                      {node.assigned_agent && (
                        <span style={{ color: '#58a6ff' }}>🤖 {node.assigned_agent.slice(0, 8)}</span>
                      )}
                    </div>
                    {selectedNode === node.id && (
                      <div style={{ marginTop: 8, paddingTop: 6, borderTop: '1px solid #21262d', fontSize: 11, color: '#8b949e' }}>
                        <div>{node.description}</div>
                        {node.dependencies.length > 0 && (
                          <div style={{ marginTop: 4 }}>
                            {t('taskgraph.dependencies')}: {node.dependencies.join(', ')}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {contextMenu && (
        <div
          ref={menuRef}
          className="taskgraph-context-menu"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onClick={e => e.stopPropagation()}
        >
          {(contextMenu.status === 'pending' || contextMenu.status === 'ready' || contextMenu.status === 'running') && (
            <button
              className="taskgraph-context-item taskgraph-context-item--danger"
              onClick={() => sendCommand('skip_task', contextMenu.nodeId)}
            >
              ⏭ {t('taskgraph.skip')}
            </button>
          )}
          {(contextMenu.status === 'failed' || contextMenu.status === 'skipped') && (
            <button
              className="taskgraph-context-item"
              onClick={() => sendCommand('retry_task', contextMenu.nodeId)}
            >
              🔄 {t('taskgraph.retry')}
            </button>
          )}
          <button
            className="taskgraph-context-item"
            onClick={() => { setSelectedNode(contextMenu.nodeId); setContextMenu(null); }}
          >
            🔍 {t('taskgraph.detail')}
          </button>
        </div>
      )}
    </div>
  );
});
