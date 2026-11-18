import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button, Input, Modal, Typography, message } from 'antd'
import { strategiesApi } from '../../api/strategiesApi'
import { errorMessage } from '../../api/errors'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useAuthStore } from '../../stores/authStore'
import type { BacktestDetail } from '../../types/backtests'
import type { StrategyWorkspaceItem } from '../../types/strategies'
import { backtestRequestId } from '../../utils/backtest'

export function SaveStrategyFromReport({ report, onSaved }: { report: BacktestDetail; onSaved?: (strategy: StrategyWorkspaceItem) => void }) {
  const owner = useCurrentUserId(), client = useQueryClient()
  const [open, setOpen] = useState(false), [name, setName] = useState(report.spec.name)
  const pending = useRef<{ fingerprint: string; id: string } | null>(null)
  const save = useMutation({
    mutationFn: async () => {
      const value = name.trim()
      if (!value) throw new Error('请填写策略名称。')
      const fingerprint = JSON.stringify([owner, report.run_id, value])
      if (!pending.current || pending.current.fingerprint !== fingerprint) pending.current = { fingerprint, id: backtestRequestId() }
      return strategiesApi.fromReport(report.run_id, value, pending.current.id)
    },
    onSuccess: strategy => {
      if (useAuthStore.getState().user?.user_id !== owner) return
      pending.current = null
      setOpen(false)
      void client.invalidateQueries({ queryKey: ['strategies', owner] })
      void client.invalidateQueries({ queryKey: ['backtests', owner, 'strategies'] })
      message.success('已保存到我的策略，原回测报告保持不变')
      onSaved?.(strategy)
    },
    onError: error => message.error(errorMessage(error)),
  })
  if (report.kind !== 'backtest') return null
  return <>
    <Button onClick={() => { setName(report.spec.name); setOpen(true) }}>保存到我的策略</Button>
    <Modal title="保存到我的策略" open={open} onCancel={() => { if (!save.isPending) setOpen(false) }}
      onOk={() => save.mutate()} confirmLoading={save.isPending} okText="确认保存" cancelButtonProps={{ disabled: save.isPending }}>
      <Typography.Paragraph>保存这份已完成报告的参数及关联记录，不重复运行回测，也不改变模拟账户。</Typography.Paragraph>
      <label htmlFor="save-strategy-name">策略名称</label>
      <Input id="save-strategy-name" value={name} maxLength={80} onChange={event => setName(event.target.value)} disabled={save.isPending} />
      {save.error && <Typography.Paragraph type="danger" style={{ marginTop: 12 }}>{errorMessage(save.error)}</Typography.Paragraph>}
    </Modal>
  </>
}
