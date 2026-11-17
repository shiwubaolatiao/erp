// 数据看板 — 经营总览
//
// 依赖接口（均来自 /api/v2）：
//   getDashboardSummary           核心指标（销售额/销量/订单/广告/费用/利润 + 成本缺失数）
//   getDashboardTrend             近 N 月趋势
//   getProductDistribution        产品销售分布（ASIN 维度）
//   getCostBreakdown              费用构成（饼图）
//   getTopReturns                 退货排名
//   getStoreComparison            店铺对比
//   getCountryComparison          站点对比
//   getTransferSummary            资金流（Transfer 提款，不计入利润）
//
// 注意：
//   · period_month 为 YYYY-MM；利润数字来自核算快照，未核算时显示为空（不是 0）
//   · 成本缺失时前端展示强提醒，因为此时利润口径不完整
import React, { useState, useEffect, useRef, useCallback } from 'react'
import { Row, Col, Select, Card, Spin, Segmented, Tooltip } from 'antd'
import {
  DollarOutlined,
  ShoppingCartOutlined,
  PercentageOutlined,
  AccountBookOutlined,
  SwapOutlined,
} from '@ant-design/icons'
import * as echarts from 'echarts'
import StatCard from '../components/StatCard'
import CardBoundary from '../components/CardBoundary'
import { useAuth } from '../contexts/AuthContext'
import { getDashboardSummary, getDashboardTrend, getProductDistribution, getCostBreakdown, getTopReturns, getStoreComparison, getCountryComparison, getTransferSummary, getStores, getCountries } from '../api'

// 年份选项
const YEAR_OPTIONS = [
  { value: 0, label: '全部' },
  { value: 2025, label: '2025年' },
  { value: 2026, label: '2026年' },
  { value: 2027, label: '2027年' },
  { value: 2028, label: '2028年' },
  { value: 2029, label: '2029年' },
  { value: 2030, label: '2030年' },
]

// 月份选项
const MONTH_OPTIONS = [
  { value: 0, label: '全年' },
  ...Array.from({ length: 12 }, (_, i) => ({
    value: i + 1,
    label: `${i + 1}月`,
  })),
]

/** 千分位格式化数字 */
function formatNumber(num) {
  if (num === null || num === undefined) return '0'
  return Number(num).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

/** 格式化百分比 */
function formatPercent(num) {
  if (num === null || num === undefined) return '0.0'
  return Number(num).toFixed(1)
}

export default function Dashboard() {
  const { hasField, user } = useAuth()
  // 筛选状态
  const [country, setCountry] = useState('')
  const [year, setYear] = useState(2026)
  const [month, setMonth] = useState(0)  // 默认全年
  const [store, setStore] = useState('')
  const [storeOptions, setStoreOptions] = useState([])
  const [countryOptions, setCountryOptions] = useState([{ value: '', label: '全部国家' }])

  // 数据状态
  const [loading, setLoading] = useState(false)
  const [summary, setSummary] = useState(null)
  const [trendData, setTrendData] = useState([])
  const [distributionData, setDistributionData] = useState([])
  const [costData, setCostData] = useState(null)
  const [returnsData, setReturnsData] = useState([])
  const [storeCompareData, setStoreCompareData] = useState([])
  const [countryCompareData, setCountryCompareData] = useState([])
  const [transferData, setTransferData] = useState(null)

  // 各区块独立的接口错误状态（组 9.1：单接口失败仅对应区块显示错误占位 + 可单独重试）
  const [summaryError, setSummaryError] = useState(null)
  const [trendError, setTrendError] = useState(null)
  const [distributionError, setDistributionError] = useState(null)
  const [costError, setCostError] = useState(null)
  const [returnsError, setReturnsError] = useState(null)
  const [storeCompareError, setStoreCompareError] = useState(null)
  const [countryCompareError, setCountryCompareError] = useState(null)
  const [transferError, setTransferError] = useState(null)
  const [trendMode, setTrendMode] = useState('monthly')
  const [costMode, setCostMode] = useState('waterfall')

  // ECharts 容器
  const chartRef = useRef(null)
  const chartInstanceRef = useRef(null)
  const pieChartRef = useRef(null)
  const pieChartInstanceRef = useRef(null)
  const barChartRef = useRef(null)
  const barChartInstanceRef = useRef(null)
  const waterfallRef = useRef(null)
  const waterfallInstanceRef = useRef(null)
  const costPieRef = useRef(null)
  const costPieInstanceRef = useRef(null)
  const returnsChartRef = useRef(null)
  const returnsChartInstanceRef = useRef(null)
  const returnsPieRef = useRef(null)
  const returnsPieInstanceRef = useRef(null)
  const storeChartRef = useRef(null)
  const storeChartInstanceRef = useRef(null)
  const countryChartRef = useRef(null)
  const countryChartInstanceRef = useRef(null)

  // ★ 切换性能：查询结果内存缓存（5 分钟 TTL，容量 60）
  //   切回看过的 筛选组合（店铺/国家/年/月）时秒出，不重复打后端。
  const fetchCache = useRef(new Map())
  const useCached = useCallback((key, fetcher) => {
    const hit = fetchCache.current.get(key)
    if (hit && Date.now() - hit.t < 5 * 60 * 1000) return Promise.resolve(hit.v)
    return fetcher().then(v => {
      fetchCache.current.set(key, { v, t: Date.now() })
      if (fetchCache.current.size > 60) {
        fetchCache.current.delete(fetchCache.current.keys().next().value)
      }
      return v
    })
  }, [])

  // 获取店铺列表
  useEffect(() => {
    fetchStores(false)
    getCountries().then(res => {
      const opts = [{ value: '', label: '全部国家' }, ...(res.data || []).map(c => ({ value: c.code, label: c.name }))]
      setCountryOptions(opts)
    }).catch(() => {})
  }, [])

  const fetchStores = (autoSelect = true) => {
    getStores().then(res => {
      const opts = [{ value: '', label: '全部店铺' }, ...(res.data || []).map(s => ({ value: s.code, label: s.name }))]
      setStoreOptions(opts)
      if (autoSelect && opts.length > 0) setStore(prev => prev || opts[0].value)
    }).catch(() => {})
  }

  // 获取汇总数据
  const fetchSummary = useCallback(async () => {
    try {
      const params = { year }
      if (country) params.country = country
      if (month) params.month = month
      if (store) params.store = store
      const res = await useCached(`s|${store}|${country}|${year}|${month}`, () => getDashboardSummary(params))
      setSummary(res.data)
      setSummaryError(null)
    } catch (err) {
      console.error('获取汇总数据失败', err)
      setSummaryError(err.response?.data?.detail || err.message)
    }
  }, [country, store, year, month, useCached])

  // 获取趋势数据
  const fetchTrend = useCallback(async () => {
    try {
      const params = { dimension: trendMode }
      if (country) params.country = country
      if (store) params.store = store
      const res = await useCached(`t|${store}|${country}|${trendMode}`, () => getDashboardTrend(params))
      setTrendData(res.data?.items || [])
      setTrendError(null)
    } catch (err) {
      console.error('获取趋势数据失败', err)
      setTrendError(err.response?.data?.detail || err.message)
    }
  }, [country, store, trendMode, useCached])

  // 获取产品分布数据
  const fetchDistribution = useCallback(async () => {
    try {
      const params = { year }
      if (country) params.country = country
      if (month) params.month = month
      if (store) params.store = store
      const res = await useCached(`d|${store}|${country}|${year}|${month}`, () => getProductDistribution(params))
      setDistributionData(res.data?.items || [])
      setDistributionError(null)
    } catch (err) {
      console.error('获取产品分布数据失败', err)
      setDistributionError(err.response?.data?.detail || err.message)
    }
  }, [country, store, year, month, useCached])

  // 获取费用拆分数据
  const fetchCostBreakdown = useCallback(async () => {
    try {
      const params = { year }
      if (country) params.country = country
      if (month) params.month = month
      if (store) params.store = store
      const res = await useCached(`c|${store}|${country}|${year}|${month}`, () => getCostBreakdown(params))
      setCostData(res.data)
      setCostError(null)
    } catch (err) {
      console.error('获取费用拆分数据失败', err)
      setCostError(err.response?.data?.detail || err.message)
    }
  }, [country, store, year, month, useCached])

  // 获取退货排名数据
  const fetchReturns = useCallback(async () => {
    try {
      const params = { year, limit: 10 }
      if (country) params.country = country
      if (month) params.month = month
      if (store) params.store = store
      const res = await useCached(`r|${store}|${country}|${year}|${month}`, () => getTopReturns(params))
      setReturnsData(res.data?.items || [])
      setReturnsError(null)
    } catch (err) {
      console.error('获取退货数据失败', err)
      setReturnsError(err.response?.data?.detail || err.message)
    }
  }, [country, store, year, month, useCached])

  // 获取店铺对比数据
  const fetchStoreCompare = useCallback(async () => {
    try {
      const params = {}
      if (country) params.country = country
      if (year) params.year = year
      if (month) params.month = month
      const res = await useCached(`sc|${country}|${year}|${month}`, () => getStoreComparison(params))
      setStoreCompareData(res.data?.items || [])
      setStoreCompareError(null)
    } catch (err) {
      console.error('获取店铺对比数据失败', err)
      setStoreCompareError(err.response?.data?.detail || err.message)
    }
  }, [country, year, month, useCached])

  // 获取国家对比数据
  const fetchCountryCompare = useCallback(async () => {
    try {
      const params = {}
      if (store) params.store = store
      if (year) params.year = year
      if (month) params.month = month
      const res = await useCached(`cc|${store}|${year}|${month}`, () => getCountryComparison(params))
      setCountryCompareData(res.data?.items || [])
      setCountryCompareError(null)
    } catch (err) {
      console.error('获取国家对比数据失败', err)
      setCountryCompareError(err.response?.data?.detail || err.message)
    }
  }, [store, year, month, useCached])

  // 获取其他交易汇总（Transfer、FBA Inventory Fee、Service Fee等）
  const fetchTransfer = useCallback(async () => {
    try {
      const params = {}
      if (country) params.country = country
      if (store) params.store = store
      if (year) params.year = year
      if (month) params.month = month
      const res = await useCached(`tf|${store}|${country}|${year}|${month}`, () => getTransferSummary(params))
      setTransferData(res.data)
      setTransferError(null)
    } catch (err) {
      console.error('获取交易汇总失败', err)
      setTransferError(err.response?.data?.detail || err.message)
    }
  }, [country, store, year, month, useCached])

  // 加载数据 —— trend 不依赖 month，单独拉取，切换月份时不再重拉趋势图；
  // 其余接口并发拉取；快速连续切换筛选时防抖 250ms 只发最后一组请求。
  useEffect(() => {
    const timer = setTimeout(() => {
      fetchTrend()
    }, 0)
    return () => clearTimeout(timer)
  }, [fetchTrend])

  useEffect(() => {
    const timer = setTimeout(() => {
      setLoading(true)
      Promise.all([fetchSummary(), fetchDistribution(), fetchCostBreakdown(), fetchReturns(), fetchStoreCompare(), fetchCountryCompare(), fetchTransfer()])
        .finally(() => setLoading(false))
    }, 250)
    return () => clearTimeout(timer)
  }, [fetchSummary, fetchDistribution, fetchCostBreakdown, fetchReturns, fetchStoreCompare, fetchCountryCompare, fetchTransfer])

  // 解析 summary 数据（规范字段；change_percent 亦为规范键名）
  const summaryData = summary || {}
  const changeData = summary?.change_percent || {}

  const coreCards = [
    hasField('profit') && { title: "净利润 (RMB)", value: summaryData.net_profit_rmb ?? 0, prefix: "¥", change: changeData.net_profit_rmb, icon: <AccountBookOutlined />, color: "#52c41a" },
    hasField('sales') && { title: "销售额 (RMB)", value: summaryData.sales_rmb ?? 0, prefix: "¥", change: changeData.sales_rmb, icon: <DollarOutlined />, color: "#1677ff" },
    hasField('sales') && { title: "订单数", value: summaryData.order_count ?? 0, change: changeData.order_count, icon: <ShoppingCartOutlined />, color: "#722ed1" },
    hasField('profit') && { title: "净利率", value: summaryData.net_profit_rate ? (summaryData.net_profit_rate * 100).toFixed(1) : 0, suffix: "%", change: changeData.net_profit_rate ? (changeData.net_profit_rate * 100).toFixed(1) : null, icon: <PercentageOutlined />, color: "#fa8c16" },
  ].filter(Boolean)
  const coreLg = coreCards.length > 0 ? Math.floor(24 / coreCards.length) : 6

  const extraCards = [
    hasField('ad_pct') && { title: "广告花费", value: summaryData.ad_spend_rmb ?? 0, prefix: "¥", icon: <DollarOutlined />, color: "#f5222d", small: true },
    hasField('ad_pct') && { title: "广告费占比", value: summaryData.sales_rmb ? (summaryData.ad_spend_rmb / summaryData.sales_rmb * 100).toFixed(1) : 0, suffix: "%", icon: <PercentageOutlined />, color: "#eb2f96", small: true },
    hasField('return_rate') && { title: "退货数量", value: summaryData.refund_qty ?? 0, suffix: "件", change: changeData.refund_qty, icon: <ShoppingCartOutlined />, color: "#722ed1", changeLabel: "环比", small: true },
    hasField('storage_pct') && { title: "仓储费占比", value: summaryData.sales_rmb ? (summaryData.storage_fee_rmb / summaryData.sales_rmb * 100).toFixed(1) : 0, suffix: "%", icon: <PercentageOutlined />, color: "#13c2c2", small: true },
  ].filter(Boolean)
  const extraLg = extraCards.length > 0 ? Math.floor(24 / extraCards.length) : 6

  // 辅助：确保图表容器有尺寸后 resize
  const resizeChart = (chart) => {
    if (chart) {
      chart.resize()
      requestAnimationFrame(() => chart.resize())
    }
  }

  // ============ 折线图（净利润 + 销售额 + 订单量） ============
  useEffect(() => {
    if (!chartRef.current) return
    if (!chartInstanceRef.current) {
      chartInstanceRef.current = echarts.init(chartRef.current)
    }
    const chart = chartInstanceRef.current

    const labels = trendData.map((item) => item.period)
    const profitData = trendData.map((item) => item.net_profit_rmb ?? 0)
    const salesData = trendData.map((item) => item.sales_rmb ?? 0)
    const ordersData = trendData.map((item) => item.order_count ?? 0)
    const adSpendData = trendData.map((item) => item.ad_spend_rmb ?? 0)
    const storageData = trendData.map((item) => item.storage_fee_rmb ?? 0)

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: function (params) {
          let html = `<strong>${params[0].axisValueLabel}</strong><br/>`
          params.forEach((p) => {
            const unit = p.seriesName === '订单量' ? '单' : '¥'
            html += `${p.marker} ${p.seriesName}：${unit}${formatNumber(p.value)}<br/>`
          })
          return html
        },
      },
      legend: {
        data: [hasField('profit') && '净利润', '销售额', '订单量', hasField('ad_pct') && '广告费', hasField('storage_pct') && '仓储费'].filter(Boolean),
        top: 4,
        textStyle: { fontSize: 13 },
      },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 56, containLabel: true },
      xAxis: {
        type: 'category',
        data: labels,
        axisLabel: { rotate: labels.length > 12 ? 30 : 0 },
      },
      yAxis: [
        {
          type: 'value',
          name: '金额 (RMB)',
          position: 'left',
          axisLabel: {
            formatter: (val) => {
              if (Math.abs(val) >= 10000) return (val / 10000).toFixed(1) + '万'
              return formatNumber(val)
            },
          },
        },
        {
          type: 'value',
          name: '订单量',
          position: 'right',
          splitLine: { show: false },
          axisLabel: { formatter: (val) => val.toLocaleString() },
        },
      ],
      series: [
        hasField('profit') && {
          name: '净利润',
          type: 'line',
          data: profitData,
          smooth: 0.4,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { width: 2.5 },
          itemStyle: { color: '#52c41a' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(82,196,26,0.3)' },
              { offset: 1, color: 'rgba(82,196,26,0.02)' },
            ]),
          },
          animationDuration: 1500,
          animationEasing: 'cubicOut',
        },
        {
          name: '销售额',
          type: 'line',
          data: salesData,
          smooth: 0.4,
          symbol: 'circle',
          symbolSize: 6,
          lineStyle: { width: 2.5 },
          itemStyle: { color: '#1677ff' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(22,119,255,0.3)' },
              { offset: 1, color: 'rgba(22,119,255,0.02)' },
            ]),
          },
          animationDuration: 1800,
          animationEasing: 'cubicOut',
        },
        {
          name: '订单量',
          type: 'line',
          yAxisIndex: 1,
          data: ordersData,
          smooth: 0.4,
          symbol: 'diamond',
          symbolSize: 7,
          lineStyle: { width: 2, type: 'dashed' },
          itemStyle: { color: '#faad14' },
          animationDuration: 2000,
          animationEasing: 'cubicOut',
        },
        hasField('ad_pct') && {
          name: '广告费',
          type: 'line',
          data: adSpendData,
          smooth: 0.4,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 2 },
          itemStyle: { color: '#f5222d' },
          animationDuration: 2200,
          animationEasing: 'cubicOut',
        },
        hasField('storage_pct') && {
          name: '仓储费',
          type: 'line',
          data: storageData,
          smooth: 0.4,
          symbol: 'circle',
          symbolSize: 5,
          lineStyle: { width: 2 },
          itemStyle: { color: '#722ed1' },
          animationDuration: 2400,
          animationEasing: 'cubicOut',
        },
      ].filter(Boolean),
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [trendData])

  // ============ 瀑布图（费用结构） ============
  useEffect(() => {
    if (!waterfallRef.current || !costData) return
    if (!waterfallInstanceRef.current) {
      waterfallInstanceRef.current = echarts.init(waterfallRef.current)
    }
    const chart = waterfallInstanceRef.current

    const { total_sales_rmb: sales, items: expenses, net_profit_rmb: net_profit, adjustment, diff } = costData

    // 构建瀑布图数据：销售额 → 各项费用 → 调整 → 净利润
    const categories = ['销售额']
    const baseData = [0]
    const expenseData = [sales]

    let running = sales
    for (const e of expenses) {
      categories.push(e.name)
      baseData.push(running - e.value)
      expenseData.push(e.value)
      running -= e.value
    }

    // 调整项（如果有）
    if (adjustment && Math.abs(adjustment) > 1) {
      categories.push('调整')
      if (adjustment > 0) {
        baseData.push(running)
        expenseData.push(adjustment)
      } else {
        baseData.push(running + adjustment)
        expenseData.push(-adjustment)
      }
      running += adjustment
    }

    // 差额（四舍五入误差等，如果有显著值则显示）
    if (diff && Math.abs(diff) > 100) {
      categories.push('差额')
      if (diff > 0) {
        baseData.push(running)
        expenseData.push(diff)
      } else {
        baseData.push(running + diff)
        expenseData.push(-diff)
      }
      running += diff
    }

    // 净利润（直接用数据库值，保证与 KPI 卡片一致）
    if (hasField('profit')) categories.push('净利润')
    baseData.push(0)
    expenseData.push(Math.max(net_profit, 0))

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function (params) {
          const real = params.find(p => p.seriesName === '费用')
          if (!real) return ''
          return `<strong>${real.axisValueLabel}</strong><br/>¥${formatNumber(real.value)}`
        },
      },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 40, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: 30, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        name: '金额 (RMB)',
        axisLabel: {
          formatter: val => {
            if (Math.abs(val) >= 10000) return (val / 10000).toFixed(1) + '万'
            return val.toLocaleString()
          },
        },
      },
      series: [
        {
          name: '底座',
          type: 'bar',
          stack: 'waterfall',
          itemStyle: { borderColor: 'transparent', color: 'transparent' },
          emphasis: { itemStyle: { borderColor: 'transparent', color: 'transparent' } },
          data: baseData,
        },
        {
          name: '费用',
          type: 'bar',
          stack: 'waterfall',
          barMaxWidth: 50,
          data: expenseData.map((val, i) => ({
            value: val,
            itemStyle: {
              color: i === 0
                // 销售额：蓝色
                ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    { offset: 0, color: '#1677ff' },
                    { offset: 1, color: '#4096ff' },
                  ])
                : i === categories.length - 1
                  // 净利润：绿色/红色
                  ? (net_profit >= 0
                      ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                          { offset: 0, color: '#52c41a' },
                          { offset: 1, color: '#95de64' },
                        ])
                      : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                          { offset: 0, color: '#cf1322' },
                          { offset: 1, color: '#ff4d4f' },
                        ]))
                  // 费用项：橙色
                  : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                      { offset: 0, color: '#ff7a45' },
                      { offset: 1, color: '#ffa940' },
                    ]),
              borderRadius: [4, 4, 0, 0],
            },
          })),
          label: {
            show: true,
            position: 'top',
            formatter: function (params) {
              if (params.value <= 0) return ''
              if (params.value >= 10000) return (params.value / 10000).toFixed(1) + '万'
              return params.value.toLocaleString()
            },
            fontSize: 11,
            color: '#666',
          },
          animationDelay: (idx) => idx * 150,
          animationDuration: 800,
          animationEasing: 'elasticOut',
        },
      ],
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [costData])

  // ============ 饼图 ============
  useEffect(() => {
    if (!pieChartRef.current || distributionData.length === 0) return
    if (!pieChartInstanceRef.current) {
      pieChartInstanceRef.current = echarts.init(pieChartRef.current)
    }
    const chart = pieChartInstanceRef.current

    const pieLimit = (!store && !country) ? 10 : 15
    let pieData = distributionData.slice(0, pieLimit).map(item => ({
      name: item.name && item.name.length > 20 ? item.name.slice(0, 20) + '...' : (item.name || '未知'),
      value: item.sales_rmb,
    }))
    const rest = distributionData.slice(pieLimit).reduce((s, i) => s + (i.sales_rmb || 0), 0)
    if (rest > 0) pieData.push({ name: '其他', value: rest })

    const option = {
      tooltip: {
        trigger: 'item',
        formatter: '{b}: ¥{c} ({d}%)'
      },
      legend: {
        orient: 'vertical',
        right: '5%',
        top: 'center',
        type: 'scroll',
      },
      series: [{
        name: '销售额',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: {
          borderRadius: 10,
          borderColor: '#fff',
          borderWidth: 2,
        },
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 'bold' },
          scaleSize: 10,
        },
        labelLine: { show: false },
        data: pieData,
        animationType: 'scale',
        animationEasing: 'elasticOut',
        animationDelay: (idx) => idx * 80,
      }],
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [distributionData, store, country])

  // ============ 柱状图 ============
  useEffect(() => {
    if (!barChartRef.current || distributionData.length === 0) return
    if (!barChartInstanceRef.current) {
      barChartInstanceRef.current = echarts.init(barChartRef.current)
    }
    const chart = barChartInstanceRef.current

    const barLimit = (!store && !country) ? 20 : 25
    const chartData = distributionData.slice(0, barLimit)
    const names = chartData.map(item => item.name && item.name.length > 15 ? item.name.slice(0, 15) + '...' : (item.name || '未知'))
    const salesData = chartData.map(item => item.sales_rmb)
    const profitData = chartData.map(item => item.net_profit_rmb ?? 0)

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function(params) {
          let html = `<strong>${params[0].axisValueLabel}</strong><br/>`
          params.forEach(p => {
            html += `${p.marker} ${p.seriesName}：¥${Number(p.value).toLocaleString()}<br/>`
          })
          return html
        },
      },
      legend: { data: hasField('profit') ? ['销售额', '净利润'] : ['销售额'], top: 4 },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 48, containLabel: true },
      xAxis: {
        type: 'category',
        data: names,
        axisLabel: { rotate: 30, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        name: '金额 (RMB)',
        axisLabel: {
          formatter: val => {
            if (Math.abs(val) >= 10000) return (val / 10000).toFixed(1) + '万'
            return val.toLocaleString()
          },
        },
      },
      series: [
        {
          name: '销售额',
          type: 'bar',
          data: salesData,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#1677ff' },
              { offset: 1, color: '#4096ff' },
            ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 40,
          animationDelay: (idx) => idx * 100,
          animationDuration: 600,
          animationEasing: 'cubicOut',
        },
        {
          name: '净利润',
          type: 'bar',
          data: profitData,
          itemStyle: {
            color: params => params.value >= 0
              ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#fa8c16' },
                  { offset: 1, color: '#ffa940' },
                ])
              : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#cf1322' },
                  { offset: 1, color: '#ff4d4f' },
                ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 40,
          animationDelay: (idx) => idx * 100 + 50,
          animationDuration: 600,
          animationEasing: 'cubicOut',
        },
      ],
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [distributionData, store, country])

  // ============ 费用结构饼图（占销售额百分比） ============
  useEffect(() => {
    if (!costPieRef.current || !costData) return
    if (!costPieInstanceRef.current) {
      costPieInstanceRef.current = echarts.init(costPieRef.current)
    }
    const chart = costPieInstanceRef.current

    const sales = costData.total_sales_rmb || 1
    const pieData = [
      ...costData.items
        .filter(e => e.value > 0)
        .map(e => ({
          name: e.name,
          value: Math.round(e.value),
          pct: (e.value / sales * 100).toFixed(1),
        })),
      // 净利润（正数才显示）
      ...(hasField('profit') && costData.net_profit_rmb > 0 ? [{
        name: '净利润',
        value: Math.round(costData.net_profit_rmb),
        pct: (costData.net_profit_rmb / sales * 100).toFixed(1),
        isProfit: true,
      }] : []),
    ]

    // 默认色 + 净利润用绿色
    const defaultColors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc', '#52c41a']
    const profitIdx = pieData.findIndex(d => d.name === '净利润')
    const colors = [...defaultColors]
    if (profitIdx >= 0) colors[profitIdx] = '#52c41a'

    const option = {
      color: colors,
      tooltip: {
        trigger: 'item',
        formatter: function(params) {
          const item = pieData.find(d => d.name === params.name)
          const pct = item ? item.pct : params.percent
          return `${params.name}: ¥${Number(params.value).toLocaleString()} (占销售额 ${pct}%)`
        }
      },
      legend: {
        orient: 'vertical',
        right: '5%',
        top: 'center',
        type: 'scroll',
      },
      series: [{
        name: '费用占比',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: { borderRadius: 10, borderColor: '#fff', borderWidth: 2 },
        label: {
          show: true,
          formatter: function(params) {
            const item = pieData.find(d => d.name === params.name)
            const pct = item ? item.pct : params.percent
            return `${params.name} ${pct}%`
          },
          fontSize: 11,
        },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 'bold' },
          scaleSize: 10,
        },
        labelLine: { show: true },
        data: pieData,
        animationType: 'scale',
        animationEasing: 'elasticOut',
        animationDelay: (idx) => idx * 80,
      }],
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [costData])

  // 费用结构切换时 resize 当前显示的图表
  useEffect(() => {
    setTimeout(() => {
      if (costMode === 'waterfall') resizeChart(waterfallInstanceRef.current)
      else resizeChart(costPieInstanceRef.current)
    }, 50)
  }, [costMode])

  // ============ 退货件数柱状图（左侧） ============
  useEffect(() => {
    if (!returnsChartRef.current || returnsData.length === 0) return
    if (!returnsChartInstanceRef.current) {
      returnsChartInstanceRef.current = echarts.init(returnsChartRef.current)
    }
    const chart = returnsChartInstanceRef.current

    const sorted = [...returnsData].sort((a, b) => b.returned - a.returned)
    const names = sorted.map(item => item.asin && item.asin.length > 20 ? item.asin.slice(0, 20) + '...' : (item.asin || '未知'))
    const qtyData = sorted.map(item => item.returned)

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function(params) {
          const idx = params[0]?.dataIndex
          const item = sorted[idx]
          if (!item) return ''
          return `<strong>${item.asin}</strong><br/>退货件数：${item.returned}件<br/>退货笔数：${item.order_count}`
        }
      },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 40, containLabel: true },
      xAxis: {
        type: 'category',
        data: names,
        axisLabel: { rotate: 30, fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        name: '退货件数',
        axisLabel: { formatter: val => val.toLocaleString() },
      },
      series: [{
        name: '退货件数',
        type: 'bar',
        data: qtyData,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#ff4d4f' },
            { offset: 1, color: '#ff7875' },
          ]),
          borderRadius: [4, 4, 0, 0],
        },
        barMaxWidth: 40,
        label: {
          show: true,
          position: 'top',
          formatter: '{c}',
          fontSize: 11,
          color: '#666',
        },
        animationDelay: (idx) => idx * 100,
        animationDuration: 600,
        animationEasing: 'cubicOut',
      }],
    }
    chart.setOption(option, true)
    resizeChart(chart)
  }, [returnsData])

  // ============ 退货率饼图（右侧） ============
  useEffect(() => {
    if (!returnsPieRef.current || returnsData.length === 0) return
    if (!returnsPieInstanceRef.current) {
      returnsPieInstanceRef.current = echarts.init(returnsPieRef.current)
    }
    const chart = returnsPieInstanceRef.current

    const sorted = [...returnsData].sort((a, b) => (b.return_rate_pct ?? 0) - (a.return_rate_pct ?? 0))
    const pieData = sorted.map(item => ({
      name: item.asin && item.asin.length > 20 ? item.asin.slice(0, 20) + '...' : (item.asin || '未知'),
      value: item.return_rate_pct,
    }))

    const option = {
      tooltip: {
        trigger: 'item',
        formatter: function(params) {
          const idx = pieData.findIndex(d => d.name === params.name)
          const item = sorted[idx]
          return item
            ? `<strong>${item.asin}</strong><br/>退货率：${item.return_rate_pct}%<br/>退货笔数：${item.order_count} / 订单数：${item.order_count}`
            : `${params.name}: ${params.value}%`
        }
      },
      legend: {
        orient: 'vertical',
        right: '5%',
        top: 'center',
        type: 'scroll',
      },
      series: [{
        name: '退货率',
        type: 'pie',
        radius: ['40%', '70%'],
        center: ['40%', '50%'],
        avoidLabelOverlap: false,
        itemStyle: { borderRadius: 10, borderColor: '#fff', borderWidth: 2 },
        label: {
          show: true,
          formatter: '{b}: {d}%',
          fontSize: 11,
        },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 'bold' },
          scaleSize: 10,
        },
        labelLine: { show: true },
        data: pieData,
        animationType: 'scale',
        animationEasing: 'elasticOut',
        animationDelay: (idx) => idx * 80,
      }],
    }
    chart.setOption(option, true)
    resizeChart(chart)
  }, [returnsData])

  // ============ 店铺对比图 ============
  useEffect(() => {
    if (!storeChartRef.current || storeCompareData.length === 0) return
    if (!storeChartInstanceRef.current) {
      storeChartInstanceRef.current = echarts.init(storeChartRef.current)
    }
    const chart = storeChartInstanceRef.current

    const names = storeCompareData.map(item => item.store_name)
    const salesData = storeCompareData.map(item => Math.round(item.sales_rmb))
    const profitData = storeCompareData.map(item => Math.round(item.net_profit_rmb))

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function(params) {
          let html = `<strong>${params[0].axisValueLabel}</strong><br/>`
          params.forEach(p => {
            html += `${p.marker} ${p.seriesName}：¥${Number(p.value).toLocaleString()}<br/>`
          })
          const idx = params[0]?.dataIndex
          const item = storeCompareData[idx]
          if (item && hasField('profit')) html += `净利率：${item.profit_rate}%`
          return html
        }
      },
      legend: { data: hasField('profit') ? ['销售额', '净利润'] : ['销售额'], top: 4 },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 48, containLabel: true },
      xAxis: { type: 'category', data: names },
      yAxis: {
        type: 'value',
        name: '金额 (RMB)',
        axisLabel: { formatter: val => val >= 10000 ? (val / 10000).toFixed(1) + '万' : val.toLocaleString() },
      },
      series: [
        {
          name: '销售额',
          type: 'bar',
          data: salesData,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#1677ff' },
              { offset: 1, color: '#4096ff' },
            ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 50,
        },
        hasField('profit') && {
          name: '净利润',
          type: 'bar',
          data: profitData,
          itemStyle: {
            color: params => params.value >= 0
              ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#52c41a' },
                  { offset: 1, color: '#95de64' },
                ])
              : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#cf1322' },
                  { offset: 1, color: '#ff4d4f' },
                ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 50,
        },
      ].filter(Boolean),
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [storeCompareData])

  // ============ 国家对比图 ============
  useEffect(() => {
    if (!countryChartRef.current || countryCompareData.length === 0) return
    if (!countryChartInstanceRef.current) {
      countryChartInstanceRef.current = echarts.init(countryChartRef.current)
    }
    const chart = countryChartInstanceRef.current

    const names = countryCompareData.map(item => item.country)
    const salesData = countryCompareData.map(item => Math.round(item.sales_rmb))
    const profitData = countryCompareData.map(item => Math.round(item.net_profit_rmb ?? 0))

    const option = {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: function(params) {
          let html = `<strong>${params[0].axisValueLabel}</strong><br/>`
          params.forEach(p => {
            html += `${p.marker} ${p.seriesName}：¥${Number(p.value).toLocaleString()}<br/>`
          })
          const idx = params[0]?.dataIndex
          const item = countryCompareData[idx]
          if (item && hasField('profit') && item.net_profit_rate !== null) {
            html += `净利率：${(item.net_profit_rate * 100).toFixed(1)}%`
          }
          return html
        }
      },
      legend: { data: hasField('profit') ? ['销售额', '净利润'] : ['销售额'], top: 4 },
      grid: { left: '3%', right: '4%', bottom: '3%', top: 48, containLabel: true },
      xAxis: { type: 'category', data: names, axisLabel: { rotate: 30, fontSize: 11 } },
      yAxis: {
        type: 'value',
        name: '金额 (RMB)',
        axisLabel: { formatter: val => val >= 10000 ? (val / 10000).toFixed(1) + '万' : val.toLocaleString() },
      },
      series: [
        {
          name: '销售额',
          type: 'bar',
          data: salesData,
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#1677ff' },
              { offset: 1, color: '#4096ff' },
            ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 40,
          animationDelay: (idx) => idx * 100,
          animationDuration: 600,
          animationEasing: 'cubicOut',
        },
        hasField('profit') && {
          name: '净利润',
          type: 'bar',
          data: profitData,
          itemStyle: {
            color: params => params.value >= 0
              ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#52c41a' },
                  { offset: 1, color: '#95de64' },
                ])
              : new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                  { offset: 0, color: '#cf1322' },
                  { offset: 1, color: '#ff4d4f' },
                ]),
            borderRadius: [4, 4, 0, 0],
          },
          barMaxWidth: 40,
          animationDelay: (idx) => idx * 100 + 50,
          animationDuration: 600,
          animationEasing: 'cubicOut',
        },
      ].filter(Boolean),
    }

    chart.setOption(option, true)
    resizeChart(chart)
  }, [countryCompareData])

  // 监听窗口 resize + cleanup
  useEffect(() => {
    const handleResize = () => {
      chartInstanceRef.current?.resize()
      pieChartInstanceRef.current?.resize()
      barChartInstanceRef.current?.resize()
      waterfallInstanceRef.current?.resize()
      costPieInstanceRef.current?.resize()
      returnsChartInstanceRef.current?.resize()
      returnsPieInstanceRef.current?.resize()
      storeChartInstanceRef.current?.resize()
      countryChartInstanceRef.current?.resize()
    }
    window.addEventListener('resize', handleResize)
    return () => {
      window.removeEventListener('resize', handleResize)
      chartInstanceRef.current?.dispose()
      chartInstanceRef.current = null
      pieChartInstanceRef.current?.dispose()
      pieChartInstanceRef.current = null
      barChartInstanceRef.current?.dispose()
      barChartInstanceRef.current = null
      waterfallInstanceRef.current?.dispose()
      waterfallInstanceRef.current = null
      costPieInstanceRef.current?.dispose()
      costPieInstanceRef.current = null
      returnsChartInstanceRef.current?.dispose()
      returnsChartInstanceRef.current = null
      returnsPieInstanceRef.current?.dispose()
      returnsPieInstanceRef.current = null
      storeChartInstanceRef.current?.dispose()
      storeChartInstanceRef.current = null
      countryChartInstanceRef.current?.dispose()
      countryChartInstanceRef.current = null
    }
  }, [])

  return (
    <Spin spinning={loading}>
      {/* 筛选栏 */}
      <div style={{ marginBottom: 24, display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <span style={{ fontWeight: 600, fontSize: 13, color: '#666' }}>店铺</span>
        <Select style={{ width: 170 }} value={store} onChange={setStore} options={storeOptions}
          onDropdownVisibleChange={(open) => { if (open) fetchStores() }} />
        <span style={{ fontWeight: 600, fontSize: 13, color: '#666' }}>国家</span>
        <Select style={{ width: 130 }} value={country} onChange={setCountry} options={countryOptions} />
        <span style={{ fontWeight: 600, fontSize: 13, color: '#666' }}>时间</span>
        <Select style={{ width: 110 }} value={year} onChange={setYear} options={YEAR_OPTIONS} />
        <Select style={{ width: 90 }} value={month} onChange={setMonth} options={MONTH_OPTIONS} />
      </div>

      {/* 结算转账（Transfer 各语言变体）— 换算RMB */}
      {user?.is_admin && (
        <CardBoundary title="结算转账" error={transferError} onRetry={fetchTransfer}
          retryKey={`tf|${store}|${country}|${year}|${month}`}>
          {transferData && transferData.total_rmb !== 0 && (
            <Card
              size="small"
              style={{ marginBottom: 16, background: 'linear-gradient(135deg, #fff7e6 0%, #fff1d4 100%)', border: '1px solid #ffe4b5' }}
              title={
                <span style={{ fontSize: 14 }}>
                  <SwapOutlined style={{ marginRight: 8, color: '#fa8c16' }} />
                  结算转账（Amazon打款至银行卡）
                </span>
              }
            >
              <div style={{ textAlign: 'center', padding: '12px 0 4px' }}>
                <div style={{ color: '#8c8c8c', fontSize: 14, marginBottom: 8 }}>合计到卡收入 (RMB)</div>
                <div style={{ fontSize: 48, fontWeight: 700, color: '#3f8600', letterSpacing: 2 }}>
                  ¥{formatNumber(Math.abs(transferData.total_rmb))}
                </div>
              </div>
            </Card>
          )}
        </CardBoundary>
      )}

      {/* 统计卡片 - 第一行：核心指标 + 第二行：广告/退货指标（同一 summary 接口） */}
      <CardBoundary title="核心指标" error={summaryError} onRetry={fetchSummary}
        retryKey={`s|${store}|${country}|${year}|${month}`}>
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          {coreCards.map((c, i) => <Col key={i} xs={24} sm={12} lg={coreLg}><StatCard {...c} /></Col>)}
        </Row>

        <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
          {extraCards.map((c, i) => <Col key={i} xs={24} sm={12} lg={extraLg}><StatCard {...c} /></Col>)}
        </Row>
      </CardBoundary>

      {/* 趋势图 */}
      <CardBoundary title="月度趋势" error={trendError} onRetry={fetchTrend}
        retryKey={`t|${store}|${country}|${trendMode}`}>
        <Card
          title="月度趋势"
          style={{ marginBottom: 16 }}
          extra={
            <Segmented
              options={[
                { label: '月度', value: 'monthly' },
                { label: '年度', value: 'yearly' },
              ]}
              value={trendMode}
              onChange={setTrendMode}
            />
          }
        >
          <div ref={chartRef} style={{ width: '100%', height: 400 }} />
        </Card>
      </CardBoundary>

      {/* 费用结构（瀑布图/占比切换） */}
      <CardBoundary title="费用结构" error={costError} onRetry={fetchCostBreakdown}
        retryKey={`c|${store}|${country}|${year}|${month}`}>
        <Card title="费用结构" style={{ marginBottom: 16 }}
          extra={
            <Segmented
              options={[
                { label: '瀑布图', value: 'waterfall' },
                { label: '占比', value: 'pie' },
              ]}
              value={costMode}
              onChange={setCostMode}
            />
          }
        >
          <div style={{ display: costMode === 'waterfall' ? 'block' : 'none' }}>
            <div ref={waterfallRef} style={{ width: '100%', height: 400 }} />
          </div>
          <div style={{ display: costMode === 'pie' ? 'block' : 'none' }}>
            <div ref={costPieRef} style={{ width: '100%', height: 400 }} />
          </div>
        </Card>
      </CardBoundary>

      {/* 退货排名：件数 + 退货率 */}
      <CardBoundary title="退货排名" error={returnsError} onRetry={fetchReturns}
        retryKey={`r|${store}|${country}|${year}|${month}`}>
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col xs={24} lg={12}>
            <Card title="退货排名 - 件数 (Top 10)" style={{ height: '100%' }}>
              {returnsData.length > 0 ? (
                <div ref={returnsChartRef} style={{ width: '100%', height: 400 }} />
              ) : (
                <div style={{ height: 400, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#999' }}>
                  暂无退货数据
                </div>
              )}
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title="退货排名 - 退货率 (Top 10)" style={{ height: '100%' }}>
              {returnsData.length > 0 ? (
                <div ref={returnsPieRef} style={{ width: '100%', height: 400 }} />
              ) : (
                <div style={{ height: 400, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#999' }}>
                  暂无退货数据
                </div>
              )}
            </Card>
          </Col>
        </Row>
      </CardBoundary>

      {/* 产品柱状图 */}
      <CardBoundary title="产品销售对比 (Top 20)" error={distributionError} onRetry={fetchDistribution}
        retryKey={`d|${store}|${country}|${year}|${month}`}>
        <Card title="产品销售对比 (Top 20)" style={{ marginBottom: 16 }}>
          <div ref={barChartRef} style={{ width: '100%', height: 400 }} />
        </Card>
      </CardBoundary>

      {/* 店铺对比 + 国家对比 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}>
          <CardBoundary title="店铺对比" error={storeCompareError} onRetry={fetchStoreCompare}
            retryKey={`sc|${country}|${year}|${month}`}>
            <Card title="店铺对比" style={{ height: '100%' }}>
              {storeCompareData.length > 0 ? (
                <div ref={storeChartRef} style={{ width: '100%', height: 400 }} />
              ) : (
                <div style={{ height: 400, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#999' }}>
                  暂无店铺数据
                </div>
              )}
            </Card>
          </CardBoundary>
        </Col>
        <Col xs={24} lg={12}>
          <CardBoundary title="国家对比" error={countryCompareError} onRetry={fetchCountryCompare}
            retryKey={`cc|${store}|${year}|${month}`}>
            <Card title="国家对比" style={{ height: '100%' }}>
              {countryCompareData.length > 0 ? (
                <div ref={countryChartRef} style={{ width: '100%', height: 400 }} />
              ) : (
                <div style={{ height: 400, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#999' }}>
                  暂无国家数据
                </div>
              )}
            </Card>
          </CardBoundary>
        </Col>
      </Row>

      {/* 数据更新时间 */}
      <div style={{ textAlign: 'right', color: '#999', fontSize: 12, padding: '8px 0' }}>
        数据更新时间：{new Date().toLocaleString('zh-CN')}
      </div>
    </Spin>
  )
}
