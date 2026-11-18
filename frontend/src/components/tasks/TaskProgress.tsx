import { Progress, Space, Typography } from 'antd'
import type { TaskRecord } from '../../types/task'
import { TaskStatusTag } from './TaskStatusTag'
import { formatTaskTime } from '../../utils/taskTime'

export function TaskProgress({ task }: { task: TaskRecord }) {
  const percent = Math.max(0, Math.min(100, Math.round((task.progress ?? 0) * 100)))
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Space wrap><TaskStatusTag status={task.status} /><Typography.Text code>{task.task_id}</Typography.Text></Space>
      <Typography.Text type="secondary">
        创建：<span title={task.created_at}>{formatTaskTime(task.created_at)}</span>
        {' · '}开始：<span title={task.started_at ?? undefined}>{formatTaskTime(task.started_at)}</span>
      </Typography.Text>
      <Progress percent={percent} status={task.status === 'failed' || task.status === 'timed_out' ? 'exception' : undefined} />
      <Typography.Text type="secondary">{task.message || '等待任务事件'}</Typography.Text>
    </Space>
  )
}
