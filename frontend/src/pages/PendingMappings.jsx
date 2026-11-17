// 商品映射审核 — 待确认区 + 异常池（三元组 (店铺, SKU, ASIN)）
// 对应文档 MM-02：新ASIN+新SKU → 待确认；SKU命中+ASIN新 / 单SKU多ASIN → 异常待审
// mapping_review_enhance：07 基准批量确认 / amzn.gr. 重归一 / 报告中心 / 勾选批量确认
import React, { useState, useEffect } from 'react'
import {
  Card, Table, Button, Select, Space, message, Tag, Alert, Popconfirm,
  Row, Col, Statistic, Tabs, Input, Modal, Descriptions,
} from 'antd'
import {
  CheckOutlined, CloseOutlined, ReloadOutlined, DatabaseOutlined,
  PlayCircleOutlined, EyeOutlined, FileExcelOutlined, SyncOutlined,
} from '@ant-design/icons'
import { downloadBlob } from '../api'
import {
  getStores, getPendingMappings, decideMapping, getReplacements, getProducts,
  importMappingBaseline, getMappingBaselineSummary, bulkConfirmMappings,
  realignReplacements, getMappingReports, getMappingReport, exportMappingReport,
} from '../api'

const CONFLICT_LABELS = {
  same_sku_diff_asin: '同SKU不同ASIN',
  same_asin_diff_sku: '同ASIN不同SKU',
  baseline_internal: '基准内冲突',
  sku_drift: 'SKU漂移红线',
}

export default function PendingMappings() {
  const [stores, setStores] = useState([])
  const [store, setStore] = useState()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [replacements, setReplacements] = useState([])
  const [products, setProducts] = useState([])
  const [keyword, setKeyword] = useState('')

  // ---- mapping_review_enhance 状态 ----
  const [baseline, setBaseline] = useState(null)
  const [importing, setImporting] = useState(false)
  const [bulkPreview, setBulkPreview] = useState(null)   // dry-run 报告
  const [bulkBusy, setBulkBusy] = useState(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState([])
  const [decidingBatch, setDecidingBatch] = useState(false)
  const [realignPreview, setRealignPreview] = useState(null)
  const [realignBusy, setRealignBusy] = useState(false)
  const [reports, setReports] = useState([])
  const [reportsLoading, setReportsLoading] = useState(false)

  useEffect(() => {
    getStores().then((r) => {
      setStores(r.data || [])
      if (r.data?.[0]) setStore(r.data[0].store_code)
    }).catch(() => {})
    loadBaseline()
  }, [])

  const loadBaseline = async () => {
    try {
      const r = await getMappingBaselineSummary()
      setBaseline(r.data)
    } catch (e) {
      setBaseline(null)
    }
  }

  const load = async () => {
    setLoading(true)
    try {
      const params = {}
      if (store) params.store = store
      const [m, rep, prods] = await Promise.all([
        getPendingMappings(params),
        getReplacements(),
        getProducts({ store, keyword: keyword || undefined, limit: 200 }),
      ])
      setItems(m.data.items || [])
      setReplacements(rep.data.items || [])
      setProducts(prods.data.items || [])
      setSelectedRowKeys([])
    } catch (e) {
      message.error('加载失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (store) load() }, [store])

  const decide = async (row, action) => {
    try {
      await decideMapping(row.id, action)
      message.success(action === 'confirm' ? '已确认，转为正式映射' : '已驳回')
      load()
    } catch (e) {
      message.error('操作失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  // ---- 基准导入 / 批量确认 / 勾选批量 / 重归一 / 报告 ----

  const doImportBaseline = async () => {
    setImporting(true)
    try {
      const r = await importMappingBaseline({ dry_run: false })
      const s = r.data.summary
      message.success(`基准导入完成：${s.files_ok}/${s.files_total} 文件，条目 ${s.items_total}（新增 ${s.items_inserted ?? 0}），冲突 ${s.baseline_conflicts} 组`)
      loadBaseline()
    } catch (e) {
      message.error('基准导入失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setImporting(false)
    }
  }

  const doBulkPreview = async () => {
    setBulkBusy(true)
    try {
      const r = await bulkConfirmMappings({ store, dry_run: true })
      setBulkPreview(r.data)
    } catch (e) {
      message.error('预览失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setBulkBusy(false)
    }
  }

  const doBulkExecute = async () => {
    setBulkBusy(true)
    try {
      const r = await bulkConfirmMappings({ store, dry_run: false })
      const s = r.data.summary
      message.success(`批量确认完成：确认 ${s.confirmed} / 未覆盖 ${s.uncovered} / 冲突 ${s.conflicts}（报告 #${r.data.report_id}）`)
      setBulkPreview(r.data)
      load()
    } catch (e) {
      message.error('执行失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setBulkBusy(false)
    }
  }

  const doDecideSelected = async () => {
    const rows = pending.filter((i) => selectedRowKeys.includes(i.id))
    if (!rows.length) return
    setDecidingBatch(true)
    let ok = 0, fail = 0
    for (const row of rows) {
      try {
        await decideMapping(row.id, 'confirm')
        ok++
      } catch {
        fail++
      }
    }
    setDecidingBatch(false)
    setSelectedRowKeys([])
    message.success(`勾选确认完成：成功 ${ok}${fail ? `，失败 ${fail}` : ''}`)
    load()
  }

  const doRealign = async (dryRun) => {
    setRealignBusy(true)
    try {
      const r = await realignReplacements({ store, dry_run: dryRun })
      setRealignPreview(r.data)
      if (!dryRun) {
        message.success(`重归一完成：修正 ${r.data.summary.changed}，保留 ${r.data.summary.kept_raw}`)
        load()
      }
    } catch (e) {
      message.error('重归一失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setRealignBusy(false)
    }
  }

  const loadReports = async () => {
    setReportsLoading(true)
    try {
      const r = await getMappingReports({ limit: 30 })
      setReports(r.data.items || [])
    } catch (e) {
      message.error('报告加载失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setReportsLoading(false)
    }
  }

  const doExportReport = async (reportId, section) => {
    try {
      const r = await exportMappingReport(reportId, section)
      downloadBlob(r.data, `映射报告_${reportId}_${section}.xlsx`)
    } catch (e) {
      message.error('导出失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const pending = items.filter((i) => i.status === 'pending')
  const abnormal = items.filter((i) => i.status === 'abnormal')

  const columns = (withReason) => [
    { title: '店铺', dataIndex: 'store_code', width: 80 },
    { title: 'SKU', dataIndex: 'sku', width: 160 },
    { title: 'ASIN', dataIndex: 'asin', width: 140 },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (v) => v === 'pending' ? <Tag color="orange">待确认</Tag>
        : v === 'abnormal' ? <Tag color="red">异常待审</Tag> : <Tag>{v}</Tag>,
    },
    ...(withReason ? [{
      title: '异常原因', dataIndex: 'note', ellipsis: true,
      render: (v) => v || '—',
    }] : []),
    {
      title: '首次出现', dataIndex: 'first_seen_at', width: 170,
      render: (v) => (v || '').replace('T', ' ').slice(0, 19),
    },
    {
      title: '操作', width: 170,
      render: (_, r) => (
        <Space size={4}>
          <Popconfirm
            title="确认后将转为正式映射"
            description="若该 SKU 已绑定其它 ASIN 会被拒绝（SKU 不允许跨 ASIN 漂移）"
            onConfirm={() => decide(r, 'confirm')}
          >
            <Button size="small" type="primary" ghost icon={<CheckOutlined />}>确认</Button>
          </Popconfirm>
          <Popconfirm title="确定驳回？" onConfirm={() => decide(r, 'reject')}>
            <Button size="small" danger icon={<CloseOutlined />}>驳回</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const bulkSummary = bulkPreview?.summary || {}

  return (
    <div style={{ padding: 4 }}>
      <Alert
        type="info" showIcon style={{ marginBottom: 16 }}
        message="商品与 ASIN-SKU 映射（三元组模型）"
        description="以 (店铺, SKU, ASIN) 三元组建模：新品自动入待确认区；SKU 撞新 ASIN / 单 SKU 多 ASIN 拦截入异常池，旧映射保留不动。待确认商品照常参与业务统计（列表标黄）。支持以 07 产品信息表为基准批量确认。"
      />

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <span>店铺</span>
          <Select style={{ width: 130 }} value={store} onChange={setStore}
            options={stores.map((s) => ({ value: s.store_code, label: s.store_code }))} />
          <Input style={{ width: 200 }} placeholder="商品关键字(ASIN/标题)"
            value={keyword} onChange={(e) => setKeyword(e.target.value)} onPressEnter={load} />
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        </Space>
      </Card>

      {/* ---- 07 基准与批量确认操作卡（mapping_review_enhance 组7.2） ---- */}
      <Card size="small" style={{ marginBottom: 16 }} title="07 基准批量确认">
        <Space wrap size={12}>
          <Tag color={baseline?.items_total ? 'green' : 'default'} icon={<DatabaseOutlined />}>
            基准条目 {baseline?.items_total ?? '未导入'}
          </Tag>
          {(baseline?.by_store || []).map((s) => (
            <Tag key={s.store_code}>{s.store_code}: {s.items}</Tag>
          ))}
          {baseline?.last_import_report_id && (
            <span style={{ color: '#888', fontSize: 12 }}>报告 #{baseline.last_import_report_id}</span>
          )}
          <Popconfirm
            title="导入 07 基准"
            description="读取服务器配置的 07 汇总目录产品信息表（幂等，重复导入只补新增）"
            onConfirm={doImportBaseline}
          >
            <Button icon={<DatabaseOutlined />} loading={importing}>导入 07 基准</Button>
          </Popconfirm>
          <Button icon={<EyeOutlined />} loading={bulkBusy} onClick={doBulkPreview}>
            批量确认预览{store ? `（${store}）` : '（全部）'}
          </Button>
          <Popconfirm
            title="执行批量确认"
            description="完全匹配项将转为正式映射（可确认项）；未覆盖与冲突项保持待确认。可在预览中先核对。"
            onConfirm={doBulkExecute}
          >
            <Button type="primary" icon={<PlayCircleOutlined />} loading={bulkBusy}
              disabled={!baseline?.items_total}>执行批量确认</Button>
          </Popconfirm>
        </Space>
      </Card>

      <Row gutter={12} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small">
            <Statistic title="待确认新品" value={pending.length}
              valueStyle={{ color: pending.length ? '#fa8c16' : '#3f8600' }} />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="异常待审" value={abnormal.length}
              valueStyle={{ color: abnormal.length ? '#cf1322' : '#3f8600' }} />
          </Card>
        </Col>
        <Col span={6}><Card size="small"><Statistic title="替换件归并 (amzn.gr.*)" value={replacements.length} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="商品主档" value={products.length} /></Card></Col>
      </Row>

      <Tabs
        onChange={(key) => { if (key === 'reports') loadReports() }}
        items={[
          {
            key: 'pending',
            label: `待确认区 (${pending.length})`,
            children: (
              <>
                <Space style={{ marginBottom: 8 }}>
                  <Button size="small" type="primary" ghost icon={<CheckOutlined />}
                    disabled={!selectedRowKeys.length} loading={decidingBatch}
                    onClick={doDecideSelected}>
                    确认选中 ({selectedRowKeys.length})
                  </Button>
                  <span style={{ color: '#888', fontSize: 12 }}>
                    勾选后逐条确认（SKU 跨 ASIN 漂移会被拒绝）；与"按基准一键确认"互补
                  </span>
                </Space>
                <Table size="small" rowKey="id" loading={loading} dataSource={pending}
                  pagination={{ pageSize: 12 }} columns={columns(false)}
                  rowSelection={{
                    selectedRowKeys,
                    onChange: setSelectedRowKeys,
                    getCheckboxProps: (r) => ({ disabled: r.status !== 'pending' }),
                  }} />
              </>
            ),
          },
          {
            key: 'abnormal',
            label: `异常池 (${abnormal.length})`,
            children: (
              <Table size="small" rowKey="id" loading={loading} dataSource={abnormal}
                pagination={{ pageSize: 12 }} columns={columns(true)} />
            ),
          },
          {
            key: 'products',
            label: `商品主档 (${products.length})`,
            children: (
              <Table size="small" rowKey="id" dataSource={products} pagination={{ pageSize: 12 }}
                columns={[
                  { title: 'ASIN', dataIndex: 'asin', width: 130 },
                  { title: '标题', dataIndex: 'title', ellipsis: true },
                  {
                    title: '状态', dataIndex: 'status', width: 100,
                    render: (v) => v === 'normal' ? <Tag color="green">正常</Tag>
                      : v === 'pending' ? <Tag color="orange">待确认</Tag> : <Tag color="red">{v}</Tag>,
                  },
                  {
                    title: 'SKU 映射', dataIndex: 'skus',
                    render: (skus) => (skus || []).map((s) => (
                      <Tag key={s.sku} color={s.status === 'normal' ? 'green' : s.status === 'abnormal' ? 'red' : 'orange'}>
                        {s.sku}
                      </Tag>
                    )),
                  },
                ]}
              />
            ),
          },
          {
            key: 'replacements',
            label: `替换件归并 (${replacements.length})`,
            children: (
              <>
                <Space style={{ marginBottom: 8 }}>
                  <Button size="small" icon={<SyncOutlined />} loading={realignBusy}
                    onClick={() => doRealign(true)}>
                    重归一预览{store ? `（${store}）` : '（全部）'}
                  </Button>
                  {realignPreview && (
                    <Popconfirm
                      title="执行重归一"
                      description="按新相似度逻辑修正归并目标（旧值留痕 prev_parent_sku，可追溯）"
                      onConfirm={() => doRealign(false)}
                    >
                      <Button size="small" type="primary" ghost loading={realignBusy}
                        disabled={realignPreview.dry_run !== true || !realignPreview.summary.changed}>
                        执行重归一（变更 {realignPreview.summary.changed}）
                      </Button>
                    </Popconfirm>
                  )}
                </Space>
                {realignPreview && (
                  <Descriptions size="small" bordered column={5} style={{ marginBottom: 8 }}>
                    <Descriptions.Item label="扫描">{realignPreview.summary.scanned}</Descriptions.Item>
                    <Descriptions.Item label="一致">{realignPreview.summary.unchanged}</Descriptions.Item>
                    <Descriptions.Item label="修正">{realignPreview.summary.changed}</Descriptions.Item>
                    <Descriptions.Item label="旧目标非法">{realignPreview.summary.invalid_target}</Descriptions.Item>
                    <Descriptions.Item label="保留原值">{realignPreview.summary.kept_raw}</Descriptions.Item>
                  </Descriptions>
                )}
                {realignPreview?.details?.changed?.length > 0 && (
                  <Table size="small" rowKey={(r) => r.replacement_sku} style={{ marginBottom: 8 }}
                    pagination={{ pageSize: 5 }}
                    dataSource={realignPreview.details.changed}
                    columns={[
                      { title: '店铺', dataIndex: 'store_code', width: 70 },
                      { title: '原始替换件', dataIndex: 'replacement_sku', ellipsis: true },
                      { title: '旧父SKU', dataIndex: 'old_parent_sku', width: 150 },
                      { title: '新父SKU', dataIndex: 'new_parent_sku', width: 150 },
                    ]} />
                )}
                <Table size="small" rowKey="id" dataSource={replacements} pagination={{ pageSize: 12 }}
                  columns={[
                    { title: '店铺', dataIndex: 'store_code', width: 80 },
                    { title: '原始替换件 SKU', dataIndex: 'replacement_sku', width: 320 },
                    { title: '归并到父 SKU', dataIndex: 'parent_sku', width: 160 },
                  ]}
                />
              </>
            ),
          },
          {
            key: 'reports',
            label: '报告中心',
            children: (
              <Table size="small" rowKey="report_id" loading={reportsLoading} dataSource={reports}
                pagination={{ pageSize: 10 }}
                columns={[
                  { title: '报告', dataIndex: 'report_id', width: 70, render: (v) => `#${v}` },
                  {
                    title: '类型', dataIndex: 'op_type', width: 150,
                    render: (v) => v === 'baseline_import' ? '基准导入' : v === 'bulk_confirm' ? '批量确认' : '重归一',
                  },
                  { title: '范围', dataIndex: 'store_scope', width: 80 },
                  {
                    title: '结果摘要', dataIndex: 'summary', ellipsis: true,
                    render: (s) => {
                      if (!s) return '—'
                      if (s.confirmed !== undefined) {
                        return `确认 ${s.confirmed} / 未覆盖 ${s.uncovered} / 冲突 ${s.conflicts ?? s.conflict ?? 0}`
                      }
                      if (s.changed !== undefined) return `修正 ${s.changed} / 保留 ${s.kept_raw}`
                      if (s.items_total !== undefined) return `条目 ${s.items_total} / 冲突 ${s.baseline_conflicts}`
                      return JSON.stringify(s).slice(0, 80)
                    },
                  },
                  {
                    title: '时间', dataIndex: 'generated_at', width: 160,
                    render: (v) => (v || '').replace('T', ' ').slice(0, 19),
                  },
                  {
                    title: '操作', width: 200,
                    render: (_, r) => (
                      <Space size={4}>
                        <Button size="small" icon={<FileExcelOutlined />}
                          onClick={() => doExportReport(r.report_id, 'all')}>导出</Button>
                        {r.op_type === 'bulk_confirm' && !r.dry_run && (
                          <Button size="small" icon={<FileExcelOutlined />}
                            onClick={() => doExportReport(r.report_id, 'uncovered')}>未覆盖</Button>
                        )}
                        {r.op_type === 'bulk_confirm' && !r.dry_run && (
                          <Button size="small" icon={<FileExcelOutlined />}
                            onClick={() => doExportReport(r.report_id, 'conflict')}>冲突</Button>
                        )}
                      </Space>
                    ),
                  },
                ]}
              />
            ),
          },
        ]}
      />

      {/* ---- 批量确认预览/执行结果 Modal（组7.2） ---- */}
      <Modal
        open={!!bulkPreview}
        title={`批量确认${bulkPreview?.dry_run ? '预览' : '执行结果'}${bulkPreview ? `（${bulkPreview.store_scope}）` : ''}`}
        onCancel={() => setBulkPreview(null)}
        width={860}
        footer={[
          bulkPreview?.dry_run ? (
            <Popconfirm key="exec" title="确认执行批量确认？"
              description="完全匹配项将转为正式映射"
              onConfirm={doBulkExecute}>
              <Button type="primary" loading={bulkBusy}>执行批量确认</Button>
            </Popconfirm>
          ) : (
            <Button key="close" onClick={() => setBulkPreview(null)}>关闭</Button>
          ),
          <Button key="cancel" onClick={() => setBulkPreview(null)}>
            {bulkPreview?.dry_run ? '取消' : '完成'}
          </Button>,
        ]}
      >
        {bulkPreview && (
          <>
            <Row gutter={12} style={{ marginBottom: 12 }}>
              <Col span={5}><Statistic title="待确认总数" value={bulkSummary.total_pending || 0} /></Col>
              <Col span={5}><Statistic title="可确认" value={bulkSummary.confirmed || 0}
                valueStyle={{ color: '#3f8600' }} /></Col>
              <Col span={5}><Statistic title="未覆盖" value={bulkSummary.uncovered || 0}
                valueStyle={{ color: '#fa8c16' }} /></Col>
              <Col span={5}><Statistic title="冲突" value={bulkSummary.conflicts ?? bulkSummary.conflict ?? 0}
                valueStyle={{ color: '#cf1322' }} /></Col>
              <Col span={4}><Statistic title="跳过" value={bulkSummary.skipped_already_normal || 0} /></Col>
            </Row>
            <Tabs size="small" items={[
              {
                key: 'confirmed', label: `可确认 (${bulkPreview.details.confirmed?.length || 0})`,
                children: (
                  <Table size="small" rowKey="mapping_id" pagination={{ pageSize: 8 }}
                    dataSource={bulkPreview.details.confirmed || []}
                    columns={[
                      { title: '店铺', dataIndex: 'store_code', width: 70 },
                      { title: 'SKU', dataIndex: 'sku' },
                      { title: 'ASIN', dataIndex: 'asin' },
                    ]} />
                ),
              },
              {
                key: 'uncovered', label: `未覆盖 (${bulkPreview.details.uncovered?.length || 0})`,
                children: (
                  <Table size="small" rowKey="mapping_id" pagination={{ pageSize: 8 }}
                    dataSource={bulkPreview.details.uncovered || []}
                    columns={[
                      { title: '店铺', dataIndex: 'store_code', width: 70 },
                      { title: 'SKU', dataIndex: 'sku' },
                      { title: 'ASIN', dataIndex: 'asin' },
                      { title: '原因', dataIndex: 'reason', width: 130 },
                    ]} />
                ),
              },
              {
                key: 'conflict', label: `冲突 (${bulkPreview.details.conflicts?.length ?? bulkPreview.details.conflict?.length ?? 0})`,
                children: (
                  <Table size="small" rowKey="mapping_id" pagination={{ pageSize: 8 }}
                    dataSource={bulkPreview.details.conflicts || bulkPreview.details.conflict || []}
                    columns={[
                      { title: '店铺', dataIndex: 'store_code', width: 70 },
                      { title: 'SKU', dataIndex: 'sku' },
                      { title: 'ASIN', dataIndex: 'asin' },
                      {
                        title: '冲突类型', dataIndex: 'conflict_type', width: 130,
                        render: (v) => <Tag color="red">{CONFLICT_LABELS[v] || v}</Tag>,
                      },
                      {
                        title: '基准值', width: 180, ellipsis: true,
                        render: (_, r) => {
                          const a = Array.isArray(r.baseline_asin) ? r.baseline_asin.join(',') : r.baseline_asin
                          const s = Array.isArray(r.baseline_skus) ? r.baseline_skus.join(',') : r.baseline_skus
                          return a || s || '—'
                        },
                      },
                    ]} />
                ),
              },
            ]} />
          </>
        )}
      </Modal>
    </div>
  )
}
