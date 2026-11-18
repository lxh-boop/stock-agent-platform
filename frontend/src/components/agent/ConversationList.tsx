import { useEffect, useState } from 'react'
import { Button, Empty, Input, List, Modal, Popconfirm, Tooltip, Typography } from 'antd'
import type { AgentSession } from '../../types/agent'
import { formatAgentTime } from '../../utils/agentTime'
import { formatTaskTime } from '../../utils/taskTime'

export function ConversationList({
  sessions,
  activeId,
  loading,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}: {
  sessions: AgentSession[]
  activeId: string
  loading: boolean
  onSelect: (conversationId: string) => void
  onCreate: () => void
  onRename: (conversationId: string, title: string) => void
  onDelete: (conversationId: string) => void
}) {
  const [renameTarget, setRenameTarget] = useState<AgentSession | null>(null)
  const [renameValue, setRenameValue] = useState('')

  useEffect(() => {
    setRenameValue(renameTarget?.title ?? '')
  }, [renameTarget])

  const submitRename = () => {
    const title = renameValue.trim()
    if (!renameTarget || !title) return
    onRename(renameTarget.conversation_id, title)
    setRenameTarget(null)
  }

  return <div className="agent-conversation-list" data-testid="agent-conversation-list">
    <div className="agent-conversation-toolbar">
      <Typography.Text strong>会话</Typography.Text>
      <Button type="primary" size="small" onClick={onCreate} data-testid="agent-create-session">新建</Button>
    </div>
    <List
      loading={loading}
      dataSource={sessions}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无会话" /> }}
      renderItem={(item) => {
        const active = item.conversation_id === activeId
        return <List.Item
          className={active ? 'agent-conversation-item agent-conversation-item--active' : 'agent-conversation-item'}
          data-testid="agent-conversation-item"
          data-conversation-id={item.conversation_id}
          title={`会话 ID：${item.conversation_id}`}
          onClick={() => onSelect(item.conversation_id)}
          actions={[
            <Tooltip title="重命名" key="rename">
              <Button
                type="text"
                size="small"
                aria-label="重命名会话"
                icon={<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m16 3 5 5L8 21H3v-5L16 3Z"/><path d="m13 6 5 5"/></svg>}
                onClick={(event) => {
                  event.stopPropagation()
                  setRenameTarget(item)
                }}
              />
            </Tooltip>,
            <Popconfirm
              key="delete"
              title="删除当前会话？"
              description="仅软删除会话，不删除运行审计记录。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => onDelete(item.conversation_id)}
            >
              <Button
                type="text" danger size="small" aria-label="删除会话"
                icon={<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg>}
                onClick={(event) => event.stopPropagation()}
              />
            </Popconfirm>,
          ]}
        >
          <List.Item.Meta
            title={<Typography.Text ellipsis title={item.title || '新会话'} strong={active}>{item.title || '新会话'}</Typography.Text>}
            description={<Typography.Text
              type="secondary"
              className="agent-conversation-time"
              title={formatTaskTime(item.last_message_at || item.updated_at)}
            >{formatAgentTime(item.last_message_at || item.updated_at)}</Typography.Text>}
          />
        </List.Item>
      }}
    />
    <Modal
      title="重命名会话"
      open={Boolean(renameTarget)}
      okText="保存"
      cancelText="取消"
      okButtonProps={{ disabled: !renameValue.trim() }}
      onOk={submitRename}
      onCancel={() => setRenameTarget(null)}
      destroyOnHidden
    >
      <Input
        value={renameValue}
        maxLength={80}
        autoFocus
        placeholder="请输入新的会话标题"
        onChange={(event) => setRenameValue(event.target.value)}
        onPressEnter={submitRename}
      />
    </Modal>
  </div>
}
