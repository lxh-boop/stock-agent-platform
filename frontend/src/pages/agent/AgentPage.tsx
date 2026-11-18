import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Empty, Row, Space, Tabs, Tag, Typography, message } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { agentApi, agentRunRequest } from '../../api/agentApi'
import { errorMessage } from '../../api/errors'
import { acknowledgeTask, cancelTask, getTask, listTasks, submitTask } from '../../api/taskApi'
import { AgentRunPanel } from '../../components/agent/AgentRunPanel'
import { AgentTaskProgress } from '../../components/agent/AgentTaskProgress'
import { ChatComposer, type StrategyDraft } from '../../components/agent/ChatComposer'
import { AgentResearchTasks } from '../../components/agent/AgentResearchTasks'
import { ChatMessageList } from '../../components/agent/ChatMessageList'
import { ConversationList } from '../../components/agent/ConversationList'
import { PendingActionPanel } from '../../components/agent/PendingActionPanel'
import { StrategyProposalPanel } from '../../components/agent/StrategyProposalPanel'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { useAgentTaskEvents } from '../../hooks/useAgentTaskEvents'
import { isUnavailableAgentTask, useAgentTaskLookup } from '../../hooks/useAgentTaskLookup'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useAgentTaskStore } from '../../stores/agentTaskStore'
import { useAuthStore } from '../../stores/authStore'
import { TERMINAL_TASK_STATUSES } from '../../types/task'

function randomId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`
}

const FINALIZE_RETRY_DELAYS_MS = [0, 1500, 4000, 8000]

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export function AgentPage() {
  useAgentTaskEvents()
  const queryClient = useQueryClient()
  const userId = useCurrentUserId()
  const location = useLocation(), navigate = useNavigate()
  const [strategyDraft, setStrategyDraft] = useState<StrategyDraft | null>(null)
  const [controlTab, setControlTab] = useState('run')

  const [conversationId, setConversationId] = useState('')
  const conversationRef = useRef(conversationId)
  conversationRef.current = conversationId
  const [messageLimit, setMessageLimit] = useState(30)
  const [selectedRunId, setSelectedRunId] = useState('')
  const autoCreateRef = useRef(false)
  const finalizingRef = useRef('')

  useEffect(() => {
    setConversationId('')
    setSelectedRunId('')
    setMessageLimit(30)
    autoCreateRef.current = false
    setStrategyDraft(null)
  }, [userId])

  useEffect(() => {
    const state = location.state as { strategyDraft?: StrategyDraft } | null
    const draft = state?.strategyDraft
    if (!draft) return
    if (typeof draft.text === 'string' && draft.text.trim() && draft.text.length <= 20_000 && typeof draft.prefillId === 'string') {
      setStrategyDraft(draft)
    }
    const { strategyDraft: consumed, ...rest } = state!
    void consumed
    navigate(location.pathname + location.search, { replace: true, state: rest })
  }, [location.pathname, location.search, location.state, navigate])

  const sessions = useQuery({
    queryKey: ['web', 'agent', 'sessions', userId],
    queryFn: () => agentApi.sessions(userId),
    enabled: Boolean(userId),
    refetchInterval: 10_000,
  })

  const createSession = useMutation({
    mutationFn: () => agentApi.createSession(userId),
    onSuccess: async (session) => {
      if (useAuthStore.getState().user?.user_id !== userId) return
      setConversationId(session.conversation_id)
      await queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  useEffect(() => {
    if (sessions.isLoading || createSession.isPending) return
    const records = sessions.data?.records ?? []
    if (conversationId && records.some((item) => item.conversation_id === conversationId)) return
    if (records.length) {
      setConversationId(records[0].conversation_id)
      return
    }
    if (!autoCreateRef.current) {
      autoCreateRef.current = true
      createSession.mutate()
    }
  }, [conversationId, createSession, sessions.data, sessions.isLoading])

  const renameSession = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => agentApi.renameSession(userId, id, title),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] }),
    onError: (error) => message.error(errorMessage(error)),
  })
  const deleteSession = useMutation({
    mutationFn: (id: string) => agentApi.deleteSession(userId, id),
    onSuccess: async (_, deletedId) => {
      if (conversationId === deletedId) setConversationId('')
      await queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] })
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  const messages = useQuery({
    queryKey: ['web', 'agent', 'messages', userId, conversationId, messageLimit],
    queryFn: () => agentApi.messages(userId, conversationId, messageLimit),
    enabled: Boolean(conversationId),
    refetchInterval: 5_000,
  })
  const pending = useQuery({
    queryKey: ['web', 'agent', 'pending', userId, conversationId],
    queryFn: () => agentApi.pendingActions(userId, conversationId),
    enabled: Boolean(conversationId),
    refetchInterval: 8_000,
  })
  const strategyProposal = useQuery({
    queryKey: ['web', 'agent', 'strategy-proposal', userId, conversationId],
    queryFn: () => agentApi.strategyProposal(userId, conversationId),
    enabled: Boolean(conversationId),
    refetchInterval: 15_000,
  })

  const storedTaskId = useAgentTaskStore((state) => state.taskId)
  const storedTaskConversation = useAgentTaskStore((state) => state.conversationId)
  const agentTask = useAgentTaskStore((state) => state.task)
  const events = useAgentTaskStore((state) => state.events)
  const setRecovery = useAgentTaskStore((state) => state.setRecovery)
  const setTask = useAgentTaskStore((state) => state.setTask)
  const clearTask = useAgentTaskStore((state) => state.clear)
  const activeTaskId = storedTaskConversation === conversationId ? storedTaskId : ''

  const taskQuery = useQuery({
    queryKey: ['agent-task', userId, activeTaskId],
    queryFn: () => getTask(activeTaskId),
    enabled: Boolean(activeTaskId),
    retry: (count, error) => !isUnavailableAgentTask(error) && count < 2,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL_TASK_STATUSES.has(status) ? false : 1_500
    },
  })
  useAgentTaskLookup(activeTaskId, conversationId, userId, taskQuery.data, taskQuery.error)

  // Self-heal: a persisted task whose conversation no longer exists cannot be
  // finalized or selected again, so it would lock every conversation's composer.
  useEffect(() => {
    if (!storedTaskId || !storedTaskConversation || !sessions.isSuccess) return
    const exists = (sessions.data?.records ?? []).some(
      (item) => item.conversation_id === storedTaskConversation,
    )
    if (!exists) clearTask()
  }, [clearTask, sessions.data, sessions.isSuccess, storedTaskConversation, storedTaskId])

  const recovery = useQuery({
    queryKey: ['agent-task-recovery', userId, conversationId],
    queryFn: async () => (await Promise.all(['agent.run', 'agent.resume'].map(task_type => listTasks({
      owner_id: userId, session_id: conversationId, task_type,
      unacknowledged_only: true, limit: 5,
    })))).flat().sort((a, b) => b.created_at.localeCompare(a.created_at)),
    enabled: Boolean(conversationId) && !storedTaskId,
    refetchInterval: 4_000,
  })
  useEffect(() => {
    const latest = recovery.data?.[0]
    if (latest && latest.owner_id === userId && latest.session_id === conversationId && !storedTaskId) {
      setRecovery(latest.task_id, conversationId)
      setTask(latest)
    }
  }, [conversationId, recovery.data, setRecovery, setTask, storedTaskId, userId])

  const cancel = useMutation({
    mutationFn: (taskId: string) => cancelTask(taskId),
    onSuccess: task => {
      if (useAuthStore.getState().user?.user_id === userId && task.owner_id === userId
        && useAgentTaskStore.getState().taskId === task.task_id) setTask(task)
    },
    onError: (error) => { if (useAuthStore.getState().user?.user_id === userId) message.error(errorMessage(error)) },
  })

  const submit = useMutation({
    mutationFn: async (question: string) => {
      if (!conversationId) throw new Error('请先创建会话')
      const messageId = randomId('msg_react')
      await agentApi.createMessage(userId, conversationId, question, messageId)
      const task = await submitTask(agentRunRequest(userId, conversationId, question, messageId))
      return task
    },
    onSuccess: async (task) => {
      if (useAuthStore.getState().user?.user_id !== userId || task.owner_id !== userId || task.session_id !== conversationId) return
      setRecovery(task.task_id, conversationId)
      setTask(task)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'messages', userId, conversationId] }),
        queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] }),
      ])
    },
    onError: (error) => message.error(errorMessage(error)),
  })

  useEffect(() => {
    const task = agentTask
    if (!task || task.owner_id !== userId || task.session_id !== conversationId || !TERMINAL_TASK_STATUSES.has(task.status)) return
    if (finalizingRef.current === task.task_id) return
    finalizingRef.current = task.task_id

    void (async () => {
      try {
        let assistantMessage: Awaited<ReturnType<typeof agentApi.finalizeTask>> | null = null
        let lastError: unknown = null
        for (const delayMs of FINALIZE_RETRY_DELAYS_MS) {
          if (delayMs > 0) await sleep(delayMs)
          if (useAuthStore.getState().user?.user_id !== userId) return
          try {
            assistantMessage = await agentApi.finalizeTask(userId, conversationId, task.task_id)
            break
          } catch (error) {
            lastError = error
          }
        }
        if (!assistantMessage) throw lastError ?? new Error('finalize_task_failed')

        if (useAuthStore.getState().user?.user_id !== userId) return
        if (assistantMessage.run_id && conversationRef.current === conversationId) setSelectedRunId(assistantMessage.run_id)
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'messages', userId, conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'pending', userId, conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'strategy-proposal', userId, conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['agent-research', userId, conversationId] }),
        ])
        await acknowledgeTask(task.task_id)
        if (useAuthStore.getState().user?.user_id === userId && useAgentTaskStore.getState().taskId === task.task_id) clearTask()
      } catch (error) {
        if (finalizingRef.current === task.task_id) finalizingRef.current = ''
        if (useAuthStore.getState().user?.user_id === userId) message.error(`Agent 结果保存失败，已自动重试：${errorMessage(error)}`)
      }
    })()
  }, [agentTask, clearTask, conversationId, queryClient, userId])

  useEffect(() => {
    if (selectedRunId) return
    const last = [...(messages.data?.records ?? [])].reverse().find((item) => item.run_id)
    if (last?.run_id) setSelectedRunId(last.run_id)
  }, [messages.data, selectedRunId])

  const currentSession = useMemo(
    () => (sessions.data?.records ?? []).find((item) => item.conversation_id === conversationId),
    [conversationId, sessions.data],
  )
  const anotherConversationTask = Boolean(storedTaskId && storedTaskConversation && storedTaskConversation !== conversationId)
  const taskActive = Boolean(activeTaskId && (!agentTask || !TERMINAL_TASK_STATUSES.has(agentTask.status)))

  if (sessions.isLoading) return <PageLoading />
  if (sessions.error) {
    return <EmptyState title="AI Agent 会话加载失败" description={errorMessage(sessions.error)} />
  }

  return <Space direction="vertical" size="large" style={{ width: '100%' }} data-testid="agent-page">
    <PageHeader
      title="AI Agent"
      description="查询本人数据、研究因子和策略，查看后台任务与待确认操作。"
    />
    {anotherConversationTask ? <Typography.Text type="secondary">
      会话 {storedTaskConversation.slice(-10)} 的任务正在运行；切回该会话可查看进度，本会话可继续提问。
    </Typography.Text> : null}

    <Row gutter={16} className="agent-workspace">
      <Col xs={24} lg={6}>
        <Card className="agent-conversation-card" styles={{ body: { padding: 0 } }}>
          <ConversationList
            sessions={sessions.data?.records ?? []}
            activeId={conversationId}
            loading={sessions.isLoading}
            onSelect={(id) => {
              setConversationId(id)
              setSelectedRunId('')
              setMessageLimit(30)
            }}
            onCreate={() => createSession.mutate()}
            onRename={(id, title) => renameSession.mutate({ id, title })}
            onDelete={(id) => deleteSession.mutate(id)}
          />
        </Card>
      </Col>
      <Col xs={24} lg={18}>
        <Card
          className="agent-chat-card"
          title={<Space><Typography.Text strong>{currentSession?.title || 'AI Agent 对话'}</Typography.Text><Tag>{messages.data?.total ?? 0} 条消息</Tag></Space>}
          extra={(messages.data?.records.length ?? 0) >= messageLimit
            ? <Button size="small" onClick={() => setMessageLimit((value) => Math.min(100, value + 20))}>加载更早消息</Button>
            : null}
        >
          <ChatMessageList
            messages={messages.data?.records ?? []}
            selectedRunId={selectedRunId}
            onSelectRun={setSelectedRunId}
          />
          <AgentTaskProgress
            task={agentTask?.session_id === conversationId ? agentTask : null}
            events={events}
            cancelling={cancel.isPending}
            onCancel={() => agentTask && cancel.mutate(agentTask.task_id)}
          />
          <div
            className="agent-composer"
            data-task-active={String(taskActive)}
          >
            <ChatComposer
              key={userId}
              strategyDraft={strategyDraft}
              disabled={!conversationId || taskActive}
              loading={submit.isPending}
              onSubmit={(value) => submit.mutateAsync(value).then(() => undefined)}
            />
          </div>
          {(pending.data?.total ?? 0) > 0 && <Button type="link" onClick={() => setControlTab('pending')}>有 {pending.data?.total} 项操作需要确认，请在下方待确认操作中处理</Button>}
        </Card>
      </Col>
    </Row>

    <AgentResearchTasks key={`${userId}:${conversationId}`} userId={userId} conversationId={conversationId} />

    <Card title="Agent 运行与控制中心">
      <Tabs
        activeKey={controlTab}
        onChange={setControlTab}
        items={[
          {
            key: 'run',
            label: '运行详情',
            children: <AgentRunPanel userId={userId} runId={selectedRunId} />,
          },
          {
            key: 'pending',
            label: `待确认操作（${pending.data?.total ?? 0}）`,
            children: <>{pending.error && <Alert type="error" message={errorMessage(pending.error)} action={<Button onClick={() => void pending.refetch()}>重试读取</Button>} />}<PendingActionPanel
              userId={userId}
              conversationId={conversationId}
              actions={pending.data?.records ?? []}
              onTask={task => {
                if (useAuthStore.getState().user?.user_id !== userId || task.session_id !== conversationId) return
                setRecovery(task.task_id, conversationId)
                setTask(task)
                setControlTab('run')
              }}
            /></>,
          },
          {
            key: 'proposal',
            label: '策略草稿',
            children: <StrategyProposalPanel data={strategyProposal.data} />,
          },
        ]}
      />
    </Card>

    <Alert
      type="warning"
      showIcon
      message="本项目仅用于机器学习、金融数据分析和项目展示"
      description="不构成投资建议，不用于实盘交易。Agent 生成的模拟盘写操作仍需二次确认并由服务端 WriteGateway 重新校验。"
    />
  </Space>
}
