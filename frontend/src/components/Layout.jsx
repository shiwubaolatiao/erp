// 布局 — 侧边菜单 / 顶栏 / 用户菜单
//
// 菜单按用户模块权限动态过滤（useAuth().hasModule）；
// 菜单分组：数据分析 / 核算与核对 / 智能体 / 管理。
// ★ 新增页面时在此登记菜单项，并在 App.jsx 注册路由。
import React, { useState } from 'react'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Dropdown, Button } from 'antd'
import { LogoutOutlined, UserOutlined } from '@ant-design/icons'
import { useAuth } from '../contexts/AuthContext'
import FloatingAI from './FloatingAI'

const SIDEBAR_ICONS = {
  dashboard: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>
  ),
  advertising: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
  ),
  query: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
  ),
  ads_agent: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/></svg>
  ),
  import: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
  ),
  weekly_report: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
  ),
  admin: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
  ),
  inventory: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
  ),
}

const PAGE_TITLES = {
  '/dashboard': '数据看板',
  '/advertising': '广告分析',
  '/query': '数据查询',
  '/ads-agent': '广告 Agent',
  '/import': '数据导入',
  '/weekly-report': '周报管理',
  '/admin': '系统管理',
}

export default function Layout() {
  const [collapsed, setCollapsed] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout, hasModule } = useAuth()

  const allMenuItems = [
    { key: '/dashboard', label: '数据看板', module: 'dashboard', section: '数据分析' },
    { key: '/advertising', label: '广告分析', module: 'advertising', section: '数据分析' },
    { key: '/query', label: '数据查询', module: 'query', section: '数据分析' },
    { key: '/snapshots', label: '核算快照', module: 'accounting', section: '核算与核对' },
    { key: '/cost-alerts', label: '成本预警', module: 'accounting', section: '核算与核对' },
    { key: '/trace', label: '数据溯源', module: 'trace', section: '核算与核对' },
    { key: '/mappings', label: '商品映射审核', module: 'admin', section: '核算与核对' },
    { key: '/ads-agent', label: '广告 Agent', module: 'ads_agent', section: '智能体' },
    { key: '/import', label: '数据导入', module: 'import', section: '管理' },
    { key: '/weekly-report', label: '周报管理', module: 'weekly_report', section: '管理' },
    { key: '/admin', label: '系统管理', module: 'admin', section: '管理' },
    { key: 'http://localhost:5175/', label: '库存系统', module: 'inventory', section: '管理' },
  ]
  const menuItems = allMenuItems.filter(item => hasModule(item.module))

  // Group by section
  const sections = []
  let currentSection = null
  for (const item of menuItems) {
    if (item.section !== currentSection) {
      currentSection = item.section
      sections.push({ label: item.section, items: [item] })
    } else {
      sections[sections.length - 1].items.push(item)
    }
  }

  const onMenuClick = ({ key }) => {
    if (key.startsWith('http')) {
      window.open(key, '_blank')
    } else {
      navigate(key)
    }
  }

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  const userMenuItems = [
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', onClick: handleLogout },
  ]

  const currentTitle = PAGE_TITLES[location.pathname] || 'LMG 数据平台'
  const isAgentPage = location.pathname === '/ads-agent'

  return (
    <div className="app-layout">
      {/* Sidebar */}
      <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-header">
          <div className="sidebar-logo">LA</div>
          <div className="sidebar-title">LEMEGA<small>运营数据智能平台</small></div>
          <button className="sidebar-collapse" onClick={() => setCollapsed(!collapsed)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
          </button>
        </div>
        <nav className="sidebar-nav" onClick={(e) => {
          const item = e.target.closest('.nav-item')
          if (item) onMenuClick({ key: item.dataset.key })
        }}>
          {sections.map(sec => (
            <div key={sec.label}>
              <div className="nav-section-label">{sec.label}</div>
              {sec.items.map(item => (
                <div
                  key={item.key}
                  data-key={item.key}
                  className={`nav-item ${location.pathname === item.key ? 'active' : ''}`}
                >
                  {SIDEBAR_ICONS[item.module]}
                  <span>{item.label}</span>
                </div>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      {/* Main */}
      <div className="main-area">
        <div className="main-header">
          <div className="main-header-title">{currentTitle}</div>
          <div className="main-header-actions">
            <Dropdown menu={{ items: userMenuItems }} placement="bottomRight">
              <Button type="text" icon={<UserOutlined />}>{user?.username}</Button>
            </Dropdown>
          </div>
        </div>
        <div
          className="main-content"
          style={isAgentPage ? { position: 'relative', padding: 0, overflow: 'hidden' } : undefined}
        >
          <Outlet />
        </div>
      </div>

      {!isAgentPage && <FloatingAI />}
    </div>
  )
}
