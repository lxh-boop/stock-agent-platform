import { Button, Input, Space, Typography } from 'antd'
import { useRef, useState } from 'react'

const QUICK_QUESTIONS = [
  '查看当前模拟盘账户和持仓',
  '查看当前预测排名前十的股票',
  '分析贵州茅台',
  '查看每日自动更新和调度状态',
]

export function ChatComposer({
  disabled,
  loading,
  onSubmit,
}: {
  disabled: boolean
  loading: boolean
  onSubmit: (value: string) => Promise<void> | void
}) {
  const [value, setValue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const busy = loading || submitting

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

  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Space wrap>
      <Typography.Text type="secondary">快捷提问：</Typography.Text>
      {QUICK_QUESTIONS.map((item) => <Button
        key={item}
        size="small"
        disabled={disabled || busy}
        onClick={() => void submit(item)}
      >{item}</Button>)}
    </Space>
    <Input.TextArea
      value={value}
      onChange={(event) => setValue(event.target.value)}
      autoSize={{ minRows: 3, maxRows: 8 }}
      maxLength={20_000}
      placeholder="请输入问题，例如：分析 600519，或查看当前模拟盘持仓"
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
      <Typography.Text type="secondary">Enter 发送，Shift + Enter 换行</Typography.Text>
      <Button
        type="primary"
        loading={busy}
        disabled={disabled || !value.trim()}
        onClick={() => void submit()}
      data-testid="agent-send-button"
      >发送</Button>
    </div>
  </Space>
}
