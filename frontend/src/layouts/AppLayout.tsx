import { useEffect, useRef, useState, type ReactNode, type UIEvent } from 'react'
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
  { key: '/factors', label: '因子库' },
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
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [openSubmenus, setOpenSubmenus] = useState<string[]>([])
  const [headerCollapsed, setHeaderCollapsed] = useState(false)
  const headerRef = useRef<HTMLElement>(null)
  const contentRef = useRef<HTMLElement>(null)
  const revealRef = useRef<HTMLButtonElement>(null)
  const scrollAnchor = useRef(0)
  const previousScrollTop = useRef(0)
  const restoreHeaderFocus = useRef(false)
  const restoreRevealFocus = useRef(false)
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

  useEffect(() => {
    if (headerCollapsed && restoreRevealFocus.current) {
      revealRef.current?.focus({ preventScroll: true })
      restoreRevealFocus.current = false
    } else if (!headerCollapsed && restoreHeaderFocus.current) {
      headerRef.current?.querySelector<HTMLAnchorElement>('.app-brand')?.focus({ preventScroll: true })
      restoreHeaderFocus.current = false
    }
  }, [headerCollapsed])

  useEffect(() => {
    setHeaderCollapsed(false)
    setNavigationOpen(false)
    setUserMenuOpen(false)
    setOpenSubmenus([])
    scrollAnchor.current = 0
    previousScrollTop.current = 0
    contentRef.current?.scrollTo({ top: 0 })
  }, [location.pathname])

  const handleContentScroll = (event: UIEvent<HTMLElement>) => {
    const top = Math.max(0, event.currentTarget.scrollTop)
    const previous = previousScrollTop.current
    previousScrollTop.current = top
    if (top <= 8) {
      scrollAnchor.current = top
      setHeaderCollapsed(false)
      return
    }
    if (top < previous) {
      scrollAnchor.current = top
      return
    }
    // Hide only once the header's original space has scrolled away. Moving the
    // sticky header with a transform keeps the content and scroll position stable.
    if (!headerCollapsed && top > (headerRef.current?.offsetHeight ?? 120) && top - scrollAnchor.current >= 24) {
      restoreRevealFocus.current = !!headerRef.current?.contains(document.activeElement)
      setHeaderCollapsed(true)
      setNavigationOpen(false)
      setUserMenuOpen(false)
      setOpenSubmenus([])
    }
  }

  const expandHeader = () => {
    scrollAnchor.current = contentRef.current?.scrollTop ?? 0
    previousScrollTop.current = scrollAnchor.current
    restoreHeaderFocus.current = true
    setHeaderCollapsed(false)
  }

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
      {headerCollapsed && (
        <button
          ref={revealRef}
          type="button"
          className="app-nav-reveal"
          aria-controls="app-navigation-header"
          aria-expanded={false}
          onClick={expandHeader}
        >
          展开导航 <span aria-hidden="true">⌄</span>
        </button>
      )}
      <Content ref={contentRef} className="app-content-scroll" data-testid="app-content-scroll" onScroll={handleContentScroll}>
        <Header
          ref={headerRef}
          id="app-navigation-header"
          className={`app-header${headerCollapsed ? ' app-header--collapsed' : ''}`}
          aria-hidden={headerCollapsed}
          inert={headerCollapsed}
        >
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
                open={userMenuOpen}
                onOpenChange={setUserMenuOpen}
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
              openKeys={openSubmenus}
              onOpenChange={setOpenSubmenus}
              onClick={navigateToPage}
              items={navigationItems}
              overflowedIndicator="更多"
            />
          </nav>
        </Header>
        <main className="app-content">{children}</main>
      </Content>
      <TaskDrawer open={taskDrawerOpen} onClose={() => setTaskDrawerOpen(false)} task={task} events={events} />
    </Layout>
  )
}
