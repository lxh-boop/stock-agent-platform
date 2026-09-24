import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Button,
  Card,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd'
import { skillsApi } from '../../api/skillsApi'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import type { SkillSummary } from '../../types/skills'

const STATUS_TAGS: Record<SkillSummary['status'], { color: string; label: string }> = {
  admitted: { color: 'green', label: '已启用' },
  disabled: { color: 'default', label: '已停用' },
  rejected: { color: 'red', label: '已拒绝' },
}

const SOURCE_LABELS: Record<SkillSummary['source_type'], string> = {
  builtin: '内置',
  editor: '在线编写',
  upload: 'zip 上传',
  github: 'GitHub 导入',
}

export function SkillsPage() {
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [githubOpen, setGithubOpen] = useState(false)
  const [githubUrl, setGithubUrl] = useState('')
  const [pendingFile, setPendingFile] = useState<File | null>(null)
  const [createForm] = Form.useForm()

  const listQuery = useQuery({ queryKey: ['web', 'skills'], queryFn: () => skillsApi.list() })
  const [detailName, setDetailName] = useState<string | null>(null)
  const detail = useQuery({
    queryKey: ['web', 'skills', 'detail', detailName],
    queryFn: () => skillsApi.detail(detailName ?? ''),
    enabled: Boolean(detailName),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['web', 'skills'] })
  const reportError = (error: unknown) => {
    const detail = (error as { payload?: { details?: { reason?: string } } })?.payload?.details?.reason
    message.error(detail ? `操作失败：${detail}` : '操作失败')
  }

  const createMutation = useMutation({
    mutationFn: (payload: { name: string; description: string; body: string }) => skillsApi.create(payload),
    onSuccess: (skill) => {
      message.success(`技能 ${skill.name} 已创建`)
      setCreateOpen(false)
      createForm.resetFields()
      refresh()
    },
    onError: reportError,
  })
  const githubMutation = useMutation({
    mutationFn: () => skillsApi.importGithub(githubUrl),
    onSuccess: (skill) => {
      message.success(`技能 ${skill.name} 已导入`)
      setGithubOpen(false)
      setGithubUrl('')
      refresh()
    },
    onError: reportError,
  })
  const uploadMutation = useMutation({
    mutationFn: () => skillsApi.upload(pendingFile as File),
    onSuccess: (skill) => {
      message.success(`技能 ${skill.name} 已上传`)
      setPendingFile(null)
      refresh()
    },
    onError: reportError,
  })
  const toggleMutation = useMutation({
    mutationFn: ({ name, enable }: { name: string; enable: boolean }) =>
      enable ? skillsApi.enable(name) : skillsApi.disable(name),
    onSuccess: () => {
      message.success('状态已更新')
      refresh()
    },
    onError: reportError,
  })
  const deleteMutation = useMutation({
    mutationFn: (name: string) => skillsApi.remove(name),
    onSuccess: () => {
      message.success('技能已删除')
      refresh()
    },
    onError: reportError,
  })

  if (listQuery.isLoading) return <PageLoading />
  if (listQuery.error) return <EmptyState title="技能列表加载失败" description={String(listQuery.error)} />

  const rows = listQuery.data?.skills ?? []
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <PageHeader
        title="技能管理"
        description="编写、导入和管理个人技能（Agent Skills 开放标准）。技能只作为方法指引注入 Agent，脚本永不执行；仅本人可见。"
      />
      <Card>
        <Space wrap>
          <Button type="primary" onClick={() => setCreateOpen(true)}>新建技能</Button>
          <Button onClick={() => setGithubOpen(true)}>从 GitHub 导入</Button>
          <Upload
            maxCount={1}
            accept=".zip"
            beforeUpload={(file) => {
              setPendingFile(file)
              return false
            }}
            onRemove={() => setPendingFile(null)}
            fileList={pendingFile ? [pendingFile as never] : []}
          >
            <Button disabled={Boolean(pendingFile)}>上传 zip 包</Button>
          </Upload>
          {pendingFile ? (
            <Button
              type="primary"
              loading={uploadMutation.isPending}
              onClick={() => uploadMutation.mutate()}
            >
              确认上传
            </Button>
          ) : null}
        </Space>
      </Card>
      <Card title={`我的技能 · ${rows.length} 个`}>
        <Table<SkillSummary>
          rowKey={(record) => `${record.source_type}:${record.name}`}
          dataSource={rows}
          pagination={false}
          columns={[
            {
              title: '名称',
              dataIndex: 'name',
              render: (value: string, record) => (
                <Space>
                  <Typography.Text strong>{value}</Typography.Text>
                  <Tag>v{record.version}</Tag>
                  {record.has_scripts ? <Tag color="orange">含脚本(不执行)</Tag> : null}
                </Space>
              ),
            },
            { title: '描述', dataIndex: 'description', ellipsis: true },
            { title: '来源', dataIndex: 'source_type', width: 110, render: (value: SkillSummary['source_type']) => SOURCE_LABELS[value] ?? value },
            {
              title: '状态',
              dataIndex: 'status',
              width: 110,
              render: (value: SkillSummary['status'], record) => (
                <Space size={4}>
                  <Tag color={STATUS_TAGS[value].color}>{STATUS_TAGS[value].label}</Tag>
                  {value === 'rejected' && record.rejection_reason ? (
                    <Typography.Text type="danger" style={{ fontSize: 12 }}>{record.rejection_reason}</Typography.Text>
                  ) : null}
                </Space>
              ),
            },
            {
              title: '操作',
              key: 'actions',
              width: 260,
              render: (_, record) => {
                const personal = record.source_type !== 'builtin'
                return (
                  <Space size={4} wrap>
                    <Button size="small" onClick={() => setDetailName(record.name)}>详情</Button>
                    {personal && record.status !== 'rejected' ? (
                      <Button
                        size="small"
                        loading={toggleMutation.isPending}
                        onClick={() => toggleMutation.mutate({ name: record.name, enable: record.status !== 'admitted' })}
                      >
                        {record.status === 'admitted' ? '停用' : '启用'}
                      </Button>
                    ) : null}
                    {personal ? (
                      <Popconfirm title="确认删除该技能？" onConfirm={() => deleteMutation.mutate(record.name)}>
                        <Button size="small" danger loading={deleteMutation.isPending}>删除</Button>
                      </Popconfirm>
                    ) : null}
                  </Space>
                )
              },
            },
          ]}
        />
      </Card>

      <Modal
        title="新建技能"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        confirmLoading={createMutation.isPending}
        onOk={() => createForm.submit()}
        okText="创建"
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => createMutation.mutate(values)}
        >
          <Form.Item
            name="name"
            label="技能名（小写字母/数字/下划线）"
            rules={[
              { required: true },
              { pattern: /^[a-z][a-z0-9_]{2,63}$/, message: '3-64 位小写字母开头，可含数字和下划线' },
            ]}
          >
            <Input placeholder="my_signal_checklist" />
          </Form.Item>
          <Form.Item name="description" label="一句话描述" rules={[{ required: true, max: 500 }]}>
            <Input placeholder="说明这个技能指导 Agent 做什么" />
          </Form.Item>
          <Form.Item
            name="body"
            label="技能正文（Markdown：Purpose / Input / Process / Constraints / Examples）"
            rules={[{ required: true, max: 64_000 }]}
          >
            <Input.TextArea rows={12} placeholder={'## Purpose\n...\n\n## Process\n1. ...\n\n## Constraints\n- ...'} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="从 GitHub 导入（仅公开仓库）"
        open={githubOpen}
        onCancel={() => setGithubOpen(false)}
        confirmLoading={githubMutation.isPending}
        onOk={() => githubMutation.mutate()}
        okText="导入"
      >
        <Input
          value={githubUrl}
          onChange={(event) => setGithubUrl(event.target.value)}
          placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
        />
        <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
          支持 repo 根目录或 /tree/分支/子目录 形式；目标目录需包含 SKILL.md。导入失败时可手动下载 zip 后用上传通道。
        </Typography.Paragraph>
      </Modal>

      <Drawer
        title={detailName ? `技能详情 · ${detailName}` : ''}
        width={720}
        open={Boolean(detailName)}
        onClose={() => setDetailName(null)}
      >
        {detail.isLoading ? (
          <Spin />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Typography.Paragraph>{detail.data?.description}</Typography.Paragraph>
            {detail.data?.source_url ? (
              <Typography.Paragraph type="secondary">来源：{detail.data.source_url}</Typography.Paragraph>
            ) : null}
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              content_hash: {detail.data?.content_hash}
            </Typography.Paragraph>
            <pre style={{ background: '#f6f6f6', padding: 16, borderRadius: 8, whiteSpace: 'pre-wrap' }}>
              {detail.data?.body}
            </pre>
          </Space>
        )}
      </Drawer>
    </Space>
  )
}
