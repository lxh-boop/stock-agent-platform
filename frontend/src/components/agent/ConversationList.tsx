import { useEffect, useState } from 'react'
import { Button, Empty, Input, List, Modal, Popconfirm, Space, Tooltip, Typography } from 'antd'
import type { AgentSession } from '../../types/agent'

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
          onClick={() => onSelect(item.conversation_id)}
          actions={[
            <Tooltip title="重命名" key="rename">
              <Button
                type="text"
                size="small"
                onClick={(event) => {
                  event.stopPropagation()
                  setRenameTarget(item)
                }}
              >改名</Button>
            </Tooltip>,
            <Popconfirm
              key="delete"
              title="删除当前会话？"
              description="仅软删除会话，不删除运行审计记录。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => onDelete(item.conversation_id)}
            >
              <Button type="text" danger size="small" onClick={(event) => event.stopPropagation()}>删除</Button>
            </Popconfirm>,
          ]}
        >
          <List.Item.Meta
            title={<Typography.Text ellipsis strong={active}>{item.title || 'New conversation'}</Typography.Text>}
            description={<Space direction="vertical" size={0}>
              <Typography.Text type="secondary" className="agent-conversation-time">
                {item.last_message_at || item.updated_at || '尚无消息'}
              </Typography.Text>
              <Typography.Text type="secondary" className="agent-conversation-id">
                {item.conversation_id.slice(-10)}
              </Typography.Text>
            </Space>}
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
