import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Empty, Modal, Space, Typography, message } from 'antd'
import { Link } from 'react-router-dom'
import { cancelTask, listTasks } from '../../api/taskApi'
import { strategiesApi } from '../../api/strategiesApi'
import { errorMessage } from '../../api/errors'
import { useAuthStore } from '../../stores/authStore'
import { TaskProgress } from '../tasks/TaskProgress'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import { backtestRequestId } from '../../utils/backtest'
import { useResearchTaskEvents } from '../../hooks/useResearchTaskEvents'
import type { TaskRecord } from '../../types/task'

export function AgentResearchTasks({ userId, conversationId }: { userId: string; conversationId: string }) {
  const client = useQueryClient()
  const saveRequests = useRef(new Map<string, string>())
  const [saveRunId, setSaveRunId] = useState('')
  const tasks = useQuery({
    queryKey: ['agent-research', userId, conversationId],
    queryFn: async () => {
      const incoming = await listTasks({ owner_id: userId, session_id: conversationId, task_type: 'backtest.run', limit: 10 })
      const cached = client.getQueryData<TaskRecord[]>(['agent-research', userId, conversationId]) ?? []
      return incoming.map(task => {
        const completed = cached.find(previous => previous.task_id === task.task_id && previous.owner_id === userId
          && previous.session_id === conversationId && TERMINAL_TASK_STATUSES.has(previous.status))
        return completed && !TERMINAL_TASK_STATUSES.has(task.status) ? completed : task
      })
    },
    enabled: Boolean(userId && conversationId),
    refetchInterval: 2500,
  })
  const records = (tasks.data ?? []).filter(t => t.owner_id === userId && t.session_id === conversationId)
  useResearchTaskEvents(userId, conversationId, records.filter(task => !TERMINAL_TASK_STATUSES.has(task.status)).map(task => task.task_id).sort().join(','))
  const cancel = useMutation({ mutationFn: cancelTask, onSuccess: () => tasks.refetch(),
    onError: e => { if (useAuthStore.getState().user?.user_id === userId) message.error(errorMessage(e)) } })
  const save = useMutation({
    mutationFn: (runId: string) => {
      if (!saveRequests.current.has(runId)) saveRequests.current.set(runId, backtestRequestId())
      return strategiesApi.fromReport(runId, undefined, saveRequests.current.get(runId)!)
    },
    onSuccess: () => {
      if (useAuthStore.getState().user?.user_id !== userId) return
      message.success('已从完成报告保存策略')
      void client.invalidateQueries({ queryKey: ['strategies'] })
    },
    onError: e => { if (useAuthStore.getState().user?.user_id === userId) message.error(errorMessage(e)) },
  })
  if (tasks.error) return <Typography.Text type="danger">研究任务读取失败：{errorMessage(tasks.error)}</Typography.Text>
  if (!records.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前会话暂无研究任务，可以让 AI Agent 研究因子或运行策略回测。" />
  return <Card title="本会话研究任务" size="small" data-testid="agent-research-tasks">
    <Space direction="vertical" style={{ width: '100%' }}>
      {records.map(task => {
        const result = task.result && typeof task.result === 'object' ? task.result as { run_id?: string; kind?: string } : null
        const runId = task.status === 'succeeded' && typeof result?.run_id === 'string' ? result.run_id : ''
        return <div key={task.task_id} style={{ width: '100%' }}>
          <TaskProgress task={task} />
          <Space wrap>
            {!TERMINAL_TASK_STATUSES.has(task.status) && <Button danger size="small" loading={cancel.isPending} onClick={() => cancel.mutate(task.task_id)}>取消研究任务</Button>}
            {runId && <Link to={`/strategies?report=${encodeURIComponent(runId)}`}>查看研究报告</Link>}
            {runId && result?.kind === 'backtest' && <Button size="small" loading={save.isPending} onClick={() => setSaveRunId(runId)}>保存策略</Button>}
          </Space>
        </div>
      })}
    </Space>
    <Modal open={Boolean(saveRunId)} title="从此完成报告保存策略？" confirmLoading={save.isPending}
      cancelButtonProps={{ disabled: save.isPending }} onCancel={() => { if (!save.isPending) setSaveRunId('') }}
      onOk={() => { void save.mutateAsync(saveRunId).then(() => setSaveRunId('')).catch(() => undefined) }}>
      保留本次参数与报告关联，不重新运行回测。
    </Modal>
  </Card>
}
