import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Empty, Row, Space, Tabs, Tag, Typography, message } from 'antd'
import { agentApi } from '../../api/agentApi'
import { acknowledgeTask, cancelTask, getTask, listTasks, submitTask } from '../../api/taskApi'
import { AgentRunPanel } from '../../components/agent/AgentRunPanel'
import { AgentTaskProgress } from '../../components/agent/AgentTaskProgress'
import { ChatComposer } from '../../components/agent/ChatComposer'
import { ChatMessageList } from '../../components/agent/ChatMessageList'
import { ConversationList } from '../../components/agent/ConversationList'
import { PendingActionPanel } from '../../components/agent/PendingActionPanel'
import { StrategyProposalPanel } from '../../components/agent/StrategyProposalPanel'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { useAgentTaskEvents } from '../../hooks/useAgentTaskEvents'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useAgentTaskStore } from '../../stores/agentTaskStore'
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

  const [conversationId, setConversationId] = useState('')
  const [messageLimit, setMessageLimit] = useState(30)
  const [selectedRunId, setSelectedRunId] = useState('')
  const autoCreateRef = useRef(false)
  const finalizingRef = useRef('')

  useEffect(() => {
    setConversationId('')
    setSelectedRunId('')
    setMessageLimit(30)
    autoCreateRef.current = false
  }, [userId])

  const sessions = useQuery({
    queryKey: ['web', 'agent', 'sessions', userId],
    queryFn: () => agentApi.sessions(userId),
    enabled: Boolean(userId),
    refetchInterval: 10_000,
  })

  const createSession = useMutation({
    mutationFn: () => agentApi.createSession(userId),
    onSuccess: async (session) => {
      setConversationId(session.conversation_id)
      await queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] })
    },
    onError: (error) => message.error(String(error)),
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
    onError: (error) => message.error(String(error)),
  })
  const deleteSession = useMutation({
    mutationFn: (id: string) => agentApi.deleteSession(userId, id),
    onSuccess: async (_, deletedId) => {
      if (conversationId === deletedId) setConversationId('')
      await queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] })
    },
    onError: (error) => message.error(String(error)),
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
    queryKey: ['agent-task', activeTaskId],
    queryFn: () => getTask(activeTaskId),
    enabled: Boolean(activeTaskId),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL_TASK_STATUSES.has(status) ? false : 1_500
    },
  })
  useEffect(() => {
    if (taskQuery.data) setTask(taskQuery.data)
  }, [setTask, taskQuery.data])

  // Self-heal: the persisted task row is gone server-side, so it can never
  // reach a terminal status and would keep the composer disabled forever.
  useEffect(() => {
    const error = taskQuery.error
    if (!error || !activeTaskId) return
    const text = error instanceof Error ? `${error.message} ${(error as { code?: string }).code ?? ''}` : String(error)
    if (/unknown task/i.test(text)) clearTask()
  }, [activeTaskId, clearTask, taskQuery.error])

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
    queryFn: () => listTasks({
      owner_id: userId,
      session_id: conversationId,
      task_type: 'agent.run',
      unacknowledged_only: true,
      limit: 5,
    }),
    enabled: Boolean(conversationId) && !storedTaskId,
    refetchInterval: 4_000,
  })
  useEffect(() => {
    const latest = recovery.data?.[0]
    if (latest && !storedTaskId) {
      setRecovery(latest.task_id, conversationId)
      setTask(latest)
    }
  }, [conversationId, recovery.data, setRecovery, setTask, storedTaskId])

  const cancel = useMutation({
    mutationFn: (taskId: string) => cancelTask(taskId),
    onSuccess: setTask,
    onError: (error) => message.error(String(error)),
  })

  const submit = useMutation({
    mutationFn: async (question: string) => {
      if (!conversationId) throw new Error('请先创建会话')
      const messageId = randomId('msg_react')
      await agentApi.createMessage(userId, conversationId, question, messageId)
      const task = await submitTask({
        task_type: 'agent.run',
        args: [question],
        kwargs: {
          user_id: userId,
          top_k: 10,
          session_id: conversationId,
        },
        owner_id: userId,
        session_id: conversationId,
        metadata: {
          surface: 'react-agent',
          conversation_id: conversationId,
          user_message_id: messageId,
          query_preview: question.slice(0, 160),
        },
        timeout_seconds: 99900,
        max_retries: 0,
      })
      return task
    },
    onSuccess: async (task) => {
      setRecovery(task.task_id, conversationId)
      setTask(task)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'messages', userId, conversationId] }),
        queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] }),
      ])
    },
    onError: (error) => message.error(String(error)),
  })

  useEffect(() => {
    const task = agentTask
    if (!task || task.session_id !== conversationId || !TERMINAL_TASK_STATUSES.has(task.status)) return
    if (finalizingRef.current === task.task_id) return
    finalizingRef.current = task.task_id

    void (async () => {
      try {
        let assistantMessage: Awaited<ReturnType<typeof agentApi.finalizeTask>> | null = null
        let lastError: unknown = null
        for (const delayMs of FINALIZE_RETRY_DELAYS_MS) {
          if (delayMs > 0) await sleep(delayMs)
          try {
            assistantMessage = await agentApi.finalizeTask(userId, conversationId, task.task_id)
            break
          } catch (error) {
            lastError = error
          }
        }
        if (!assistantMessage) throw lastError ?? new Error('finalize_task_failed')

        if (assistantMessage.run_id) setSelectedRunId(assistantMessage.run_id)
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'messages', userId, conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'sessions', userId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'pending', userId, conversationId] }),
          queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'strategy-proposal', userId, conversationId] }),
        ])
        await acknowledgeTask(task.task_id)
        clearTask()
      } catch (error) {
        finalizingRef.current = ''
        message.error(`Agent 结果保存失败，已自动重试：${String(error)}`)
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
    return <EmptyState title="AI Agent 会话加载失败" description={String(sessions.error)} />
  }

  return <Space direction="vertical" size="large" style={{ width: '100%' }} data-testid="agent-page">
    <PageHeader
      title="AI Agent"
      description="会话、任务、SSE 与审批均通过 FastAPI；不连接真实交易。"
    />
    <Alert
      type="info"
      showIcon
      message={`当前用户：${userId} · 当前会话：${conversationId ? conversationId.slice(-10) : '-'}`}
      description="浏览器只保存会话标识、task_id 和 last_event_id，不保存回答结果、持仓、密钥或确认令牌。"
    />
    {anotherConversationTask ? <Alert
      type="info"
      showIcon
      message="另一个会话的任务正在运行，不影响本会话提问"
      description={`任务所属会话：${storedTaskConversation.slice(-10)}。不同会话可以并发回答；切回该会话可查看其任务进度。`}
    /> : null}

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
              disabled={!conversationId || taskActive}
              loading={submit.isPending}
              onSubmit={(value) => submit.mutateAsync(value).then(() => undefined)}
            />
          </div>
        </Card>
      </Col>
    </Row>

    <Card title="Agent 运行与控制中心">
      <Tabs
        items={[
          {
            key: 'run',
            label: '运行详情',
            children: <AgentRunPanel userId={userId} runId={selectedRunId} />,
          },
          {
            key: 'pending',
            label: `待确认操作（${pending.data?.total ?? 0}）`,
            children: <PendingActionPanel
              userId={userId}
              conversationId={conversationId}
              actions={pending.data?.records ?? []}
            />,
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
