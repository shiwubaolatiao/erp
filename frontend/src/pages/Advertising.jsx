// 广告分析 — 广告花费与效果指标
//
// 依赖接口：getAdvertisingSummary / getAdvertisingDetail（均映射到 /query/ad）
//
// 注意：广告报表**没有日期列**，指标按导入批次的归属月份归集；
//      合并多个月的工作簿单月广告费会略有偏差（销售/利润不受影响）。
import React, { useState, useEffect } from 'react'
import { Row, Col, Select, Card, Table, Statistic, Spin, message } from 'antd'
import { AimOutlined, LineChartOutlined } from '@ant-design/icons'
import { getAdvertisingSummary, getAdvertisingDetail, getStores, getCountries } from '../api'

const YEAR_OPTIONS = [
  { value: 0, label: '全部' },
  { value: 2025, label: '2025年' }, { value: 2026, label: '2026年' }, { value: 2027, label: '2027年' },
]
const MONTH_OPTIONS = [
  { value: 0, label: '全部' },
  ...Array.from({ length: 12 }, (_, i) => ({ value: i + 1, label: `${i + 1}月` })),
]

function formatMoney(num, decimals = 2) {
  if (num === null || num === undefined) return '0.00'
  return Number(num).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}
function formatPercent(num) {
  if (num === null || num === undefined) return '0.00'
  return Number(num).toFixed(2)
}
function formatInt(num) {
  if (num === null || num === undefined) return '0'
  return Number(num).toLocaleString('en-US')
}

export default function Advertising() {
  const [country, setCountry] = useState('')
  const [store, setStore] = useState('')
  const [storeOptions, setStoreOptions] = useState([])
  const [countryOptions, setCountryOptions] = useState([])
  const [adYear, setAdYear] = useState(2026)
  const [adMonth, setAdMonth] = useState(5)

  const [loading, setLoading] = useState(false)
  const [summary, setSummary] = useState(null)
  const [detailData, setDetailData] = useState([])
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20, total: 0 })
  const [sorter, setSorter] = useState({})
  const fetchIdRef = React.useRef(0)

  useEffect(() => {
    getStores().then(res => {
      setStoreOptions([{value:'',label:'全部店铺'},...(res.data||[]).map(s=>({value:s.code,label:s.name}))])
    }).catch(()=>{})
    getCountries().then(res => {
      setCountryOptions([{value:'',label:'全部国家'},...(res.data||[]).map(c=>({value:c.code,label:c.name}))])
    }).catch(()=>{})
  }, [])

  const fetchSummary = async () => {
    try {
      const params = {}
      if (country) params.country = country
      if (adYear && adMonth) params.period_month = `${adYear}-${String(adMonth).padStart(2, '0')}`
      else if (adYear) params.year = adYear
      if (store) params.store = store
      const res = await getAdvertisingSummary(params)
      setSummary(res.data)
    } catch (err) {
      message.error('获取广告汇总失败：' + (err.response?.data?.detail || err.message))
    }
  }

  const fetchDetail = async () => {
    const fetchId = ++fetchIdRef.current
    setLoading(true)
    try {
      const params = {}
      if (adYear && adMonth) params.period_month = `${adYear}-${String(adMonth).padStart(2, '0')}`
      else if (adYear) params.year = adYear
      if (country) params.country = country
      if (store) params.store = store
      const res = await getAdvertisingDetail(params)
      if (fetchId !== fetchIdRef.current) return  // 过期请求，忽略响应
      const data = res.data
      const items = data.items || []
      setDetailData(items)
      setPagination(prev => ({
        ...prev, current: 1, total: items.length,
      }))
    } catch (err) {
      if (fetchId !== fetchIdRef.current) return
      message.error('获取广告明细失败：' + (err.response?.data?.detail || err.message))
    } finally {
      if (fetchId === fetchIdRef.current) setLoading(false)
    }
  }

  useEffect(() => {
    setSorter({})
    setPagination(prev => ({ ...prev, current: 1 }))
    fetchSummary()
    fetchDetail()
  }, [store, country, adYear, adMonth])

  // 后端 /query/ad 返回全量行：分页与排序均在前端本地完成
  const handleTableChange = (pag, _, newSorter) => {
    setSorter(newSorter)
    setPagination(prev => ({ ...prev, current: pag.current, pageSize: pag.pageSize }))
  }

  const columns = [
    { title: 'SKU', dataIndex: 'sku', key: 'sku', width: 180, ellipsis: true, sorter: (a, b) => (a.sku || '').localeCompare(b.sku || ''),
      sortOrder: sorter.field === 'sku' ? sorter.order : null },
    { title: 'ASIN', dataIndex: 'asin', key: 'asin', width: 140, sorter: (a, b) => (a.asin || '').localeCompare(b.asin || ''),
      sortOrder: sorter.field === 'asin' ? sorter.order : null },
    { title: '国家', dataIndex: 'country', key: 'country', width: 70 },
    { title: '花费(RMB)', dataIndex: 'spend', key: 'spend', width: 120, align: 'right', sorter: (a, b) => (a.spend || 0) - (b.spend || 0),
      sortOrder: sorter.field === 'spend' ? sorter.order : null, render: v => `¥${formatMoney(v)}` },
    { title: '销售额(RMB)', dataIndex: 'sales', key: 'sales', width: 130, align: 'right', sorter: (a, b) => (a.sales || 0) - (b.sales || 0),
      sortOrder: sorter.field === 'sales' ? sorter.order : null, render: v => `¥${formatMoney(v)}` },
    { title: 'ACOS', dataIndex: 'acos', key: 'acos', width: 90, align: 'right', sorter: (a, b) => (a.acos || 0) - (b.acos || 0),
      sortOrder: sorter.field === 'acos' ? sorter.order : null, render: v => `${formatPercent(v)}%` },
    { title: 'ROAS', dataIndex: 'roas', key: 'roas', width: 90, align: 'right', sorter: (a, b) => (a.roas || 0) - (b.roas || 0),
      sortOrder: sorter.field === 'roas' ? sorter.order : null, render: v => formatPercent(v) },
    { title: 'CTR', dataIndex: 'ctr', key: 'ctr', width: 80, align: 'right', sorter: (a, b) => (a.ctr || 0) - (b.ctr || 0),
      sortOrder: sorter.field === 'ctr' ? sorter.order : null, render: v => `${formatPercent(v)}%` },
    { title: 'CPC', dataIndex: 'cpc', key: 'cpc', width: 90, align: 'right', sorter: (a, b) => (a.cpc || 0) - (b.cpc || 0),
      sortOrder: sorter.field === 'cpc' ? sorter.order : null, render: v => `¥${formatMoney(v)}` },
    { title: '展示', dataIndex: 'impressions', key: 'impressions', width: 100, align: 'right', sorter: (a, b) => (a.impressions || 0) - (b.impressions || 0),
      sortOrder: sorter.field === 'impressions' ? sorter.order : null, render: v => formatInt(v) },
    { title: '点击', dataIndex: 'clicks', key: 'clicks', width: 80, align: 'right', sorter: (a, b) => (a.clicks || 0) - (b.clicks || 0),
      sortOrder: sorter.field === 'clicks' ? sorter.order : null, render: v => formatInt(v) },
    { title: '订单', dataIndex: 'orders', key: 'orders', width: 70, align: 'right', sorter: (a, b) => (a.orders || 0) - (b.orders || 0),
      sortOrder: sorter.field === 'orders' ? sorter.order : null, render: v => formatInt(v) },
    { title: '转化率', key: 'conversion_rate', width: 90, align: 'right', sorter: (a, b) => (a.clicks ? a.orders / a.clicks : 0) - (b.clicks ? b.orders / b.clicks : 0),
      sortOrder: sorter.field === 'conversion_rate' ? sorter.order : null,
      render: (_, r) => `${formatPercent(r.clicks ? (r.orders / r.clicks) * 100 : 0)}%` },
  ]

  return (
    <div>
      <style>{`.row-high-acos>td{background:#fff1f0!important}.row-high-acos:hover>td{background:#ffccc7!important}`}</style>
      <div style={{ marginBottom: 24, display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
        <Select style={{ width: 160 }} value={store} onChange={setStore} options={storeOptions} placeholder="全部店铺" />
        <Select style={{ width: 140 }} value={country} onChange={setCountry} options={countryOptions} placeholder="全部国家" />
        <Select style={{ width: 110 }} value={adYear} onChange={setAdYear} options={YEAR_OPTIONS} />
        <Select style={{ width: 90 }} value={adMonth} onChange={setAdMonth} options={MONTH_OPTIONS} />
      </div>
      <Spin spinning={loading && !summary}>
        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          <Col xs={24} sm={8}><Card hoverable><Statistic title="总花费(RMB)" value={summary?.total_spend ?? 0} precision={2} prefix="¥" valueStyle={{ color: '#cf1322' }} /></Card></Col>
          <Col xs={24} sm={8}><Card hoverable><Statistic title="平均 ACOS" value={(summary?.acos ?? 0) * 100} precision={2} suffix="%" prefix={<AimOutlined />} valueStyle={{ color: '#faad14' }} /></Card></Col>
          <Col xs={24} sm={8}><Card hoverable><Statistic title="平均 ROAS" value={summary?.roas ?? 0} precision={2} prefix={<LineChartOutlined />} valueStyle={{ color: '#52c41a' }} /></Card></Col>
        </Row>
      </Spin>
      <Card title="广告明细">
        <Table
          rowKey={(r) => r.id || (r.asin + '_' + r.country)}
          columns={columns}
          dataSource={detailData}
          loading={loading}
          pagination={{ ...pagination, showSizeChanger: true, showQuickJumper: true, showTotal: t => `共 ${t} 条` }}
          onChange={handleTableChange}
          rowClassName={r => (r.acos > 50 ? 'row-high-acos' : '')}
          scroll={{ x: 1570 }}
          size="middle"
        />
      </Card>
    </div>
  )
}
