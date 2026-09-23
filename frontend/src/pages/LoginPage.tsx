import { useState } from 'react'
import { Alert, Button, Card, Form, Input, Tabs, Typography } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { authApi, type AuthUserInfo } from '../api/authApi'
import { errorMessage } from '../api/errors'
import { useAuthStore } from '../stores/authStore'

interface LoginFormValues {
  username: string
  password: string
}

interface RegisterFormValues {
  username: string
  password: string
  display_name?: string
}

export function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const setUser = useAuthStore((state) => state.setUser)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [pending, setPending] = useState(false)
  const from = (location.state as { from?: string } | null)?.from ?? '/dashboard'

  const finish = (user: AuthUserInfo, message: string) => {
    setUser(user)
    setInfo(message)
    navigate(from, { replace: true })
  }

  const handleLogin = async (values: LoginFormValues) => {
    setPending(true)
    setError('')
    try {
      const payload = await authApi.login(values.username.trim(), values.password)
      finish(payload.user, `欢迎回来，${payload.user.display_name || payload.user.user_id}`)
    } catch (err) {
      const status = (err as { response?: { status?: number; data?: { detail?: string } } })?.response?.status
      setError(status === 401 ? '用户名或密码错误' : errorMessage(err))
    } finally {
      setPending(false)
    }
  }

  const handleRegister = async (values: RegisterFormValues) => {
    setPending(true)
    setError('')
    try {
      const payload = await authApi.register(values.username.trim(), values.password, values.display_name?.trim() ?? '')
      finish(
        payload.user,
        payload.first_account
          ? '注册成功，你已自动成为平台管理员'
          : '注册成功，已自动登录',
      )
    } catch (err) {
      const status = (err as { response?: { status?: number; data?: { detail?: string } } })?.response?.status
      if (status === 409) setError('该用户名已被注册')
      else if (status === 422) setError('用户名需以字母或数字开头，3-32 位（字母、数字、_、-）')
      else setError(errorMessage(err))
    } finally {
      setPending(false)
    }
  }

  const loginForm = (
    <Form<LoginFormValues> layout="vertical" onFinish={handleLogin} requiredMark={false}>
      <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名' }]}>
        <Input placeholder="用户名" autoComplete="username" />
      </Form.Item>
      <Form.Item name="password" label="密码" rules={[{ required: true, message: '请输入密码' }]}>
        <Input.Password placeholder="密码" autoComplete="current-password" />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={pending}>登录</Button>
    </Form>
  )

  const registerForm = (
    <Form<RegisterFormValues> layout="vertical" onFinish={handleRegister} requiredMark={false}>
      <Form.Item
        name="username"
        label="用户名"
        rules={[
          { required: true, message: '请输入用户名' },
          { pattern: /^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$/, message: '3-32 位，字母或数字开头，可用 _ 和 -' },
        ]}
      >
        <Input placeholder="用户名（登录标识，注册后不可改）" autoComplete="username" />
      </Form.Item>
      <Form.Item name="display_name" label="显示名称（可选）">
        <Input placeholder="页面上展示的名字" maxLength={64} />
      </Form.Item>
      <Form.Item name="password" label="密码" rules={[{ required: true, min: 6, message: '密码至少 6 位' }]}>
        <Input.Password placeholder="至少 6 位" autoComplete="new-password" />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={pending}>注册并登录</Button>
    </Form>
  )

  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', background: '#f5f7fa' }}>
      <Card style={{ width: 400 }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 4 }}>
          A股每日股票评分系统
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center' }}>
          登录后使用 AI Agent 与模拟盘工作台
        </Typography.Paragraph>
        {error ? <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} /> : null}
        <Tabs
          centered
          items={[
            { key: 'login', label: '登录', children: loginForm },
            { key: 'register', label: '注册', children: registerForm },
          ]}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', textAlign: 'center' }}>
          本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
        </Typography.Text>
      </Card>
    </div>
  )
}
