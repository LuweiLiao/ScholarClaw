import { useCallback, useEffect, useRef, useState } from 'react';
import type { ProjectInfo, Artifact } from '../types';
import { useLocale } from '../i18n';
import FolderPicker from './FolderPicker';

const STATUS_ICONS: Record<string, { color: string; icon: string }> = {
  running:     { color: '#22c55e', icon: '▶' },
  queued:      { color: '#f59e0b', icon: '⏳' },
  completed:   { color: '#3b82f6', icon: '✓' },
  interrupted: { color: '#ef4444', icon: '⏸' },
  new:         { color: '#94a3b8', icon: '○' },
};

type SubmitMode = 'lab' | 'reproduce';
type ReferencePdfUpload = { name: string; contentBase64: string };
type LatexFileUpload = { name: string; contentBase64: string };

const LATEX_EXTS = ['.tex', '.bib', '.sty', '.cls', '.bst'];

function stageName(n: number, t: (k: string) => string): string {
  const key = `stage.${n}`;
  const translated = t(key);
  return translated !== key ? translated : `S${n}`;
}

interface Props {
  ws: WebSocket | null;
  projects: ProjectInfo[];
  connected: boolean;
  selectedProjectId: string | null;
  artifactsByProject: Record<string, Artifact[]>;
  discussionMode: boolean;
  onToggleDiscussion: () => void;
  onSelect: (projectId: string) => void;
  onResume: (projectId: string) => void;
  onPause: (projectId: string) => void;
  onRestart: (projectId: string) => void;
  onDelete: (projectId: string) => void;
  onQuickSubmit: (
    topic: string,
    mode: SubmitMode,
    researchAngles: string[],
    referencePapers: string,
    referenceFiles: ReferencePdfUpload[],
    paths: { codebases?: string; datasets?: string; checkpoints?: string },
    latexFiles: LatexFileUpload[],
    workspaceDir: string,
    mainTexFile: string,
    layerModels: Record<string, { base_url: string; api_key: string; model: string }>,
  ) => void;
}

export default function ProjectPanel({ ws, projects, connected, selectedProjectId, artifactsByProject, discussionMode, onToggleDiscussion, onSelect, onResume, onPause, onRestart, onDelete, onQuickSubmit }: Props) {
  const [panelOpen, setPanelOpen] = useState(true);
  const [mode, setMode] = useState<SubmitMode>('lab');
  const [topicInput, setTopicInput] = useState('');
  const [anglesInput, setAnglesInput] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [refPapersInput, setRefPapersInput] = useState('');
  const [referenceFiles, setReferenceFiles] = useState<Array<ReferencePdfUpload & { key: string }>>([]);
  const [latexFiles, setLatexFiles] = useState<Array<LatexFileUpload & { key: string }>>([]);

  const [showPaths, setShowPaths] = useState(false);
  const [workspaceDir, setWorkspaceDir] = useState('');
  const [mainTexFile, setMainTexFile] = useState('');
  const [codebasesPath, setCodebasesPath] = useState('');
  const [datasetsPath, setDatasetsPath] = useState('');
  const [checkpointsPath, setCheckpointsPath] = useState('');

  // Layer model overrides (each layer: base_url, api_key, model)
  type LayerModelCfg = { base_url: string; api_key: string; model: string };
  const emptyLM = (): LayerModelCfg => ({ base_url: '', api_key: '', model: '' });
  const [showLayerModels, setShowLayerModels] = useState(false);
  const [ideaLM, setIdeaLM] = useState<LayerModelCfg>(emptyLM());
  const [experimentLM, setExperimentLM] = useState<LayerModelCfg>(emptyLM());
  const [codingLM, setCodingLM] = useState<LayerModelCfg>(emptyLM());
  const [executionLM, setExecutionLM] = useState<LayerModelCfg>(emptyLM());
  const [writingLM, setWritingLM] = useState<LayerModelCfg>(emptyLM());
  const lmHasValue = (c: LayerModelCfg) => !!(c.base_url || c.api_key || c.model);

  // Model test state: 'idle' | 'testing' | 'ok' | 'fail'
  type TestStatus = 'idle' | 'testing' | 'ok' | 'fail';
  const [testStatus, setTestStatus] = useState<Record<string, { status: TestStatus; error?: string }>>({});
  const pendingTests = useRef<Map<string, string>>(new Map());

  const testModel = useCallback((layerKey: string, cfg: LayerModelCfg) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (!cfg.base_url && !cfg.model) return;
    const requestId = `${layerKey}-${Date.now()}`;
    pendingTests.current.set(requestId, layerKey);
    setTestStatus(prev => ({ ...prev, [layerKey]: { status: 'testing' } }));
    ws.send(JSON.stringify({
      command: 'test_model_config',
      requestId,
      config: { base_url: cfg.base_url, api_key: cfg.api_key, model: cfg.model },
    }));
  }, [ws]);

  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'test_model_result' && msg.payload?.requestId) {
          const layerKey = pendingTests.current.get(msg.payload.requestId);
          pendingTests.current.delete(msg.payload.requestId);
          if (layerKey) {
            setTestStatus(prev => ({
              ...prev,
              [layerKey]: {
                status: msg.payload.ok ? 'ok' : 'fail',
                error: msg.payload.error || '',
              },
            }));
          }
        }
      } catch { /* ignore non-json */ }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws]);

  // Folder picker state: which field is being picked
  type PickerTarget = 'workspace' | 'codebases' | 'datasets' | 'checkpoints' | 'mainTex' | null;
  const [pickerTarget, setPickerTarget] = useState<PickerTarget>(null);
  const openPicker = (target: PickerTarget) => setPickerTarget(target);
  const onPickerSelect = (path: string) => {
    if (pickerTarget === 'workspace') setWorkspaceDir(path);
    else if (pickerTarget === 'codebases') setCodebasesPath(path);
    else if (pickerTarget === 'datasets') setDatasetsPath(path);
    else if (pickerTarget === 'checkpoints') setCheckpointsPath(path);
    else if (pickerTarget === 'mainTex') setMainTexFile(path);
    setPickerTarget(null);
  };
  const referenceFileInputRef = useRef<HTMLInputElement | null>(null);
  const latexFileInputRef = useRef<HTMLInputElement | null>(null);
  const { t, locale } = useLocale();

  const modeInfo: Record<SubmitMode, { label: string; icon: string; placeholder: string; desc: string }> = {
    lab: {
      label: t('project.mode.lab'),
      icon: '🔬',
      placeholder: t('project.placeholder.lab'),
      desc: t('project.mode.lab_desc'),
    },
    reproduce: {
      label: t('project.mode.reproduce'),
      icon: '📄',
      placeholder: t('project.placeholder.reproduce'),
      desc: t('project.mode.reproduce_desc'),
    },
  };

  const info = modeInfo[mode];
  const [showModeHelp, setShowModeHelp] = useState(false);

  const arrayBufferToBase64 = (buffer: ArrayBuffer) => {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
  };

  const pickReferenceFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []).filter((file) =>
      file.name.toLowerCase().endsWith('.pdf') || file.type === 'application/pdf'
    );
    if (files.length === 0) return;

    const uploads = await Promise.all(files.map(async (file) => ({
      key: `${file.name}:${file.size}:${file.lastModified}`,
      name: file.name,
      contentBase64: arrayBufferToBase64(await file.arrayBuffer()),
    })));

    setReferenceFiles((prev) => {
      const existing = new Set(prev.map((item) => item.key));
      return [...prev, ...uploads.filter((item) => !existing.has(item.key))];
    });
    e.target.value = '';
  };

  const removeReferenceFile = (key: string) => {
    setReferenceFiles((prev) => prev.filter((item) => item.key !== key));
  };

  const pickLatexFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []).filter((file) =>
      LATEX_EXTS.some(ext => file.name.toLowerCase().endsWith(ext))
    );
    if (files.length === 0) return;
    const uploads = await Promise.all(files.map(async (file) => ({
      key: `${file.name}:${file.size}:${file.lastModified}`,
      name: file.name,
      contentBase64: arrayBufferToBase64(await file.arrayBuffer()),
    })));
    setLatexFiles((prev) => {
      const existing = new Set(prev.map((item) => item.key));
      return [...prev, ...uploads.filter((item) => !existing.has(item.key))];
    });
    e.target.value = '';
  };

  const removeLatexFile = (key: string) => {
    setLatexFiles((prev) => prev.filter((item) => item.key !== key));
  };

  const submit = () => {
    const text = topicInput.trim();
    if (!text) return;
    const angles = mode === 'lab'
      ? anglesInput.split(/[,，、;；]/).map(s => s.trim()).filter(Boolean)
      : [];
    const paths: { codebases?: string; datasets?: string; checkpoints?: string } = {};
    if (codebasesPath.trim()) paths.codebases = codebasesPath.trim();
    if (datasetsPath.trim()) paths.datasets = datasetsPath.trim();
    if (checkpointsPath.trim()) paths.checkpoints = checkpointsPath.trim();
    const lm: Record<string, { base_url: string; api_key: string; model: string }> = {};
    for (const [key, cfg] of [['idea', ideaLM], ['experiment', experimentLM], ['coding', codingLM], ['execution', executionLM], ['writing', writingLM]] as const) {
      if (lmHasValue(cfg)) lm[key] = { base_url: cfg.base_url.trim(), api_key: cfg.api_key.trim(), model: cfg.model.trim() };
    }
    onQuickSubmit(
      text,
      mode,
      angles,
      refPapersInput.trim(),
      referenceFiles.map(({ name, contentBase64 }) => ({ name, contentBase64 })),
      paths,
      latexFiles.map(({ name, contentBase64 }) => ({ name, contentBase64 })),
      workspaceDir.trim(),
      mainTexFile.trim(),
      lm,
    );
    setTopicInput('');
    setAnglesInput('');
    setRefPapersInput('');
    setReferenceFiles([]);
    setLatexFiles([]);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  };

  const sorted = [...projects].sort((a, b) => {
    const o: Record<string, number> = { running: 0, queued: 1, interrupted: 2, new: 3, completed: 4 };
    return (o[a.status] ?? 5) - (o[b.status] ?? 5);
  });

  const running = projects.filter(p => p.status === 'running').length;
  const interrupted = projects.filter(p => p.status === 'interrupted').length;

  const goLabel = t('project.go_submit');

  const toggleExpand = (projectId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setExpandedId(prev => prev === projectId ? null : projectId);
  };

  return (
    <>
    {pickerTarget && (
      <FolderPicker
        ws={ws}
        title={
          pickerTarget === 'workspace'   ? '选择工作区文件夹' :
          pickerTarget === 'codebases'   ? '选择代码库文件夹' :
          pickerTarget === 'datasets'    ? '选择数据集文件夹' :
          pickerTarget === 'checkpoints' ? '选择模型权重文件夹' :
          '选择主 .tex 文件'
        }
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
          <div className="submit-section">
            <div className="mode-row">
              <div className="mode-selector">
                <button
                  className={`mode-btn ${mode === 'lab' && discussionMode ? 'active' : ''}`}
                  onClick={() => { setMode('lab'); if (!discussionMode) onToggleDiscussion(); }}
                  title={t('discussion.hint_on')}
                >
                  🔬 {t('project.mode.lab_discuss')}
                </button>
                <button
                  className={`mode-btn ${mode === 'lab' && !discussionMode ? 'active' : ''}`}
                  onClick={() => { setMode('lab'); if (discussionMode) onToggleDiscussion(); }}
                  title={t('discussion.hint_off')}
                >
                  🔬 {t('project.mode.lab_independent')}
                </button>
                <button
                  className={`mode-btn ${mode === 'reproduce' ? 'active' : ''}`}
                  onClick={() => setMode('reproduce')}
                >
                  📄 {modeInfo.reproduce.label}
                </button>
              </div>
              <div className="mode-help-wrapper">
                <button className="mode-info-btn" type="button" onClick={() => setShowModeHelp(!showModeHelp)}>?</button>
                {showModeHelp && (
                  <div className="mode-help-popup">
                    {t('project.mode.help')}
                  </div>
                )}
              </div>
            </div>

            <div className="project-input-stack">
              <textarea
                className="project-topic-input"
                placeholder={info.placeholder}
                value={topicInput}
                onChange={e => setTopicInput(e.target.value)}
                onKeyDown={onKey}
                rows={2}
                disabled={!connected}
              />
              {mode === 'lab' && (
                <div className="project-angles-row">
                  <input
                    className="project-angles-input"
                    placeholder={t('project.angles_placeholder')}
                    value={anglesInput}
                    onChange={e => setAnglesInput(e.target.value)}
                    onKeyDown={onKey}
                    disabled={!connected}
                  />
                </div>
              )}
              <div className="project-ref-papers-toggle">
                <button
                  className="ref-papers-toggle-btn"
                  type="button"
                  onClick={() => setShowPaths(!showPaths)}
                >
                  {showPaths ? '▾' : '▸'} {t('paths.toggle')}
                  {(workspaceDir.trim() || refPapersInput.trim() || referenceFiles.length > 0 || latexFiles.length > 0 || codebasesPath.trim() || datasetsPath.trim() || checkpointsPath.trim()) && (
                    <span className="ref-badge">
                      {[
                        workspaceDir.trim() ? 'ws' : '',
                        refPapersInput.trim() || referenceFiles.length > 0 ? 'refs' : '',
                        latexFiles.length > 0 ? 'tex' : '',
                        codebasesPath,
                        datasetsPath,
                        checkpointsPath,
                      ].filter(s => s.trim()).length}
                    </span>
                  )}
                </button>
              </div>
              {showPaths && (
                <div className="paths-grid">
                  {/* ── Workspace folder ── */}
                  <label className="path-field workspace-field">
                    <span className="path-label workspace-label">
                      📁 {t('project.workspace_label')}
                    </span>
                    <div className="workspace-hint">{t('project.workspace_hint')}</div>
                    <div className="path-input-with-action">
                      <span className={`path-selected-value ${!workspaceDir ? 'placeholder' : ''}`}>
                        {workspaceDir || t('project.workspace_placeholder')}
                      </span>
                      <button
                        className="path-action-btn"
                        type="button"
                        onClick={() => openPicker('workspace')}
                        disabled={!connected}
                        title="浏览文件夹"
                      >+</button>
                      {workspaceDir && (
                        <button
                          className="path-clear-btn"
                          type="button"
                          onClick={() => { setWorkspaceDir(''); setMainTexFile(''); }}
                        >×</button>
                      )}
                    </div>
                    {workspaceDir.trim() && (
                      <div className="path-input-with-action main-tex-row">
                        <span className={`path-selected-value ${!mainTexFile ? 'placeholder' : ''}`}>
                          {mainTexFile || t('project.workspace_main_tex_placeholder')}
                        </span>
                        <button
                          className="path-action-btn"
                          type="button"
                          onClick={() => openPicker('mainTex')}
                          disabled={!connected}
                          title="浏览 .tex 文件"
                        >+</button>
                        {mainTexFile && (
                          <button
                            className="path-clear-btn"
                            type="button"
                            onClick={() => setMainTexFile('')}
                          >×</button>
                        )}
                      </div>
                    )}
                  </label>
                  <label className="path-field">
                    <span className="path-label">{t('project.ref_papers_label')}</span>
                    <div className="path-input-with-action">
                      <input
                        className="path-input"
                        placeholder={t('project.ref_papers_placeholder')}
                        value={refPapersInput}
                        onChange={e => setRefPapersInput(e.target.value)}
                        disabled={!connected}
                      />
                      <button
                        className="path-action-btn"
                        type="button"
                        title={t('project.ref_papers_pick_pdf')}
                        onClick={() => referenceFileInputRef.current?.click()}
                        disabled={!connected}
                      >
                        +
                      </button>
                      <input
                        ref={referenceFileInputRef}
                        className="hidden-file-input"
                        type="file"
                        accept=".pdf,application/pdf"
                        multiple
                        onChange={(e) => { void pickReferenceFiles(e); }}
                        disabled={!connected}
                      />
                    </div>
                    {referenceFiles.length > 0 && (
                      <div className="selected-file-list">
                        {referenceFiles.map((file) => (
                          <span key={file.key} className="selected-file-chip">
                            <span className="selected-file-name">{file.name}</span>
                            <button
                              className="selected-file-remove"
                              type="button"
                              title={t('project.ref_papers_remove_pdf')}
                              onClick={() => removeReferenceFile(file.key)}
                            >
                              ×
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </label>
                  <label className="path-field">
                    <span className="path-label">{t('project.latex_label')}</span>
                    <div className="path-input-with-action">
                      <span className="path-hint">{t('project.latex_hint')}</span>
                      <button
                        className="path-action-btn latex-upload-btn"
                        type="button"
                        title={t('project.latex_pick')}
                        onClick={() => latexFileInputRef.current?.click()}
                        disabled={!connected}
                      >
                        TeX
                      </button>
                      <input
                        ref={latexFileInputRef}
                        className="hidden-file-input"
                        type="file"
                        accept=".tex,.bib,.sty,.cls,.bst"
                        multiple
                        onChange={(e) => { void pickLatexFiles(e); }}
                        disabled={!connected}
                      />
                    </div>
                    {latexFiles.length > 0 && (
                      <div className="selected-file-list">
                        {latexFiles.map((file) => (
                          <span key={file.key} className="selected-file-chip latex-chip">
                            <span className="latex-chip-icon">TeX</span>
                            <span className="selected-file-name">{file.name}</span>
                            <button
                              className="selected-file-remove"
                              type="button"
                              title={t('project.latex_remove')}
                              onClick={() => removeLatexFile(file.key)}
                            >×</button>
                          </span>
                        ))}
                      </div>
                    )}
                  </label>
                  {[
                    { key: 'codebases', label: t('paths.codebases'), val: codebasesPath, set: setCodebasesPath, placeholder: t('paths.codebases_placeholder') },
                    { key: 'datasets',  label: t('paths.datasets'),  val: datasetsPath,  set: setDatasetsPath,  placeholder: t('paths.datasets_placeholder') },
                    { key: 'checkpoints', label: t('paths.checkpoints'), val: checkpointsPath, set: setCheckpointsPath, placeholder: t('paths.checkpoints_placeholder') },
                  ].map(({ key, label, val, set, placeholder }) => (
                    <label key={key} className="path-field">
                      <span className="path-label">{label}</span>
                      <div className="path-input-with-action">
                        <span className={`path-selected-value ${!val ? 'placeholder' : ''}`}>
                          {val || placeholder}
                        </span>
                        <button
                          className="path-action-btn"
                          type="button"
                          onClick={() => openPicker(key as 'codebases' | 'datasets' | 'checkpoints')}
                          disabled={!connected}
                          title="浏览文件夹"
                        >+</button>
                        {val && (
                          <button
                            className="path-clear-btn"
                            type="button"
                            onClick={() => set('')}
                          >×</button>
                        )}
                      </div>
                    </label>
                  ))}
                </div>
              )}
              <div className="layer-models-toggle">
                <button
                  className="ref-papers-toggle-btn"
                  type="button"
                  onClick={() => setShowLayerModels(!showLayerModels)}
                >
                  {showLayerModels ? '▾' : '▸'} {t('layer_models.toggle')}
                  {[ideaLM, experimentLM, codingLM, executionLM, writingLM].some(lmHasValue) && (
                    <span className="ref-badge">
                      {[ideaLM, experimentLM, codingLM, executionLM, writingLM].filter(lmHasValue).length}
                    </span>
                  )}
                </button>
              </div>
              {showLayerModels && (
                <div className="layer-models-grid">
                  {([
                    { key: 'idea',       cfg: ideaLM,       set: setIdeaLM },
                    { key: 'experiment', cfg: experimentLM, set: setExperimentLM },
                    { key: 'coding',     cfg: codingLM,     set: setCodingLM },
                    { key: 'execution',  cfg: executionLM,  set: setExecutionLM },
                    { key: 'writing',    cfg: writingLM,    set: setWritingLM },
                  ] as const).map(({ key, cfg, set }) => {
                    const ts = testStatus[key] || { status: 'idle' as TestStatus };
                    return (
                    <div key={key} className="layer-model-group">
                      <div className="layer-model-header">
                        <span className="layer-model-label">{t(`layer_models.${key}`)}</span>
                        <div className="layer-model-test-area">
                          {ts.status === 'ok' && <span className="lm-test-badge lm-test-ok" title="">{t('layer_models.test_ok')}</span>}
                          {ts.status === 'fail' && <span className="lm-test-badge lm-test-fail" title={ts.error || ''}>{t('layer_models.test_fail')}</span>}
                          {ts.status === 'testing' && <span className="lm-test-badge lm-test-ing">{t('layer_models.testing')}</span>}
                          <button
                            className="lm-test-btn"
                            type="button"
                            onClick={() => testModel(key, cfg)}
                            disabled={!connected || !lmHasValue(cfg) || ts.status === 'testing'}
                          >
                            {t('layer_models.test')}
                          </button>
                        </div>
                      </div>
                      <div className="layer-model-fields">
                        <input
                          className="layer-model-input lm-url"
                          placeholder={t('layer_models.base_url_placeholder')}
                          value={cfg.base_url}
                          onChange={e => set({ ...cfg, base_url: e.target.value })}
                          disabled={!connected}
                          title={t('layer_models.base_url')}
                        />
                        <input
                          className="layer-model-input lm-key"
                          type="password"
                          placeholder={t('layer_models.api_key_placeholder')}
                          value={cfg.api_key}
                          onChange={e => set({ ...cfg, api_key: e.target.value })}
                          disabled={!connected}
                          title={t('layer_models.api_key')}
                        />
                        <input
                          className="layer-model-input lm-model"
                          placeholder={t('layer_models.model_placeholder')}
                          value={cfg.model}
                          onChange={e => set({ ...cfg, model: e.target.value })}
                          disabled={!connected}
                          title={t('layer_models.model')}
                        />
                      </div>
                    </div>
                    );
                  })}
                </div>
              )}
              <button
                className={`project-go-btn ${mode === 'lab' ? 'lab-mode' : 'reproduce-mode'}`}
                onClick={submit}
                disabled={!connected || !topicInput.trim()}
              >
                {goLabel}
              </button>
            </div>
          </div>

          {sorted.length > 0 && <div className="panel-divider" />}

          {sorted.length === 0 ? (
            <div className="project-empty">
              {mode === 'lab' ? t('project.empty_lab') : t('project.empty_reproduce')}
            </div>
          ) : (
            <div className="project-list-inner">
              {sorted.map(proj => {
                const cfg = STATUS_ICONS[proj.status] || STATUS_ICONS.new;
                const statusLabel = t(`project.status.${proj.status}`);
                const fs = proj.firstStage || 1;
                const completedCount = proj.lastCompletedStage >= fs ? proj.lastCompletedStage - fs + 1 : 0;
                const pct = proj.totalStages > 0
                  ? Math.round((completedCount / proj.totalStages) * 100)
                  : 0;
                const isSelected = selectedProjectId === proj.projectId;
                const isExpanded = expandedId === proj.projectId;
                const arts = artifactsByProject[proj.projectId] || [];

                return (
                  <div
                    key={proj.projectId}
                    className={`project-card project-${proj.status}${isSelected ? ' selected' : ''}${isExpanded ? ' expanded' : ''}`}
                  >
                    <div className="project-summary" onClick={(e) => { onSelect(proj.projectId); toggleExpand(proj.projectId, e); }}>
                      <span className="project-status-dot" style={{ background: cfg.color }} title={statusLabel}>{cfg.icon}</span>
                      <span className="project-summary-name" title={proj.topic || proj.projectId}>
                        {proj.topic || proj.projectId}
                      </span>
                      <span className="project-summary-progress">{pct}%</span>
                      {arts.length > 0 && <span className="project-summary-arts">📦{arts.length}</span>}
                      {proj.intervention && <span className="project-intervention-icon" title={t('project.intervention_needed')}>⚠</span>}
                      <span className={`project-expand-arrow${isExpanded ? ' open' : ''}`}>▸</span>
                    </div>

                    {/* Mini progress bar */}
                    <div className="project-mini-bar" onClick={(e) => toggleExpand(proj.projectId, e)}>
                      <div className="project-mini-fill" style={{ width: `${pct}%`, background: cfg.color }} />
                    </div>

                    {isExpanded && (
                      <div className="project-detail">
                        <div className="project-detail-meta">
                          <span className="project-id-label" title={proj.projectId}>{proj.projectId}</span>
                          <span className="project-status-badge" style={{ background: cfg.color }}>
                            {cfg.icon} {statusLabel}
                          </span>
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

                        {/* Artifacts by repo — hidden for now */}

                        {proj.intervention && (
                          <div className="project-intervention-box">
                            <div className="project-intervention-title">⚠ {t('project.intervention_needed')}</div>
                            <pre className="project-intervention-detail">{proj.intervention}</pre>
                          </div>
                        )}

                        {/* Actions */}
                        <div className="project-detail-actions">
                          {proj.status === 'interrupted' && connected && (
                            <button className="project-resume-btn" onClick={(e) => { e.stopPropagation(); onResume(proj.projectId); }}>
                              {t('project.resume')}
                            </button>
                          )}
                          {(proj.status === 'running' || proj.status === 'queued') && connected && (
                            <button className="project-pause-btn" onClick={(e) => { e.stopPropagation(); onPause(proj.projectId); }}>
                              {t('project.pause')}
                            </button>
                          )}
                          {connected && (
                            <button className="project-restart-btn" onClick={(e) => {
                              e.stopPropagation();
                              if (window.confirm(t('project.restart_confirm', { id: proj.projectId }))) {
                                onRestart(proj.projectId);
                              }
                            }}>
                              {t('project.restart')}
                            </button>
                          )}
                          <button
                            className="project-delete-btn"
                            title={t('project.delete_title')}
                            onClick={(e) => {
                              e.stopPropagation();
                              if (window.confirm(t('project.delete_confirm', { id: proj.projectId }))) {
                                onDelete(proj.projectId);
                              }
                            }}
                          >
                            {t('project.delete')}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
    </>
  );
}
