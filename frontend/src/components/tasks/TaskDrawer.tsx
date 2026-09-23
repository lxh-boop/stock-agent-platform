import { useMutation, useQuery } from '@tanstack/react-query'
import { Button, Drawer, List, Space, Tabs, Typography, message } from 'antd'
import { acknowledgeTask, listTasks } from '../../api/taskApi'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useTaskStore } from '../../stores/taskStore'
import type { TaskEvent, TaskRecord } from '../../types/task'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import { TaskProgress } from './TaskProgress'
import { TaskStatusTag } from './TaskStatusTag'

export function TaskDrawer({ open, onClose, task, events }: { open: boolean; onClose: () => void; task: TaskRecord | null; events: TaskEvent[] }) {
  const ownerId = useCurrentUserId()
  const setTask = useTaskStore((state) => state.setTask)
  const clear = useTaskStore((state) => state.clear)
  const recent = useQuery({ queryKey: ['task-center', ownerId], queryFn: () => listTasks({ owner_id: ownerId, limit: 30 }), enabled: open, refetchInterval: open ? 3000 : false })
  const acknowledge = useMutation({
    mutationFn: acknowledgeTask,
    onSuccess: async (record) => {
      message.success('任务已确认')
      if (task?.task_id === record.task_id) clear()
      await recent.refetch()
    },
    onError: (error) => message.error(String(error)),
  })
  return <Drawer title="全局任务中心" open={open} onClose={onClose} width={600}>
    <Tabs items={[
      { key:'current', label:'当前任务', children:<>
        {task ? <Space direction="vertical" style={{width:'100%'}}><TaskProgress task={task} />{TERMINAL_TASK_STATUSES.has(task.status) && !task.acknowledged_at ? <Button onClick={()=>acknowledge.mutate(task.task_id)} loading={acknowledge.isPending}>确认并清理</Button> : null}</Space> : <Typography.Text type="secondary">暂无活动任务</Typography.Text>}
        <List style={{marginTop:20}} size="small" header={<Typography.Text strong>SSE 事件</Typography.Text>} dataSource={[...events].reverse()} locale={{emptyText:'尚未收到事件'}} renderItem={(event)=><List.Item><List.Item.Meta title={`${event.sequence} · ${event.event_type}`} description={String(event.data.message ?? event.created_at)} /></List.Item>} />
      </> },
      { key:'recent', label:'最近任务', children:<List loading={recent.isLoading} dataSource={recent.data ?? []} locale={{emptyText:'暂无任务'}} renderItem={(item)=><List.Item actions={[<Button key="select" type="link" onClick={()=>setTask(item)}>查看</Button>, ...(TERMINAL_TASK_STATUSES.has(item.status) && !item.acknowledged_at ? [<Button key="ack" type="link" onClick={()=>acknowledge.mutate(item.task_id)}>确认</Button>] : [])]}><List.Item.Meta title={<Space><TaskStatusTag status={item.status}/><Typography.Text>{item.task_type}</Typography.Text></Space>} description={`${item.task_id.slice(-12)} · ${item.message || item.updated_at}`} /></List.Item>} /> },
    ]} />
  </Drawer>
}
