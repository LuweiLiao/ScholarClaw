import { memo, useMemo, useState } from 'react';
import type { ActivityEvent, AgentLayer, LobsterAgent, SupervisorTab, TaskGraphInfo } from '../types';
import ConversationView from './ConversationView';
import ActivityTimeline from './ActivityTimeline';

interface Props {
  activities: ActivityEvent[];
  agents: LobsterAgent[];
  taskGraph: TaskGraphInfo | null;
  selectedProjectId: string | null;
  connected: boolean;
  ws: WebSocket | null;
  t: (key: string, vars?: Record<string, string>) => string;
}

const ACTIVITY_TYPES = [
  'thinking',
  'llm_call',
  'llm_response',
  'tool_call',
  'tool_result',
  'file_read',
  'file_write',
  'stage_transition',
  'user_message',
  'human_feedback',
  'metaprompt_update',
  'error',
];

function optionLabel(value: string, t: Props['t']): string {
  if (!value) return t('supervisor.filter_all');
  return t(`supervisor.type_${value}`) || value;
}

export default memo(function SupervisorPanel({
  activities,
  agents,
  taskGraph,
  selectedProjectId,
  connected,
  ws,
  t,
}: Props) {
  const [tab, setTab] = useState<SupervisorTab>('conversation');
  const [agentFilter, setAgentFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [feedback, setFeedback] = useState('');
  const [feedbackLayer, setFeedbackLayer] = useState<'all' | AgentLayer>('all');
  const [chatAgentId, setChatAgentId] = useState('');
  const [chatText, setChatText] = useState('');
  const [promptLayer, setPromptLayer] = useState<'system' | 'domain' | 'project' | 'node'>('project');
  const [promptNodeId, setPromptNodeId] = useState('');
  const [promptSystem, setPromptSystem] = useState('');
  const [promptUser, setPromptUser] = useState('');

  const scopedActivities = useMemo(() => {
    return activities.filter((activity) => {
      if (selectedProjectId && activity.projectId && activity.projectId !== selectedProjectId) return false;
      if (agentFilter && activity.agentId !== agentFilter) return false;
      if (typeFilter && activity.activityType !== typeFilter) return false;
      return true;
    });
  }, [activities, selectedProjectId, agentFilter, typeFilter]);

  const activeAgents = useMemo(
    () => agents.filter(agent => ['working', 'waiting_discussion', 'discussing', 'awaiting_approval'].includes(agent.status)),
    [agents],
  );

  const graphNodes = useMemo(() => Object.values(taskGraph?.nodes || {}), [taskGraph]);

  const sendFeedback = () => {
    const content = feedback.trim();
    if (!content || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      command: 'human_feedback',
      content,
      targetLayer: feedbackLayer,
      projectId: selectedProjectId || '',
    }));
    setFeedback('');
  };

  const prepareCorrection = (event: ActivityEvent) => {
    setTab('intervention');
    setFeedbackLayer(event.layer);
    setChatAgentId(event.agentId);
    setFeedback([
      t('supervisor.correction_prefix'),
      `Agent: ${event.agentName}`,
      event.stage ? `Stage: S${event.stage}` : '',
      event.nodeId ? `Node: ${event.nodeId}` : '',
      `Event: ${event.summary}`,
      '',
    ].filter(Boolean).join('\n'));
  };

  const sendAgentChat = () => {
    const message = chatText.trim();
    if (!message || !chatAgentId || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ command: 'agent_chat', agentId: chatAgentId, message }));
    setChatText('');
  };

  const savePrompt = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !selectedProjectId) return;
    ws.send(JSON.stringify({
      command: 'save_metaprompt',
      projectId: selectedProjectId,
      layer: promptLayer,
      nodeId: promptLayer === 'node' ? promptNodeId.trim() : undefined,
      system: promptSystem,
      user: promptUser,
      scope: 'project',
      recordVersion: true,
    }));
  };

  const resetPrompt = () => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !selectedProjectId) return;
    ws.send(JSON.stringify({
      command: 'reset_metaprompt',
      projectId: selectedProjectId,
      layer: promptLayer,
      nodeId: promptLayer === 'node' ? promptNodeId.trim() : undefined,
      scope: 'project',
    }));
  };

  // Compute current "live turn" indicator: most recent llm_request without a
  // matching llm_response, or the last llm_response if everything is settled.
  const liveTurn = useMemo(() => {
    let latestRequest: ActivityEvent | null = null;
    let latestResponse: ActivityEvent | null = null;
    for (const evt of scopedActivities) {
      if (evt.activityType === 'llm_request') {
        if (!latestRequest || evt.timestamp >= latestRequest.timestamp) latestRequest = evt;
      } else if (evt.activityType === 'llm_response') {
        if (!latestResponse || evt.timestamp >= latestResponse.timestamp) latestResponse = evt;
      }
    }
    if (latestRequest && (!latestResponse || latestResponse.timestamp < latestRequest.timestamp)) {
      return { status: 'pending' as const, evt: latestRequest };
    }
    if (latestResponse) {
      return { status: 'done' as const, evt: latestResponse };
    }
    return null;
  }, [scopedActivities]);

  return (
    <aside className="supervisor-panel">
      <div className="supervisor-header">
        <div>
          <div className="supervisor-title">{t('supervisor.title')}</div>
          <div className="supervisor-subtitle">{t('supervisor.subtitle')}</div>
        </div>
        <span className={`supervisor-conn ${connected ? 'is-on' : ''}`}>{connected ? '●' : '○'}</span>
      </div>

      {liveTurn && (
        <div className="supervisor-status">
          <span className={liveTurn.status === 'pending' ? 'ss-pending' : 'ss-active'}>
            {liveTurn.status === 'pending' ? '⏳ ' : '✅ '}
            <strong>{liveTurn.evt.agentName}</strong>
            {liveTurn.evt.stage ? ` · S${liveTurn.evt.stage}` : ''}
            {liveTurn.evt.model ? ` · ${liveTurn.evt.model}` : ''}
            {' · '}
            {liveTurn.status === 'pending' ? t('conversation.waiting_response') : liveTurn.evt.summary}
          </span>
        </div>
      )}

      <div className="supervisor-tabs">
        {(['process', 'conversation', 'prompt', 'intervention'] as SupervisorTab[]).map(key => (
          <button
            key={key}
            type="button"
            className={`supervisor-tab ${tab === key ? 'active' : ''}`}
            onClick={() => setTab(key)}
          >
            {t(`supervisor.tab_${key}`)}
          </button>
        ))}
      </div>

      <div className="supervisor-filters">
        <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
          <option value="">{t('supervisor.filter_agent_all')}</option>
          {agents.map(agent => (
            <option key={agent.id} value={agent.id}>{agent.name}</option>
          ))}
        </select>
        <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)}>
          <option value="">{t('supervisor.filter_type_all')}</option>
          {ACTIVITY_TYPES.map(type => (
            <option key={type} value={type}>{optionLabel(type, t)}</option>
          ))}
        </select>
      </div>

      {tab === 'process' && (
        <div className="supervisor-body supervisor-body--timeline">
          <ActivityTimeline activities={scopedActivities} t={t} onCorrect={prepareCorrection} />
        </div>
      )}

      {tab === 'conversation' && (
        <div className="supervisor-body">
          <ConversationView activities={scopedActivities} t={t} />
        </div>
      )}

      {tab === 'prompt' && (
        <div className="supervisor-body supervisor-form">
          <label>
            {t('supervisor.prompt_scope')}
            <select value={promptLayer} onChange={e => setPromptLayer(e.target.value as typeof promptLayer)}>
              <option value="system">System</option>
              <option value="domain">Domain</option>
              <option value="project">Project</option>
              <option value="node">Node</option>
            </select>
          </label>
          {promptLayer === 'node' && (
            <label>
              {t('supervisor.node_id')}
              <select value={promptNodeId} onChange={e => setPromptNodeId(e.target.value)}>
                <option value="">{t('supervisor.select_node')}</option>
                {graphNodes.map(node => (
                  <option key={node.id} value={node.id}>{node.title || node.id}</option>
                ))}
              </select>
            </label>
          )}
          <label>
            {t('supervisor.system_overlay')}
            <textarea value={promptSystem} onChange={e => setPromptSystem(e.target.value)} rows={5} />
          </label>
          <label>
            {t('supervisor.user_overlay')}
            <textarea value={promptUser} onChange={e => setPromptUser(e.target.value)} rows={7} />
          </label>
          <div className="supervisor-actions">
            <button type="button" onClick={savePrompt} disabled={!connected || !selectedProjectId}>
              {t('supervisor.save_prompt')}
            </button>
            <button type="button" onClick={resetPrompt} disabled={!connected || !selectedProjectId}>
              {t('supervisor.reset_prompt')}
            </button>
          </div>
        </div>
      )}

      {tab === 'intervention' && (
        <div className="supervisor-body supervisor-form">
          <div className="supervisor-section-title">{t('supervisor.feedback_title')}</div>
          <label>
            {t('supervisor.target_layer')}
            <select value={feedbackLayer} onChange={e => setFeedbackLayer(e.target.value as typeof feedbackLayer)}>
              <option value="all">{t('supervisor.all_layers')}</option>
              <option value="idea">Idea</option>
              <option value="experiment">Experiment</option>
              <option value="coding">Coding</option>
              <option value="execution">Execution</option>
              <option value="writing">Writing</option>
            </select>
          </label>
          <textarea
            value={feedback}
            onChange={e => setFeedback(e.target.value)}
            placeholder={t('supervisor.feedback_placeholder')}
            rows={5}
          />
          <button type="button" onClick={sendFeedback} disabled={!connected || !feedback.trim()}>
            {t('supervisor.send_feedback')}
          </button>

          <div className="supervisor-section-title">{t('supervisor.chat_title')}</div>
          <label>
            {t('supervisor.agent')}
            <select value={chatAgentId} onChange={e => setChatAgentId(e.target.value)}>
              <option value="">{t('supervisor.select_agent')}</option>
              {activeAgents.concat(agents.filter(a => !activeAgents.includes(a))).map(agent => (
                <option key={agent.id} value={agent.id}>{agent.name} · {agent.status}</option>
              ))}
            </select>
          </label>
          <textarea
            value={chatText}
            onChange={e => setChatText(e.target.value)}
            placeholder={t('supervisor.chat_placeholder')}
            rows={4}
          />
          <button type="button" onClick={sendAgentChat} disabled={!connected || !chatAgentId || !chatText.trim()}>
            {t('supervisor.send_chat')}
          </button>
        </div>
      )}
    </aside>
  );
});
