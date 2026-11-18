import { useState, type ReactNode } from 'react'
import { Button, Dropdown, Layout, Menu, type MenuProps } from 'antd'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiStatus } from '../components/common/ApiStatus'
import { TaskDrawer } from '../components/tasks/TaskDrawer'
import { useApiHealth } from '../hooks/useApiHealth'
import { useAppStore } from '../stores/appStore'
import { useAuthStore } from '../stores/authStore'
import { useTaskStore } from '../stores/taskStore'

const { Header, Content } = Layout
const routeKey = (pathname: string) => pathname.startsWith('/stocks') ? '/stocks' : pathname
const navigationItems: MenuProps['items'] = [
  { key: '/dashboard', label: '首页 / 预测排名' },
  { key: '/stocks', label: '个股详情' },
  { key: 'model-group', label: '模型与回测', children: [
    { key: '/models/metrics', label: '模型指标' },
    { key: '/models/search', label: '模型搜索结果' },
    { key: '/backtests', label: '策略研究' },
  ] },
  { key: '/news', label: '新闻事件' },
  { key: '/paper-trading', label: 'AI 模拟盘' },
  { key: '/agent', label: 'AI Agent' },
  { key: '/settings', label: '系统设置' },
  { key: '/monitor', label: '系统监控' },
  { key: 'platform-group', label: '平台诊断', children: [
    { key: '/platform', label: '连接与合同' },
    { key: '/runtime', label: '任务运行时' },
  ] },
]

export function AppLayout({ children }: { children: ReactNode }) {
  const [navigationOpen, setNavigationOpen] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const health = useApiHealth()
  const taskDrawerOpen = useAppStore((state) => state.taskDrawerOpen)
  const setTaskDrawerOpen = useAppStore((state) => state.setTaskDrawerOpen)
  const task = useTaskStore((state) => state.task)
  const events = useTaskStore((state) => state.events)
  const user = useAuthStore((state) => state.user)
  const logout = useAuthStore((state) => state.logout)
  const selectedKeys = [routeKey(location.pathname)]
  const navigateToPage: MenuProps['onClick'] = ({ key }) => {
    setNavigationOpen(false)
    navigate(key)
  }

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <Layout className="app-shell">
      <Header className="app-header">
        <div className="app-header-inner">
          <Link className="app-brand" to="/dashboard" aria-label="Stock Agent 首页">
            <span className="app-brand-name">Stock Agent</span>
            <span className="app-brand-caption">A股每日股票评分系统</span>
          </Link>
          <Dropdown
            open={navigationOpen}
            onOpenChange={setNavigationOpen}
            trigger={['click']}
            placement="bottomRight"
            menu={{ items: navigationItems, selectedKeys, onClick: navigateToPage }}
          >
            <Button className="app-mobile-nav" aria-label="打开页面导航" aria-expanded={navigationOpen}>
              导航 <span aria-hidden="true">▾</span>
            </Button>
          </Dropdown>
          <div className="app-header-actions">
            <div className="app-service-status">
              <ApiStatus health={health.data} />
            </div>
            <Button onClick={() => setTaskDrawerOpen(true)} data-testid="open-task-drawer">
              任务中心{task ? ` · ${task.status}` : ''}
            </Button>
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'role', label: `身份：${user?.role === 'admin' ? '管理员' : '普通用户'}`, disabled: true },
                  { type: 'divider' },
                  { key: 'logout', label: '退出登录', onClick: handleLogout },
                ],
              }}
            >
              <Button data-testid="user-menu" className="app-user-menu">
                <span className="app-user-name" title={user?.display_name || user?.user_id}>
                  {user?.display_name || user?.user_id || '未登录'}
                </span>
              </Button>
            </Dropdown>
          </div>
        </div>
        <nav className="app-top-nav" aria-label="主导航">
          <Menu
            mode="horizontal"
            selectedKeys={selectedKeys}
            onClick={navigateToPage}
            items={navigationItems}
            overflowedIndicator="更多"
          />
        </nav>
      </Header>
      <Content className="app-content-scroll" data-testid="app-content-scroll">
        <main className="app-content">{children}</main>
      </Content>
      <TaskDrawer open={taskDrawerOpen} onClose={() => setTaskDrawerOpen(false)} task={task} events={events} />
    </Layout>
  )
}
