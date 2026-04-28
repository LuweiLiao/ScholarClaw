import { useReducer, useEffect, useCallback, useRef, useState, useMemo } from 'react';
import { AgentLayer, ALL_LAYERS, ALL_REPOS } from './types';
import type { AppState, WSMessage, ResourceStats, LobsterAgent, Artifact, ProjectScanResult, PlannerStatus, CoordinationSessionInfo, CenterTab, TaskGraphInfo, ProjectArchiveInfo, CreateTaskPayload } from './types';
import { INITIAL_AGENTS, createMockMessageGenerator } from './mock';
import LayerPanel from './components/LayerPanel';
import ResourceMonitor from './components/ResourceMonitor';
import LogPanel from './components/LogPanel';
import DataFlowArrow from './components/DataFlowArrow';
import DataShelf from './components/DataShelf';
import ProjectPanel from './components/ProjectPanel';
import CreateTaskWizard from './components/CreateTaskWizard';
import PlannerChat from './components/PlannerChat';
import AgentDialog from './components/AgentDialog';
import DiffViewer from './components/DiffViewer';
import ResizeHandle from './components/ResizeHandle';
import StageDetailPanel from './components/StageDetailPanel';
import ArtifactViewer from './components/ArtifactViewer';
import ConversationView from './components/ConversationView';
import CommandConsole from './components/CommandConsole';
import ApprovalDialog from './components/ApprovalDialog';
import TaskGraphView from './components/TaskGraphView';
import { LocaleContext, makeT } from './i18n';
import type { Locale } from './i18n';
import './App.css';

type Action =
  | WSMessage
  | { type: 'set_connected'; payload: boolean }
  | { type: 'set_mock'; payload: boolean }
  | { type: 'set_res_connected'; payload: boolean }
  | { type: 'clear_agents' }
  | { type: 'select_project'; payload: string | null }
  | { type: 'set_active_tab'; payload: CenterTab }
  | { type: 'set_approval_mode'; payload: 'auto' | 'confirm_writes' | 'confirm_all' }
  | { type: 'dismiss_approval'; payload: string };

function upsertAgent(agents: LobsterAgent[], payload: LobsterAgent): LobsterAgent[] {
  const idx = agents.findIndex((a) => a.id === payload.id);
  if (idx >= 0) {
    const prev = agents[idx];
    if (
      prev.status === payload.status &&
      prev.currentStage === payload.currentStage &&
      prev.currentTask === payload.currentTask
    ) {
      return agents;
    }
    const updated = [...agents];
    updated[idx] = { ...updated[idx], ...payload };
    return updated;
  }
  return [...agents, payload];
}

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case 'agent_update':
      return { ...state, agents: upsertAgent(state.agents, action.payload) };
    case 'stage_update':
      return {
        ...state,
        agents: state.agents.map((a) => {
          if (a.id !== action.payload.agentId) return a;
          return {
            ...a,
            stageProgress: { ...a.stageProgress, [action.payload.stage]: action.payload.status },
          };
        }),
      };
    case 'artifact_produced': {
      const a = action.payload;
      const key = `${a.projectId}:${a.stage ?? 0}:${a.filename}`;
      const exists = state.artifacts.some(
        (x) => `${x.projectId}:${x.stage ?? 0}:${x.filename}` === key,
      );
      if (exists) return state;
      return { ...state, artifacts: [a, ...state.artifacts] };
    }
    case 'resource_stats':
      return { ...state, resources: action.payload, resConnected: true };
    case 'log':
      return { ...state, logs: [...state.logs, action.payload] };
    case 'set_connected':
      return { ...state, connected: action.payload };
    case 'set_res_connected':
      return { ...state, resConnected: action.payload };
    case 'queue_update': {
      const prev = JSON.stringify(state.queues);
      const next = JSON.stringify(action.payload);
      return prev === next ? state : { ...state, queues: action.payload };
    }
    case 'project_list':
      return { ...state, projects: action.payload };
    case 'select_project':
      return { ...state, selectedProjectId: action.payload };
    case 'set_mock':
      return { ...state, mockMode: action.payload };
    case 'agent_removed':
      return { ...state, agents: state.agents.filter((a) => a.id !== action.payload.id) };
    case 'project_name':
      return {
        ...state,
        projects: state.projects.map((p) =>
          p.projectId === action.payload.projectId
            ? { ...p, projectName: action.payload.projectName }
            : p
        ),
      };
    case 'clear_agents':
      return { ...state, agents: [], artifacts: [], logs: [], queues: {}, activities: [] };
    case 'agent_activity': {
      const MAX_ACTIVITIES = 500;
      const acts = state.activities.length >= MAX_ACTIVITIES
        ? [...state.activities.slice(-MAX_ACTIVITIES + 1), action.payload]
        : [...state.activities, action.payload];
      return { ...state, activities: acts };
    }
    case 'approval_request':
      return { ...state, approvalRequests: [...state.approvalRequests, action.payload] };
    case 'dismiss_approval':
      return { ...state, approvalRequests: state.approvalRequests.filter(r => r.requestId !== action.payload) };
    case 'task_graph_update':
      return { ...state, taskGraph: action.payload as TaskGraphInfo };
    case 'set_active_tab':
      return { ...state, activeTab: action.payload };
    case 'set_approval_mode':
      return { ...state, approvalMode: action.payload };
    case 'system':
      return state;
    default:
      return state;
  }
}

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

const INITIAL_STATE: AppState = {
  agents: [],
  artifacts: [],
  logs: [],
  queues: {},
  chatMessages: [],
  projects: [],
  selectedProjectId: null,
  resources: null,
  resConnected: false,
  connected: false,
  mockMode: false,
  activities: [],
  activeTab: 'overview',
  approvalRequests: [],
  taskGraph: null,
  approvalMode: 'auto',
};

export default function App() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const [agentWsUrl] = useState(`${WS_PROTO}//${window.location.host}/ws/agents`);
  const [resWsUrl] = useState(`${WS_PROTO}//${window.location.host}/ws/resources`);
  const [discussionMode, setDiscussionMode] = useState(true);
  const [locale, setLocale] = useState<Locale>(() =>
    (localStorage.getItem('scholar-locale') as Locale) || 'en'
  );
  const [theme, setTheme] = useState<'dark' | 'light'>(() =>
    (localStorage.getItem('scholar-theme') as 'dark' | 'light') || 'dark'
  );
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('scholar-theme', theme);
  }, [theme]);
  const t = useMemo(() => makeT(locale), [locale]);
  const localeCtx = useMemo(() => ({
    locale, setLocale: (l: Locale) => { setLocale(l); localStorage.setItem('scholar-locale', l); }, t,
  }), [locale, t]);
  const agentWsRef = useRef<WebSocket | null>(null);
  const resWsRef = useRef<WebSocket | null>(null);
  const mockCleanup = useRef<(() => void) | null>(null);
  const firstListRef = useRef(false);
  const mockModeRef = useRef(false);

  // v2.0 state
  const [scanResult, setScanResult] = useState<ProjectScanResult | null>(null);
  const [plannerStatus, setPlannerStatus] = useState<PlannerStatus | null>(null);
  const [plannerProjectId, setPlannerProjectId] = useState<string>('');
  const [showPlanner, setShowPlanner] = useState(false);
  const [coordSessions, setCoordSessions] = useState<Record<string, CoordinationSessionInfo[]>>({});
  const [archives, setArchives] = useState<ProjectArchiveInfo[]>([]);
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [dialogAgentId, setDialogAgentId] = useState<string | null>(null);
  const [activeDiff, setActiveDiff] = useState<{ file: string; original: string; modified: string; timestamp?: number } | null>(null);
  const [activeStageDetail, setActiveStageDetail] = useState<{ projectId: string; stage: number } | null>(null);
  const [activeArtifact, setActiveArtifact] = useState<{ projectId: string; stage: number; filename: string; dir?: string } | null>(null);
  const [leftPanelWidth, setLeftPanelWidth] = useState(290);
  const [rightPanelWidth, setRightPanelWidth] = useState(340);

  mockModeRef.current = state.mockMode;

  // All WS/mock logic uses refs to avoid useCallback dependency cycles
  const dispatchMsg = useCallback((msg: WSMessage) => {
    if (firstListRef.current && msg.type === 'agent_update') {
      dispatch({ type: 'clear_agents' });
      firstListRef.current = false;
    }
    dispatch(msg);
  }, []);

  const doStopMock = useCallback(() => {
    if (mockCleanup.current) { mockCleanup.current(); mockCleanup.current = null; }
  }, []);

  const doStartMock = useCallback(() => {
    doStopMock();
    dispatch({ type: 'set_mock', payload: true });
    dispatch({ type: 'clear_agents' });
    INITIAL_AGENTS.forEach((a) => dispatch({ type: 'agent_update', payload: a }));
    mockCleanup.current = createMockMessageGenerator(dispatchMsg);
    dispatch({ type: 'set_connected', payload: true });
  }, [dispatchMsg, doStopMock]);

  // ── Agent Bridge WS ──
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnects = 0;
    let timer: ReturnType<typeof setTimeout>;

    function connect() {
      if (ws) { ws.close(); ws = null; }
      try {
        ws = new WebSocket(agentWsUrl);
        agentWsRef.current = ws;
        ws.onopen = () => {
          doStopMock();
          dispatch({ type: 'set_connected', payload: true });
          dispatch({ type: 'set_mock', payload: false });
          reconnects = 0;
          firstListRef.current = true;
          ws!.send(JSON.stringify({ command: 'list_agents' }));
          ws!.send(JSON.stringify({ command: 'list_archives' }));
          try {
            const saved = localStorage.getItem('scholar-global-llm');
            if (saved) {
              const cfg = JSON.parse(saved);
              if (cfg.model) ws!.send(JSON.stringify({ command: 'set_global_llm', config: cfg }));
            }
          } catch { /* ignore */ }
        };
        ws.onmessage = (e) => {
          try {
            const msg = JSON.parse(e.data);
            if (msg.type === 'project_scan_result') {
              setScanResult(msg.payload);
              if (msg.payload.existingProjectId) {
                setPlannerProjectId(msg.payload.existingProjectId);
              }
            } else if (msg.type === 'planner_status') {
              setPlannerStatus(msg.payload);
            } else if (msg.type === 'planner_proposals') {
              setPlannerStatus(prev => prev ? { ...prev, proposals: msg.payload.proposals, status: 'proposing' } : prev);
            } else if (msg.type === 'coordination_update') {
              setCoordSessions(prev => ({ ...prev, [msg.payload.projectId]: msg.payload.sessions }));
            } else if (msg.type === 'archive_list') {
              setArchives(msg.payload.archives || []);
            } else if (msg.type === 'diff_detail' && msg.payload?.data) {
              setActiveDiff(msg.payload.data);
            } else {
              dispatchMsg(msg);
            }
          } catch { /* ignore */ }
        };
        ws.onclose = () => {
          dispatch({ type: 'set_connected', payload: false });
          reconnects++;
          const delay = Math.min(3000 * Math.pow(1.5, reconnects), 20000);
          timer = setTimeout(connect, delay);
        };
        ws.onerror = () => ws?.close();
      } catch {
        dispatch({ type: 'set_connected', payload: false });
      }
    }
    connect();
    return () => { clearTimeout(timer); ws?.close(); ws = null; doStopMock(); };
  }, [agentWsUrl, dispatchMsg, doStopMock, doStartMock]);

  // ── Resource Monitor WS ──
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnects = 0;
    let timer: ReturnType<typeof setTimeout>;

    function connect() {
      if (ws) { ws.close(); ws = null; }
      try {
        ws = new WebSocket(resWsUrl);
        resWsRef.current = ws;
        ws.onopen = () => { dispatch({ type: 'set_res_connected', payload: true }); reconnects = 0; };
        ws.onmessage = (e) => {
          try {
            const msg = JSON.parse(e.data) as { type: string; payload: ResourceStats };
            if (msg.type === 'resource_stats') dispatch({ type: 'resource_stats', payload: msg.payload });
          } catch { /* ignore */ }
        };
        ws.onclose = () => {
          dispatch({ type: 'set_res_connected', payload: false });
          reconnects++;
          timer = setTimeout(connect, Math.min(2000 * Math.pow(1.5, reconnects), 15000));
        };
        ws.onerror = () => ws?.close();
      } catch {
        dispatch({ type: 'set_res_connected', payload: false });
      }
    }
    connect();
    return () => { clearTimeout(timer); ws?.close(); ws = null; };
  }, [resWsUrl]);

  const toggleDiscussionMode = () => {
    const next = !discussionMode;
    setDiscussionMode(next);
    const ws = agentWsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ command: 'set_discussion_mode', enabled: next }));
    }
  };

  const submitCreateTask = useCallback((payload: CreateTaskPayload) => {
    const ws = agentWsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        command: 'quick_submit',
        topic: payload.topic,
        mode: payload.mode,
        researchAngles: payload.researchAngles.length > 0 ? payload.researchAngles : undefined,
        referencePapers: payload.referencePapers || undefined,
        referenceFiles: payload.referenceFiles.length > 0 ? payload.referenceFiles : undefined,
        latexFiles: payload.latexFiles.length > 0 ? payload.latexFiles : undefined,
        workspaceDir: payload.workspaceDir || undefined,
        mainTexFile: payload.mainTexFile || undefined,
        codebasesDir: payload.paths?.codebases || undefined,
        datasetsDir: payload.paths?.datasets || undefined,
        checkpointsDir: payload.paths?.checkpoints || undefined,
        layerModels: Object.keys(payload.layerModels).length > 0 ? payload.layerModels : undefined,
      }));
    }
  }, []);

  // ── Memoized derived state ──
  const ideaAgents = useMemo(() => state.agents.filter((a) => a.layer === AgentLayer.IDEA), [state.agents]);
  const expAgents = useMemo(() => state.agents.filter((a) => a.layer === AgentLayer.EXPERIMENT), [state.agents]);
  const codeAgents = useMemo(() => state.agents.filter((a) => a.layer === AgentLayer.CODING), [state.agents]);
  const execAgents = useMemo(() => state.agents.filter((a) => a.layer === AgentLayer.EXECUTION), [state.agents]);
  const agentMap = useMemo(() => ({
    [AgentLayer.IDEA]: ideaAgents,
    [AgentLayer.EXPERIMENT]: expAgents,
    [AgentLayer.CODING]: codeAgents,
    [AgentLayer.EXECUTION]: execAgents,
  }), [ideaAgents, expAgents, codeAgents, execAgents]);

  const ideaLogs = useMemo(() => state.logs.filter((l) => l.layer === AgentLayer.IDEA), [state.logs]);
  const expLogs = useMemo(() => state.logs.filter((l) => l.layer === AgentLayer.EXPERIMENT), [state.logs]);
  const codeLogs = useMemo(() => state.logs.filter((l) => l.layer === AgentLayer.CODING), [state.logs]);
  const execLogs = useMemo(() => state.logs.filter((l) => l.layer === AgentLayer.EXECUTION), [state.logs]);
  const logMap = useMemo(() => ({
    [AgentLayer.IDEA]: ideaLogs,
    [AgentLayer.EXPERIMENT]: expLogs,
    [AgentLayer.CODING]: codeLogs,
    [AgentLayer.EXECUTION]: execLogs,
  }), [ideaLogs, expLogs, codeLogs, execLogs]);

  const artifactsByProject = useMemo(() => {
    const map: Record<string, Artifact[]> = {};
    for (const a of state.artifacts) {
      const pid = a.projectId || '_global';
      if (!map[pid]) map[pid] = [];
      map[pid].push(a);
    }
    return map;
  }, [state.artifacts]);

  const artifactsByRepo = useMemo(() => {
    const map: Record<string, Artifact[]> = {};
    for (const a of state.artifacts) {
      if (!map[a.repoId]) map[a.repoId] = [];
      map[a.repoId].push(a);
    }
    return map;
  }, [state.artifacts]);

  const workingCount = useMemo(() => state.agents.filter((a) => ['working', 'waiting_discussion', 'discussing'].includes(a.status)).length, [state.agents]);
  const errorCount = useMemo(() => state.agents.filter((a) => a.status === 'error').length, [state.agents]);

  const sendFeedback = useCallback((content: string, targetLayer?: string) => {
    const msg: ChatMessage = {
      id: `chat-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
      sender: 'human',
      content,
      timestamp: Date.now(),
      targetLayer: (targetLayer as ChatMessage['targetLayer']) || 'all',
    };
    dispatch({ type: 'add_chat_message', payload: msg });

    const ws = agentWsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        command: 'human_feedback',
        content,
        targetLayer: msg.targetLayer,
        messageId: msg.id,
      }));
    }

    if (mockModeRef.current) {
      setTimeout(() => {
        const ack = createMockFeedbackAck(targetLayer || 'all');
        dispatch({ type: 'add_chat_message', payload: ack });
      }, 300 + Math.random() * 500);

      setTimeout(() => {
        const analysis = createMockLLMAnalysis(content, targetLayer || 'all');
        dispatch({ type: 'add_chat_message', payload: analysis });
      }, 2500 + Math.random() * 3000);
    }
  }, []);

  return (
    <LocaleContext.Provider value={localeCtx}>
    <div className="app">
      <header className="app-header">
        <div className="header-left">
          <h1>🦞 {t('header.title')}</h1>
          <span className="header-subtitle">1.0.0</span>
        </div>
        <div className="header-stats">
          <span className="stat">Agent <b>{state.agents.length}</b></span>
          <span className="stat">{t('header.active')} <b>{workingCount}</b></span>
          <span className="stat">❌ <b>{errorCount}</b></span>
          <span className="stat">📦 <b>{state.artifacts.length}</b></span>
        </div>
        <div className="header-right">
          <button
            className="btn-sm create-task-btn"
            onClick={() => setShowCreateWizard(true)}
            disabled={!state.connected}
          >
            新建研究任务
          </button>
          <button
            className="btn-sm theme-toggle-btn"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            title={theme === 'dark' ? t('header.theme_light') : t('header.theme_dark')}
          >
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
          <button
            className="btn-sm lang-toggle-btn"
            onClick={() => localeCtx.setLocale(locale === 'zh' ? 'en' : 'zh')}
            title={locale === 'zh' ? 'Switch to English' : '切换到中文'}
          >
            {locale === 'zh' ? 'EN' : '中文'}
          </button>
        </div>
      </header>

      {showCreateWizard && (
        <CreateTaskWizard
          ws={agentWsRef.current}
          connected={state.connected}
          discussionMode={discussionMode}
          t={t}
          onToggleDiscussion={toggleDiscussionMode}
          onSubmit={submitCreateTask}
          onClose={() => setShowCreateWizard(false)}
        />
      )}

      <div className="main-layout">
        <div className="side-panel repo-panel" style={{ width: leftPanelWidth }}>
          <ProjectPanel
            ws={agentWsRef.current}
            projects={state.projects}
            archives={archives}
            connected={state.connected}
            selectedProjectId={state.selectedProjectId}
            artifactsByProject={artifactsByProject}
            onSelect={(projectId) => dispatch({ type: 'select_project', payload: projectId })}
            onResume={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'resume_project', projectId }));
              }
            }}
            onPause={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'pause_project', projectId }));
              }
            }}
            onRestart={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'restart_project', projectId }));
              }
            }}
            onDelete={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'delete_project', projectId }));
                if (state.selectedProjectId === projectId) {
                  dispatch({ type: 'select_project', payload: null });
                }
              }
            }}
            onOpenFolder={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'open_project_folder', projectId, target: 'auto' }));
              }
            }}
            onArchive={(projectId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'archive_project', projectId }));
              }
            }}
            onRestoreArchive={(archiveId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'restore_archive', archiveId, overwrite: true }));
              }
            }}
            onRefreshArchives={() => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'list_archives' }));
              }
            }}
            onUpdateLayerModels={(projectId, layerModels) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                  command: 'update_layer_models',
                  projectId,
                  layerModels,
                }));
              }
            }}
            scanResult={scanResult}
            coordSessions={coordSessions}
            onScanProject={(workspaceDir, mainTexFile) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                const pid = `proj-${Date.now().toString(36)}`;
                setPlannerProjectId(pid);
                ws.send(JSON.stringify({
                  command: 'scan_project',
                  workspaceDir,
                  mainTexFile,
                  projectId: pid,
                }));
              }
            }}
            onRestoreProject={(projectId) => {
              setPlannerProjectId(projectId);
              dispatch({ type: 'select_project', payload: projectId });
            }}
            onStartPlanning={(projectId, workspaceDir, mainTexFile, llmConfig) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                setPlannerProjectId(projectId);
                ws.send(JSON.stringify({
                  command: 'planner_start',
                  projectId,
                  workspaceDir,
                  mainTexFile,
                  llmConfig,
                }));
                setShowPlanner(true);
              }
            }}
          />
        </div>

        <ResizeHandle storageKey="left" defaultWidth={290} minWidth={200} maxWidth={500} side="left" onResize={setLeftPanelWidth} />

        <div className="pyramid-container">
          <div className="center-tabs">
            {(['overview', 'timeline', 'tasks'] as CenterTab[]).map(tab => (
              <button
                key={tab}
                className={`center-tab ${state.activeTab === tab ? 'center-tab--active' : ''}`}
                onClick={() => dispatch({ type: 'set_active_tab', payload: tab })}
              >
                {tab === 'overview' && '📊 '}
                {tab === 'timeline' && '⏱️ '}
                {tab === 'tasks' && '📋 '}
                {t(`tab.${tab}`)}
                {tab === 'timeline' && state.activities.length > 0 && (
                  <span className="center-tab-badge">{state.activities.length}</span>
                )}
              </button>
            ))}
          </div>

          {state.activeTab === 'overview' && (
            <>
              <ResourceMonitor stats={state.resources} connected={state.resConnected} />
              <div className="pyramid-wrapper">
                <div className="pyramid">
                  {ALL_LAYERS.map((layer, idx) => {
                    const hasWorking = agentMap[layer].some((a) => ['working', 'waiting_discussion', 'discussing'].includes(a.status));
                    return (
                      <div key={layer} className="pyramid-tier">
                        <LayerPanel
                          layer={layer}
                          agents={agentMap[layer]}
                          logs={logMap[layer]}
                          tierIndex={idx}
                          selectedProjectId={state.selectedProjectId}
                          activities={state.activities}
                          onAgentClick={(agentId) => setDialogAgentId(agentId)}
                          onStageClick={(stage) => {
                            const pid = state.selectedProjectId || state.projects?.[0]?.projectId;
                            if (pid) setActiveStageDetail({ projectId: pid, stage });
                          }}
                          onStopAgent={(agentId) => {
                            const ws = agentWsRef.current;
                            if (ws && ws.readyState === WebSocket.OPEN) {
                              ws.send(JSON.stringify({ command: 'stop_agent', agentId }));
                            }
                          }}
                        />
                        {idx < ALL_LAYERS.length - 1 && (
                          <DataFlowArrow active={hasWorking} />
                        )}
                      </div>
                    );
                  })}
                </div>
                <div className={`feedback-loop ${agentMap[AgentLayer.EXECUTION].some((a) => a.status === 'done') ? 'active' : ''}`}>
                  <div className="fb-line fb-bottom" />
                  <div className="fb-line fb-side"><div className="fb-pulse" /></div>
                  <div className="fb-line fb-top" />
                  <div className="fb-tip" />
                </div>
              </div>
            </>
          )}

          {state.activeTab === 'timeline' && (
            <ConversationView
              activities={state.activities}
              t={t}
            />
          )}

          {state.activeTab === 'tasks' && (
            <TaskGraphView
              taskGraph={state.taskGraph}
              t={t}
              ws={agentWsRef.current}
              selectedProjectId={state.selectedProjectId}
            />
          )}
        </div>

        <ResizeHandle storageKey="right" defaultWidth={340} minWidth={250} maxWidth={600} side="right" onResize={setRightPanelWidth} />

        <div className="side-panel log-panel" style={{ width: rightPanelWidth }}>
          <LogPanel logs={state.logs} />
          {/* <QueuePanel queues={state.queues} /> */}
          <div className="shelf-section">
            <div className="shelf-section-header">
              <span className="shelf-section-title">{t('header.shared_repo')}</span>
            </div>
            <div className="shelf-list">
              {ALL_REPOS.map((repoId) => (
                <DataShelf
                  key={repoId}
                  repoId={repoId}
                  artifacts={artifactsByRepo[repoId] || []}
                />
              ))}
            </div>
          </div>
        </div>
      </div>

      {showPlanner && (
        <PlannerChat
          ws={agentWsRef.current}
          projectId={plannerProjectId}
          plannerStatus={plannerStatus}
          t={t}
          onClose={() => setShowPlanner(false)}
          onConfirmed={() => {
            setShowPlanner(false);
            setPlannerStatus(null);
            setScanResult(null);
          }}
        />
      )}

      {activeDiff && (
        <DiffViewer diff={activeDiff} onClose={() => setActiveDiff(null)} />
      )}

      {activeStageDetail && (
        <StageDetailPanel
          projectId={activeStageDetail.projectId}
          stage={activeStageDetail.stage}
          ws={agentWsRef.current}
          onClose={() => setActiveStageDetail(null)}
        />
      )}

      {activeArtifact && (
        <ArtifactViewer
          projectId={activeArtifact.projectId}
          stage={activeArtifact.stage}
          filename={activeArtifact.filename}
          dir={activeArtifact.dir}
          ws={agentWsRef.current}
          onClose={() => setActiveArtifact(null)}
        />
      )}

      {dialogAgentId && (() => {
        const agent = state.agents.find((a) => a.id === dialogAgentId);
        if (!agent) return null;
        return (
          <AgentDialog
            agent={agent}
            logs={state.logs}
            ws={agentWsRef.current}
            activities={state.activities.filter(a => a.agentId === dialogAgentId)}
            t={t}
            onClose={() => setDialogAgentId(null)}
            onStopAgent={(agentId) => {
              const ws = agentWsRef.current;
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ command: 'stop_agent', agentId }));
              }
            }}
          />
        );
      })()}

      {state.approvalRequests.length > 0 && (
        <ApprovalDialog
          requests={state.approvalRequests}
          t={t}
          onApprove={(requestId) => {
            const ws = agentWsRef.current;
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ command: 'approval_response', requestId, approved: true }));
            }
            dispatch({ type: 'dismiss_approval', payload: requestId });
          }}
          onReject={(requestId, comment) => {
            const ws = agentWsRef.current;
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ command: 'approval_response', requestId, approved: false, comment }));
            }
            dispatch({ type: 'dismiss_approval', payload: requestId });
          }}
          onApproveAll={(actionType) => {
            state.approvalRequests
              .filter(r => r.actionType === actionType)
              .forEach(r => {
                const ws = agentWsRef.current;
                if (ws && ws.readyState === WebSocket.OPEN) {
                  ws.send(JSON.stringify({ command: 'approval_response', requestId: r.requestId, approved: true }));
                }
                dispatch({ type: 'dismiss_approval', payload: r.requestId });
              });
          }}
        />
      )}

      <CommandConsole
        messages={state.chatMessages}
        connected={state.connected}
        t={t}
        onSend={(content, targetLayer) => {
          const ws = agentWsRef.current;
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              command: 'chat_input',
              content,
              targetLayer: targetLayer || 'all',
              projectId: state.selectedProjectId || '',
            }));
            dispatch({
              type: 'chat_message',
              payload: {
                id: `user-${Date.now()}`,
                role: 'user',
                content,
                targetLayer: targetLayer || 'all',
                timestamp: Date.now(),
              },
            });
          }
        }}
      />
    </div>
    </LocaleContext.Provider>
  );
}
