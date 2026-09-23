import { useEffect, useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Row,
  Space,
  Tag,
  Typography,
  message,
} from 'antd'
import { createWriteMeta } from '../../api/writeOperationApi'
import { ApiContractError } from '../../api/errors'
import { settingsApi } from '../../api/settingsApi'
import { SchedulerStatusPanel } from '../../components/dashboard/SchedulerStatusPanel'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { useAuthStore } from '../../stores/authStore'
import type { PublicSettings, SettingsUpdateRequest } from '../../types/settings'

interface SettingsFormValues {
  llm_mode: 'api' | 'local'
  api_provider: string
  api_base_url: string
  api_model: string
  api_credential?: string
  clear_api_credential: boolean
  local_base_url: string
  local_model: string
  tushare_credential?: string
  clear_tushare_credential: boolean
  scheduler_enabled: boolean
  scheduler_hour: number
  scheduler_minute: number
  scheduler_catch_up: boolean
}

const EMPTY_SETTINGS: Pick<PublicSettings, 'feature_flags' | 'credentials' | 'llm' | 'configuration' | 'scheduler'> = {
  feature_flags: {},
  credentials: { tushare_configured: false, llm_configured: false },
  llm: { mode: '', provider: '', model: '', endpoint_configured: false, profile_id: '' },
  configuration: {
    llm_mode: 'api',
    api: { provider: 'openai_compatible', base_url: '', model: '', configured: false, custom_configured: false, default_available: false },
    local: { provider: 'ollama_local', base_url: 'http://127.0.0.1:11434/v1', effective_base_url: '', model: 'stock-agent-qwen3-4b' },
    tushare: { configured: false, custom_configured: false, default_available: false },
  },
  scheduler: { enabled: false, hour: 20, minute: 0, timezone: 'Asia/Shanghai', catch_up: true, runtime_running: false, job_registered: false, next_run_time: '', expected_signal_date: '', latest_signal_date: '', stale: false, last_started_at: '', last_finished_at: '', last_trade_date: '', last_status: 'unknown', current_step: '', last_error: '' },
}

function settingsErrorMessage(error: unknown): string {
  if (error instanceof ApiContractError) {
    const reason = error.details.reason
    const parts = [
      error.message,
      reason ? String(reason) : '',
      `code=${error.code}`,
    ].filter(Boolean)
    return parts.join('；')
  }
  return String(error ?? '未知错误')
}

function normalizeApiBaseUrl(value: string): string {
  const text = value.trim().replace(/\/+$/, '')
  if (text === 'https://open.bigmodel.cn/api/v1') return 'https://open.bigmodel.cn/api/paas/v4'
  return text
}

function configuredText(custom: boolean, fallback: boolean, configured: boolean): string {
  if (custom) return '已自定义'
  if (fallback) return '使用默认配置'
  return configured ? '已配置' : '未配置'
}

export function SettingsPage() {
  const queryClient = useQueryClient()
  const [form] = Form.useForm<SettingsFormValues>()
  const user = useAuthStore((state) => state.user)
  const isAdmin = user?.role === 'admin'
  const query = useQuery({ queryKey: ['web', 'settings'], queryFn: settingsApi.get })

  const settings = useMemo<PublicSettings | undefined>(() => query.data
    ? { ...EMPTY_SETTINGS, ...query.data, feature_flags: query.data.feature_flags ?? {}, credentials: query.data.credentials ?? EMPTY_SETTINGS.credentials, llm: query.data.llm ?? EMPTY_SETTINGS.llm, configuration: query.data.configuration ?? EMPTY_SETTINGS.configuration, scheduler: query.data.scheduler ?? EMPTY_SETTINGS.scheduler }
    : undefined, [query.data])

  useEffect(() => {
    if (!settings) return
    form.setFieldsValue({
      llm_mode: settings.configuration.llm_mode,
      api_provider: settings.configuration.api.provider,
      api_base_url: settings.configuration.api.base_url,
      api_model: settings.configuration.api.model,
      api_credential: '',
      clear_api_credential: false,
      local_base_url: settings.configuration.local.base_url,
      local_model: settings.configuration.local.model,
      tushare_credential: '',
      clear_tushare_credential: false,
      scheduler_enabled: settings.scheduler.enabled,
      scheduler_hour: settings.scheduler.hour,
      scheduler_minute: settings.scheduler.minute,
      scheduler_catch_up: settings.scheduler.catch_up,
    })
  }, [form, settings])

  const mutation = useMutation({
    mutationFn: (values: SettingsFormValues) => {
      const payload: SettingsUpdateRequest = {
        ...createWriteMeta('runtime-settings'),
        ...values,
        api_base_url: normalizeApiBaseUrl(values.api_base_url),
        api_credential: values.clear_api_credential ? undefined : values.api_credential?.trim() || undefined,
        tushare_credential: values.clear_tushare_credential ? undefined : values.tushare_credential?.trim() || undefined,
        confirmed: true,
      }
      return settingsApi.update(payload)
    },
    onSuccess: async () => {
      message.success('配置已保存；新的 Agent 或数据任务将使用最新配置')
      form.setFieldsValue({ api_credential: '', tushare_credential: '', clear_api_credential: false, clear_tushare_credential: false })
      await queryClient.invalidateQueries({ queryKey: ['web', 'settings'] })
    },
    onError: (error) => message.error(`配置保存失败：${settingsErrorMessage(error)}`),
  })

  const watchedMode = Form.useWatch('llm_mode', form)

  if (query.isLoading) return <PageLoading />
  if (query.error || !settings) {
    return <EmptyState title="设置读取失败" description={String(query.error ?? '无数据')} />
  }

  const mode = watchedMode ?? settings.configuration.llm_mode
  const api = settings.configuration.api
  const local = settings.configuration.local
  const tushare = settings.configuration.tushare

  const submit = async () => {
    const values = await form.validateFields()
    Modal.confirm({
      title: '确认保存运行配置？',
      content: '配置会写入本机 local_app_config.json。密钥不会回显到浏览器，正在运行的任务不受影响。',
      okText: '确认保存',
      onOk: () => mutation.mutate(values),
    })
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <PageHeader
        title="系统设置"
        description="配置本地模型、远程大模型 API 和 Tushare；敏感值只在保存请求中提交，不会由服务端回显。"
      />
      <Alert
        type="info"
        showIcon
        message="配置生效范围"
        description="保存后，下一次 Agent、新闻或行情任务会重新读取配置。正在运行的任务继续使用启动时的配置快照。"
      />
      {!isAdmin ? (
        <Alert
          type="warning"
          showIcon
          message="仅管理员可修改运行配置"
          description="LLM、数据源与调度配置是平台级全局资源，当前账号为普通用户，只能查看。"
        />
      ) : null}

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="业务配置">
            <Descriptions column={1}>
              <Descriptions.Item label="股票池">{settings.universe}</Descriptions.Item>
              <Descriptions.Item label="模型后端">{settings.model_backend}</Descriptions.Item>
              <Descriptions.Item label="模型版本">{settings.model_version}</Descriptions.Item>
              <Descriptions.Item label="默认 TopK">{settings.default_topk}</Descriptions.Item>
              <Descriptions.Item label="当前用户">{settings.current_user_id}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="能力状态">
            <Descriptions column={1}>
              {Object.entries(settings.feature_flags).map(([key, value]) => (
                <Descriptions.Item key={key} label={key}>
                  <Tag color={value ? 'success' : 'default'}>{value ? '开启' : '关闭'}</Tag>
                </Descriptions.Item>
              ))}
              <Descriptions.Item label="Tushare">
                <Tag color={tushare.configured ? 'success' : 'warning'}>
                  {configuredText(tushare.custom_configured, tushare.default_available, tushare.configured)}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="LLM">
                <Tag color={settings.credentials.llm_configured ? 'success' : 'warning'}>
                  {settings.credentials.llm_configured ? '可用' : '未完整配置'}
                </Tag>
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Form form={form} layout="vertical" requiredMark={false}>
        <Card title="大模型配置">
          <Form.Item name="llm_mode" label="运行模式" rules={[{ required: true }]}>
            <Radio.Group optionType="button" buttonStyle="solid">
              <Radio.Button value="local">本地模型</Radio.Button>
              <Radio.Button value="api">远程 API</Radio.Button>
            </Radio.Group>
          </Form.Item>

          {mode === 'api' ? (
            <Row gutter={[16, 0]}>
              <Col xs={24} lg={8}>
                <Form.Item name="api_provider" label="Provider" rules={[{ required: true, message: '请填写 Provider' }]}>
                  <Input placeholder="openai_compatible / deepseek" />
                </Form.Item>
              </Col>
              <Col xs={24} lg={8}>
                <Form.Item name="api_model" label="模型名称" rules={[{ required: true, message: '请填写模型名称' }]}>
                  <Input placeholder="例如 gpt-4o-mini" />
                </Form.Item>
              </Col>
              <Col xs={24} lg={8}>
                <Form.Item name="api_base_url" label="API Base URL">
                  <Input placeholder="OpenAI 官方接口可留空" />
                </Form.Item>
              </Col>
              <Col xs={24} lg={12}>
                <Form.Item label={`API 凭据（${configuredText(api.custom_configured, api.default_available, api.configured)}）`} name="api_credential">
                  <Input type="password" autoComplete="new-password" placeholder="留空表示保留当前配置或继续使用默认配置" />
                </Form.Item>
              </Col>
              <Col xs={24} lg={12}>
                <Form.Item name="clear_api_credential" valuePropName="checked" label="凭据操作">
                  <Checkbox>清除本地自定义凭据，改用环境默认配置</Checkbox>
                </Form.Item>
              </Col>
            </Row>
          ) : (
            <Row gutter={[16, 0]}>
              <Col xs={24} lg={12}>
                <Form.Item name="local_model" label="Ollama 模型名称" rules={[{ required: true, message: '请填写本地模型名称' }]}>
                  <Input placeholder="例如 qwen3:4b" />
                </Form.Item>
              </Col>
              <Col xs={24} lg={12}>
                <Form.Item name="local_base_url" label="本地模型服务地址" rules={[{ required: true, message: '请填写本地服务地址' }]}>
                  <Input placeholder="http://127.0.0.1:11434/v1" />
                </Form.Item>
              </Col>
              {local.effective_base_url ? (
                <Col span={24}>
                  <Typography.Text type="secondary">当前容器实际访问地址：{local.effective_base_url}</Typography.Text>
                </Col>
              ) : null}
            </Row>
          )}
        </Card>

        <Card title="Tushare 配置" style={{ marginTop: 16 }}>
          <Row gutter={[16, 0]}>
            <Col xs={24} lg={12}>
              <Form.Item label={`Token（${configuredText(tushare.custom_configured, tushare.default_available, tushare.configured)}）`} name="tushare_credential">
                <Input type="password" autoComplete="new-password" placeholder="留空表示保留当前配置或继续使用默认配置" />
              </Form.Item>
            </Col>
            <Col xs={24} lg={12}>
              <Form.Item name="clear_tushare_credential" valuePropName="checked" label="Token 操作">
                <Checkbox>清除本地自定义 Token，改用环境默认配置</Checkbox>
              </Form.Item>
            </Col>
          </Row>
        </Card>

        <Card title="每日自动更新" style={{ marginTop: 16 }}>
          <Row gutter={[16, 0]}>
            <Col xs={24} lg={8}>
              <Form.Item name="scheduler_enabled" valuePropName="checked" label="计划任务">
                <Checkbox>启用 FastAPI 常驻自动更新</Checkbox>
              </Form.Item>
            </Col>
            <Col xs={12} lg={6}>
              <Form.Item name="scheduler_hour" label="小时" rules={[{ required: true, message: '请填写小时' }]}>
                <InputNumber min={0} max={23} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} lg={6}>
              <Form.Item name="scheduler_minute" label="分钟" rules={[{ required: true, message: '请填写分钟' }]}>
                <InputNumber min={0} max={59} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} lg={8}>
              <Form.Item name="scheduler_catch_up" valuePropName="checked" label="错过计划时间">
                <Checkbox>API 启动后自动补跑缺失交易日</Checkbox>
              </Form.Item>
            </Col>
            <Col span={24}>
              <Typography.Text type="secondary">
                定时任务会先执行行情增量更新与排名生成，再执行新闻、用户推荐和模拟盘链路；无需保持浏览器页面打开。
              </Typography.Text>
            </Col>
          </Row>
        </Card>

        <Space style={{ marginTop: 16 }}>
          <Button type="primary" loading={mutation.isPending} onClick={submit} disabled={!isAdmin}>保存配置</Button>
          <Button onClick={() => void query.refetch()}>重新读取</Button>
        </Space>
      </Form>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="当前生效 LLM">
            <Descriptions column={1}>
              <Descriptions.Item label="模式">{settings.llm.mode || '—'}</Descriptions.Item>
              <Descriptions.Item label="Provider">{settings.llm.provider || '—'}</Descriptions.Item>
              <Descriptions.Item label="Model">{settings.llm.model || '—'}</Descriptions.Item>
              <Descriptions.Item label="Endpoint">{settings.llm.endpoint_configured ? '已配置' : '未配置'}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <SchedulerStatusPanel settings={settings} />
        </Col>
      </Row>
    </Space>
  )
}
