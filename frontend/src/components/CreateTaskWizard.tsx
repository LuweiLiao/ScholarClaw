import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CreateTaskPayload, LayerModelCfg, SubmitMode, ReferencePdfUpload, LatexFileUpload } from '../types';
import FolderPicker from './FolderPicker';

const LAYER_KEYS = ['idea', 'experiment', 'coding', 'execution', 'writing'] as const;
const LATEX_EXTS = ['.tex', '.bib', '.sty', '.cls', '.bst'];

type TestStatus = 'idle' | 'testing' | 'ok' | 'fail';
type PickerTarget = 'workspace' | 'codebases' | 'datasets' | 'checkpoints' | 'mainTex' | null;

interface Props {
  ws: WebSocket | null;
  connected: boolean;
  discussionMode: boolean;
  t: (key: string, vars?: Record<string, string | number>) => string;
  onToggleDiscussion: () => void;
  onSubmit: (payload: CreateTaskPayload) => void;
  onClose: () => void;
}

const emptyLM = (): LayerModelCfg => ({ base_url: '', api_key: '', model: '' });
const hasLM = (cfg: LayerModelCfg) => !!(cfg.base_url || cfg.api_key || cfg.model);

function arrayBufferToBase64(buffer: ArrayBuffer) {
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

export default function CreateTaskWizard({
  ws,
  connected,
  discussionMode,
  t,
  onToggleDiscussion,
  onSubmit,
  onClose,
}: Props) {
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState<SubmitMode>('lab');
  const [topic, setTopic] = useState('');
  const [anglesInput, setAnglesInput] = useState('');
  const [referencePapers, setReferencePapers] = useState('');
  const [referenceFiles, setReferenceFiles] = useState<Array<ReferencePdfUpload & { key: string }>>([]);
  const [latexFiles, setLatexFiles] = useState<Array<LatexFileUpload & { key: string }>>([]);
  const [workspaceDir, setWorkspaceDir] = useState('');
  const [mainTexFile, setMainTexFile] = useState('');
  const [codebasesPath, setCodebasesPath] = useState('');
  const [datasetsPath, setDatasetsPath] = useState('');
  const [checkpointsPath, setCheckpointsPath] = useState('');
  const [approvalMode, setApprovalMode] = useState(() => {
    try { return localStorage.getItem('scholar-approval-mode') || 'auto'; } catch { return 'auto'; }
  });

  const [globalLLM, setGlobalLLM] = useState<LayerModelCfg>(() => {
    try {
      const saved = localStorage.getItem('scholar-global-llm');
      if (saved) return JSON.parse(saved);
    } catch { /* ignore */ }
    return emptyLM();
  });
  const [globalLLMTest, setGlobalLLMTest] = useState<{ status: TestStatus; error?: string }>({ status: 'idle' });
  const globalTestPending = useRef('');

  const [layerModels, setLayerModels] = useState<Record<string, LayerModelCfg>>(
    Object.fromEntries(LAYER_KEYS.map(k => [k, emptyLM()])),
  );
  const [testStatus, setTestStatus] = useState<Record<string, { status: TestStatus; error?: string }>>({});
  const pendingTests = useRef<Map<string, string>>(new Map());
  const requestSeq = useRef(0);
  const [pickerTarget, setPickerTarget] = useState<PickerTarget>(null);
  const referenceFileInputRef = useRef<HTMLInputElement | null>(null);
  const latexFileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!ws) return;
    const handler = (ev: MessageEvent) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type !== 'test_model_result' || !msg.payload?.requestId) return;
        const rid = msg.payload.requestId as string;
        if (rid === globalTestPending.current) {
          globalTestPending.current = '';
          setGlobalLLMTest({ status: msg.payload.ok ? 'ok' : 'fail', error: msg.payload.error || '' });
          return;
        }
        const layerKey = pendingTests.current.get(rid);
        pendingTests.current.delete(rid);
        if (layerKey) {
          setTestStatus(prev => ({
            ...prev,
            [layerKey]: { status: msg.payload.ok ? 'ok' : 'fail', error: msg.payload.error || '' },
          }));
        }
      } catch {
        // Ignore messages from other websocket features.
      }
    };
    ws.addEventListener('message', handler);
    return () => ws.removeEventListener('message', handler);
  }, [ws]);

  const steps = [
    t('wizard.step.basic'),
    t('wizard.step.resources'),
    t('wizard.step.models'),
    t('wizard.step.run'),
  ];

  const researchAngles = useMemo(
    () => mode === 'lab'
      ? anglesInput.split(/[,，、;；]/).map(s => s.trim()).filter(Boolean)
      : [],
    [anglesInput, mode],
  );

  const updateGlobalLLM = (patch: Partial<LayerModelCfg>) => {
    setGlobalLLM(prev => ({ ...prev, ...patch }));
    setGlobalLLMTest({ status: 'idle' });
  };

  const testModelConfig = useCallback((requestId: string, cfg: LayerModelCfg) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      command: 'test_model_config',
      requestId,
      config: { base_url: cfg.base_url, api_key: cfg.api_key, model: cfg.model },
    }));
  }, [ws]);

  const testGlobalLLM = () => {
    if (!globalLLM.base_url && !globalLLM.model) return;
    requestSeq.current += 1;
    const requestId = `wizard-global-${requestSeq.current}`;
    globalTestPending.current = requestId;
    setGlobalLLMTest({ status: 'testing' });
    testModelConfig(requestId, globalLLM);
  };

  const saveGlobalLLM = () => {
    localStorage.setItem('scholar-global-llm', JSON.stringify(globalLLM));
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ command: 'set_global_llm', config: globalLLM }));
    }
  };

  const testLayerModel = (key: string, cfg: LayerModelCfg) => {
    if (!cfg.base_url && !cfg.model) return;
    requestSeq.current += 1;
    const requestId = `wizard-${key}-${requestSeq.current}`;
    pendingTests.current.set(requestId, key);
    setTestStatus(prev => ({ ...prev, [key]: { status: 'testing' } }));
    testModelConfig(requestId, cfg);
  };

  const pickReferenceFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []).filter(file =>
      file.name.toLowerCase().endsWith('.pdf') || file.type === 'application/pdf',
    );
    const uploads = await Promise.all(files.map(async file => ({
      key: `${file.name}:${file.size}:${file.lastModified}`,
      name: file.name,
      contentBase64: arrayBufferToBase64(await file.arrayBuffer()),
    })));
    setReferenceFiles(prev => {
      const existing = new Set(prev.map(item => item.key));
      return [...prev, ...uploads.filter(item => !existing.has(item.key))];
    });
    e.target.value = '';
  };

  const pickLatexFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []).filter(file =>
      LATEX_EXTS.some(ext => file.name.toLowerCase().endsWith(ext)),
    );
    const uploads = await Promise.all(files.map(async file => ({
      key: `${file.name}:${file.size}:${file.lastModified}`,
      name: file.name,
      contentBase64: arrayBufferToBase64(await file.arrayBuffer()),
    })));
    setLatexFiles(prev => {
      const existing = new Set(prev.map(item => item.key));
      return [...prev, ...uploads.filter(item => !existing.has(item.key))];
    });
    e.target.value = '';
  };

  const onPickerSelect = (path: string) => {
    if (pickerTarget === 'workspace') setWorkspaceDir(path);
    else if (pickerTarget === 'codebases') setCodebasesPath(path);
    else if (pickerTarget === 'datasets') setDatasetsPath(path);
    else if (pickerTarget === 'checkpoints') setCheckpointsPath(path);
    else if (pickerTarget === 'mainTex') setMainTexFile(path);
    setPickerTarget(null);
  };

  const submit = () => {
    const cleanLayerModels = Object.fromEntries(
      Object.entries(layerModels)
        .filter(([, cfg]) => hasLM(cfg))
        .map(([key, cfg]) => [key, {
          base_url: cfg.base_url.trim(),
          api_key: cfg.api_key.trim(),
          model: cfg.model.trim(),
        }]),
    );
    if (Object.keys(cleanLayerModels).length === 0) {
      alert(t('wizard.no_models_configured') || '请至少配置一个 LLM 模型');
      return;
    }
    localStorage.setItem('scholar-approval-mode', approvalMode);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ command: 'set_approval_mode', mode: approvalMode }));
    }
    saveGlobalLLM();
    onSubmit({
      topic: topic.trim(),
      mode,
      researchAngles,
      referencePapers: referencePapers.trim(),
      referenceFiles: referenceFiles.map(({ name, contentBase64 }) => ({ name, contentBase64 })),
      latexFiles: latexFiles.map(({ name, contentBase64 }) => ({ name, contentBase64 })),
      workspaceDir: workspaceDir.trim(),
      mainTexFile: mainTexFile.trim(),
      paths: {
        codebases: codebasesPath.trim() || undefined,
        datasets: datasetsPath.trim() || undefined,
        checkpoints: checkpointsPath.trim() || undefined,
      },
      layerModels: cleanLayerModels,
    });
    onClose();
  };

  return (
    <div className="wizard-overlay" onClick={onClose}>
      {pickerTarget && (
        <FolderPicker
          ws={ws}
          title={pickerTarget === 'mainTex' ? '选择主 .tex 文件' : '选择文件夹'}
          mode={pickerTarget === 'mainTex' ? 'file' : 'folder'}
          filterExts={pickerTarget === 'mainTex' ? ['.tex'] : undefined}
          initialPath={pickerTarget === 'mainTex' ? workspaceDir : undefined}
          onSelect={onPickerSelect}
          onClose={() => setPickerTarget(null)}
        />
      )}
      <div className="wizard-panel" onClick={e => e.stopPropagation()}>
        <div className="wizard-header">
          <div>
            <h2>{t('wizard.title')}</h2>
            <p>{t('wizard.subtitle')}</p>
          </div>
          <button className="wizard-close" onClick={onClose}>×</button>
        </div>

        <div className="wizard-steps">
          {steps.map((label, idx) => (
            <button
              key={label}
              type="button"
              className={`wizard-step ${idx === step ? 'active' : ''} ${idx < step ? 'done' : ''}`}
              onClick={() => setStep(idx)}
            >
              <span>{idx + 1}</span>{label}
            </button>
          ))}
        </div>

        <div className="wizard-body">
          {step === 0 && (
            <div className="wizard-section">
              <div className="wizard-mode-grid">
                <button className={mode === 'lab' ? 'active' : ''} onClick={() => setMode('lab')} type="button">
                  {discussionMode ? t('project.mode.lab_discuss') : t('project.mode.lab_independent')}
                </button>
                <button className={mode === 'reproduce' ? 'active' : ''} onClick={() => setMode('reproduce')} type="button">
                  {t('project.mode.reproduce')}
                </button>
              </div>
              {mode === 'lab' && (
                <button type="button" className="wizard-secondary" onClick={onToggleDiscussion}>
                  {discussionMode ? t('project.mode.lab_desc') : t('project.mode.lab_independent_desc')}
                </button>
              )}
              <label className="wizard-field">
                <span>{t('wizard.topic')}</span>
                <textarea value={topic} onChange={e => setTopic(e.target.value)} placeholder={mode === 'lab' ? t('project.placeholder.lab') : t('project.placeholder.reproduce')} />
              </label>
              {mode === 'lab' && (
                <label className="wizard-field">
                  <span>{t('wizard.angles')}</span>
                  <input value={anglesInput} onChange={e => setAnglesInput(e.target.value)} placeholder={t('project.angles_placeholder')} />
                </label>
              )}
            </div>
          )}

          {step === 1 && (
            <div className="wizard-section">
              <label className="wizard-field">
                <span>{t('project.ref_papers_label')}</span>
                <textarea value={referencePapers} onChange={e => setReferencePapers(e.target.value)} placeholder={t('project.ref_papers_placeholder')} />
              </label>
              <div className="wizard-action-row">
                <input ref={referenceFileInputRef} type="file" accept="application/pdf,.pdf" multiple hidden onChange={pickReferenceFiles} />
                <button type="button" onClick={() => referenceFileInputRef.current?.click()}>{t('project.ref_papers_pick_pdf')}</button>
                <input ref={latexFileInputRef} type="file" accept={LATEX_EXTS.join(',')} multiple hidden onChange={pickLatexFiles} />
                <button type="button" onClick={() => latexFileInputRef.current?.click()}>{t('project.latex_pick')}</button>
              </div>
              {[...referenceFiles, ...latexFiles].length > 0 && (
                <div className="wizard-file-list">
                  {referenceFiles.map(f => <span key={f.key}>{f.name}</span>)}
                  {latexFiles.map(f => <span key={f.key}>{f.name}</span>)}
                </div>
              )}
              {([
                ['workspace', t('project.workspace_label'), workspaceDir, setWorkspaceDir, t('project.workspace_placeholder')],
                ['codebases', 'Codebases', codebasesPath, setCodebasesPath, 'D:/codebases'],
                ['datasets', 'Datasets', datasetsPath, setDatasetsPath, 'D:/datasets'],
                ['checkpoints', 'Checkpoints', checkpointsPath, setCheckpointsPath, 'D:/checkpoints'],
              ] as const).map(([target, label, value, setter, placeholder]) => (
                <label key={target} className="wizard-field">
                  <span>{label}</span>
                  <div className="wizard-input-action">
                    <input value={value} onChange={e => setter(e.target.value)} placeholder={placeholder} />
                    <button type="button" onClick={() => setPickerTarget(target)}>{t('planner.browse')}</button>
                  </div>
                </label>
              ))}
              <label className="wizard-field">
                <span>{t('planner.main_tex')}</span>
                <div className="wizard-input-action">
                  <input value={mainTexFile} onChange={e => setMainTexFile(e.target.value)} placeholder={t('planner.select_tex')} />
                  <button type="button" onClick={() => setPickerTarget('mainTex')} disabled={!workspaceDir}>{t('planner.browse')}</button>
                </div>
              </label>
            </div>
          )}

          {step === 2 && (
            <div className="wizard-section">
              <div className="wizard-model-card">
                <h3>{t('global_llm.title')}</h3>
                <div className="layer-model-fields">
                  <input className="layer-model-input lm-url" value={globalLLM.base_url} onChange={e => updateGlobalLLM({ base_url: e.target.value })} placeholder={t('layer_models.base_url_placeholder')} />
                  <input className="layer-model-input lm-key" type="password" value={globalLLM.api_key} onChange={e => updateGlobalLLM({ api_key: e.target.value })} placeholder={t('layer_models.api_key_placeholder')} />
                  <input className="layer-model-input lm-model" value={globalLLM.model} onChange={e => updateGlobalLLM({ model: e.target.value })} placeholder={t('layer_models.model_placeholder')} />
                  <button type="button" className="lm-test-btn" onClick={testGlobalLLM}>
                    {globalLLMTest.status === 'idle' ? t('layer_models.test')
                      : globalLLMTest.status === 'testing' ? t('layer_models.testing')
                      : globalLLMTest.status === 'ok' ? t('layer_models.test_ok')
                      : t('layer_models.test_fail')}
                  </button>
                </div>
              </div>
              {LAYER_KEYS.map(key => {
                const cfg = layerModels[key] || emptyLM();
                const status = testStatus[key] || { status: 'idle' as TestStatus };
                return (
                  <div key={key} className="layer-model-group">
                    <div className="layer-model-header">
                      <span className="layer-model-label">{t(`layer_models.${key}`)}</span>
                      <button type="button" className="lm-test-btn" onClick={() => testLayerModel(key, cfg)} disabled={!hasLM(cfg) || status.status === 'testing'}>
                        {status.status === 'idle' ? t('layer_models.test') : status.status === 'testing' ? t('layer_models.testing') : status.status === 'ok' ? t('layer_models.test_ok') : t('layer_models.test_fail')}
                      </button>
                    </div>
                    <div className="layer-model-fields">
                      <input className="layer-model-input lm-url" value={cfg.base_url} onChange={e => setLayerModels(prev => ({ ...prev, [key]: { ...cfg, base_url: e.target.value } }))} placeholder={t('layer_models.base_url_placeholder')} />
                      <input className="layer-model-input lm-key" type="password" value={cfg.api_key} onChange={e => setLayerModels(prev => ({ ...prev, [key]: { ...cfg, api_key: e.target.value } }))} placeholder={t('layer_models.api_key_placeholder')} />
                      <input className="layer-model-input lm-model" value={cfg.model} onChange={e => setLayerModels(prev => ({ ...prev, [key]: { ...cfg, model: e.target.value } }))} placeholder={t('layer_models.model_placeholder')} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {step === 3 && (
            <div className="wizard-section">
              <label className="wizard-field">
                <span>{t('approval.mode')}</span>
                <select value={approvalMode} onChange={e => setApprovalMode(e.target.value)}>
                  <option value="auto">{t('approval.mode.auto')}</option>
                  <option value="confirm_writes">{t('approval.mode.confirm_writes')}</option>
                  <option value="confirm_all">{t('approval.mode.confirm_all')}</option>
                </select>
              </label>
              <div className="wizard-summary">
                <strong>{topic || t('wizard.topic_empty')}</strong>
                <span>{mode === 'lab' ? (discussionMode ? t('project.mode.lab_discuss') : t('project.mode.lab_independent')) : t('project.mode.reproduce')}</span>
                <span>{researchAngles.length > 0 ? researchAngles.join(', ') : t('wizard.no_angles')}</span>
                <span>{Object.values(layerModels).filter(hasLM).length} {t('wizard.layer_models_configured')}</span>
              </div>
            </div>
          )}
        </div>

        <div className="wizard-footer">
          <button type="button" onClick={() => setStep(s => Math.max(0, s - 1))} disabled={step === 0}>{t('wizard.prev')}</button>
          {step < steps.length - 1 ? (
            <button type="button" onClick={() => setStep(s => Math.min(steps.length - 1, s + 1))}>{t('wizard.next')}</button>
          ) : (
            <button
              type="button"
              className="wizard-submit"
              onClick={submit}
              disabled={!connected || !topic.trim() || !Object.values(layerModels).some(hasLM)}
            >
              {t('wizard.submit')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
