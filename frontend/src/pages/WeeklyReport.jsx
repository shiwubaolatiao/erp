// 周报管理 — 撰写 / AI 润色 / 提交审核 / 审核通过或驳回 / 导出 Word
//
// 依赖接口：
//   getWeeklyReports / createWeeklyReport / updateWeeklyReport / deleteWeeklyReport
//   polishWeeklyReport    AI 润色（依赖 AI_API_KEY）
//   submitWeeklyReport / reviewWeeklyReport  审核流
//   uploadReportImage / deleteReportImage    配图
//   exportWeeklyReport    导出 Word
import React, { useState, useEffect, useCallback, useRef } from 'react'
import {
  Button, Select, DatePicker, Form, Input, Upload, Tag, Space, message,
  Popconfirm, Image, Card, Modal, Collapse, Empty, Spin, Tooltip, Row, Col, Divider,
} from 'antd'
import {
  PlusOutlined, DeleteOutlined, ExportOutlined, RobotOutlined, CheckOutlined,
  UploadOutlined, ArrowLeftOutlined, FileTextOutlined, PictureOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import isoWeek from 'dayjs/plugin/isoWeek'
dayjs.extend(isoWeek)
dayjs.locale('zh-cn')
import {
  getWeeklyReports, createWeeklyReport, getWeeklyReport,
  updateWeeklyReport, deleteWeeklyReport, polishWeeklyReport,
  submitWeeklyReport, reviewWeeklyReport, uploadReportImage,
  deleteReportImage, exportWeeklyReport, getStores,
} from '../api'
import { useAuth } from '../contexts/AuthContext'

const { TextArea } = Input

const STATUS = {
  draft:     { color: 'default',    text: '草稿' },
  submitted: { color: 'processing', text: '待审批' },
  approved:  { color: 'success',    text: '已通过' },
  rejected:  { color: 'error',      text: '已退回' },
}

const TEMPLATE = `## 本周概况


## 销售业绩


## 问题与挑战


## 下周计划
`

// ===== 只读内容渲染（解析 Markdown + 图片标记）=====
function RenderedContent({ text, images, reportId }) {
  if (!text) return <div style={{ color: '#ccc' }}>（无内容）</div>
  const lines = text.split('\n')
  return lines.map((line, i) => {
    const t = line.trim()
    if (!t) return <div key={i} style={{ height: 6 }} />
    if (t.startsWith('[图片:') && t.endsWith(']')) {
      const fname = t.slice(4, -1)
      const img = images.find(im => im.filename === fname)
      if (img) {
        return (
          <div key={i} style={{ textAlign: 'center', margin: '12px 0' }}>
            <Image src={`/api/weekly-reports/${reportId}/images/${img.id}`} style={{ maxWidth: '100%' }} />
          </div>
        )
      }
      return <div key={i} style={{ color: '#999', textAlign: 'center' }}>[图片: {fname}]</div>
    }
    if (t.startsWith('## ')) return <h3 key={i} style={{ marginTop: 16, fontSize: 16 }}>{t.slice(3)}</h3>
    if (t.startsWith('# '))  return <h2 key={i} style={{ fontSize: 18 }}>{t.slice(2)}</h2>
    if (t.startsWith('- ') || t.startsWith('* ')) return <li key={i} style={{ marginLeft: 24 }}>{t.slice(2)}</li>
    return <p key={i} style={{ margin: '4px 0', lineHeight: 1.8 }}>{t}</p>
  })
}

export default function WeeklyReport() {
  const { user } = useAuth()
  const isAdmin = user?.is_admin

  const [reports, setReports] = useState([])
  const [stores, setStores] = useState([])
  const [loading, setLoading] = useState(false)

  // 视图: list | edit | view
  const [view, setView] = useState('list')
  const [editingId, setEditingId] = useState(null)
  const [currentReport, setCurrentReport] = useState(null)

  // 表单
  const [storeCode, setStoreCode] = useState(null)
  const [reportDate, setReportDate] = useState(dayjs())
  const [title, setTitle] = useState('')
  const [content, setContent] = useState(TEMPLATE)
  const [polished, setPolished] = useState('')
  const [images, setImages] = useState([])
  const [polishing, setPolishing] = useState(false)
  const [saving, setSaving] = useState(false)

  const [reviewOpen, setReviewOpen] = useState(false)
  const [reviewingId, setReviewingId] = useState(null)
  const [reviewForm] = Form.useForm()
  const taRef = useRef(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await getWeeklyReports()
      setReports(res.data)
    } catch { message.error('加载失败') }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    getStores().then(r => setStores(r.data)).catch(() => {})
    load()
  }, [load])

  // ===== 新建 =====
  const handleCreate = () => {
    setEditingId(null); setCurrentReport(null)
    setStoreCode(null); setReportDate(dayjs())
    setTitle(`周报 - ${dayjs().format('YYYY-MM-DD')}`)
    setContent(TEMPLATE); setPolished(''); setImages([])
    setView('edit')
  }

  // ===== 编辑 =====
  const handleEdit = async (r) => {
    try {
      const res = await getWeeklyReport(r.id)
      const d = res.data
      setEditingId(d.id); setCurrentReport(d)
      setStoreCode(d.store_code); setTitle(d.title)
      setContent(d.content || ''); setPolished(d.polished_content || '')
      setImages(d.images || [])
      setView('edit')
    } catch { message.error('加载失败') }
  }

  // ===== 查看（管理员）=====
  const handleView = async (r) => {
    try {
      const res = await getWeeklyReport(r.id)
      const d = res.data
      setEditingId(d.id); setCurrentReport(d); setTitle(d.title)
      setContent(d.content || ''); setPolished(d.polished_content || '')
      setImages(d.images || [])
      setView('view')
    } catch { message.error('加载失败') }
  }

  const handleBack = () => { setView('list'); load() }

  // ===== 保存/提交 =====
  const handleSave = async (submit = false) => {
    if (!storeCode) { message.warning('请选择店铺'); return }
    if (!title.trim()) { message.warning('请输入标题'); return }
    setSaving(true)
    try {
      if (editingId) {
        await updateWeeklyReport(editingId, { title, content, polished_content: polished })
        if (submit) { await submitWeeklyReport(editingId); message.success('已提交') }
        else { message.success('已保存') }
      } else {
        const res = await createWeeklyReport({ store: storeCode, date: reportDate.format('YYYY-MM-DD'), title, content })
        setEditingId(res.data.id); setCurrentReport(res.data)
        if (submit) { await submitWeeklyReport(res.data.id); message.success('已创建并提交') }
        else { message.success('已创建草稿') }
      }
      if (submit) handleBack()
    } catch (e) { message.error(e?.response?.data?.detail || '操作失败') }
    finally { setSaving(false) }
  }

  // ===== AI润色 =====
  const handlePolish = async () => {
    if (!editingId) { message.warning('请先保存'); return }
    if (!content.trim()) { message.warning('内容为空'); return }
    setPolishing(true)
    try {
      const res = await polishWeeklyReport(editingId)
      setPolished(res.data.polished_content)
      message.success('AI润色完成')
    } catch (e) { message.error(e?.response?.data?.detail || '润色失败') }
    finally { setPolishing(false) }
  }

  // ===== 图片上传 → 插入文档 =====
  const handleUpload = async ({ file, onSuccess, onError }) => {
    if (!editingId) { message.warning('请先保存周报'); onError?.(new Error()); return }
    try {
      const res = await uploadReportImage(editingId, file)
      const d = res.data
      setImages(prev => [...prev, d])
      // 在光标位置插入图片标记
      const mark = `\n[图片:${d.filename}]\n`
      const ta = taRef.current
      if (ta) {
        const s = ta.selectionStart, e = ta.selectionEnd
        setContent(c => c.substring(0, s) + mark + c.substring(e))
        setTimeout(() => { ta.focus(); ta.setSelectionRange(s + mark.length, s + mark.length) }, 0)
      } else {
        setContent(c => c + mark)
      }
      onSuccess?.(d)
    } catch { onError?.(new Error()); message.error('上传失败') }
  }

  const handleDeleteImage = async (imgId, filename) => {
    try {
      await deleteReportImage(editingId, imgId)
      setImages(prev => prev.filter(im => im.id !== imgId))
      setContent(c => c.replace(`\n[图片:${filename}]\n`, '\n'))
    } catch { message.error('删除失败') }
  }

  // ===== 导出 =====
  const handleExport = async (record) => {
    const id = record?.id || editingId
    if (!id) return
    try {
      const res = await exportWeeklyReport(id)
      let fname = '周报.docx'
      const d = res.headers['content-disposition']
      if (d) { const m = d.match(/filename\*=UTF-8''(.+)/); if (m) fname = decodeURIComponent(m[1]) }
      const blob = new Blob([res.data], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a'); a.href = url; a.download = fname; a.click()
      URL.revokeObjectURL(url)
    } catch { message.error('导出失败') }
  }

  // ===== 审批 =====
  const handleReview = async () => {
    try {
      const v = await reviewForm.validateFields()
      await reviewWeeklyReport(reviewingId, { action: v.action, comment: v.comment || '' })
      message.success(v.action === 'approve' ? '已通过' : '已退回')
      setReviewOpen(false); reviewForm.resetFields(); handleBack()
    } catch (e) { if (e?.errorFields) return; message.error('审批失败') }
  }

  const handleDelete = async (id) => {
    try { await deleteWeeklyReport(id); message.success('已删除'); load() }
    catch (e) { message.error(e?.response?.data?.detail || '删除失败') }
  }

  // ===== 管理员按周次分组 =====
  const groups = (() => {
    const m = {}
    reports.forEach(r => {
      const k = `${r.year}-W${String(r.week_num).padStart(2, '0')}`
      if (!m[k]) m[k] = { key: k, year: r.year, week: r.week_num, range: r.date_range, items: [] }
      m[k].items.push(r)
    })
    return Object.values(m).sort((a, b) => b.key.localeCompare(a.key))
  })()

  const canEdit = !currentReport || ['draft', 'rejected'].includes(currentReport.status)

  // ================================================================
  // 文档编辑/查看页面
  // ================================================================
  if (view === 'edit' || view === 'view') {
    const readOnly = view === 'view'
    const showPolished = polished && (readOnly || !canEdit)

    return (
      <div style={{ background: '#f0f2f5', minHeight: '100vh' }}>
        {/* 顶部工具栏 */}
        <div style={{
          position: 'sticky', top: 0, zIndex: 10, background: '#fff',
          borderBottom: '1px solid #e8e8e8', padding: '10px 24px',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <Button icon={<ArrowLeftOutlined />} onClick={handleBack}>返回</Button>
          <Space>
            <Button icon={<ExportOutlined />} onClick={() => handleExport()}>导出Word</Button>
            {!readOnly && canEdit && (
              <>
                <Button loading={saving} onClick={() => handleSave(false)}>
                  {editingId ? '保存' : '创建草稿'}
                </Button>
                <Button type="primary" loading={saving} onClick={() => handleSave(true)}>
                  {editingId ? '提交' : '创建并提交'}
                </Button>
              </>
            )}
            {readOnly && isAdmin && currentReport?.status === 'submitted' && (
              <Button type="primary" icon={<CheckOutlined />} onClick={() => {
                setReviewingId(editingId); setReviewOpen(true)
              }}>审批</Button>
            )}
            {readOnly && isAdmin && (
              <Popconfirm title="确认删除该周报？" onConfirm={() => handleDelete(editingId)}>
                <Button danger icon={<DeleteOutlined />}>删除</Button>
              </Popconfirm>
            )}
          </Space>
        </div>

        {/* 纸张区域 */}
        <div style={{ display: 'flex', justifyContent: 'center', padding: '40px 20px 80px' }}>
          <div style={{
            width: '100%', maxWidth: 800, background: '#fff',
            padding: '60px 72px', boxShadow: '0 2px 12px rgba(0,0,0,0.08)',
            minHeight: 700,
          }}>
            {/* 店铺 + 周次 */}
            <Row gutter={24} style={{ marginBottom: 28 }}>
              <Col span={12}>
                <div style={{ marginBottom: 4, color: '#bbb', fontSize: 12 }}>店铺</div>
                {readOnly || !canEdit || editingId ? (
                  <div style={{ fontSize: 15 }}>{currentReport?.store_name || '-'}</div>
                ) : (
                  <Select
                    style={{ width: '100%' }} value={storeCode} onChange={setStoreCode}
                    placeholder="选择店铺"
                    options={stores.map(s => ({ value: s.code, label: s.name }))}
                  />
                )}
              </Col>
              <Col span={12}>
                <div style={{ marginBottom: 4, color: '#bbb', fontSize: 12 }}>周次</div>
                {editingId ? (
                  <div style={{ fontSize: 15 }}>
                    {currentReport?.year}年第{currentReport?.week_num}周
                    <span style={{ color: '#999', marginLeft: 8 }}>({currentReport?.date_range})</span>
                  </div>
                ) : (
                  <div>
                    <DatePicker
                      style={{ width: '100%' }}
                      value={reportDate}
                      onChange={setReportDate}
                      format="YYYY年MM月DD日"
                    />
                    {reportDate && (
                      <div style={{ fontSize: 12, color: '#999', marginTop: 4 }}>
                        {reportDate.year()}年第{reportDate.isoWeek()}周
                        （{reportDate.startOf('isoWeek').format('MM/DD')} - {reportDate.endOf('isoWeek').format('MM/DD')}）
                      </div>
                    )}
                  </div>
                )}
              </Col>
            </Row>

            {/* 状态标签 */}
            {currentReport && (
              <div style={{ textAlign: 'center', marginBottom: 16 }}>
                <Tag color={STATUS[currentReport.status]?.color} style={{ fontSize: 13, padding: '2px 12px' }}>
                  {STATUS[currentReport.status]?.text}
                </Tag>
              </div>
            )}

            {/* 标题 */}
            <input
              style={{
                width: '100%', border: 'none', outline: 'none', textAlign: 'center',
                fontSize: 26, fontWeight: 'bold', marginBottom: 32, padding: '8px 0',
                background: 'transparent',
              }}
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="请输入标题"
              disabled={readOnly || !canEdit}
            />

            {/* 正文 */}
            {readOnly ? (
              <div style={{ fontSize: 14, lineHeight: 1.8, minHeight: 300 }}>
                <RenderedContent
                  text={showPolished ? polished : content}
                  images={images}
                  reportId={editingId}
                />
              </div>
            ) : (
              <textarea
                ref={taRef}
                style={{
                  width: '100%', border: 'none', outline: 'none', resize: 'none',
                  fontSize: 14, lineHeight: 1.8, minHeight: 350,
                  padding: 0, background: 'transparent', fontFamily: 'inherit',
                }}
                value={content}
                onChange={e => setContent(e.target.value)}
                placeholder="请输入周报内容，支持 ## 小标题、- 列表、[图片:名] 标记..."
              />
            )}

            {/* AI润色 */}
            {!readOnly && canEdit && (
              <div style={{ marginTop: 28, paddingTop: 16, borderTop: '1px dashed #e8e8e8' }}>
                <Space style={{ marginBottom: 8 }}>
                  <Button icon={<RobotOutlined />} loading={polishing} onClick={handlePolish} disabled={!editingId}>
                    AI润色
                  </Button>
                  {!editingId && <span style={{ color: '#ccc', fontSize: 12 }}>请先保存周报</span>}
                </Space>
                {polished && (
                  <TextArea
                    value={polished} onChange={e => setPolished(e.target.value)}
                    rows={8} style={{ marginTop: 8 }}
                    placeholder="润色后内容（可编辑，导出时优先使用）"
                  />
                )}
              </div>
            )}

            {/* 图片上传 */}
            {!readOnly && canEdit && (
              <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px dashed #e8e8e8' }}>
                <Upload customRequest={handleUpload} showUploadList={false} multiple disabled={!editingId}>
                  <Button icon={<UploadOutlined />} disabled={!editingId}>插入图片到文档</Button>
                </Upload>
                {!editingId && <span style={{ color: '#ccc', fontSize: 12, marginLeft: 8 }}>请先保存周报</span>}
                {images.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
                    {images.map(img => (
                      <div key={img.id} style={{ position: 'relative', width: 90 }}>
                        <Image
                          src={`/api/weekly-reports/${editingId}/images/${img.id}`}
                          width={90} height={90}
                          style={{ objectFit: 'cover', borderRadius: 4 }}
                        />
                        <Button
                          size="small" danger type="primary" icon={<DeleteOutlined />}
                          style={{ position: 'absolute', top: 0, right: 0, borderRadius: '0 4px 0 4px' }}
                          onClick={() => handleDeleteImage(img.id, img.filename)}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* 审批信息 */}
            {readOnly && currentReport && (currentReport.status === 'approved' || currentReport.status === 'rejected') && (
              <div style={{ marginTop: 28, padding: 16, background: '#fafafa', borderRadius: 4 }}>
                <div style={{ fontWeight: 'bold', marginBottom: 8 }}>审批信息</div>
                {currentReport.reviewer && <div>审批人：{currentReport.reviewer}</div>}
                {currentReport.reviewed_at && <div>审批时间：{currentReport.reviewed_at}</div>}
                {currentReport.review_comment && <div>审批意见：{currentReport.review_comment}</div>}
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  // ================================================================
  // 列表页面
  // ================================================================
  return (
    <div>
      {/* ===== 管理员视角：按周次分组 ===== */}
      {isAdmin ? (
        <div>
          <h2 style={{ marginBottom: 20 }}>周报审批</h2>
          {loading ? (
            <Spin style={{ display: 'block', margin: '80px auto' }} />
          ) : groups.length === 0 ? (
            <Empty description="暂无周报" style={{ marginTop: 80 }} />
          ) : (
            <Collapse accordion>
              {groups.map(g => (
                <Collapse.Panel
                  key={g.key}
                  header={
                    <Space>
                      <FileTextOutlined />
                      <span style={{ fontSize: 15, fontWeight: 500 }}>
                        {g.year}年第{g.week}周
                      </span>
                      <span style={{ color: '#999', fontSize: 13 }}>{g.range}</span>
                      <Tag>{g.items.length}份</Tag>
                    </Space>
                  }
                >
                  <Row gutter={[16, 16]}>
                    {g.items.map(r => (
                      <Col key={r.id} xs={24} sm={12}>
                        <Card
                          size="small" hoverable
                          onClick={() => handleView(r)}
                          actions={[
                            <Tooltip title="导出Word" key="export">
                              <ExportOutlined onClick={e => { e.stopPropagation(); handleExport(r) }} />
                            </Tooltip>,
                            r.status === 'submitted' && (
                              <Tooltip title="审批" key="review">
                                <CheckOutlined onClick={e => { e.stopPropagation(); setReviewingId(r.id); setReviewOpen(true) }} />
                              </Tooltip>
                            ),
                          ].filter(Boolean)}
                        >
                          <div style={{ marginBottom: 8 }}>
                            <Tag color={STATUS[r.status]?.color}>{STATUS[r.status]?.text}</Tag>
                          </div>
                          <Card.Meta
                            title={<span style={{ fontSize: 14 }}>{r.title}</span>}
                            description={
                              <Space split={<Divider type="vertical" />} style={{ fontSize: 12, color: '#999' }}>
                                <span>{r.store_name}</span>
                                <span>{r.author}</span>
                                {r.image_count > 0 && <span><PictureOutlined /> {r.image_count}</span>}
                              </Space>
                            }
                          />
                        </Card>
                      </Col>
                    ))}
                  </Row>
                </Collapse.Panel>
              ))}
            </Collapse>
          )}
        </div>
      ) : (
        /* ===== 普通用户视角：卡片列表 ===== */
        <div>
          <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ margin: 0 }}>我的周报</h2>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>新建周报</Button>
          </div>
          {loading ? (
            <Spin style={{ display: 'block', margin: '80px auto' }} />
          ) : reports.length === 0 ? (
            <Empty description="暂无周报，点击「新建周报」开始" style={{ marginTop: 80 }} />
          ) : (
            <Row gutter={[16, 16]}>
              {reports.map(r => (
                <Col key={r.id} xs={24} sm={12} md={8} lg={6}>
                  <Card
                    hoverable size="small"
                    onClick={() => r.status === 'approved' ? handleView(r) : handleEdit(r)}
                    actions={[
                      <Tooltip title="导出" key="export">
                        <ExportOutlined onClick={e => { e.stopPropagation(); handleExport(r) }} />
                      </Tooltip>,
                      r.status !== 'approved' && (
                        <Popconfirm key="del" title="确认删除？" onConfirm={e => { e?.stopPropagation(); handleDelete(r.id) }}>
                          <DeleteOutlined onClick={e => e.stopPropagation()} />
                        </Popconfirm>
                      ),
                    ].filter(Boolean)}
                  >
                    <div style={{ marginBottom: 6 }}>
                      <Tag color={STATUS[r.status]?.color}>{STATUS[r.status]?.text}</Tag>
                    </div>
                    <Card.Meta
                      title={<span style={{ fontSize: 14 }}>{r.title}</span>}
                      description={
                        <div style={{ fontSize: 12, color: '#999' }}>
                          <div>{r.store_name} | {r.year}年第{r.week_num}周</div>
                          <div style={{ marginTop: 2 }}>{r.date_range} | {r.author}</div>
                          {r.image_count > 0 && <div style={{ marginTop: 2 }}><PictureOutlined /> {r.image_count}张图片</div>}
                        </div>
                      }
                    />
                  </Card>
                </Col>
              ))}
            </Row>
          )}
        </div>
      )}

      {/* 审批Modal */}
      <Modal
        title="审批周报" open={reviewOpen}
        onCancel={() => { setReviewOpen(false); reviewForm.resetFields() }}
        onOk={handleReview} okText="确认" cancelText="取消"
      >
        <Form form={reviewForm} layout="vertical">
          <Form.Item name="action" label="审批结果" rules={[{ required: true, message: '请选择' }]}>
            <Select options={[{ value: 'approve', label: '通过' }, { value: 'reject', label: '退回' }]} />
          </Form.Item>
          <Form.Item name="comment" label="审批意见">
            <TextArea rows={3} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
