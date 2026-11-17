// 核算快照 — 执行核算、版本对比、下钻溯源、导出
import React, { useState, useEffect } from 'react'
import {
  Card, Table, Button, Select, Space, message, Tag, Modal, Descriptions,
  Statistic, Row, Col, Alert, Input, Divider, Popconfirm, DatePicker,
} from 'antd'
import { ThunderboltOutlined, DownloadOutlined, DiffOutlined } from '@ant-design/icons'
import {
  getStores, calculateProfit, getSnapshots, getSnapshotDetail,
  compareSnapshots, exportSnapshot, traceSnapshotItem, downloadBlob,
  waitAccountingTask,
} from '../api'

const COUNTRIES = ['', 'UK', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'BE', 'IE', 'US', 'CA', 'MX', 'AE', 'SA', 'AU', 'PL']

export default function Snapshots() {
  const [stores, setStores] = useState([])
  const [store, setStore] = useState()
  const [country, setCountry] = useState('')
  const [period, setPeriod] = useState(() => {
    const d = new Date()
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
  })
  const [snapshotName, setSnapshotName] = useState('')
  const [snapshots, setSnapshots] = useState([])
  const [loading, setLoading] = useState(false)
  const [calculating, setCalculating] = useState(false)
  const [detail, setDetail] = useState(null)
  const [compare, setCompare] = useState(null)
  const [selected, setSelected] = useState([])
  const [traceItem, setTraceItem] = useState(null)

  useEffect(() => {
    getStores().then((r) => {
      setStores(r.data || [])
      if (r.data?.[0]) setStore(r.data[0].store_code)
    }).catch(() => message.error('加载店铺失败'))
  }, [])

  const load = async () => {
    setLoading(true)
    try {
      const res = await getSnapshots({ store, period_month: period })
      setSnapshots(res.data.items || [])
    } catch (e) {
      message.error('加载快照失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (store) load() }, [store, period])

  const doCalculate = async () => {
    if (!store || !period) return message.warning('请选择店铺与月份')
    setCalculating(true)
    const msgKey = 'calc-task'
    message.loading({ content: '核算任务已提交…', key: msgKey, duration: 0 })
    try {
      const res = await calculateProfit({
        store, country: country || '', period_month: period, snapshot_name: snapshotName,
      })
      const task = await waitAccountingTask(res.data.task_id, (t) => {
        message.loading({
          content: `核算进行中 ${t.progress}%（${t.stage || '...'}）`,
          key: msgKey, duration: 0,
        })
      })
      if (task.status !== 'success') {
        throw new Error(task.error?.split('\n')[0] || `任务状态: ${task.status}`)
      }
      const d = task.snapshot
      message.destroy(msgKey)
      Modal.success({
        title: `核算完成（版本 v${d.version}）`,
        content: (
          <div>
            <p>净利润：<b>{d.net_profit_rmb?.toLocaleString()} 元</b>（净利率 {(d.net_profit_rate * 100).toFixed(2)}%）</p>
            <p>销售额：{d.product_sales_rmb?.toLocaleString()} 元 ｜ 汇率：{d.exchange_rate}</p>
            <p>订单数：{d.order_count} ｜ 净销量：{d.order_qty}</p>
            {!d.cost_complete && (
              <p style={{ color: '#cf1322' }}>
                成本不完整：{d.cost_incomplete_skus} 个 SKU 缺成本/运费，利润口径不完整（请到「成本预警」补录）
              </p>
            )}
          </div>
        ),
      })
      setSnapshotName('')
      load()
    } catch (e) {
      message.destroy(msgKey)
      message.error('核算失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setCalculating(false)
    }
  }

  const openDetail = async (id) => {
    try {
      const res = await getSnapshotDetail(id)
      setDetail(res.data)
    } catch (e) {
      message.error('加载明细失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const doCompare = async () => {
    if (selected.length !== 2) return message.warning('请勾选两个快照版本进行对比')
    try {
      const res = await compareSnapshots(selected[0], selected[1])
      setCompare(res.data)
    } catch (e) {
      message.error('对比失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const doExport = async (id) => {
    try {
      const res = await exportSnapshot(id, true)
      downloadBlob(res.data, `核算快照_${store}_${period}_v${id}.xlsx`)
      message.success('导出成功')
    } catch (e) {
      message.error('导出失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const openTrace = async (itemId) => {
    try {
      const res = await traceSnapshotItem(itemId)
      setTraceItem(res.data)
    } catch (e) {
      message.error('溯源失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  return (
    <div style={{ padding: 4 }}>
      <Alert
        type="info" showIcon style={{ marginBottom: 16 }}
        message="利润口径（与旧系统算法完全一致）"
        description="净利润 = net_amazon × 汇率 − 采购成本(RMB) − 头程运费(RMB) − (广告+仓储+超龄仓储+退货+入库配置+移除)×汇率。net_amazon 仅计入 Order/Refund 行并按分桶规则计入 Adjustment；Service Fee/Amazon Fees/FBA Inventory Fee/FBA transaction fees/Transfer 不计入利润（Transfer 单独资金流统计）。"
      />

      <Card size="small" title="执行核算" style={{ marginBottom: 16 }}>
        <Space wrap>
          <span>店铺</span>
          <Select style={{ width: 130 }} value={store} onChange={setStore}
            options={stores.map((s) => ({ value: s.store_code, label: s.store_code }))} />
          <span>站点</span>
          <Select style={{ width: 110 }} value={country} onChange={setCountry}
            options={COUNTRIES.map((c) => ({ value: c, label: c || '全部' }))} />
          <span>月份</span>
          <Input style={{ width: 110 }} value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="YYYY-MM" />
          <Input style={{ width: 200 }} value={snapshotName} onChange={(e) => setSnapshotName(e.target.value)}
            placeholder="快照命名（可选）" />
          <Button type="primary" icon={<ThunderboltOutlined />} loading={calculating} onClick={doCalculate}>
            执行核算
          </Button>
          <Button onClick={load}>刷新列表</Button>
          <Popconfirm title="对比选中的两个版本？" onConfirm={doCompare} disabled={selected.length !== 2}>
            <Button icon={<DiffOutlined />} disabled={selected.length !== 2}>版本对比</Button>
          </Popconfirm>
        </Space>
      </Card>

      <Card size="small" title={`核算快照（${snapshots.length}）`}>
        <Table
          size="small" rowKey="id" loading={loading}
          dataSource={snapshots} pagination={{ pageSize: 10 }}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: (keys) => setSelected(keys.slice(-2)),
            getCheckboxProps: (r) => ({ disabled: selected.length >= 2 && !selected.includes(r.id) }),
          }}
          columns={[
            { title: '店铺', dataIndex: 'store_code', width: 80 },
            { title: '站点', dataIndex: 'country', width: 70, render: (v) => v || '全部' },
            { title: '月份', dataIndex: 'period_month', width: 90 },
            { title: '版本', dataIndex: 'version', width: 70, render: (v) => <Tag color="blue">v{v}</Tag> },
            { title: '名称', dataIndex: 'snapshot_name', width: 160, ellipsis: true },
            {
              title: '净利润(元)', dataIndex: 'net_profit_rmb', width: 130, align: 'right',
              render: (v) => <b style={{ color: v >= 0 ? '#3f8600' : '#cf1322' }}>{Number(v).toLocaleString()}</b>,
            },
            { title: '净利率', dataIndex: 'net_profit_rate', width: 90, align: 'right', render: (v) => `${(v * 100).toFixed(2)}%` },
            { title: '销售额(元)', dataIndex: 'product_sales_rmb', width: 130, align: 'right', render: (v) => Number(v).toLocaleString() },
            {
              title: '成本完整性', dataIndex: 'cost_complete', width: 120,
              render: (v, r) => v ? <Tag color="green">完整</Tag> : <Tag color="red">缺 {r.cost_incomplete_skus} SKU</Tag>,
            },
            {
              title: '操作', width: 170,
              render: (_, r) => (
                <Space size={4}>
                  <Button size="small" type="link" onClick={() => openDetail(r.id)}>明细</Button>
                  <Button size="small" type="link" icon={<DownloadOutlined />} onClick={() => doExport(r.id)}>导出</Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      {/* 快照明细 */}
      <Modal
        open={!!detail} onCancel={() => setDetail(null)} footer={null} width={1200}
        title={detail ? `快照明细 — ${detail.snapshot.store_code} ${detail.snapshot.country || '全部'} ${detail.snapshot.period_month} v${detail.snapshot.version}` : ''}
      >
        {detail && (
          <>
            <Row gutter={12} style={{ marginBottom: 14 }}>
              <Col span={4}><Statistic title="净利润(元)" value={detail.snapshot.net_profit_rmb} precision={2} /></Col>
              <Col span={4}><Statistic title="净利率" value={detail.snapshot.net_profit_rate * 100} precision={2} suffix="%" /></Col>
              <Col span={4}><Statistic title="销售额(元)" value={detail.snapshot.product_sales_rmb} precision={2} /></Col>
              <Col span={3}><Statistic title="net_amazon" value={detail.snapshot.net_amazon} precision={2} /></Col>
              <Col span={3}><Statistic title="汇率" value={detail.snapshot.exchange_rate} precision={4} /></Col>
              <Col span={3}><Statistic title="采购成本(元)" value={detail.snapshot.product_cost_rmb} precision={2} /></Col>
              <Col span={3}><Statistic title="头程运费(元)" value={detail.snapshot.freight_cost_rmb} precision={2} /></Col>
            </Row>
            <Row gutter={12} style={{ marginBottom: 14 }}>
              <Col span={4}><Statistic title="广告费" value={detail.snapshot.ad_spend} precision={2} /></Col>
              <Col span={4}><Statistic title="仓储费" value={detail.snapshot.storage_fee} precision={2} /></Col>
              <Col span={4}><Statistic title="超龄仓储费" value={detail.snapshot.aged_storage_fee} precision={2} /></Col>
              <Col span={4}><Statistic title="退货处理费" value={detail.snapshot.returns_fee} precision={2} /></Col>
              <Col span={4}><Statistic title="入库配置费" value={detail.snapshot.inbound_fee} precision={2} /></Col>
              <Col span={4}><Statistic title="移除费" value={detail.snapshot.removal_fee} precision={2} /></Col>
            </Row>
            {!detail.snapshot.cost_complete && (
              <Alert type="warning" showIcon style={{ marginBottom: 10 }}
                message={`成本不完整：${detail.snapshot.cost_incomplete_skus} 个 SKU 缺成本/运费，本快照利润口径不完整（请到「成本预警」补录后重算）`} />
            )}
            {detail.snapshot.rate_source === 'default_fallback' && (
              <Alert type="warning" showIcon style={{ marginBottom: 10 }}
                message={`汇率缺失，使用默认值：站点 ${(detail.snapshot.rate_missing_countries || []).join(', ') || '—'} 回退默认汇率折算，请到「管理 → 汇率」维护后重算`} />
            )}
            <Table
              size="small" rowKey="id" dataSource={detail.items}
              pagination={{ pageSize: 15 }}
              columns={[
                { title: 'ASIN', dataIndex: 'asin', width: 120 },
                { title: 'SKU', dataIndex: 'sku', width: 130 },
                { title: '站点', dataIndex: 'country', width: 70, render: (v) => v || '—' },
                { title: 'net_amazon', dataIndex: 'net_amazon', width: 110, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: '净销量', dataIndex: 'quantity', width: 80, align: 'right' },
                { title: '采购成本', dataIndex: 'product_cost_rmb', width: 100, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: '运费', dataIndex: 'freight_cost_rmb', width: 90, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: '广告费', dataIndex: 'ad_spend', width: 90, align: 'right', render: (v) => Number(v).toFixed(2) },
                {
                  title: '净利润(元)', dataIndex: 'net_profit_rmb', width: 120, align: 'right',
                  render: (v) => <b style={{ color: v >= 0 ? '#3f8600' : '#cf1322' }}>{Number(v).toFixed(2)}</b>,
                },
                {
                  title: '成本状态', dataIndex: 'cost_status', width: 110,
                  render: (v, r) => v === 'normal' ? <Tag color="green">正常</Tag>
                    : v === 'inherited' ? <Tag color="blue">沿用{r.cost_inherited_from}</Tag>
                      : <Tag color="red">缺失</Tag>,
                },
                {
                  title: '溯源', width: 90,
                  render: (_, r) => <Button size="small" type="link" onClick={() => openTrace(r.id)}>下钻</Button>,
                },
              ]}
            />
          </>
        )}
      </Modal>

      {/* 版本对比 */}
      <Modal
        open={!!compare} onCancel={() => setCompare(null)} footer={null} width={1000}
        title="版本对比"
      >
        {compare && (
          <>
            <Row gutter={16} style={{ marginBottom: 14 }}>
              <Col span={8}><Statistic title={`版本 A (v${compare.snapshot_a.version})`} value={compare.snapshot_a.profit} precision={2} /></Col>
              <Col span={8}><Statistic title={`版本 B (v${compare.snapshot_b.version})`} value={compare.snapshot_b.profit} precision={2} /></Col>
              <Col span={8}>
                <Statistic title="差额 (B−A)" value={compare.total_delta} precision={2}
                  valueStyle={{ color: compare.total_delta >= 0 ? '#3f8600' : '#cf1322' }} />
              </Col>
            </Row>
            <div style={{ marginBottom: 8 }}>差异 SKU 数：<b>{compare.changed_items}</b></div>
            <Table
              size="small" rowKey={(r) => `${r.asin}-${r.sku}-${r.country || ''}`} dataSource={compare.diffs}
              pagination={{ pageSize: 12 }}
              columns={[
                { title: 'ASIN', dataIndex: 'asin' },
                { title: 'SKU', dataIndex: 'sku' },
                { title: '站点', dataIndex: 'country', width: 70, render: (v) => v || '—' },
                { title: 'A 利润', dataIndex: 'profit_a', align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: 'B 利润', dataIndex: 'profit_b', align: 'right', render: (v) => Number(v).toFixed(2) },
                {
                  title: '差额', dataIndex: 'delta', align: 'right',
                  render: (v) => <span style={{ color: v >= 0 ? '#3f8600' : '#cf1322' }}>{Number(v).toFixed(2)}</span>,
                },
              ]}
            />
          </>
        )}
      </Modal>

      {/* 快照明细 → 原始交易行溯源 */}
      <Modal
        open={!!traceItem} onCancel={() => setTraceItem(null)} footer={null} width={1200}
        title={traceItem ? `溯源：${traceItem.item.asin} / ${traceItem.item.sku} → ${traceItem.fact_count} 条原始交易` : ''}
      >
        {traceItem && (
          <>
            <Descriptions size="small" column={4} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="快照版本">v{traceItem.snapshot.version}</Descriptions.Item>
              <Descriptions.Item label="站点/月份">{traceItem.snapshot.country} / {traceItem.snapshot.period_month}</Descriptions.Item>
              <Descriptions.Item label="汇率">{traceItem.snapshot.exchange_rate}</Descriptions.Item>
              <Descriptions.Item label="毛利(元)">{traceItem.item.net_profit_rmb}</Descriptions.Item>
            </Descriptions>
            <Table
              size="small" rowKey="fact_id" dataSource={traceItem.source_facts}
              pagination={{ pageSize: 12 }} scroll={{ y: 380 }}
              columns={[
                { title: 'fact ID', dataIndex: 'fact_id', width: 90 },
                { title: '业务日期', dataIndex: 'biz_date', width: 160, render: (v) => (v || '').replace('T', ' ').slice(0, 19) },
                { title: '类型', dataIndex: 'type', width: 100 },
                { title: '数量', dataIndex: 'quantity', width: 70, align: 'right' },
                { title: '销售额', dataIndex: 'product_sales', width: 100, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: '佣金', dataIndex: 'selling_fee', width: 100, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: 'FBA费', dataIndex: 'fba_fee', width: 100, align: 'right', render: (v) => Number(v).toFixed(2) },
                { title: '原始SKU', dataIndex: 'sku', width: 150, ellipsis: true },
                {
                  title: '原始行ID', dataIndex: 'raw_row_id', width: 110,
                  render: (v) => <Tag color="blue">{v}</Tag>,
                },
              ]}
            />
            <div style={{ marginTop: 8, fontSize: 12, color: '#888' }}>
              提示：原始行ID 可在「数据溯源」页粘贴搜索，查看 Excel 单元格原值。
            </div>
          </>
        )}
      </Modal>
    </div>
  )
}
