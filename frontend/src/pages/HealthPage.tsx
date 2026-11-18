import { Alert, Card, Col, Descriptions, Row, Space, Statistic, Typography } from 'antd'
import { useApiHealth } from '../hooks/useApiHealth'
import { PageLoading } from '../components/common/PageLoading'
import { errorMessage } from '../api/errors'

export function HealthPage() {
  const health = useApiHealth()
  if (health.isPending) return <PageLoading message="正在验证 React → Nginx → FastAPI" />
  if (health.isError) return <Alert type="error" showIcon message="FastAPI 连接失败" description={errorMessage(health.error)} />

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={2}>React 基础设施与公共合同</Typography.Title>
        <Typography.Paragraph type="secondary">
          React 已成为正式前端入口。所有页面通过 Nginx 同源代理调用 FastAPI，并继续遵守已冻结的 HTTP、Task 和 SSE 合同。
        </Typography.Paragraph>
      </div>
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}><Card><Statistic title="API 状态" value="已连接" valueStyle={{ color: '#16a34a' }} /></Card></Col>
        <Col xs={24} md={8}><Card><Statistic title="API 版本" value={health.data.version} /></Card></Col>
        <Col xs={24} md={8}><Card><Statistic title="部署模式" value={health.data.deployment_mode} /></Card></Col>
      </Row>
      <Card title="健康检查响应" data-testid="health-card">
        <Descriptions bordered column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="service">{health.data.service}</Descriptions.Item>
          <Descriptions.Item label="status">{health.data.status}</Descriptions.Item>
          <Descriptions.Item label="version">{health.data.version}</Descriptions.Item>
          <Descriptions.Item label="deployment_mode">{health.data.deployment_mode}</Descriptions.Item>
        </Descriptions>
      </Card>
    </Space>
  )
}
