// 广告 Agent — 会话式广告诊断（支持流式回答与文件上传）
//
// 依赖接口：
//   getAdsSessions / createAdsSession / deleteAdsSession
//   streamAdsMessage    SSE 流式对话（fetch + ReadableStream）
//   sendAdsWithFiles    带附件提问
//   getAdsActions / exportAdsActions   行动项清单与导出
//   getAdsReports / downloadAdsReport  分析报告中心
//
// 注意：Agent 的数据来源是本地 unified 广告报表库（只读，不触发下载）+ 平台核算快照；
//      "今天/上周"这类高频问题必须走本地报表库，平台库只有月度粒度。
import React, { useState, useRef, useEffect, useCallback } from 'react'
import { Button, Popconfirm, Empty, Spin, message } from 'antd'
import {
  PlusOutlined, DeleteOutlined, PaperClipOutlined, CloseOutlined, DownloadOutlined,
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  getAdsSessions, createAdsSession, getAdsSession, deleteAdsSession,
  sendAdsWithFiles, streamAdsMessage, getAdsReports, downloadAdsReport,
} from '../api'

const REPORT_MARKER = '[[REPORT]]'

const WELCOME_PROMPTS = [
  { icon: '🔍', title: '有哪些花费浪费？', desc: '识别低效广告支出和烧钱产品' },
  { icon: '📊', title: '当前ACOS怎么样？', desc: '全店铺ACOS分档分析概览' },
  { icon: '📈', title: 'TACoS分析', desc: '广告销售额占比与整体利润' },
  { icon: '📋', title: '对比上月数据', desc: '环比变化趋势与异常波动' },
]

const mdComponents = {
  table: ({ children }) => <table style={{ borderCollapse: 'collapse', width: '100%', margin: '10px 0', fontSize: 12, border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', overflow: 'hidden' }}>{children}</table>,
  th: ({ children }) => <th style={{ background: 'var(--bg-secondary)', padding: '6px 10px', textAlign: 'left', fontWeight: 600, borderBottom: '1px solid var(--border)' }}>{children}</th>,
  td: ({ children }) => <td style={{ padding: '5px 10px', borderBottom: '1px solid var(--border)' }}>{children}</td>,
  p: ({ children }) => <p style={{ margin: '6px 0' }}>{children}</p>,
  ul: ({ children }) => <ul style={{ margin: '6px 0', paddingLeft: 22 }}>{children}</ul>,
  ol: ({ children }) => <ol style={{ margin: '6px 0', paddingLeft: 22 }}>{children}</ol>,
  li: ({ children }) => <li style={{ margin: '3px 0' }}>{children}</li>,
  strong: ({ children }) => <strong style={{ fontWeight: 600 }}>{children}</strong>,
  code: ({ children }) => <code style={{ background: 'var(--bg-code)', padding: '2px 6px', borderRadius: 4, fontSize: 12 }}>{children}</code>,
  hr: () => <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: '8px 0' }} />,
  h1: ({ children }) => <h1 style={{ fontWeight: 700, fontSize: 18, margin: '12px 0 6px' }}>{children}</h1>,
  h2: ({ children }) => <h2 style={{ fontWeight: 700, fontSize: 16, margin: '14px 0 6px' }}>{children}</h2>,
  h3: ({ children }) => <h3 style={{ fontWeight: 600, fontSize: 14, margin: '10px 0 4px' }}>{children}</h3>,
  blockquote: ({ children }) => <blockquote style={{ margin: '8px 0', padding: '6px 12px', borderLeft: '3px solid var(--accent)', background: 'var(--accent-light)', borderRadius: '0 var(--radius-sm) var(--radius-sm) 0' }}>{children}</blockquote>,
  pre: ({ children }) => <pre style={{ background: 'var(--bg-code)', padding: 12, borderRadius: 'var(--radius-sm)', overflowX: 'auto', border: '1px solid var(--border)' }}>{children}</pre>,
}

const AgentIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/></svg>
)

const SendIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
)

const ClipIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
)

const StopIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>
)

const DeepThinkIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
)

export default function AdsAgent() {
  const [sessions, setSessions] = useState([])
  const [currentSession, setCurrentSession] = useState(null)
  const [messages, setMessages] = useState([])
  const [reports, setReports] = useState([])
  const [input, setInput] = useState('')
  const [pendingFiles, setPendingFiles] = useState([])
  const [uploading, setUploading] = useState(false)
  const [loadingSessions, setLoadingSessions] = useState(false)
  const chatEndRef = useRef(null)
  const fileInputRef = useRef(null)
  const textareaRef = useRef(null)
  // 每个会话独立的流控制器 + 消息缓存：切换会话不再打断后台生成
  const controllersRef = useRef({})
  const messagesCacheRef = useRef({})
  const currentIdRef = useRef(null)
  // 深度思考开关：开=V4 Pro(深度推理)，关=V4 Flash(快速)
  const [deepThink, setDeepThink] = useState(() => localStorage.getItem('ads_agent_deepthink') === '1')
  const activeModel = deepThink ? 'deepseek-v4-pro' : 'deepseek-v4-flash'

  const getSessionMessages = (id) => messagesCacheRef.current[id] || []
  const writeSessionMessages = (id, valueOrFn) => {
    const prev = getSessionMessages(id)
    const next = typeof valueOrFn === 'function' ? valueOrFn(prev) : valueOrFn
    messagesCacheRef.current[id] = next
    if (currentIdRef.current === id) setMessages(next)
  }
  // 当前会话是否正在生成（派生值，切换会话后互不影响）
  const sending = messages.some(m => m.streaming)

  const toggleDeepThink = () => {
    setDeepThink(prev => {
      const next = !prev
      localStorage.setItem('ads_agent_deepthink', next ? '1' : '0')
      return next
    })
  }

  const loadSessions = useCallback(async () => {
    setLoadingSessions(true)
    try {
      const res = await getAdsSessions()
      setSessions(res.data)
    } catch { /* ignore */ }
    setLoadingSessions(false)
  }, [])

  const loadReports = useCallback(async () => {
    try {
      const res = await getAdsReports()
      setReports(res.data || [])
    } catch { /* ignore */ }
  }, [])

  const handleDownloadReport = async (filename) => {
    try {
      await downloadAdsReport(filename)
    } catch {
      message.error(`下载失败：${filename}`)
    }
  }

  // 从链接中提取报告文件名（兼容绝对/相对 URL 与 URL 编码）
  const extractReportFilename = (href) => {
    let filename = href.split('?')[0].split('/').pop() || ''
    try { filename = decodeURIComponent(filename) } catch { /* 保留原样 */ }
    return filename
  }

  // Markdown 渲染组件：拦截报告下载链接，改为带鉴权的下载（避免直接点链接 401）
  const mdWithReports = {
    ...mdComponents,
    a: ({ href, children }) => {
      if (typeof href === 'string' && /ads-agent\/reports\//.test(href)) {
        const filename = extractReportFilename(href)
        return (
          <button
            className="agent-report-btn"
            onClick={() => handleDownloadReport(filename)}
            style={{ cursor: 'pointer', margin: '2px 0' }}
          >
            <DownloadOutlined style={{ fontSize: 12 }} /> {children || filename}
          </button>
        )
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>
    },
  }

  useEffect(() => { loadSessions(); loadReports() }, [loadSessions, loadReports])

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending])

  const handleNewSession = async () => {
    try {
      const res = await createAdsSession()
      setSessions(prev => [res.data, ...prev])
      currentIdRef.current = res.data.id
      setCurrentSession(res.data)
      messagesCacheRef.current[res.data.id] = []
      setMessages([])
      setPendingFiles([])
    } catch { message.error('创建会话失败') }
  }

  const handleSelectSession = async (session) => {
    // 切换会话不打断后台生成：直接切视图，生成中的会话继续在后台跑
    currentIdRef.current = session.id
    setCurrentSession(session)
    const cached = messagesCacheRef.current[session.id]
    if (cached) {
      setMessages(cached)
      setPendingFiles([])
      return
    }
    try {
      const res = await getAdsSession(session.id)
      setCurrentSession(res.data)
      const msgs = res.data.messages || []
      const mapped = msgs.length > 0 ? msgs.map(m => {
        // 兼容历史 [[REPORT]] 消息：渲染为文件卡片
        if (m.role === 'assistant' && m.content.startsWith(REPORT_MARKER)) {
          const nl = m.content.indexOf('\n')
          const filename = m.content.slice(REPORT_MARKER.length, nl < 0 ? undefined : nl)
          return { role: 'file', filename }
        }
        return { role: m.role === 'assistant' ? 'agent' : 'user', content: m.content }
      }) : []
      messagesCacheRef.current[session.id] = mapped
      setMessages(mapped)
      setPendingFiles([])
    } catch { message.error('加载会话失败') }
  }

  const handleStop = () => {
    // 只停止当前会话的生成，不影响其它后台会话
    const c = controllersRef.current[currentSession?.id]
    if (c) c.abort()
  }

  const handleDeleteSession = async (id) => {
    try {
      await deleteAdsSession(id)
      if (controllersRef.current[id]) controllersRef.current[id].abort()
      delete controllersRef.current[id]
      delete messagesCacheRef.current[id]
      setSessions(prev => prev.filter(s => s.id !== id))
      if (currentSession?.id === id) {
        currentIdRef.current = null
        setCurrentSession(null)
        setMessages([])
      }
      message.success('已删除')
    } catch { message.error('删除失败') }
  }

  const handleAttachFile = (e) => {
    const files = Array.from(e.target.files || [])
    if (files.length > 0) {
      setPendingFiles(prev => [...prev, ...files])
    }
    // Reset input so same file can be selected again
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleRemoveFile = (index) => {
    setPendingFiles(prev => prev.filter((_, i) => i !== index))
  }

  // 流式对话：写入指定会话的消息缓存，切走会话也继续在后台生成
  const runStreamingChat = async (sessionId, text, modelValue) => {
    const controller = new AbortController()
    controllersRef.current[sessionId] = controller

    writeSessionMessages(sessionId, prev => [...prev, { role: 'agent', content: '', thinking: '', tools: [], streaming: true }])
    const patch = (fn) => {
      writeSessionMessages(sessionId, prev => {
        const si = prev.findIndex(m => m.streaming)
        if (si < 0) return prev
        const next = [...prev]
        next[si] = fn({ ...next[si] })
        return next
      })
    }
    const finish = () => {
      delete controllersRef.current[sessionId]
      loadSessions()
      loadReports()
    }

    let res
    try {
      res = await streamAdsMessage(sessionId, text, modelValue, controller.signal)
    } catch (e) {
      if (e.name === 'AbortError') {
        patch(m => ({ ...m, streaming: false, content: m.content ? m.content + '\n\n（已停止）' : '（已停止）' }))
      } else {
        patch(m => ({ ...m, streaming: false, content: `❌ 请求失败：${e.message}` }))
      }
      finish()
      return
    }
    if (!res.ok || !res.body) {
      let detail = ''
      try { detail = await res.text() } catch { /* ignore */ }
      patch(m => ({ ...m, streaming: false, content: `❌ 请求失败（HTTP ${res.status}）：${detail}` }))
      finish()
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finished = false
    try {
      while (!finished) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let nl
        while ((nl = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nl).trim()
          buffer = buffer.slice(nl + 1)
          if (!line.startsWith('data:')) continue
          let ev
          try { ev = JSON.parse(line.slice(5).trim()) } catch { continue }
          switch (ev.type) {
            case 'thinking':
              patch(m => ({ ...m, thinking: m.thinking + ev.delta }))
              break
            case 'text':
              patch(m => ({ ...m, content: m.content + ev.delta }))
              break
            case 'tool_call':
              patch(m => ({ ...m, tools: [...m.tools, { name: ev.name, status: 'running', preview: '' }] }))
              break
            case 'tool_result':
              patch(m => ({
                ...m,
                tools: m.tools.map(t => {
                  if (t.status === 'running' && t.name === ev.name) {
                    return { ...t, status: 'done', preview: ev.preview || '' }
                  }
                  return t
                }),
              }))
              break
            case 'report': {
              // 只输出文件：显示紧凑的文件卡片（不展示正文）
              if (ev.filename) {
                writeSessionMessages(sessionId, prev => [...prev, { role: 'file', filename: ev.filename }])
              }
              break
            }
            case 'done':
              patch(m => ({ ...m, content: ev.reply || m.content, streaming: false }))
              finished = true
              break
            case 'error':
              patch(m => ({ ...m, streaming: false, content: m.content || `❌ ${ev.message}` }))
              finished = true
              break
            default:
              break
          }
          if (finished) break
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        patch(m => ({ ...m, streaming: false, content: m.content ? m.content + '\n\n（已停止）' : '（已停止）' }))
      } else {
        patch(m => ({ ...m, streaming: false, content: m.content || `❌ 读取失败：${e.message}` }))
      }
    } finally {
      finish()
    }
  }

  const handleSend = async (text) => {
    text = (text || input).trim()
    if (!text && pendingFiles.length === 0) return
    if (!currentSession) { message.warning('请先创建会话'); return }
    if (uploading) return
    const sid = currentSession.id
    if (getSessionMessages(sid).some(m => m.streaming)) return

    const fileList = pendingFiles.length > 0
      ? `\n📎 附件: ${pendingFiles.map(f => f.name).join(', ')}`
      : ''
    const displayContent = text + fileList
    writeSessionMessages(sid, prev => [...prev, { role: 'user', content: displayContent }])

    const filesToSend = [...pendingFiles]
    setInput('')
    setPendingFiles([])

    // Auto-resize textarea back
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    try {
      if (filesToSend.length > 0) {
        // 带附件：仍走原接口（解析+分析一次性返回）
        setUploading(true)
        const res = await sendAdsWithFiles(sid, text, filesToSend, activeModel)
        writeSessionMessages(sid, prev => [...prev, { role: 'agent', content: res.data.reply }])
      } else {
        // 纯文本：流式输出（后台可继续）
        await runStreamingChat(sid, text, activeModel)
      }
      loadSessions()
      loadReports()
    } catch (e) {
      writeSessionMessages(sid, prev => [...prev, { role: 'agent', content: `❌ 请求失败：${e?.response?.data?.detail || e.message}` }])
    } finally {
      setUploading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleInput = (e) => {
    setInput(e.target.value)
    // Auto-resize
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 140) + 'px'
  }

  const showWelcome = !currentSession || (messages.length === 0 && !sending)

  return (
    <div style={{ display: 'flex', gap: 0, height: '100%', position: 'relative' }}>
      {/* Session list */}
      <div className="chat-session-panel">
        <div style={{ padding: 12, borderBottom: '1px solid var(--border)' }}>
          <button className="chat-send-btn" style={{ width: '100%', borderRadius: 'var(--radius-sm)', height: 36, fontSize: 14, gap: 6, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={handleNewSession}>
            <PlusOutlined style={{ fontSize: 14 }} />
            <span>新建会话</span>
          </button>
        </div>
        <div style={{ flex: 1, overflow: 'auto' }}>
          {loadingSessions ? (
            <div style={{ textAlign: 'center', padding: 20 }}><Spin size="small" /></div>
          ) : sessions.length === 0 ? (
            <Empty description="暂无会话" style={{ marginTop: 40 }} />
          ) : (
            sessions.map(s => (
              <div
                key={s.id}
                className={`chat-session-item ${currentSession?.id === s.id ? 'active' : ''}`}
                onClick={() => handleSelectSession(s)}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div style={{ flex: 1, overflow: 'hidden' }}>
                    <div className="chat-session-title">{s.title}</div>
                    <div className="chat-session-meta">{s.message_count} 条 · {s.last_active_at}</div>
                  </div>
                  <Popconfirm title="确认删除？" onConfirm={(e) => { e.stopPropagation(); handleDeleteSession(s.id) }}>
                    <button
                      className="chat-tool-btn"
                      onClick={(e) => e.stopPropagation()}
                      style={{ width: 26, height: 26, flexShrink: 0 }}
                    >
                      <DeleteOutlined style={{ fontSize: 13, color: 'var(--red)' }} />
                    </button>
                  </Popconfirm>
                </div>
              </div>
            ))
          )}
        </div>
        {reports.length > 0 && (
          <div className="chat-reports-panel">
            <div className="chat-reports-title">📄 报告中心（点击下载）</div>
            {reports.map((r, i) => (
              <div key={i} className="chat-report-item" onClick={() => handleDownloadReport(r.filename)}>
                <span className="chat-report-name" title={r.filename}>{r.filename}</span>
                <DownloadOutlined style={{ fontSize: 13, flexShrink: 0, color: 'var(--accent)' }} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Chat area */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
        {/* Messages */}
        <div style={{ flex: 1, overflow: 'auto', padding: '20px 24px' }}>
          {showWelcome ? (
            <div className="welcome-screen">
              <div className="welcome-logo"><AgentIcon /></div>
              <div className="welcome-title">广告分析 Agent</div>
              <div className="welcome-subtitle">
                上传广告报告自动分析 · 查询销量利润数据 · 发现 ACOS 异常 · 生成操作清单<br/>Powered by DeepSeek
              </div>
              <div className="welcome-prompts">
                {WELCOME_PROMPTS.map((p, i) => (
                  <div key={i} className="welcome-prompt" onClick={() => { if (currentSession) handleSend(p.title) }}>
                    <span className="pi">{p.icon}</span>
                    <div className="pt">{p.title}</div>
                    <div className="pd">{p.desc}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="chat-container">
              {messages.map((msg, i) => (
                msg.role === 'file' ? (
                  <div key={i} className="msg-row file">
                    <div className="msg-avatar agent"><AgentIcon /></div>
                    <div className="msg-bubble file-card">
                      <span className="file-card-icon">{msg.filename.toLowerCase().endsWith('.xlsx') ? '📊' : msg.filename.toLowerCase().endsWith('.pdf') ? '📕' : '📄'}</span>
                      <span className="file-card-name" title={msg.filename}>{msg.filename}</span>
                      <button className="agent-report-btn" onClick={() => handleDownloadReport(msg.filename)}>
                        <DownloadOutlined style={{ fontSize: 12 }} /> 下载文件
                      </button>
                    </div>
                  </div>
                ) : (
                <div key={i} className={`msg-row ${msg.role}`}>
                  <div className={`msg-avatar ${msg.role}`}>
                    {msg.role === 'user' ? (currentSession?.id ? 'U' : 'U') : <AgentIcon />}
                  </div>
                  <div className="msg-bubble">
                    {msg.role === 'agent' ? (
                      <div className="agent-content">
                        {!!msg.thinking && (
                          <details className="agent-thinking" open={!!msg.streaming}>
                            <summary className="agent-thinking-summary">
                              <DeepThinkIcon />
                              <span>{msg.streaming ? '深度思考中…' : '已深度思考'}</span>
                              <span className="agent-thinking-chevron">▾</span>
                            </summary>
                            <pre className="agent-thinking-text">{msg.thinking}</pre>
                          </details>
                        )}
                        {!!msg.thinking && !!msg.content && (
                          <div className="agent-thinking-divider">— 思考结束 · 开始输出 —</div>
                        )}
                        {msg.tools?.length > 0 && (
                          <div className="agent-tools">
                            {msg.tools.map((t, i) => (
                              <span key={i} className={`agent-tool-chip ${t.status}`} title={t.preview || ''}>
                                {t.status === 'running' ? '⏳' : '✅'} {t.name}
                                {t.status === 'running' ? ' · 执行中' : ''}
                              </span>
                            ))}
                          </div>
                        )}
                        {msg.content ? (
                          msg.streaming ? (
                            <div className="agent-streaming-text">{msg.content}</div>
                          ) : (
                            <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdWithReports}>
                              {msg.content}
                            </ReactMarkdown>
                          )
                        ) : msg.streaming ? (
                          <span className="agent-waiting">正在思考…</span>
                        ) : null}
                      </div>
                    ) : msg.content}
                  </div>
                </div>
                )
              ))}
              {uploading && (
                <div className="chat-loading">
                  <div className="msg-avatar agent"><AgentIcon /></div>
                  <div className="msg-bubble" style={{ background: 'var(--bg-primary)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: '12px 18px' }}>
                    <Spin size="small" />
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
          )}
        </div>

        {/* Input area */}
        <div className="chat-input-area">
          {pendingFiles.length > 0 && (
            <div className="pending-files">
              {pendingFiles.map((f, i) => (
                <span key={i} className="pending-file-tag">
                  <PaperClipOutlined style={{ fontSize: 12 }} />
                  {f.name}
                  <CloseOutlined style={{ fontSize: 11, cursor: 'pointer', marginLeft: 2 }} onClick={() => handleRemoveFile(i)} />
                </span>
              ))}
            </div>
          )}
          <div className="chat-input-wrapper">
            <div className="chat-input-row">
              <textarea
                ref={textareaRef}
                className="chat-textarea"
                placeholder={currentSession ? '输入你的问题，Enter 发送，Shift+Enter 换行...' : '请先创建会话'}
                rows={1}
                value={input}
                onChange={handleInput}
                onKeyDown={handleKeyDown}
                disabled={!currentSession || sending || uploading}
              />
            </div>
            <div className="chat-input-tools">
              <button
                className={`deepthink-toggle ${deepThink ? 'active' : ''}`}
                onClick={toggleDeepThink}
                disabled={sending || uploading}
                title={deepThink ? '已开启深度思考（V4 Pro）' : '开启深度思考（V4 Pro，更慢但更强）'}
              >
                <DeepThinkIcon />
                <span>深度思考</span>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                style={{ display: 'none' }}
                onChange={handleAttachFile}
              />
              <button
                className="chat-tool-btn"
                title="上传文件"
                disabled={!currentSession || sending || uploading}
                onClick={() => fileInputRef.current?.click()}
              >
                <ClipIcon />
              </button>
              {sending ? (
                <button className="chat-send-btn chat-stop-btn" onClick={handleStop} title="停止生成">
                  <StopIcon />
                </button>
              ) : (
                <button
                  className="chat-send-btn"
                  onClick={() => handleSend()}
                  disabled={(!input.trim() && pendingFiles.length === 0) || !currentSession || sending || uploading}
                >
                  <SendIcon />
                </button>
              )}
            </div>
          </div>
          <div style={{ textAlign: 'center', fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
            LEMEGA Ads Agent · 基于 DeepSeek 驱动 · 内容仅供参考
          </div>
        </div>
      </div>
    </div>
  )
}
