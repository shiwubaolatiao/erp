// 前端 API 层 — 与后端 /api/v2 的唯一契约
//
// 约定：
//   1. 所有请求 baseURL = /api/v2（旧系统 /api/* 由并行运行的旧后端提供）
//   2. token 由请求拦截器自动附加；401 自动跳登录
//   3. 函数名尽量沿用旧版，减少页面改动；新功能在文件后半部分
//   4. 改动后端接口时，先跑 `node check_api_exports.mjs` 校验页面引用是否齐全
//
// 分组：看板 / 广告 / 导入 / 店铺站点 / 汇率 / 核算快照 / 查询 / 溯源 /
//      映射审核 / 系统设置 / 导出 / 认证 / 用户 / AI / 周报 / 广告 Agent
import axios from 'axios'

// 新后端统一前缀 /api/v2（旧系统 /api/* 由并行运行的旧后端继续提供）
const api = axios.create({
  baseURL: '/api/v2',
  timeout: 600000,
})

// 请求拦截：自动带上 token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：401 自动跳转登录
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

// ==================== 看板 ====================
export const getDashboardSummary = (params) => api.get('/dashboard/summary', { params })
export const getDashboardTrend = (params) => api.get('/dashboard/trend', { params })
export const getProductDistribution = (params) => api.get('/dashboard/product-distribution', { params })
export const getCostBreakdown = (params) => api.get('/dashboard/fee-composition', { params })
export const getTopReturns = (params) => api.get('/dashboard/refund-ranking', { params })
export const getStoreComparison = (params) => api.get('/dashboard/store-compare', { params })
export const getCountryComparison = (params) => api.get('/dashboard/country-compare', { params })
export const getTransferSummary = (params) => api.get('/dashboard/transfer', { params })
// 兼容旧名（旧页面调用）
export const getStoreComparisonLegacy = (params) => api.get('/dashboard/store-compare', { params })

// ==================== 广告 ====================
export const getAdvertisingSummary = (params) => api.get('/query/ad', { params })
export const getAdvertisingDetail = (params) => api.get('/query/ad', { params })

// ==================== 导入（统一入口 + 异步任务） ====================
export const getImportSupported = () => api.get('/import/supported')

// 统一上传：多文件 + 归属月份 + 店铺（后台异步解析，立即返回 task_id）
export const uploadFiles = (files, store, periodMonth) => {
  const formData = new FormData()
  ;(files || []).forEach(f => formData.append('files', f))
  if (store) formData.append('store', store)
  if (periodMonth) formData.append('period_month', periodMonth)
  return api.post('/import/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 600000,
  })
}

// 轮询导入任务直到终态；onProgress(task) 每次回调（done_files/total_files/current 进度展示用）
// task 形态：{status, progress, stage, total_files, done_files, current,
//            batches: [{batch_id, file_name, status, success, error}]}
const IMPORT_POLL_MS = 1500
const IMPORT_TIMEOUT_MS = 30 * 60 * 1000      // 单次导入最长等待 30 分钟

export async function waitImportTask(taskId, onProgress) {
  const started = Date.now()
  for (;;) {
    const res = await api.get(`/import/tasks/${taskId}`)
    const t = res.data
    if (onProgress) onProgress(t)
    if (['success', 'failed', 'interrupted'].includes(t.status)) return t
    if (Date.now() - started > IMPORT_TIMEOUT_MS) {
      throw new Error('导入超时（超过 30 分钟），任务在后台继续，可稍后到「数据导入 → 导入批次」查看结果')
    }
    await new Promise(r => setTimeout(r, IMPORT_POLL_MS))
  }
}

// 把批次报告里的异常行转成页面可读的字符串
export function importErrorLines(report) {
  return (report?.errors || []).map(
    e => `${e.sheet_name || ''}${e.row_no ? ' 第' + e.row_no + '行' : ''}: ${e.error_message}`)
}

export const getImportTasks = () => api.get('/import/tasks')
export const getImportTaskStatus = (taskId) => api.get(`/import/tasks/${taskId}`)
export const getImportBatches = () => api.get('/import/batches')
export const getBatchReport = (batchId) => api.get(`/import/batches/${batchId}/report`)
export const lockBatch = (batchId) => api.post(`/import/batches/${batchId}/lock`)
export const unlockBatch = (batchId) => api.post(`/import/batches/${batchId}/unlock`)

// ==================== 店铺 / 站点 ====================
export const getStores = () => api.get('/stores')
export const createStore = (code, name) => api.post('/stores', { store_code: code, store_name: name })
export const updateStore = (id, data) => api.put(`/stores/${id}`, data)
export const deleteStore = (id) => api.delete(`/stores/${id}`)
export const getCountries = () => api.get('/stores/sites')
export const getStoreCountries = (storeName) => api.get('/stores/sites')
// 站点 CRUD（旧页面 Admin 使用）
export const createCountry = (code, name, currency = '', region = '') =>
  api.post('/admin/countries', { site_code: code, country_name: name, default_currency: currency, region })
export const updateCountry = (code, name) => api.put(`/admin/countries/${code}`, { country_name: name })
export const deleteCountry = (code) => api.delete(`/admin/countries/${code}`)

// ==================== 汇率 ====================
export const getExchangeRates = (params) => api.get('/admin/exchange-rates', { params })
// 兼容旧签名：第一个参数可能是国家代码（'UK'）也可能是站点主键 id（旧 Admin 页传的是 id）
export const createExchangeRate = (countryCodeOrId, yearMonth, rate, store) => {
  const payload = { period_month: yearMonth, rate_to_rmb: rate }
  if (store) payload.store = store
  const asStr = String(countryCodeOrId ?? '')
  if (/^\d+$/.test(asStr)) payload.country_id = Number(asStr)
  else payload.country_code = asStr
  return api.post('/admin/exchange-rates', payload)
}
export const upsertExchangeRate = (data) => api.post('/admin/exchange-rates', data)
export const updateExchangeRate = (id, rate) => api.put(`/admin/exchange-rates/${id}`, { rate_to_rmb: rate })
export const deleteExchangeRate = (id) => api.delete(`/admin/exchange-rates/${id}`)

// ==================== 商品与成本（旧页面 Admin 使用） ====================
export const getProducts = (params) => api.get('/query/products', { params })
export const createProduct = (data) => api.post('/admin/products', data)
export const updateProduct = (id, data) => api.put(`/admin/products/${id}`, data)
export const deleteProduct = (id) => api.delete(`/admin/products/${id}`)
export const getProductCosts = (productId) => api.get(`/admin/products/${productId}/costs`)
export const createProductCost = (productId, data) => api.post(`/admin/products/${productId}/costs`, data)
export const updateProductCost = (productId, costId, data) =>
  api.put(`/admin/products/${productId}/costs/${costId}`, data)
export const deleteProductCost = (productId, costId) =>
  api.delete(`/admin/products/${productId}/costs/${costId}`)

// ==================== 核算（利润/快照/成本预警） ====================
export const calculateProfit = (data) => api.post('/accounting/calculate', data)
export const getSnapshots = (params) => api.get('/accounting/snapshots', { params })
export const getSnapshotDetail = (id) => api.get(`/accounting/snapshots/${id}`)
export const compareSnapshots = (a, b) => api.get('/accounting/compare', { params: { snapshot_a: a, snapshot_b: b } })
export const getCostAlerts = (params) => api.get('/accounting/cost-alerts', { params })
export const createCostEntry = (data) => api.post('/accounting/cost-entry', data)
export const ignoreCostAlert = (id) => api.post(`/accounting/cost-alerts/${id}/ignore`)

// ---- 核算后台任务（组6：POST /calculate 默认异步，返回 {task_id, poll_url}） ----
export const getAccountingTask = (taskId) => api.get(`/accounting/tasks/${taskId}`)
export const listAccountingTasks = (limit = 30) => api.get('/accounting/tasks', { params: { limit } })

const ACC_POLL_MS = 1000
const ACC_TIMEOUT_MS = 10 * 60 * 1000         // 单次核算最长等待 10 分钟

// 轮询核算任务直到终态；onProgress(task) 每秒回调（progress/stage 展示用）
export async function waitAccountingTask(taskId, onProgress) {
  const started = Date.now()
  for (;;) {
    const res = await getAccountingTask(taskId)
    const t = res.data
    if (onProgress) onProgress(t)
    if (['success', 'failed', 'interrupted'].includes(t.status)) return t
    if (Date.now() - started > ACC_TIMEOUT_MS) {
      throw new Error('核算超时（超过 10 分钟），请到任务列表查看结果')
    }
    await new Promise(r => setTimeout(r, ACC_POLL_MS))
  }
}

// 兼容旧名（Admin 页"重算"按钮）
export const recalculateProfit = (params) => api.post('/accounting/calculate', {
  store: params.store, country: params.country || '',
  period_month: params.year && params.month ? `${params.year}-${String(params.month).padStart(2, '0')}` : params.period_month,
})

// ==================== 查询 ====================
export const getMonthlySummary = (params) => api.get('/query/sales', { params })
export const getCountrySummary = (params) => api.get('/query/sales', { params: { ...params, group_by: 'country' } })
export const getFeeSummary = (params) => api.get('/query/fees', { params })

// ==================== 溯源（核心新能力） ====================
export const traceBatch = (batchId) => api.get(`/trace/batch/${batchId}`)
export const listBatches = () => api.get('/trace/batches')
export const traceRawRow = (rawRowId) => api.get(`/trace/raw-row/${rawRowId}`)
export const traceSnapshotItem = (itemId) => api.get(`/trace/snapshot-item/${itemId}`)
export const traceFact = (factType, factId) => api.get(`/trace/fact/${factType}/${factId}`)
export const searchRaw = (keyword, limit = 50) => api.get('/trace/search', { params: { keyword, limit } })

// ==================== 映射审核（三元组 / 待确认区 / 异常池） ====================
export const getPendingMappings = (params) => api.get('/admin/mappings/pending', { params })
export const decideMapping = (id, action) => api.post(`/admin/mappings/${id}/decide`, { action })
export const getReplacements = () => api.get('/admin/replacements')

// ==================== 映射基准与批量操作（mapping_review_enhance） ====================
export const importMappingBaseline = (data) => api.post('/admin/mapping-review/baseline/import', data)
export const getMappingBaselineSummary = () => api.get('/admin/mapping-review/baseline/summary')
export const bulkConfirmMappings = (data) => api.post('/admin/mapping-review/bulk-confirm', data)
export const rollbackBulkConfirm = (reportId) => api.post('/admin/mapping-review/bulk-confirm/rollback', { report_id: reportId })
export const realignReplacements = (data) => api.post('/admin/mapping-review/replacement/realign', data)
export const assignReplacement = (data) => api.post('/admin/mapping-review/replacement/assign', data)
export const getMappingReports = (params) => api.get('/admin/mapping-review/reports', { params })
export const getMappingReport = (reportId) => api.get(`/admin/mapping-review/reports/${reportId}`)
export const exportMappingReport = (reportId, section) =>
  api.get(`/admin/mapping-review/reports/${reportId}/export`, { params: { section }, responseType: 'blob' })

// ==================== 系统设置（别名/词典） ====================
export const getSheetAliases = () => api.get('/admin/sheet-aliases')
export const upsertSheetAlias = (data) => api.post('/admin/sheet-aliases', data)
export const deleteSheetAlias = (id) => api.delete(`/admin/sheet-aliases/${id}`)
export const getHeaderAliases = () => api.get('/admin/header-aliases')
export const upsertHeaderAlias = (data) => api.post('/admin/header-aliases', data)
export const deleteHeaderAlias = (id) => api.delete(`/admin/header-aliases/${id}`)
export const getOperationLogs = (params) => api.get('/admin/operation-logs', { params })
export const getDataStats = () => api.get('/admin/stats')

// ==================== 导出 ====================
export const exportSnapshot = (id, confirm = true) =>
  api.get(`/export/snapshot/${id}`, { params: { confirm }, responseType: 'blob' })
export const exportSales = (params) =>
  api.get('/export/sales', { params: { ...params, confirm: true }, responseType: 'blob' })
export const exportRawRows = (batchId) =>
  api.get('/export/raw-rows', { params: { batch_id: batchId, confirm: true }, responseType: 'blob' })
export const exportCosts = (params) => api.get('/export/costs', { params, responseType: 'blob' })
export const exportTransfer = (params) =>
  api.get('/export/transfer', { params: { ...params, confirm: true }, responseType: 'blob' })
export const exportMissingSkus = (params) =>
  api.get('/export/missing-skus', { params: { ...params, confirm: true }, responseType: 'blob' })
// 兼容旧名
export const exportMonthlySummary = (params) => exportSales(params)
export const exportCountrySummary = (params) => exportSales(params)

// 通用下载助手
export const downloadBlob = (data, filename) => {
  const url = window.URL.createObjectURL(new Blob([data]))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.URL.revokeObjectURL(url)
}

// ==================== 认证 ====================
export const authLogin = async (username, password) => {
  const formData = new URLSearchParams()
  formData.append('username', username)
  formData.append('password', password)
  const res = await api.post('/auth/login', formData, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
  return res.data
}
export const authMe = async () => {
  const res = await api.get('/auth/me')
  return res.data
}
export const authChangePassword = (data) => api.post('/auth/change-password', data)

// ==================== 用户管理 ====================
export const getUsers = async () => (await api.get('/auth/users')).data
export const createUser = async (data) => (await api.post('/auth/users', data)).data
export const updateUser = async (userId, data) => (await api.put(`/auth/users/${userId}`, data)).data
export const deleteUser = async (userId) => (await api.delete(`/auth/users/${userId}`)).data

// ==================== AI 对话 ====================
export const aiChat = (message, history = []) => api.post('/ai/chat', { message, history })
export const aiTools = () => api.get('/ai/tools')

// ==================== 周报 ====================
export const getWeeklyReports = (params) => api.get('/weekly-reports', { params })
export const createWeeklyReport = (data) => api.post('/weekly-reports', data)
export const getWeeklyReport = (id) => api.get(`/weekly-reports/${id}`)
export const updateWeeklyReport = (id, data) => api.put(`/weekly-reports/${id}`, data)
export const deleteWeeklyReport = (id) => api.delete(`/weekly-reports/${id}`)
export const exportWeeklyReport = (id) => api.get(`/weekly-reports/${id}/export`, { responseType: 'blob' })
export const polishWeeklyReport = (content, instruction) =>
  api.post('/weekly-reports/polish', { content, instruction })
export const submitWeeklyReport = (id) => api.post(`/weekly-reports/${id}/review`, { action: 'submit' })
export const reviewWeeklyReport = (id, data) => api.post(`/weekly-reports/${id}/review`, data)
export const uploadReportImage = (id, file, description) => {
  const formData = new FormData()
  formData.append('file', file)
  if (description) formData.append('description', description)
  return api.post(`/weekly-reports/${id}/images`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
export const deleteReportImage = (imageId) => api.delete(`/weekly-reports/images/${imageId}`)

// ==================== 广告 Agent ====================
export const getAdsSessions = () => api.get('/ads-agent/sessions')
export const createAdsSession = () => api.post('/ads-agent/sessions')
export const getAdsSession = (id) => api.get(`/ads-agent/sessions/${id}`)
export const deleteAdsSession = (id) => api.delete(`/ads-agent/sessions/${id}`)
export const sendAdsMessage = (sessionId, message, model) =>
  api.post(`/ads-agent/sessions/${sessionId}/chat`, { message, model }, { timeout: 360000 })

// 流式对话：返回 fetch Response（text/event-stream），由调用方读取 SSE
export const streamAdsMessage = (sessionId, message, model, signal) => {
  const token = localStorage.getItem('token')
  return fetch(`/api/v2/ads-agent/sessions/${sessionId}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ message, model }),
    signal,
  })
}
export const uploadAdsFile = (sessionId, file) => {
  const formData = new FormData()
  formData.append('file', file)
  return api.post(`/ads-agent/sessions/${sessionId}/upload`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 360000,
  })
}
export const sendAdsWithFiles = (sessionId, message, files, model) => {
  const formData = new FormData()
  if (message) formData.append('message', message)
  ;(files || []).forEach(f => formData.append('files', f))
  if (model) formData.append('model', model)
  return api.post(`/ads-agent/sessions/${sessionId}/send`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 360000,
  })
}
export const getAdsActions = (sessionId) => api.get(`/ads-agent/sessions/${sessionId}/actions`)
export const exportAdsActions = (sessionId) =>
  api.get(`/ads-agent/sessions/${sessionId}/actions/export`, { responseType: 'blob' })

// ==================== 广告 Agent 报告中心 ====================
export const getAdsReports = () => api.get('/ads-agent/reports')
export const downloadAdsReport = async (filename) => {
  const res = await api.get(`/ads-agent/reports/${encodeURIComponent(filename)}`, {
    responseType: 'blob',
    timeout: 120000,
  })
  downloadBlob(res.data, filename)
}

export default api
