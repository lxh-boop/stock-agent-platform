import { Button, Empty, Space, Tag, Typography } from 'antd'
import type { AgentMessage } from '../../types/agent'
import { MarkdownContent } from '../common/MarkdownContent'

export function ChatMessageList({
  messages,
  selectedRunId,
  onSelectRun,
}: {
  messages: AgentMessage[]
  selectedRunId: string
  onSelectRun: (runId: string) => void
}) {
  if (!messages.length) {
    return <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description="当前会话还没有消息，可以直接输入问题。"
    />
  }

  return <div className="agent-chat-messages" data-testid="agent-message-list">
    {messages.map((item) => {
      const assistant = item.role === 'assistant'
      return <div
        key={item.message_id}
        className={`agent-chat-row ${assistant ? 'agent-chat-row--assistant' : 'agent-chat-row--user'}`}
      >
        <div className="agent-chat-bubble">
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color={assistant ? 'blue' : 'green'}>{assistant ? 'AI Agent' : '用户'}</Tag>
              <Typography.Text type="secondary">{item.created_at}</Typography.Text>
              {item.run_id ? <Button
                size="small"
                type={selectedRunId === item.run_id ? 'primary' : 'default'}
                onClick={() => onSelectRun(item.run_id)}
              >运行详情</Button> : null}
            </Space>
            {assistant
              ? <MarkdownContent content={item.content} />
              : <Typography.Paragraph className="agent-message-content">
                {item.content}
              </Typography.Paragraph>}
          </Space>
        </div>
      </div>
    })}
  </div>
}
