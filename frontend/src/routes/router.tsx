import { useEffect } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { Spin } from 'antd'
import { HealthPage } from '../pages/HealthPage'
import { LoginPage } from '../pages/LoginPage'
import { RuntimePage } from '../pages/RuntimePage'
import { NotFoundPage } from '../pages/NotFoundPage'
import { RankingPage } from '../pages/dashboard/RankingPage'
import { StockDetailPage } from '../pages/dashboard/StockDetailPage'
import { ModelMetricsPage } from '../pages/dashboard/ModelMetricsPage'
import { ModelSearchPage } from '../pages/dashboard/ModelSearchPage'
import { BacktestPage } from '../pages/dashboard/BacktestPage'
import { NewsPage } from '../pages/dashboard/NewsPage'
import { SettingsPage } from '../pages/dashboard/SettingsPage'
import { SystemMonitorPage } from '../pages/monitor/SystemMonitorPage'
import { PaperTradingPage } from '../pages/paper/PaperTradingPage'
import { AgentPage } from '../pages/agent/AgentPage'
import { useAuthStore } from '../stores/authStore'

function RequireAuth() {
  const status = useAuthStore((state) => state.status)
  const bootstrap = useAuthStore((state) => state.bootstrap)
  const location = useLocation()

  useEffect(() => {
    if (status === 'unknown') void bootstrap()
  }, [status, bootstrap])

  if (status === 'unknown') {
    return <div style={{ display: 'grid', placeItems: 'center', minHeight: '60vh' }}><Spin size="large" /></div>
  }
  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Outlet />
}

export function AppRoutes() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route element={<RequireAuth />}>
      <Route path="/" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={<RankingPage />} />
      <Route path="/stocks" element={<StockDetailPage />} />
      <Route path="/stocks/:stockCode" element={<StockDetailPage />} />
      <Route path="/models/metrics" element={<ModelMetricsPage />} />
      <Route path="/models/search" element={<ModelSearchPage />} />
      <Route path="/backtests" element={<BacktestPage />} />
      <Route path="/news" element={<NewsPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/paper-trading" element={<PaperTradingPage />} />
      <Route path="/agent" element={<AgentPage />} />
      <Route path="/monitor" element={<SystemMonitorPage />} />
      <Route path="/platform" element={<HealthPage />} />
      <Route path="/runtime" element={<RuntimePage />} />
    </Route>
    <Route path="/health" element={<Navigate to="/platform" replace />} />
    <Route path="*" element={<NotFoundPage />} />
  </Routes>
}
