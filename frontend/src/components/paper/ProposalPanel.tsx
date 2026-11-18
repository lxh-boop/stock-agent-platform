import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Collapse, Input, Modal, Space, Tag, Typography, message } from 'antd'
import { PaperSectionCard } from './PaperSectionCard'
import { paperTradingApi } from '../../api/paperTradingApi'
import { queryKeys } from '../../api/queryKeys'
import { submitTask } from '../../api/taskApi'
import { createWriteMeta } from '../../api/writeOperationApi'
import { useSessionStore } from '../../stores/sessionStore'
import { useTaskStore } from '../../stores/taskStore'
import { RecordTable } from '../common/RecordTable'
import type { PaperProposal } from '../../types/paperTrading'

export function ProposalPanel({ userId, proposals }: { userId: string; proposals: PaperProposal[] }) {
  const queryClient = useQueryClient()
  const sessionId = useSessionStore((state) => state.sessionId)
  const setTask = useTaskStore((state) => state.setTask)
  const clearTask = useTaskStore((state) => state.clear)
  const [confirmations, setConfirmations] = useState<Record<string, string>>({})
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.paperProposals(userId) })
    await queryClient.invalidateQueries({ queryKey: queryKeys.paperTrading(userId) })
  }
  const commit = useMutation({
    mutationFn: async ({ proposal, text }: { proposal: PaperProposal; text: string }) => {
      const meta = createWriteMeta('proposal-commit')
      if (proposal.intent === 'paper_backfill') {
        clearTask()
        const task = await submitTask({
          task_type: 'paper-trading.backfill',
          kwargs: { user_id:userId, plan_id:proposal.plan_id, confirmation_text:text, ...meta },
          owner_id:userId, session_id:sessionId,
          metadata:{surface:'react-stage6-3',operation:'paper-backfill-commit'},
          timeout_seconds:997200, max_retries:0,
        })
        setTask(task)
        return { task_id: task.task_id }
      }
      return paperTradingApi.commitProposal(proposal.plan_id, { ...meta, user_id:userId, confirmation_text:text })
    },
    onSuccess: async (result) => {
      message.success('task_id' in result ? '历史回填任务已提交，可取消并在刷新后恢复' : '服务端已处理确认请求')
      await refresh()
    },
    onError: (error) => message.error(String(error)),
  })
  const reject = useMutation({
    mutationFn: (planId: string) => paperTradingApi.rejectProposal(planId, { ...createWriteMeta('proposal-reject'), user_id:userId, reason:'react_user_rejected' }),
    onSuccess: async () => { message.success('预案已拒绝'); await refresh() },
    onError: (error) => message.error(String(error)),
  })
  if (!proposals.length) return <PaperSectionCard sectionKey="proposals" title="待确认预案"><Typography.Text type="secondary">当前没有资金或回填待确认预案</Typography.Text></PaperSectionCard>
  return <PaperSectionCard sectionKey="proposals" title="待确认预案" extra={<Tag color="warning">服务端权威状态</Tag>}>
    <Collapse items={proposals.map((proposal) => {
      const typed = (confirmations[proposal.plan_id] ?? '').trim().toUpperCase()
      const valid = typed === proposal.confirmation_phrase.toUpperCase()
      return {
        key:proposal.plan_id,
        label:<Space><Typography.Text code>{proposal.plan_id.slice(-10)}</Typography.Text><Tag>{proposal.intent}</Tag><Tag color={proposal.confirmation_status === 'pending' ? 'warning' : 'default'}>{proposal.confirmation_status || 'unknown'}</Tag></Space>,
        children:<Space direction="vertical" style={{width:'100%'}}>
          <Alert type="warning" showIcon message={proposal.intent === 'paper_backfill' ? '历史回填将通过长任务执行' : '确认前请核对变更与警告'} description={(proposal.warnings ?? []).map(String).join('；') || '无额外警告'} />
          <RecordTable records={(proposal.proposed_changes ?? []).filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')} maxColumns={14} />
          <Typography.Text>请输入确认短语：<Typography.Text code>{proposal.confirmation_phrase}</Typography.Text></Typography.Text>
          <Input value={confirmations[proposal.plan_id] ?? ''} onChange={(event)=>setConfirmations((state)=>({...state,[proposal.plan_id]:event.target.value}))} placeholder={proposal.confirmation_phrase} />
          <Space>
            <Button type="primary" danger disabled={!valid || proposal.confirmation_status !== 'pending'} loading={commit.isPending} onClick={()=>Modal.confirm({title:'最终确认执行预案？',content:proposal.intent === 'paper_backfill' ? '任务提交后将重新校验预案，并在独立 Worker 中执行历史回填。' : '执行后将由服务端重新校验业务状态并写入模拟盘。',okText:'确认执行',okButtonProps:{danger:true},onOk:()=>commit.mutate({proposal,text:confirmations[proposal.plan_id] ?? ''})})}>确认执行</Button>
            <Button disabled={proposal.confirmation_status !== 'pending'} loading={reject.isPending} onClick={()=>reject.mutate(proposal.plan_id)}>拒绝预案</Button>
          </Space>
        </Space>,
      }
    })} />
  </PaperSectionCard>
}
