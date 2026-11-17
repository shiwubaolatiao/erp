import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import Layout from './components/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Advertising from './pages/Advertising'
import DataImport from './pages/DataImport'
import DataQuery from './pages/DataQuery'
import SystemAdmin from './pages/Admin'
import WeeklyReport from './pages/WeeklyReport'
import AdsAgent from './pages/AdsAgent'
import Trace from './pages/Trace'
import Snapshots from './pages/Snapshots'
import CostAlerts from './pages/CostAlerts'
import PendingMappings from './pages/PendingMappings'
import ForceChangePassword from './pages/ForceChangePassword'
import { Spin } from 'antd'

function ProtectedRoute({ children }) {
  const { user, loading } = useAuth()
  if (loading) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  return children
}

function DefaultRoute() {
  const { hasModule } = useAuth()
  const routes = [
    { module: 'dashboard', path: '/dashboard' },
    { module: 'accounting', path: '/snapshots' },
    { module: 'query', path: '/query' },
    { module: 'advertising', path: '/advertising' },
    { module: 'import', path: '/import' },
    { module: 'trace', path: '/trace' },
    { module: 'weekly_report', path: '/weekly-report' },
    { module: 'ads_agent', path: '/ads-agent' },
    { module: 'admin', path: '/admin' },
  ]
  for (const r of routes) {
    if (hasModule(r.module)) return <Navigate to={r.path} replace />
  }
  return <Navigate to="/login" replace />
}

function AppRoutes() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    )
  }

  // 组11.3：种子账号初始密码未改 — 所有路径一律拦截到强制改密页
  if (user?.must_change_password) {
    return <ForceChangePassword />
  }

  return (
    <Routes>
      <Route path="/login" element={user ? <DefaultRoute /> : <Login />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <Layout />
          </ProtectedRoute>
        }
      >
        <Route index element={<DefaultRoute />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="advertising" element={<Advertising />} />
        <Route path="import" element={<DataImport />} />
        <Route path="query" element={<DataQuery />} />
        <Route path="snapshots" element={<Snapshots />} />
        <Route path="cost-alerts" element={<CostAlerts />} />
        <Route path="trace" element={<Trace />} />
        <Route path="mappings" element={<PendingMappings />} />
        <Route path="admin" element={<SystemAdmin />} />
        <Route path="weekly-report" element={<WeeklyReport />} />
        <Route path="ads-agent" element={<AdsAgent />} />
      </Route>
      <Route path="*" element={<DefaultRoute />} />
    </Routes>
  )
}

export default function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </ConfigProvider>
  )
}
