import { Alert, Button, Card, Space, Typography } from 'antd'
import { errorMessage } from '../api/errors'
import { CancelTaskButton } from '../components/tasks/CancelTaskButton'
import { TaskProgress } from '../components/tasks/TaskProgress'
import { useDiagnosticTask } from '../hooks/useTask'
import { useCurrentUserId } from '../hooks/useCurrentUserId'
import { useSessionStore } from '../stores/sessionStore'
import { useTaskStore } from '../stores/taskStore'

export function RuntimePage() {
  const task = useTaskStore((state) => state.task)
  const events = useTaskStore((state) => state.events)
  const clear = useTaskStore((state) => state.clear)
  const ownerId = useCurrentUserId()
  const sessionId = useSessionStore((state) => state.sessionId)
  const { submit, cancel } = useDiagnosticTask()

  const failure = submit.error ?? cancel.error

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={2}>Task API 与 SSE 验证</Typography.Title>
        <Typography.Paragraph type="secondary">
          该页面提交安全的 diagnostic.sleep 测试任务，验证 task_id、进度事件、终态和刷新恢复，不调用金融业务。
        </Typography.Paragraph>
      </div>
      {failure ? <Alert type="error" showIcon message="任务调用失败" description={errorMessage(failure)} /> : null}
      <Card title="测试身份与会话">
        <Space direction="vertical" size="small">
          <Typography.Text>任务归属（登录账号）：<Typography.Text strong>{ownerId}</Typography.Text></Typography.Text>
          <Typography.Text>会话标识：<Typography.Text code>{sessionId}</Typography.Text></Typography.Text>
        </Space>
      </Card>
      <Card title="SSE 诊断任务" data-testid="task-card">
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          {task ? <TaskProgress task={task} /> : <Alert type="info" message="尚未提交任务" showIcon />}
          <Space wrap>
            <Button type="primary" loading={submit.isPending} onClick={() => submit.mutate()} data-testid="run-diagnostic-task">
              运行 SSE 连接测试
            </Button>
            {task ? <CancelTaskButton task={task} loading={cancel.isPending} onCancel={() => cancel.mutate(task.task_id)} /> : null}
            <Button onClick={clear} disabled={!task}>清除本地任务记录</Button>
          </Space>
          <Typography.Text type="secondary" data-testid="event-count">已接收事件：{events.length}</Typography.Text>
        </Space>
      </Card>
    </Space>
  )
}
