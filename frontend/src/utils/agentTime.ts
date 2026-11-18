/** Compact local time for chat metadata; keep the full value in a tooltip. */
export function formatAgentTime(value?: string | null): string {
  if (!value) return '尚无消息'
  const time = new Date(value)
  if (Number.isNaN(time.getTime())) return value
  return time.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
