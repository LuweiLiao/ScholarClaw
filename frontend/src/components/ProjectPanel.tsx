import { memo, useCallback, useEffect, useRef, useState } from 'react';
import type {
  Artifact,
  CoordinationSessionInfo,
  LayerModelCfg,
  ProjectArchiveInfo,
  ProjectInfo,
  ProjectScanResult,
} from '../types';
import { useLocale } from '../i18n';
import FolderPicker from './FolderPicker';
import ScanResultCard from './ScanResultCard';
import CoordinationPanel from './CoordinationPanel';

const STATUS_ICONS: Record<string, { color: string; icon: string }> = {
  running:     { color: '#22c55e', icon: '▶' },
  queued:      { color: '#f59e0b', icon: '⏳' },
  completed:   { color: '#3b82f6', icon: '✓' },
  interrupted: { color: '#ef4444', icon: '⏸' },
  new:         { color: '#94a3b8', icon: '○' },
};

const LAYER_KEYS = ['idea', 'experiment', 'coding', 'execution', 'writing'] as const;
const emptyLM = (): LayerModelCfg => ({ base_url: '', api_key: '', model: '' });
const emptyLMRecord = (): Record<string, LayerModelCfg> =>
  Object.fromEntries(LAYER_KEYS.map(k => [k, emptyLM()]));
const hasLM = (cfg: LayerModelCfg) => !!(cfg.base_url || cfg.api_key || cfg.model);

interface Props {
  ws: WebSocket | null;
  projects: ProjectInfo[];
  archives: ProjectArchiveInfo[];
  connected: boolean;
  selectedProjectId: string | null;
  artifactsByProject: Record<string, Artifact[]>;
  onSelect: (projectId: string) => void;
  onResume: (projectId: string) => void;
  onPause: (projectId: string) => void;
  onRestart: (projectId: string) => void;
  onDelete: (projectId: string) => void;
  onOpenFolder: (projectId: string) => void;
  onArchive: (projectId: string) => void;
  onRestoreArchive: (archiveId: string) => void;
  onRefreshArchives: () => void;
  onUpdateLayerModels: (projectId: string, layerModels: Record<string, LayerModelCfg>) => void;
  scanResult?: ProjectScanResult | null;
  coordSessions?: Record<string, CoordinationSessionInfo[]>;
  onScanProject?: (workspaceDir: string, mainTexFile: string) => void;
  onStartPlanning?: (
    projectId: string,
    workspaceDir: string,
    mainTexFile: string,
    llmConfig: LayerModelCfg,
  ) => void;
  onRestoreProject?: (projectId: string) => void;
}

function stageName(n: number, t: (k: string) => string): string {
  const key = `stage.${n}`;
  const translated = t(key);
  return translated !== key ? translated : `S${n}`;
}

function readSavedGlobalLLM(): LayerModelCfg {
  try {
    const raw = localStorage.getItem('scholar-global-llm');
    if (raw) return { ...emptyLM(), ...JSON.parse(raw) };
  } catch {
    // Ignore broken local storage entries.
  }
  return emptyLM();
}

export default memo(function ProjectPanel({
  ws,
  projects,
  archives,
  connected,
  selectedProjectId,
  artifactsByProject,
  onSelect,
  onResume,
  onPause,
  onRestart,
  onDelete,
  onOpenFolder,
  onArchive,
  onRestoreArchive,
  onRefreshArchives,
  onUpdateLayerModels,
  scanResult,
  coordSessions,
  onScanProject,
  onStartPlanning,
  onRestoreProject,
}: Props) {
  const { t, locale } = useLocale();
  const [panelOpen, setPanelOpen] = useState(true);
  const [showArchives, setShowArchives] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [workspaceDir, setWorkspaceDir] = useState('');
  const [mainTexFile, setMainTexFile] = useState('');
  const [ignoreExisting, setIgnoreExisting] = useState(false);
  const [pickerTarget, setPickerTarget] = useState<'openProject' | 'mainTex' | null>(null);
  const [scanning, setScanning] = useState(false);

  type TestStatus = 'idle' | 'testing' | 'ok' | 'fail';
  type SaveStatus = 'idle' | 'saving' | 'saved' | 'fail';
  const [editLMProjectId, setEditLMProjectId] = useState<string | null>(null);
  const [editLMData, setEditLMData] = useState<Record<string, LayerModelCfg>>(emptyLMRecord());
  const [editLMTestStatus, setEditLMTestStatus] = useState<Record<string, { status: TestStatus; error?: string }>>({});
  const [lmSaveStatus, setLmSaveStatus] = useState<SaveStatus>('idle');
  const editPendingTests = useRef<Map<string, string>>(new Map());
  const requestSeq = useRef(0);

  useEffect(() => {
    if (!scanResult) return;
    const timer = setTimeout(() => setScanning(false), 0);
    return () => clearTimeout(timer);
  }, [scanResult]);

  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'test_model_result' && msg.payload?.requestId) {
          const layerKey = editPendingTests.current.get(msg.payload.requestId);
          editPendingTests.current.delete(msg.payload.requestId);
          if (layerKey) {
            setEditLMTestStatus(prev => ({
              ...prev,
              [layerKey]: {
                status: msg.payload.ok ? 'ok' : 'fail',
                error: msg.payload.error || '',
              },
            }));
          }
        }
        if (msg.type === 'update_layer_models_result' && msg.payload) {
          setLmSaveStatus(msg.payload.ok ? 'saved' : 'fail');
          if (msg.payload.ok) setTimeout(() => setLmSaveStatus('idle'), 2000);
        }
      } catch {
        // Other websocket messages belong to sibling panels.
      }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws]);

  const sorted = [...projects].sort((a, b) => {
    const order: Record<string, number> = { running: 0, queued: 1, interrupted: 2, new: 3, completed: 4 };
    return (order[a.status] ?? 5) - (order[b.status] ?? 5);
  });
  const running = projects.filter(p => p.status === 'running').length;
  const interrupted = projects.filter(p => p.status === 'interrupted').length;

  const openEditLM = useCallback((proj: ProjectInfo) => {
    const data = emptyLMRecord();
    const existing = proj.layerModels || {};
    for (const k of LAYER_KEYS) {
      if (existing[k]) data[k] = { ...data[k], ...existing[k] };
    }
    setEditLMData(data);
    setEditLMProjectId(proj.projectId);
    setEditLMTestStatus({});
    setLmSaveStatus('idle');
  }, []);

  const testEditModel = (layerKey: string, cfg: LayerModelCfg) => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !hasLM(cfg)) return;
    requestSeq.current += 1;
    const requestId = `edit-${layerKey}-${requestSeq.current}`;
    editPendingTests.current.set(requestId, layerKey);
    setEditLMTestStatus(prev => ({ ...prev, [layerKey]: { status: 'testing' } }));
    ws.send(JSON.stringify({ command: 'test_model_config', requestId, config: cfg }));
  };

  const saveEditLM = (projectId: string) => {
    const cleaned = Object.fromEntries(
      Object.entries(editLMData).filter(([, cfg]) => hasLM(cfg)),
    );
    setLmSaveStatus('saving');
    onUpdateLayerModels(projectId, cleaned);
  };

  const onPickerSelect = (path: string) => {
    if (pickerTarget === 'openProject') {
      setWorkspaceDir(path);
      setMainTexFile('');
      setIgnoreExisting(false);
      setScanning(true);
      onScanProject?.(path, '');
      setTimeout(() => setScanning(false), 3000);
    } else if (pickerTarget === 'mainTex') {
      setMainTexFile(path);
    }
    setPickerTarget(null);
  };

  return (
    <>
      {pickerTarget && (
        <FolderPicker
          ws={ws}
          title={pickerTarget === 'mainTex' ? '选择主 .tex 文件' : '选择项目文件夹'}
          mode={pickerTarget === 'mainTex' ? 'file' : 'folder'}
          filterExts={pickerTarget === 'mainTex' ? ['.tex'] : undefined}
          initialPath={pickerTarget === 'mainTex' ? workspaceDir : undefined}
          onSelect={onPickerSelect}
          onClose={() => setPickerTarget(null)}
        />
      )}

      <div className="project-panel">
        <div className="project-panel-header" onClick={() => setPanelOpen(!panelOpen)}>
          <h3>
            <span className="panel-icon">📋</span> {t('project.title')}
            {projects.length > 0 && <span className="project-count">{projects.length}</span>}
            {running > 0 && <span className="project-count running-count">{t('project.count_running', { n: running })}</span>}
            {interrupted > 0 && <span className="project-count interrupted-count">{t('project.count_interrupted', { n: interrupted })}</span>}
          </h3>
          <span className="expand-arrow">{panelOpen ? '▾' : '▸'}</span>
        </div>

        {panelOpen && (
          <div className="project-panel-body">
            <div className="open-project-section">
              <button
                className="open-project-btn"
                disabled={!connected || scanning}
                onClick={() => setPickerTarget('openProject')}
              >
                📂 {scanning ? t('scanner.scanning') : t('scanner.open_project')}
              </button>
            </div>

            {workspaceDir && (
              <div className="main-tex-selector">
                <div className="main-tex-row">
                  <span className="main-tex-label">📄 {t('planner.main_tex')}</span>
                  <div className="main-tex-value-row">
                    <span className={`main-tex-value ${!mainTexFile ? 'placeholder' : ''}`}>
                      {mainTexFile ? mainTexFile.split(/[/\\]/).pop() : t('planner.select_tex')}
                    </span>
                    <button
                      className="main-tex-pick-btn"
                      type="button"
                      onClick={() => setPickerTarget('mainTex')}
                      disabled={!connected}
                    >
                      {t('planner.browse')}
                    </button>
                    {mainTexFile && (
                      <button className="main-tex-clear-btn" type="button" onClick={() => setMainTexFile('')}>×</button>
                    )}
                  </div>
                </div>
              </div>
            )}

            {scanResult?.existingProjectId && !ignoreExisting && (
              <div className="existing-project-notice">
                <div className="existing-project-info">
                  <span className="existing-project-icon">🔄</span>
                  <div className="existing-project-text">
                    <strong>{t('scanner.existing_project')}</strong>
                    <span className="existing-project-topic">
                      {scanResult.existingConfig?.topic || scanResult.existingProjectId}
                    </span>
                  </div>
                </div>
                <div className="existing-project-actions">
                  <button
                    className="restore-project-btn"
                    onClick={() => {
                      onRestoreProject?.(scanResult.existingProjectId!);
                      onResume(scanResult.existingProjectId!);
                    }}
                  >
                    {t('scanner.restore_project')}
                  </button>
                  <button className="new-project-btn" onClick={() => setIgnoreExisting(true)}>
                    {t('scanner.new_project')}
                  </button>
                </div>
              </div>
            )}

            {scanResult && !scanResult.error && (
              <ScanResultCard
                scan={scanResult}
                t={t}
                onStartPlanning={!(scanResult.existingProjectId && !ignoreExisting) ? () => {
                  const llm = readSavedGlobalLLM();
                  onStartPlanning?.(
                    scanResult.projectId || `proj-${Date.now().toString(36)}`,
                    scanResult.workspaceDir || workspaceDir,
                    mainTexFile,
                    llm,
                  );
                } : undefined}
                planningDisabled={!connected || !mainTexFile}
                planningHint={!mainTexFile && workspaceDir ? t('planner.tex_required') : undefined}
              />
            )}

            {sorted.length > 0 && <div className="panel-divider" />}

            {sorted.length === 0 ? (
              <div className="project-empty">{t('project.empty_lab')}</div>
            ) : (
              <div className="project-list-inner">
                {sorted.map(proj => {
                  const cfg = STATUS_ICONS[proj.status] || STATUS_ICONS.new;
                  const statusLabel = t(`project.status.${proj.status}`);
                  const fs = proj.firstStage || 1;
                  const completedCount = proj.lastCompletedStage >= fs ? proj.lastCompletedStage - fs + 1 : 0;
                  const pct = proj.totalStages > 0 ? Math.round((completedCount / proj.totalStages) * 100) : 0;
                  const isSelected = selectedProjectId === proj.projectId;
                  const isExpanded = expandedId === proj.projectId;
                  const arts = artifactsByProject[proj.projectId] || [];

                  return (
                    <div key={proj.projectId} className={`project-card project-${proj.status}${isSelected ? ' selected' : ''}${isExpanded ? ' expanded' : ''}`}>
                      <div className="project-summary" onClick={() => { onSelect(proj.projectId); setExpandedId(prev => prev === proj.projectId ? null : proj.projectId); }}>
                        <span className="project-status-dot" style={{ background: cfg.color }} title={statusLabel}>{cfg.icon}</span>
                        <span className="project-summary-name" title={proj.projectName || proj.topic || proj.projectId}>
                          {proj.projectName || proj.topic || proj.projectId}
                        </span>
                        <span className="project-summary-progress">{pct}%</span>
                        {arts.length > 0 && <span className="project-summary-arts">📦{arts.length}</span>}
                        {proj.intervention && <span className="project-intervention-icon" title={t('project.intervention_needed')}>⚠</span>}
                        <span className={`project-expand-arrow${isExpanded ? ' open' : ''}`}>▸</span>
                      </div>

                      <div className="project-mini-bar">
                        <div className="project-mini-fill" style={{ width: `${pct}%`, background: cfg.color }} />
                      </div>

                      {isExpanded && (
                        <div className="project-detail">
                          <div className="project-detail-meta">
                            {proj.projectName && <span className="project-name-label">{proj.projectName}</span>}
                            <span className="project-id-label" title={proj.projectId}>{proj.projectId}</span>
                            <span className="project-status-badge" style={{ background: cfg.color }}>{cfg.icon} {statusLabel}</span>
                          </div>

                          <div className="project-detail-stage">
                            {completedCount}/{proj.totalStages} {t('project.stages_label')}
                            {proj.lastCompletedStage > 0 && (
                              <> · {stageName(proj.lastCompletedStage, t)}
                                {completedCount < proj.totalStages && (
                                  <span className="project-next-stage"> → {stageName(proj.lastCompletedStage + 1, t)}</span>
                                )}
                              </>
                            )}
                          </div>

                          {proj.timestamp && (
                            <div className="project-detail-time">
                              {new Date(proj.timestamp).toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US')}
                            </div>
                          )}

                          {(proj.workspaceDir || proj.projectDir) && (
                            <div className="project-folder-hint" title={proj.workspaceDir || proj.projectDir}>
                              {proj.workspaceDir ? `Workspace: ${proj.workspaceDir}` : `Runs: ${proj.projectDir}`}
                            </div>
                          )}

                          {proj.intervention && (
                            <div className="project-intervention-box">
                              <div className="project-intervention-title">⚠ {t('project.intervention_needed')}</div>
                              <pre className="project-intervention-detail">{proj.intervention}</pre>
                            </div>
                          )}

                          {connected && (
                            <div className="project-lm-section" onClick={e => e.stopPropagation()}>
                              <button
                                className="project-lm-toggle"
                                type="button"
                                onClick={() => editLMProjectId === proj.projectId ? setEditLMProjectId(null) : openEditLM(proj)}
                              >
                                {editLMProjectId === proj.projectId ? '▾' : '▸'} {t('layer_models.edit_title')}
                                {proj.layerModels && Object.keys(proj.layerModels).length > 0 && (
                                  <span className="ref-badge">{Object.keys(proj.layerModels).length}</span>
                                )}
                              </button>
                              {editLMProjectId === proj.projectId && (
                                <div className="project-lm-editor">
                                  {LAYER_KEYS.map(key => {
                                    const modelCfg = editLMData[key] || emptyLM();
                                    const status = editLMTestStatus[key] || { status: 'idle' as TestStatus };
                                    return (
                                      <div key={key} className="layer-model-group compact">
                                        <div className="layer-model-header">
                                          <span className="layer-model-label">{t(`layer_models.${key}`)}</span>
                                          <div className="layer-model-test-area">
                                            {status.status === 'ok' && <span className="lm-test-badge lm-test-ok">{t('layer_models.test_ok')}</span>}
                                            {status.status === 'fail' && <span className="lm-test-badge lm-test-fail" title={status.error || ''}>{t('layer_models.test_fail')}</span>}
                                            {status.status === 'testing' && <span className="lm-test-badge lm-test-ing">{t('layer_models.testing')}</span>}
                                            <button className="lm-test-btn" type="button" onClick={() => testEditModel(key, modelCfg)} disabled={!hasLM(modelCfg) || status.status === 'testing'}>{t('layer_models.test')}</button>
                                          </div>
                                        </div>
                                        <div className="layer-model-fields">
                                          <input className="layer-model-input lm-url" value={modelCfg.base_url} onChange={e => setEditLMData(prev => ({ ...prev, [key]: { ...modelCfg, base_url: e.target.value } }))} placeholder={t('layer_models.base_url_placeholder')} />
                                          <input className="layer-model-input lm-key" type="password" value={modelCfg.api_key} onChange={e => setEditLMData(prev => ({ ...prev, [key]: { ...modelCfg, api_key: e.target.value } }))} placeholder={t('layer_models.api_key_placeholder')} />
                                          <input className="layer-model-input lm-model" value={modelCfg.model} onChange={e => setEditLMData(prev => ({ ...prev, [key]: { ...modelCfg, model: e.target.value } }))} placeholder={t('layer_models.model_placeholder')} />
                                        </div>
                                      </div>
                                    );
                                  })}
                                  <div className="project-lm-save-row">
                                    <button className={`project-lm-save-btn${lmSaveStatus === 'saved' ? ' saved' : lmSaveStatus === 'fail' ? ' fail' : ''}`} onClick={() => saveEditLM(proj.projectId)} disabled={lmSaveStatus === 'saving'}>
                                      {lmSaveStatus === 'saving' ? t('layer_models.saving')
                                        : lmSaveStatus === 'saved' ? t('layer_models.saved')
                                        : lmSaveStatus === 'fail' ? t('layer_models.save_fail')
                                        : t('layer_models.save')}
                                    </button>
                                  </div>
                                </div>
                              )}
                            </div>
                          )}

                          {coordSessions?.[proj.projectId] && coordSessions[proj.projectId].length > 0 && (
                            <CoordinationPanel sessions={coordSessions[proj.projectId]} t={t} />
                          )}

                          <div className="project-detail-actions">
                            {proj.status === 'interrupted' && connected && (
                              <button className="project-resume-btn" onClick={(e) => { e.stopPropagation(); onResume(proj.projectId); }}>{t('project.resume')}</button>
                            )}
                            {(proj.status === 'running' || proj.status === 'queued') && connected && (
                              <button className="project-pause-btn" onClick={(e) => { e.stopPropagation(); onPause(proj.projectId); }}>{t('project.pause')}</button>
                            )}
                            {connected && <button className="project-archive-btn" onClick={(e) => { e.stopPropagation(); onArchive(proj.projectId); }}>存档</button>}
                            {connected && <button className="project-open-folder-btn" onClick={(e) => { e.stopPropagation(); onOpenFolder(proj.projectId); }}>打开文件夹</button>}
                            {connected && (
                              <button className="project-restart-btn" onClick={(e) => {
                                e.stopPropagation();
                                if (window.confirm(t('project.restart_confirm', { id: proj.projectId }))) onRestart(proj.projectId);
                              }}>{t('project.restart')}</button>
                            )}
                            <button className="project-delete-btn" title={t('project.delete_title')} onClick={(e) => {
                              e.stopPropagation();
                              if (window.confirm(t('project.delete_confirm', { id: proj.projectId }))) onDelete(proj.projectId);
                            }}>{t('project.delete')}</button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            <div className="archive-section">
              <button
                className="ref-papers-toggle-btn"
                type="button"
                onClick={() => {
                  const next = !showArchives;
                  setShowArchives(next);
                  if (next) onRefreshArchives();
                }}
              >
                {showArchives ? '▾' : '▸'} 历史存档
                {archives.length > 0 && <span className="ref-badge">{archives.length}</span>}
              </button>
              {showArchives && (
                <div className="archive-list">
                  {archives.length === 0 ? (
                    <div className="project-empty compact">暂无存档</div>
                  ) : archives.map(item => (
                    <div key={item.archiveId} className="archive-card">
                      <div className="archive-title" title={item.topic || item.projectId}>
                        {item.projectName || item.topic || item.projectId}
                      </div>
                      <div className="archive-meta">
                        {new Date(item.createdAt).toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US')}
                      </div>
                      <button
                        className="project-resume-btn"
                        type="button"
                        disabled={!connected}
                        onClick={() => {
                          if (window.confirm(`恢复存档 "${item.archiveId}"？当前同名项目会被覆盖。`)) {
                            onRestoreArchive(item.archiveId);
                          }
                        }}
                      >
                        恢复
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
});
