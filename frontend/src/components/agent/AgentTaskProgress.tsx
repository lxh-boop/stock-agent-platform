import { Button, List, Space, Typography } from 'antd'
import type { TaskEvent, TaskRecord } from '../../types/task'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import { TaskProgress } from '../tasks/TaskProgress'

export function AgentTaskProgress({
  task,
  events,
  cancelling,
  onCancel,
}: {
  task: TaskRecord | null
  events: TaskEvent[]
  cancelling: boolean
  onCancel: () => void
}) {
  if (!task) return null
  const terminal = TERMINAL_TASK_STATUSES.has(task.status)
  return <div className="agent-task-panel" data-testid="agent-task-panel">
    <TaskProgress task={task} />
    {!terminal ? <Button danger loading={cancelling} onClick={onCancel} style={{ alignSelf: 'flex-start' }}>取消任务</Button> : null}
    <List
      size="small"
      header={<Typography.Text strong>最近 SSE 事件</Typography.Text>}
      dataSource={events.slice(-8).reverse()}
      locale={{ emptyText: '正在等待任务事件' }}
      renderItem={(event) => <List.Item>
        <Space>
          <Typography.Text code>{event.sequence}</Typography.Text>
          <Typography.Text>{event.event_type}</Typography.Text>
          <Typography.Text type="secondary">{String(event.data.message ?? event.created_at)}</Typography.Text>
        </Space>
      </List.Item>}
    />
  </div>
}
