import { useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Modal, Row, Space, Tag, Typography, message } from 'antd'
import { PaperSectionCard } from './PaperSectionCard'
import { listTasks, submitTask } from '../../api/taskApi'
import { queryKeys } from '../../api/queryKeys'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useSessionStore } from '../../stores/sessionStore'
import { useTaskStore } from '../../stores/taskStore'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import { TaskProgress } from '../tasks/TaskProgress'

interface Action {
  taskType: string
  title: string
  description: string
  kwargs: Record<string, unknown>
  timeout: number
}

export function PaperTaskActions({ profileComplete }: { profileComplete: boolean }) {
  const userId = useCurrentUserId()
  const sessionId = useSessionStore((state) => state.sessionId)
  const setTask = useTaskStore((state) => state.setTask)
  const clear = useTaskStore((state) => state.clear)
  const activeTask = useTaskStore((state) => state.task)
  const queryClient = useQueryClient()
  const activeQuery = useQuery({ queryKey: ['paper-active-tasks', userId, sessionId], queryFn: () => listTasks({ owner_id: userId, session_id: sessionId, active_only: true, limit: 20 }), refetchInterval: 3000 })
  useEffect(() => {
    if (!activeTask && activeQuery.data?.[0]) setTask(activeQuery.data[0])
  }, [activeQuery.data, activeTask, setTask])
  useEffect(() => {
    if (activeTask && TERMINAL_TASK_STATUSES.has(activeTask.status)) {
      queryClient.invalidateQueries({ queryKey: queryKeys.paperTrading(userId) })
      queryClient.invalidateQueries({ queryKey: queryKeys.paperProposals(userId) })
    }
  }, [activeTask, queryClient, userId])

  const mutation = useMutation({
    mutationFn: async (action: Action) => {
      clear()
      return submitTask({ task_type: action.taskType, kwargs: action.kwargs, owner_id: userId, session_id: sessionId, metadata: { surface: 'react-stage6-3', page: 'paper-trading' }, timeout_seconds: action.timeout, max_retries: 0 })
    },
    onSuccess: (task) => { setTask(task); message.success('任务已提交，可在任务中心查看和恢复') },
    onError: (error) => message.error(String(error)),
  })
  const actions: Action[] = [
    { taskType:'paper-trading.update', title:'更新 AI 模拟盘', description:'基于已有最新排名和新闻证据更新模拟盘，不重新训练模型。', kwargs:{user_id:userId,top_k:50,dry_run:false,paper_trading_enabled:true,sync_kwargs:{}}, timeout:991800 },
    { taskType:'paper-profile.ai-news-adjustment', title:'运行新闻调整', description:'执行新闻/RAG 评分调整并刷新可用结果。', kwargs:{user_id:userId,top_k:50,paper_trading_enabled:false,dry_run:false}, timeout:991800 },
    { taskType:'paper-profile.scheduler-manual', title:'手动运行调度器', description:'以当前用户触发一次后台调度任务。', kwargs:{user_id:userId,all_users:false,force:false,dry_run:false}, timeout:991800 },
  ]
  return <PaperSectionCard sectionKey="task-actions" title="长任务与恢复" extra={<Tag>Task API + SSE</Tag>}>
    {!profileComplete ? <Alert type="warning" showIcon message="请先补全用户画像和模拟资金，再更新模拟盘。" style={{marginBottom:16}} /> : null}
    <Row gutter={[16,16]}>{actions.map((action)=><Col xs={24} lg={8} key={action.taskType}><Card size="small" title={action.title}><Space direction="vertical"><Typography.Text type="secondary">{action.description}</Typography.Text><Button disabled={!profileComplete || mutation.isPending || Boolean(activeTask && !TERMINAL_TASK_STATUSES.has(activeTask.status))} onClick={()=>Modal.confirm({title:`确认${action.title}？`,content:'任务提交后以服务端状态为准；页面刷新后仍可恢复。',okText:'确认提交',onOk:()=>mutation.mutate(action)})}>{action.title}</Button></Space></Card></Col>)}</Row>
    {activeTask ? <div style={{marginTop:16}}><TaskProgress task={activeTask} /></div> : null}
  </PaperSectionCard>
}
