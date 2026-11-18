import { Button, Dropdown, Input, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'

export interface StrategyDraft { text: string; prefillId: string }

const QUICK_QUESTIONS = [
  '查看我的策略和最近回测',
  '查看本人可用因子和数据日期范围',
  '分析贵州茅台',
  '查看每日自动更新和调度状态',
]

export function ChatComposer({
  disabled,
  loading,
  onSubmit,
  strategyDraft,
}: {
  disabled: boolean
  loading: boolean
  onSubmit: (value: string) => Promise<void> | void
  strategyDraft?: StrategyDraft | null
}) {
  const [value, setValue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const consumedDraft = useRef('')
  const [pendingDraft, setPendingDraft] = useState<StrategyDraft | null>(null)
  const busy = loading || submitting

  useEffect(() => {
    if (!strategyDraft || consumedDraft.current === strategyDraft.prefillId) return
    consumedDraft.current = strategyDraft.prefillId
    if (value.trim()) setPendingDraft(strategyDraft)
    else setValue(strategyDraft.text)
  }, [strategyDraft, value])

  const submit = async (question = value) => {
    const normalized = question.trim()
    if (!normalized || disabled || loading || submittingRef.current) return
    submittingRef.current = true
    setSubmitting(true)
    try {
      await onSubmit(normalized)
      setValue((current) => current === question ? '' : current)
    } catch {
      // The parent shows the API error. Retain the draft and consume the
      // rejection so both the button and Enter remain usable for a retry.
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  return <div className="agent-composer-inner">
    {pendingDraft && <Space wrap data-testid="strategy-draft-conflict">
      <Typography.Text>策略草稿已准备好，当前输入已保留。</Typography.Text>
      <Button disabled={busy} onClick={() => { setValue(pendingDraft.text); setPendingDraft(null) }}>使用策略草稿替换输入</Button>
      <Button onClick={() => setPendingDraft(null)}>忽略草稿</Button>
    </Space>}
    <Input.TextArea
      value={value}
      onChange={(event) => setValue(event.target.value)}
      autoSize={{ minRows: 2, maxRows: 5 }}
      maxLength={20_000}
      placeholder="请输入问题，例如：研究 MA5 因子策略，或查看最近回测报告"
      data-testid="agent-composer-input"
      disabled={disabled}
      onPressEnter={(event) => {
        if (!event.shiftKey) {
          event.preventDefault()
          void submit()
        }
      }}
    />
    <div className="agent-composer-actions">
      <Space size={8}>
        <Dropdown
          trigger={['click']}
          placement="topLeft"
          disabled={disabled || busy}
          menu={{ items: QUICK_QUESTIONS.map((item) => ({ key: item, label: item })), onClick: ({ key }) => void submit(key) }}
        >
          <Button size="small" disabled={disabled || busy} data-testid="agent-quick-questions">快捷提问</Button>
        </Dropdown>
        <Typography.Text type="secondary" className="agent-composer-hint">Enter 发送 · Shift + Enter 换行</Typography.Text>
      </Space>
      <Button
        type="primary"
        loading={busy}
        disabled={disabled || !value.trim()}
        onClick={() => void submit()}
        data-testid="agent-send-button"
      >发送</Button>
    </div>
  </div>
}
