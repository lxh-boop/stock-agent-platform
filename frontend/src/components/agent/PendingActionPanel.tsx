import { Button, Card, Empty, Input, List, Modal, Space, Tag, Typography, message } from 'antd'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { agentApi } from '../../api/agentApi'
import { errorMessage } from '../../api/errors'
import { useAuthStore } from '../../stores/authStore'
import type { AgentPendingAction } from '../../types/agent'
import type { TaskRecord } from '../../types/task'

function actionKey(item: AgentPendingAction, action: string): string {
  return `${item.proposal_id}:${item.proposal_version ?? 1}:${item.payload_hash ?? ''}:${action}`
}

export function PendingActionPanel({
  userId,
  conversationId,
  actions,
  onTask,
}: {
  userId: string
  conversationId: string
  actions: AgentPendingAction[]
  onTask?: (task: TaskRecord) => void
}) {
  const queryClient = useQueryClient()
  const [review, setReview] = useState<{ item: AgentPendingAction; action: 'confirm' | 'reject' } | null>(null)
  const [confirmationText, setConfirmationText] = useState('')
  const complete = async (response: Awaited<ReturnType<typeof agentApi.confirmPendingAction>>) => {
    if (useAuthStore.getState().user?.user_id !== userId) return
    if (response.task?.owner_id === userId && response.task.session_id === conversationId) onTask?.(response.task)
    message.success(response.task ? '审批决定已提交后台处理' : '待确认操作已处理')
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['web', 'agent', 'pending', userId, conversationId] }),
      queryClient.invalidateQueries({ queryKey: ['agent-task-recovery', userId, conversationId] }),
      queryClient.invalidateQueries({ queryKey: ['agent-research', userId, conversationId] }),
    ])
  }
  const confirm = useMutation({
    mutationFn: async ({ item, text }: { item: AgentPendingAction; text: string }) =>
      agentApi.confirmPendingAction(
        userId,
        conversationId,
        item.proposal_id,
        text,
        actionKey(item, 'confirm'),
        actionKey(item, 'confirm'),
      ),
    onSuccess: complete,
    onError: (error) => { if (useAuthStore.getState().user?.user_id === userId) message.error(errorMessage(error)) },
  })
  const reject = useMutation({
    mutationFn: async (item: AgentPendingAction) =>
      agentApi.rejectPendingAction(
        userId,
        conversationId,
        item.proposal_id,
        actionKey(item, 'reject'),
        actionKey(item, 'reject'),
      ),
    onSuccess: complete,
    onError: (error) => { if (useAuthStore.getState().user?.user_id === userId) message.error(errorMessage(error)) },
  })

  const openConfirm = (item: AgentPendingAction) => {
    setConfirmationText('')
    setReview({item, action:'confirm'})
  }

  const busy = confirm.isPending || reject.isPending
  const submitReview = () => {
    if (!review || busy) return
    if (review.action === 'confirm' && confirmationText.trim().toUpperCase() !== review.item.confirmation_phrase.toUpperCase()) {
      message.error('确认短语不一致')
      return
    }
    const operation = review.action === 'confirm'
      ? confirm.mutateAsync({item:review.item, text:confirmationText.trim()})
      : reject.mutateAsync(review.item)
    void operation.then(() => setReview(null)).catch(() => undefined)
  }

  if (!actions.length) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前会话没有待确认操作" />
  }

  return <><List
    dataSource={actions}
    renderItem={(item) => <List.Item>
      <Card
        size="small"
        style={{ width: '100%' }}
        title={<Space><Tag color="orange">{item.execution_status === 'queued' || item.execution_status === 'running' ? '处理中' : item.execution_status === 'blocked' ? '需要核对' : item.can_retry && item.decision ? '可重试' : '等待确认'}</Tag><Typography.Text>{item.operation_type || item.intent}</Typography.Text></Space>}
        extra={<Typography.Text type="secondary">{item.created_at}</Typography.Text>}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>Proposal：{item.proposal_id}</Typography.Text>
          {item.error && <Typography.Text type="danger">{item.error}</Typography.Text>}
          <pre className="agent-json-block">{JSON.stringify({
            before: item.before_state_summary,
            changes: item.proposed_changes,
            after: item.after_state_preview,
            warnings: item.warnings,
          }, null, 2)}</pre>
          <Space>
            <Button type="primary" disabled={item.can_retry === false || item.decision === 'rejected' || reject.isPending} loading={confirm.isPending} onClick={() => openConfirm(item)}>{item.decision === 'approved' ? '重试已批准操作' : '确认'}</Button>
            <Button danger disabled={item.can_retry === false || item.decision === 'approved' || confirm.isPending} loading={reject.isPending}
              onClick={() => setReview({item, action:'reject'})}>拒绝</Button>
          </Space>
        </Space>
      </Card>
    </List.Item>}
  />
    <Modal open={Boolean(review)} title={review?.action === 'confirm' ? '确认执行 Agent 待确认操作？' : '拒绝该计划？'} width={620}
      okText={review?.action === 'confirm' ? '确认执行' : '拒绝'} cancelText="取消"
      confirmLoading={busy} cancelButtonProps={{disabled:busy}} okButtonProps={{danger:review?.action === 'reject'}}
      onCancel={() => { if (!busy) setReview(null) }} onOk={submitReview}>
      {review?.action === 'confirm' ? <Space direction="vertical" style={{width:'100%'}}>
        <Typography.Paragraph>服务端会重新校验当前账号、参数和审批状态。请输入：
          <Typography.Text code>{review.item.confirmation_phrase}</Typography.Text>
        </Typography.Paragraph>
        <Input value={confirmationText} onChange={event => setConfirmationText(event.target.value)} placeholder={review.item.confirmation_phrase} />
      </Space> : '拒绝后不会执行本次申请的操作。'}
    </Modal>
  </>
}
