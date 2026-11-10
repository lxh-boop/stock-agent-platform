import { useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Modal, Space, Tag, Typography, message } from 'antd'
import { submitTask } from '../../api/taskApi'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useSessionStore } from '../../stores/sessionStore'
import { useTaskStore } from '../../stores/taskStore'
import type { PublicSettings } from '../../types/settings'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import { TaskProgress } from '../tasks/TaskProgress'

function statusColor(status: string): string {
  if (status === 'success') return 'success'
  if (status === 'running') return 'processing'
  if (status === 'failed') return 'error'
  if (status === 'partial_success') return 'warning'
  return 'default'
}

export function SchedulerStatusPanel({ settings }: { settings: PublicSettings }) {
  const scheduler = settings.scheduler
  const ownerId = useCurrentUserId()
  const sessionId = useSessionStore((state) => state.sessionId)
  const activeTask = useTaskStore((state) => state.task)
  const setTask = useTaskStore((state) => state.setTask)
  const clearTask = useTaskStore((state) => state.clear)
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: async () => {
      clearTask()
      return submitTask({
        task_type: 'paper-profile.scheduler-manual',
        kwargs: {
          all_users: true,
          force: true,
          dry_run: false,
          output_dir: 'outputs',
        },
        owner_id: ownerId,
        session_id: sessionId,
        metadata: { surface: 'react-stage6-7', page: 'settings' },
        timeout_seconds: 997200,
        max_retries: 0,
      })
    },
    onSuccess: (task) => {
      setTask(task)
      message.success('完整日更任务已提交，可在任务中心查看')
    },
    onError: (error) => message.error(`任务提交失败：${String(error)}`),
  })

  useEffect(() => {
    if (activeTask && TERMINAL_TASK_STATUSES.has(activeTask.status)) {
      void queryClient.invalidateQueries({ queryKey: ['web', 'settings'] })
      void queryClient.invalidateQueries({ queryKey: ['web', 'dashboard'] })
    }
  }, [activeTask, queryClient])

  const taskBusy = Boolean(activeTask && !TERMINAL_TASK_STATUSES.has(activeTask.status))

  return (
    <Card title="自动更新运行状态">
      <Descriptions column={1} size="small">
        <Descriptions.Item label="计划状态">
          <Tag color={scheduler.enabled ? 'success' : 'default'}>
            {scheduler.enabled ? '已启用' : '未启用'}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="调度器进程">
          <Tag color={scheduler.runtime_running && scheduler.job_registered ? 'success' : 'warning'}>
            {scheduler.runtime_running && scheduler.job_registered ? '运行中' : '未注册'}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="计划时间">
          {String(scheduler.hour).padStart(2, '0')}:{String(scheduler.minute).padStart(2, '0')}（{scheduler.timezone || 'Asia/Shanghai'}）
        </Descriptions.Item>
        <Descriptions.Item label="下次运行">{scheduler.next_run_time || '—'}</Descriptions.Item>
        <Descriptions.Item label="上次开始">{scheduler.last_started_at || '—'}</Descriptions.Item>
        <Descriptions.Item label="上次完成">{scheduler.last_finished_at || '—'}</Descriptions.Item>
        <Descriptions.Item label="上次状态">
          <Tag color={statusColor(scheduler.last_status)}>{scheduler.last_status || 'unknown'}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="当前步骤">{scheduler.current_step || '—'}</Descriptions.Item>
        {scheduler.last_failed_step ? (
          <Descriptions.Item label="失败步骤"><Typography.Text type="danger">{scheduler.last_failed_step}</Typography.Text></Descriptions.Item>
        ) : null}
        <Descriptions.Item label="应有信号日">{scheduler.expected_signal_date || '—'}</Descriptions.Item>
        <Descriptions.Item label="当前信号日">
          <Tag color={scheduler.stale ? 'error' : 'success'}>{scheduler.latest_signal_date || '无数据'}</Tag>
        </Descriptions.Item>
      </Descriptions>

      {scheduler.stale ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message="排名数据落后于最近交易日"
          description="启用补跑后，API 重启或错过计划时间会自动补跑；也可以立即运行。"
        />
      ) : null}
      {scheduler.last_error ? (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message={scheduler.last_failed_step ? `上次失败：${scheduler.last_failed_step}` : '上次错误'}
          description={
            <Typography.Paragraph copyable style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
              {scheduler.last_error_type ? `${scheduler.last_error_type}: ` : ''}{scheduler.last_error}
            </Typography.Paragraph>
          }
        />
      ) : null}

      <Space direction="vertical" style={{ width: '100%', marginTop: 16 }}>
        <Button
          type="primary"
          loading={mutation.isPending}
          disabled={taskBusy}
          onClick={() => Modal.confirm({
            title: '确认立即执行完整日更？',
            content: '任务会先下载行情并生成最新排名，再执行新闻、用户推荐和模拟盘链路。',
            okText: '确认运行',
            onOk: () => mutation.mutate(),
          })}
        >
          立即运行完整日更
        </Button>
        <Typography.Text type="secondary">
          自动任务由 FastAPI 常驻调度器执行，不依赖浏览器页面或 Streamlit。
        </Typography.Text>
        {activeTask?.task_type === 'paper-profile.scheduler-manual' ? <TaskProgress task={activeTask} /> : null}
      </Space>
    </Card>
  )
}
