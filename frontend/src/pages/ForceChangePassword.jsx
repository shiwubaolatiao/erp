// 强制改密页 — 种子账号初始密码登录后必须改密（组11.3）
//
// 触发条件：user.must_change_password === true（AppRoutes 层统一拦截，
// 覆盖"刚登录"与"刷新页面恢复登录态"两种路径）。改密成功后由
// AuthContext 更新 user，路由自动放行进入主界面。
import React, { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { authChangePassword } from '../api/index'

const PAGE_CSS = `
.fcp-root { position:fixed; inset:0; display:flex; align-items:center; justify-content:center;
  background:#fafbfc; font-family:'Inter','Noto Sans SC','PingFang SC','Microsoft YaHei',sans-serif; }
.fcp-card { background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:2.8rem 2.5rem 2.4rem;
  width:min(440px,88vw); box-shadow:0 4px 24px rgba(0,0,0,0.06),0 1px 4px rgba(0,0,0,0.04); }
.fcp-header { text-align:center; margin-bottom:1.8rem; }
.fcp-header h2 { font-weight:600; font-size:1.3rem; color:#1a1d28; letter-spacing:0.06em; }
.fcp-sep { width:36px; height:3px; background:#2563eb; border-radius:2px; margin:12px auto 0; }
.fcp-tip { margin:0 0 1.4rem; padding:0.7rem 0.9rem; background:#eff6ff; border:1px solid #bfdbfe;
  border-radius:8px; font-size:0.8rem; color:#1d4ed8; line-height:1.6; }
.fcp-group { margin-bottom:1.3rem; }
.fcp-group label { display:block; font-size:0.85rem; font-weight:500; color:#1a1d28; margin-bottom:0.4rem; }
.fcp-group input { width:100%; padding:0.8rem 1.2rem; background:#f9fafb; border:1.5px solid #e5e7eb;
  border-radius:10px; color:#1a1d28; font-size:0.95rem; outline:none; transition:all 0.3s ease; box-sizing:border-box; }
.fcp-group input:focus { border-color:#93c5fd; background:#fff; box-shadow:0 0 0 3px rgba(37,99,235,0.08); }
.fcp-btn { width:100%; margin-top:0.6rem; padding:0.9rem; background:#2563eb; border:none; border-radius:10px;
  color:#fff; font-weight:500; font-size:1.05rem; letter-spacing:0.1em; cursor:pointer; transition:all 0.3s ease; }
.fcp-btn:hover { background:#3b82f6; }
.fcp-btn:disabled { opacity:0.7; cursor:not-allowed; }
.fcp-link { display:block; margin-top:1rem; text-align:center; font-size:0.8rem; color:#6b7280; cursor:pointer; }
.fcp-link:hover { color:#ef4444; }
.fcp-error { margin-top:0.8rem; text-align:center; font-size:0.8rem; color:#ef4444; min-height:1.2em; }
`

export default function ForceChangePassword() {
  const { user, logout, completePasswordChange } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    const form = e.target
    const oldPwd = form.old_password.value
    const newPwd = form.new_password.value
    const confirmPwd = form.confirm_password.value
    if (!oldPwd || !newPwd || !confirmPwd) {
      setError('请填写完整')
      return
    }
    if (newPwd.length < 6) {
      setError('新密码长度至少 6 位')
      return
    }
    if (newPwd !== confirmPwd) {
      setError('两次输入的新密码不一致')
      return
    }
    if (newPwd === oldPwd) {
      setError('新密码不能与原密码相同')
      return
    }
    setLoading(true)
    try {
      await authChangePassword({ old_password: oldPwd, new_password: newPwd })
      completePasswordChange()
    } catch (err) {
      setError(err.response?.data?.detail || '改密失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fcp-root">
      <style>{PAGE_CSS}</style>
      <div className="fcp-card">
        <div className="fcp-header">
          <h2>首次登录请修改密码</h2>
          <div className="fcp-sep" />
        </div>
        <p className="fcp-tip">
          账号 <b>{user?.username}</b> 当前使用的是系统初始密码，
          为保障数据安全，请先设置新密码后再进入系统。
        </p>
        <form onSubmit={handleSubmit} autoComplete="off">
          <div className="fcp-group">
            <label>原密码</label>
            <input type="password" name="old_password" placeholder="请输入当前密码" autoComplete="off" autoFocus />
          </div>
          <div className="fcp-group">
            <label>新密码</label>
            <input type="password" name="new_password" placeholder="至少 6 位" autoComplete="new-password" />
          </div>
          <div className="fcp-group">
            <label>确认新密码</label>
            <input type="password" name="confirm_password" placeholder="再次输入新密码" autoComplete="new-password" />
          </div>
          <button type="submit" className="fcp-btn" disabled={loading}>
            {loading ? '提交中…' : '确认修改'}
          </button>
        </form>
        {error && <div className="fcp-error">{error}</div>}
        <span className="fcp-link" onClick={logout}>退出登录</span>
      </div>
    </div>
  )
}