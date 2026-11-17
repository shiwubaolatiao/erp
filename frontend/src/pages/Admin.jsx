// 系统管理 — 店铺/站点/汇率/商品与成本/别名词典/用户/审计日志
//
// 依赖接口：
//   店铺站点：getStores / createStore / updateStore / deleteStore / getCountries
//   汇率：    getExchangeRates / createExchangeRate / updateExchangeRate / deleteExchangeRate
//   商品成本：getProducts / createProduct / getProductCosts / createProductCost …
//   词典：    getSheetAliases / getHeaderAliases（改词典即可适配新表头，无需改代码）
//   用户：    getUsers / createUser / updateUser / deleteUser
//   审计：    getOperationLogs
//
// 注意：删除类接口在本系统中均为**软删除/受保护删除**，避免破坏溯源链。
import React, { useState, useEffect } from 'react'
import { Card, Table, Button, Modal, Input, InputNumber, message, Popconfirm, Space, Tabs, Select, Spin, Switch, Tag, Progress } from 'antd'
import { PlusOutlined, DeleteOutlined, EditOutlined, ShopOutlined, GlobalOutlined, DollarOutlined, ReloadOutlined, UserOutlined, ToolOutlined } from '@ant-design/icons'
import {
  getStores, createStore, updateStore, deleteStore,
  getCountries, createCountry, updateCountry, deleteCountry,
  getExchangeRates, createExchangeRate, updateExchangeRate, deleteExchangeRate,
  getStoreCountries, recalculateProfit, getAccountingTask, exportMissingSkus,
  getUsers, createUser, updateUser, deleteUser,
  getProducts, createProduct, updateProduct, deleteProduct,
  getProductCosts, createProductCost, updateProductCost, deleteProductCost,
} from '../api'
import { useAuth } from '../contexts/AuthContext'

export default function SystemAdmin() {
  const [tab, setTab] = useState('stores')
  const { user } = useAuth()

  const tabs = [
    { key: 'stores', label: <><ShopOutlined /> 店铺管理</>, children: <StoreManager /> },
    { key: 'countries', label: <><GlobalOutlined /> 国家管理</>, children: <CountryManager /> },
    { key: 'rates', label: <><DollarOutlined /> 汇率管理</>, children: <RateManager /> },
    { key: 'products', label: <><ToolOutlined /> 产品管理</>, children: <ProductManager /> },
    { key: 'tools', label: <><ReloadOutlined /> 数据工具</>, children: <DataTools /> },
  ]

  if (user?.is_admin) {
    tabs.push({ key: 'users', label: <><UserOutlined /> 用户管理</>, children: <UserManager /> })
  }

  return (
    <Tabs activeKey={tab} onChange={setTab} items={tabs} />
  )
}

function StoreManager() {
  const [stores, setStores] = useState([]); const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false); const [code, setCode] = useState(''); const [name, setName] = useState(''); const [editing, setEditing] = useState(false)
  const fetch = async () => { setLoading(true); setStores((await getStores()).data||[]); setLoading(false) }
  useEffect(() => { fetch() }, [])
  const save = async () => {
    if (editing) {
      await updateStore(code, name, code)
    } else {
      await createStore(code, name)
    }
    message.success(editing?'已更新':'已创建'); setOpen(false); setEditing(false); fetch()
  }
  return (<Card><Space style={{marginBottom:16}}><Button type="primary" icon={<PlusOutlined/>} onClick={()=>{setEditing(false);setCode('');setName('');setOpen(true)}}>创建店铺</Button></Space>
    <Table rowKey="id" size="small" loading={loading} dataSource={stores} pagination={false}
      columns={[{title:'代码',dataIndex:'code',width:200},{title:'名称',dataIndex:'name'},
        {title:'操作',width:140,render:(_,r)=>(<Space size={0}>
          <Button size="small" icon={<EditOutlined/>} onClick={()=>{setEditing(true);setCode(r.code);setName(r.name);setOpen(true)}}/>
          <Popconfirm title="确定删除？" onConfirm={async()=>{await deleteStore(r.code);fetch()}}><Button danger size="small" icon={<DeleteOutlined/>}/></Popconfirm>
        </Space>)}]}/>
    <Modal title={editing?'编辑店铺':'创建店铺'} open={open} onOk={save} onCancel={()=>setOpen(false)}>
      <div style={{marginBottom:8}}>代码 <Input value={code} onChange={e=>setCode(e.target.value)} placeholder="如 LMG-NA" disabled={editing}/></div>
      <div>名称 <Input value={name} onChange={e=>setName(e.target.value)} placeholder="如 LMG北美站"/></div>
    </Modal></Card>)
}

function CountryManager() {
  const [data, setData] = useState([]); const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false); const [code, setCode] = useState(''); const [name, setName] = useState(''); const [editing, setEditing] = useState(false)
  const fetch = async () => { setLoading(true); setData((await getCountries()).data||[]); setLoading(false) }
  useEffect(()=>{fetch()},[])
  const save = async () => {
    if (editing) { await updateCountry(code, name) }
    else { await createCountry(code.toUpperCase(), name) }
    message.success(editing?'已更新':'已创建'); setOpen(false); setEditing(false); fetch()
  }
  return (<Card><Space style={{marginBottom:16}}><Button type="primary" icon={<PlusOutlined/>} onClick={()=>{setEditing(false);setCode('');setName('');setOpen(true)}}>创建国家</Button></Space>
    <Table rowKey="id" size="small" loading={loading} dataSource={data} pagination={false}
      columns={[{title:'代码',dataIndex:'code',width:80},{title:'名称',dataIndex:'name',width:150},
        {title:'操作',width:140,render:(_,r)=>(<Space size={0}>
          <Button size="small" icon={<EditOutlined/>} onClick={()=>{setEditing(true);setCode(r.code);setName(r.name);setOpen(true)}}/>
          <Popconfirm title="确定删除？" onConfirm={async()=>{await deleteCountry(r.code);fetch()}}><Button danger size="small" icon={<DeleteOutlined/>}/></Popconfirm>
        </Space>)}]}/>
    <Modal title={editing?'编辑国家':'创建国家'} open={open} onOk={save} onCancel={()=>setOpen(false)}>
      <div style={{marginBottom:8}}>代码 <Input value={code} onChange={e=>setCode(e.target.value)} placeholder="如 FR" disabled={editing}/></div>
      <div>名称 <Input value={name} onChange={e=>setName(e.target.value)} placeholder="如 法国站"/></div>
    </Modal></Card>)
}

function RateManager() {
  const [rates, setRates] = useState([]); const [countries, setCountries] = useState([]); const [stores, setStores] = useState([]); const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false); const [editing, setEditing] = useState(false); const [editId, setEditId] = useState(null)
  const [cid, setCid] = useState(null); const [rate, setRate] = useState(0)
  const [selectedStore, setSelectedStore] = useState(null)
  const [selectedYear, setSelectedYear] = useState('2026'); const [selectedMonth, setSelectedMonth] = useState(5)
  const ym = `${selectedYear}-${String(selectedMonth).padStart(2,'0')}`

  const fetchRatesAndStores = async () => {
    setLoading(true)
    const [r, s] = await Promise.all([getExchangeRates(), getStores()])
    setRates(r.data||[]); setStores(s.data||[]); setLoading(false)
  }
  useEffect(()=>{fetchRatesAndStores()},[])

  useEffect(() => {
    if (selectedStore) {
      getStoreCountries(selectedStore).then(res => { setCountries(res.data || []) })
    } else {
      getCountries().then(res => { setCountries(res.data || []) })
    }
  }, [selectedStore])

  const filtered = rates.filter(r => {
    if (selectedStore && r.store !== selectedStore) return false
    if (r.year_month !== ym) return false
    return true
  })

  const tableData = countries.map(c => {
    const existing = filtered.find(r => r.country_code === c.code)
    return {
      key: c.id, country_code: c.code, country_name: c.name,
      rate: existing ? existing.rate : null, id: existing ? existing.id : null, hasRate: !!existing,
    }
  })

  const openEdit = (record) => { setEditing(true); setEditId(record.id); setCid(record.key); setRate(record.rate || 0); setOpen(true) }
  const openAdd = (record) => { setEditing(false); setEditId(null); setCid(record ? record.key : countries[0]?.id); setRate(0); setOpen(true) }
  const save = async () => {
    if (editing) { await updateExchangeRate(editId, rate) }
    else { await createExchangeRate(cid, ym, rate, selectedStore) }
    message.success(editing ? '已更新' : '已创建')
    setOpen(false); setEditing(false); fetchRatesAndStores()
  }

  return (
    <div>
      <div style={{display:'flex',gap:16,marginBottom:16,alignItems:'center'}}>
        <span>店铺</span>
        <Select style={{width:180}} value={selectedStore} onChange={setSelectedStore} allowClear placeholder="全部"
          options={stores.map(s=>({value:s.name,label:s.name}))}/>
        <span style={{marginLeft:16}}>年月</span>
        <Select style={{width:100}} value={selectedYear} onChange={setSelectedYear}
          options={[2025,2026,2027,2028,2029,2030].map(y=>({value:String(y),label:String(y)}))}/>
        <Select style={{width:90}} value={selectedMonth} onChange={setSelectedMonth}
          options={Array.from({length:12},(_,i)=>({value:i+1,label:`${i+1}月`}))}/>
      </div>
      <Table rowKey="key" size="small" loading={loading} dataSource={tableData} pagination={false}
        columns={[
          {title:'国家',dataIndex:'country_code',width:80,render:(v,r)=><strong>{v}</strong>},
          {title:'名称',dataIndex:'country_name',width:100},
          {title:'汇率 (兑人民币)',dataIndex:'rate',width:150,
            render:(v,r)=> r.hasRate ? <span style={{fontSize:16,fontWeight:600}}>{v?.toFixed(4)}</span> : <span style={{color:'#ccc'}}>未设置</span>},
          {title:'操作',width:120,render:(_,r)=>(
            r.hasRate
              ? <Space size={0}>
                  <Button size="small" type="link" icon={<EditOutlined/>} onClick={()=>openEdit(r)}>修改</Button>
                  <Popconfirm title="确定删除？" onConfirm={async()=>{await deleteExchangeRate(r.id);fetchRatesAndStores()}}>
                    <Button size="small" type="link" danger icon={<DeleteOutlined/>}>删除</Button>
                  </Popconfirm>
                </Space>
              : <Button size="small" type="link" icon={<PlusOutlined/>} onClick={()=>openAdd(r)}>设置</Button>
          )},
        ]}/>
      <Modal title={editing ? '修改汇率' : '设置汇率'} open={open} onOk={save} onCancel={()=>setOpen(false)} okText="保存" cancelText="取消">
        <div style={{marginBottom:12}}><span>国家：</span><strong>{countries.find(c=>c.id===cid)?.code} {countries.find(c=>c.id===cid)?.name}</strong></div>
        <div style={{marginBottom:12}}><span>年月：</span><strong>{ym}</strong></div>
        <div><span>汇率：</span><InputNumber value={rate} onChange={setRate} step={0.01} style={{width:200}} autoFocus/><span style={{marginLeft:8,color:'#999'}}>→ RMB</span></div>
      </Modal>
    </div>
  )
}

function DataTools() {
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState(null)
  const [stores, setStores] = useState([])
  const [selectedStore, setSelectedStore] = useState('')
  const [selectedYear, setSelectedYear] = useState('2026')
  const [selectedMonth, setSelectedMonth] = useState(null)
  // 异步重算任务状态
  const [recalcTask, setRecalcTask] = useState(null)   // { taskId, status, current, done, total, message, error }
  const [pollRef] = useState({ timer: null })

  useEffect(() => { getStores().then(r => setStores(r.data||[])) }, [])

  useEffect(() => () => { if (pollRef.timer) clearInterval(pollRef.timer) }, [pollRef])

  const handleRecalculate = async () => {
    setLoading(true)
    setResults(null)
    setRecalcTask({ taskId: '', status: 'queued', progress: 0, stage: '', message: '正在提交任务…', error: null })
    try {
      const params = {}
      if (selectedStore) params.store = selectedStore
      if (selectedYear) params.year = selectedYear
      if (selectedMonth) params.month = selectedMonth
      const res = await recalculateProfit(params)
      const taskId = res.data.task_id
      if (!taskId) { message.error(res.data.detail || '提交失败'); setLoading(false); setRecalcTask(null); return }
      setRecalcTask(t => ({ ...t, taskId, status: res.data.status || 'queued' }))
      // 轮询核算任务状态（组6：后台任务化，进度可见）
      if (pollRef.timer) clearInterval(pollRef.timer)
      pollRef.timer = setInterval(async () => {
        try {
          const s = await getAccountingTask(taskId)
          const t = s.data
          setRecalcTask(prev => ({ ...prev, status: t.status, progress: t.progress, stage: t.stage, error: t.error }))
          if (t.status === 'success') {
            clearInterval(pollRef.timer); pollRef.timer = null
            const d = t.snapshot || {}
            setResults({ [`${d.period_month || ''} ${d.country || '全站汇总'}`]: {
              sales_rmb: d.product_sales_rmb, net_profit_rmb: d.net_profit_rmb,
              summary_count: d.order_count } })
            message.success(`重算完成：净利润 ${d.net_profit_rmb?.toLocaleString()} 元（快照 v${d.version}）`)
            setLoading(false)
          } else if (t.status === 'failed' || t.status === 'interrupted') {
            clearInterval(pollRef.timer); pollRef.timer = null
            message.error('重算失败: ' + (t.error?.split('\n')[0] || (t.status === 'interrupted' ? '任务中断（服务重启）' : '未知错误')))
            setLoading(false)
          }
        } catch (e) {
          clearInterval(pollRef.timer); pollRef.timer = null
          message.error('查询任务状态失败: ' + (e.response?.data?.detail || e.message))
          setLoading(false)
        }
      }, 2000)
    } catch (e) {
      message.error('重算失败: ' + (e.response?.data?.detail || e.message))
      setLoading(false); setRecalcTask(null)
    }
  }

  const handleExportMissing = async () => {
    setLoading(true)
    try {
      const params = {}
      if (selectedStore) params.store = selectedStore
      if (selectedYear) params.year = selectedYear
      if (selectedMonth) params.month = selectedMonth
      message.loading({ content: '正在导出缺失清单...', key: 'missing' })
      const res = await exportMissingSkus(params)
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url
      const fn = `缺失SKU清单_${selectedStore || '全部店铺'}_${selectedYear || ''}${selectedMonth ? String(selectedMonth).padStart(2, '0') : ''}.xlsx`
      a.download = fn
      a.click()
      URL.revokeObjectURL(url)
      message.success({ content: '导出完成', key: 'missing' })
    } catch (e) { message.error({ content: '导出失败：' + (e.response?.data?.detail || e.message), key: 'missing' }) }
    setLoading(false)
  }

  return (
    <Card title="数据重算工具" extra={<Spin spinning={loading} />}>
      <div style={{display:'flex',gap:12,marginBottom:16,alignItems:'center',flexWrap:'wrap'}}>
        <span>店铺</span>
        <Select style={{width:160}} value={selectedStore} onChange={setSelectedStore}
          options={[{value:'',label:'全部店铺'},...stores.map(s=>({value:s.name,label:s.name}))]}/>
        <span>年份</span>
        <Select style={{width:100}} value={selectedYear} onChange={setSelectedYear} allowClear placeholder="全部"
          options={[2025,2026,2027].map(y=>({value:String(y),label:String(y)}))}/>
        <span>月份</span>
        <Select style={{width:90}} value={selectedMonth} onChange={setSelectedMonth} allowClear placeholder="全部"
          options={Array.from({length:12},(_,i)=>({value:i+1,label:`${i+1}月`}))}/>
        <Button type="primary" icon={<ReloadOutlined/>} loading={loading} disabled={recalcTask?.status === 'queued' || recalcTask?.status === 'running'} onClick={handleRecalculate}>
          重算
        </Button>
        <Button icon={<ToolOutlined/>} loading={loading} onClick={handleExportMissing}>
          导出缺失SKU清单
        </Button>
      </div>
      {recalcTask && !['success', 'failed', 'interrupted'].includes(recalcTask.status) && (
        <div style={{marginBottom:16, padding: '8px 0'}}>
          <Space direction="vertical" style={{width:'100%'}}>
            {recalcTask.status === 'queued' && !recalcTask.taskId && <span style={{color:'#999'}}>{recalcTask.message || '正在提交任务…'}</span>}
            {recalcTask.taskId && (
              <>
                <span>重算进度：{recalcTask.progress || 0}%{recalcTask.stage ? `（阶段：${recalcTask.stage}）` : ''}</span>
                <Progress percent={recalcTask.progress || 0} status="active" />
              </>
            )}
          </Space>
        </div>
      )}
      {recalcTask && recalcTask.status === 'failed' && (
        <div style={{marginBottom:16, color:'#ff4d4f'}}>重算失败：{recalcTask.error?.split('\n')[0] || '未知错误'}</div>
      )}
      {recalcTask && recalcTask.status === 'interrupted' && (
        <div style={{marginBottom:16, color:'#fa8c16'}}>任务中断（服务重启），请重试</div>
      )}
      {results && (
        <Table rowKey="country" size="small" style={{marginTop:16}} dataSource={Object.entries(results).map(([k,v])=>({country:k,...v}))} pagination={false}
          columns={[
            {title:'国家',dataIndex:'country',width:80},
            {title:'销售额(RMB)',dataIndex:'sales_rmb',width:150,render:v=>v?.toLocaleString()},
            {title:'净利润(RMB)',dataIndex:'net_profit_rmb',width:150,render:v=><span style={{color:v>=0?'#52c41a':'#ff4d4f',fontWeight:600}}>{v?.toLocaleString()}</span>},
            {title:'记录数',dataIndex:'summary_count',width:100},
          ]}/>
      )}
    </Card>
  )
}

function ProductManager() {
  const [stores, setStores] = useState([])
  const [selectedStore, setSelectedStore] = useState(null)
  const [keyword, setKeyword] = useState('')
  const [data, setData] = useState([])
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(false); const [editId, setEditId] = useState(null)
  const [asin, setAsin] = useState(''); const [sku, setSku] = useState(''); const [pname, setPname] = useState(''); const [pstatus, setPstatus] = useState('normal')
  const [expandedRow, setExpandedRow] = useState(null)
  const [costs, setCosts] = useState([])
  const [costOpen, setCostOpen] = useState(false); const [costYear, setCostYear] = useState('2026'); const [costMonth, setCostMonth] = useState(null); const [costRmb, setCostRmb] = useState(0); const [costFreight, setCostFreight] = useState(0)
  const [costEditing, setCostEditing] = useState(false); const [costEditId, setCostEditId] = useState(null)

  useEffect(() => { getStores().then(r => setStores(r.data||[])) }, [])

  const fetch = async (p = 1) => {
    setLoading(true)
    try {
      const res = await getProducts({ store: selectedStore, keyword, limit: 200 })
      setData(res.data.items||[]); setPage(p)
    } catch (e) { message.error('加载失败') }
    setLoading(false)
  }

  const handleCreate = async () => {
    if (!asin || !selectedStore) { message.warn('请填写ASIN和店铺'); return }
    try {
      const payload = { store: selectedStore, asin, title: pname }
      if (sku) payload.sku = sku
      await createProduct(payload)
      message.success('创建成功'); setOpen(false); fetch()
    } catch (e) { message.error(e.response?.data?.detail || '创建失败') }
  }

  const handleUpdate = async () => {
    try {
      await updateProduct(editId, { title: pname, status: pstatus })
      message.success('已更新'); setOpen(false); fetch()
    } catch (e) { message.error(e.response?.data?.detail || '更新失败') }
  }

  const openEdit = (r) => { setEditing(true); setEditId(r.id); setAsin(r.asin); setSku((r.skus||[])[0]?.sku||''); setPname(r.title); setPstatus(r.status||'normal'); setOpen(true) }
  const openAdd = () => { setEditing(false); setEditId(null); setAsin(''); setSku(''); setPname(''); setPstatus('normal'); setOpen(true) }

  const loadCosts = async (productId) => {
    try {
      const res = await getProductCosts(productId)
      setCosts(res.data||[])
    } catch (e) { setCosts([]) }
  }

  const handleExpand = (expanded, record) => {
    if (expanded) { setExpandedRow(record.id); loadCosts(record.id) }
    else { setExpandedRow(null); setCosts([]) }
  }

  const addCost = () => { setCostEditing(false); setCostEditId(null); setCostYear('2026'); setCostMonth(null); setCostRmb(0); setCostFreight(0); setCostOpen(true) }
  const editCost = (c) => { setCostEditing(true); setCostEditId(c.id); setCostRmb(c.cost_rmb ?? 0); setCostFreight(c.freight_json?.default ?? 0); setCostOpen(true) }

  const saveCost = async () => {
    if (!costYear || !costMonth) { message.warn('请选择年月'); return }
    const ym = `${costYear}-${String(costMonth).padStart(2, '0')}`
    try {
      if (costEditing) {
        await updateProductCost(expandedRow, costEditId, { cost_rmb: costRmb, freight: costFreight })
      } else {
        await createProductCost(expandedRow, { period: ym, cost_rmb: costRmb, freight: costFreight })
      }
      message.success(costEditing ? '已更新' : '已创建'); setCostOpen(false); loadCosts(expandedRow)
    } catch (e) { message.error(e.response?.data?.detail || '保存失败') }
  }

  const delCost = async (costId) => {
    await deleteProductCost(expandedRow, costId)
    message.success('已删除'); loadCosts(expandedRow)
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <span>店铺</span>
        <Select style={{ width: 160 }} value={selectedStore} onChange={v => setSelectedStore(v)} allowClear placeholder="全部"
          options={stores.map(s => ({ value: s.code, label: s.name }))} />
        <Input.Search placeholder="ASIN / SKU / 产品名" value={keyword} onChange={e => setKeyword(e.target.value)}
          onSearch={() => fetch(1)} style={{ width: 250 }} allowClear />
        <Button type="primary" icon={<PlusOutlined />} onClick={openAdd}>新增产品</Button>
      </Space>

      <Table rowKey="id" size="small" loading={loading} dataSource={data}
        pagination={{ current: page, total: data.length, pageSize: 20, onChange: setPage, showTotal: t => `共 ${t} 条` }}
        expandable={{
          expandedRowKeys: expandedRow ? [expandedRow] : [],
          onExpand: handleExpand,
          expandedRowRender: () => (
            <div style={{ padding: '8px 0' }}>
              <Space style={{ marginBottom: 8 }}>
                <Button size="small" type="primary" icon={<PlusOutlined />} onClick={addCost}>新增月份成本</Button>
              </Space>
              <Table rowKey="id" size="small" dataSource={costs} pagination={false}
                columns={[
                  { title: '月份', dataIndex: 'period', width: 100 },
                  { title: '成本(¥)', dataIndex: 'cost_rmb', width: 120, render: v => v?.toFixed(2) },
                  { title: '运费/台(¥)', width: 120, render: (_, c) => c.freight_json?.default?.toFixed(2) },
                  { title: '操作', width: 120, render: (_, c) => (
                    <Space size={0}>
                      <Button size="small" type="link" onClick={() => editCost(c)}>编辑</Button>
                      <Popconfirm title="确定删除？" onConfirm={() => delCost(c.id)}>
                        <Button size="small" type="link" danger>删除</Button>
                      </Popconfirm>
                    </Space>
                  )},
                ]} />
            </div>
          ),
        }}
        columns={[
          { title: 'ASIN', dataIndex: 'asin', width: 160 },
          { title: 'SKU', dataIndex: 'skus', width: 200, ellipsis: true,
            render: v => (v || []).map(x => x.sku).filter(Boolean).join(', ') || <span style={{color:'#ccc'}}>未映射</span> },
          { title: '产品名', dataIndex: 'title', width: 180, ellipsis: true },
          { title: '状态', dataIndex: 'status', width: 90,
            render: v => v === 'normal' ? <Tag color="green">正常</Tag> : v === 'pending' ? <Tag color="orange">待确认</Tag> : <Tag color="red">异常</Tag> },
          { title: '操作', width: 140, render: (_, r) => (
            <Space size={0}>
              <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>编辑</Button>
              <Popconfirm title="确定删除？" onConfirm={async () => {
                try { await deleteProduct(r.id); message.success('已删除'); fetch() }
                catch (e) { message.error(e.response?.data?.detail || '删除失败') }
              }}>
                <Button danger size="small" icon={<DeleteOutlined />} />
              </Popconfirm>
            </Space>
          )},
        ]} />

      {/* 产品弹窗 */}
      <Modal title={editing ? '编辑产品' : '新增产品'} open={open} onOk={editing ? handleUpdate : handleCreate}
        onCancel={() => setOpen(false)} okText="保存" cancelText="取消">
        {!editing && (
          <div style={{ marginBottom: 8 }}>
            <span>店铺：</span>
            <Select style={{ width: 200 }} value={selectedStore} onChange={setSelectedStore}
              options={stores.map(s => ({ value: s.code, label: s.name }))} />
          </div>
        )}
        <div style={{ marginBottom: 8 }}><span>ASIN：</span><Input value={asin} onChange={e => setAsin(e.target.value)} disabled={editing} /></div>
        <div style={{ marginBottom: 8 }}><span>SKU：</span><Input value={sku} onChange={e => setSku(e.target.value)} disabled={editing} placeholder="选填，留空不建映射" /></div>
        <div style={{ marginBottom: 8 }}><span>名称：</span><Input value={pname} onChange={e => setPname(e.target.value)} /></div>
        {editing && (
          <div><span>状态：</span>
            <Select style={{ width: 120 }} value={pstatus} onChange={setPstatus}
              options={[{ value: 'normal', label: '正常' }, { value: 'pending', label: '待确认' }, { value: 'abnormal', label: '异常' }]} />
          </div>
        )}
      </Modal>

      {/* 成本弹窗 */}
      <Modal title={costEditing ? '编辑成本' : '新增月份成本'} open={costOpen} onOk={saveCost}
        onCancel={() => setCostOpen(false)} okText="保存" cancelText="取消">
        {!costEditing && (
          <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
            <span>月份：</span>
            <Select style={{ width: 90 }} value={costYear} onChange={setCostYear}
              options={[2025, 2026, 2027].map(y => ({ value: String(y), label: String(y) }))} />
            <span>年</span>
            <Select style={{ width: 110 }} value={costMonth} onChange={setCostMonth} placeholder="选择月份"
              options={Array.from({ length: 12 }, (_, i) => ({ value: i + 1, label: `${i + 1}月` }))} />
          </div>
        )}
        <div style={{ marginBottom: 8 }}><span>成本(¥)：</span><InputNumber value={costRmb} onChange={setCostRmb} step={0.01} style={{ width: 200 }} /></div>
        <div><span>运费/台(¥)：</span><InputNumber value={costFreight} onChange={setCostFreight} step={0.01} style={{ width: 200 }} /></div>
      </Modal>
    </div>
  )
}

const MODULE_LABELS = [
  { key: 'dashboard', label: '数据看板' }, { key: 'query', label: '数据查询' },
  { key: 'advertising', label: '广告分析' }, { key: 'ads_agent', label: '广告Agent' },
  { key: 'import', label: '数据导入' }, { key: 'weekly_report', label: '周报管理' },
  { key: 'admin', label: '系统管理' }, { key: 'inventory', label: '库存系统' },
]
const FIELD_LABELS = [
  { key: 'sales', label: '销售额/订单' }, { key: 'profit', label: '净利润/净利率' },
  { key: 'cost', label: '采购成本/单价' }, { key: 'freight', label: '运费/运费台' },
  { key: 'ad_pct', label: '广告费占比' }, { key: 'storage_pct', label: '仓储费占比' },
  { key: 'return_rate', label: '退货率/退货数量' },
]

function PermissionEditor({ perms, setPerms, stores }) {
  if (!perms) return null
  const toggleStore = (code) => {
    const s = perms.stores || []
    setPerms({ ...perms, stores: s.includes(code) ? s.filter(x => x !== code) : [...s, code] })
  }
  const toggleModule = (key) => setPerms({ ...perms, modules: { ...perms.modules, [key]: !perms.modules[key] } })
  const toggleField = (key) => setPerms({ ...perms, fields: { ...perms.fields, [key]: !perms.fields[key] } })
  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <strong>可访问店铺</strong><small style={{ color: '#999', marginLeft: 8 }}>不选=全部</small>
        <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {stores.map(s => <Tag.CheckableTag key={s.code} checked={perms.stores?.includes(s.code)} onChange={() => toggleStore(s.code)}>{s.code}</Tag.CheckableTag>)}
        </div>
      </div>
      <div style={{ marginBottom: 16 }}>
        <strong>功能模块</strong>
        <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          {MODULE_LABELS.map(m => <span key={m.key}><Switch size="small" checked={perms.modules?.[m.key]} onChange={() => toggleModule(m.key)} /><span style={{ marginLeft: 4 }}>{m.label}</span></span>)}
        </div>
      </div>
      <div>
        <strong>可见字段</strong>
        <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          {FIELD_LABELS.map(f => <span key={f.key}><Switch size="small" checked={perms.fields?.[f.key]} onChange={() => toggleField(f.key)} /><span style={{ marginLeft: 4 }}>{f.label}</span></span>)}
        </div>
      </div>
    </div>
  )
}

function UserManager() {
  const [users, setUsers] = useState([])
  const [stores, setStores] = useState([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [isAdmin, setIsAdmin] = useState(false)
  const [createPerms, setCreatePerms] = useState(null)
  const [editOpen, setEditOpen] = useState(false)
  const [editUser, setEditUser] = useState(null)
  const [editPassword, setEditPassword] = useState('')
  const [editIsAdmin, setEditIsAdmin] = useState(false)
  const [editPerms, setEditPerms] = useState(null)

  const defaultPerms = () => ({ stores: [], modules: { dashboard: true, advertising: false, ads_agent: false, import: false, query: true, admin: false, inventory: false, weekly_report: true }, fields: { profit: true, cost: false, freight: false, ad_pct: true, storage_pct: true, return_rate: true, sales: true } })

  const fetch = async () => {
    setLoading(true)
    try { setUsers(await getUsers()); setStores((await getStores()).data || []) } catch (e) { /* ignore */ }
    setLoading(false)
  }
  useEffect(() => { fetch() }, [])

  const handleCreate = async () => {
    if (!username || !password) { message.warning('请填写用户名和密码'); return }
    try {
      await createUser({ username, password, is_admin: isAdmin, permissions: isAdmin ? null : createPerms })
      message.success('用户创建成功')
      setOpen(false); setUsername(''); setPassword(''); setIsAdmin(false); setCreatePerms(null); fetch()
    } catch (e) {
      message.error(e.response?.data?.detail || '创建失败')
    }
  }

  const handleUpdate = async () => {
    if (!editUser) return
    try {
      const data = { is_admin: editIsAdmin }
      if (editPassword) data.password = editPassword
      if (!editIsAdmin) data.permissions = editPerms
      await updateUser(editUser.id, data)
      message.success('更新成功')
      setEditOpen(false); setEditUser(null); setEditPassword(''); setEditPerms(null); fetch()
    } catch (e) {
      message.error(e.response?.data?.detail || '更新失败')
    }
  }

  const handleDelete = async (userId) => {
    try {
      await deleteUser(userId)
      message.success('已删除'); fetch()
    } catch (e) {
      message.error(e.response?.data?.detail || '删除失败')
    }
  }

  return (
    <Card>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setCreatePerms(defaultPerms()); setOpen(true) }}>创建用户</Button>
      </Space>
      <Table rowKey="id" size="small" loading={loading} dataSource={users} pagination={false}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 50 },
          { title: '用户名', dataIndex: 'username', width: 120 },
          { title: '角色', dataIndex: 'is_admin', width: 80, render: v => v ? <Tag color="red">管理员</Tag> : <Tag color="blue">普通</Tag> },
          { title: '权限摘要', render: (_, r) => {
            if (r.is_admin) return <Tag color="red">全部权限</Tag>
            const p = r.permissions || {}
            const mods = Object.entries(p.modules || {}).filter(([, v]) => v).map(([k]) => k).join(',')
            const sts = (p.stores || []).join(',') || '全部'
            return <span style={{ fontSize: 12, color: '#999' }}>店铺:{sts} | 模块:{mods}</span>
          }},
          { title: '操作', width: 150, render: (_, r) => (
            <Space size={0}>
              <Button size="small" icon={<EditOutlined />} onClick={() => {
                setEditUser(r); setEditPassword(''); setEditIsAdmin(!!r.is_admin)
                setEditPerms(r.permissions || defaultPerms()); setEditOpen(true)
              }}>编辑</Button>
              {r.username !== 'admin' && (
                <Popconfirm title="确定删除？" onConfirm={() => handleDelete(r.id)}>
                  <Button danger size="small" icon={<DeleteOutlined />} />
                </Popconfirm>
              )}
            </Space>
          )},
        ]} />

      <Modal title="创建用户" open={open} onOk={handleCreate} onCancel={() => setOpen(false)} width={600}
        styles={{ body: { maxHeight: '60vh', overflow: 'auto' } }}>
        <div style={{ marginBottom: 12 }}>
          <span>用户名：</span>
          <Input value={username} onChange={e => setUsername(e.target.value)} placeholder="输入用户名" />
        </div>
        <div style={{ marginBottom: 12 }}>
          <span>密码：</span>
          <Input.Password value={password} onChange={e => setPassword(e.target.value)} placeholder="输入密码" />
        </div>
        <div style={{ marginBottom: 12 }}>
          <span>管理员权限：</span>
          <Switch checked={isAdmin} onChange={setIsAdmin} style={{ marginLeft: 8 }} />
        </div>
        {!isAdmin && createPerms && (
          <PermissionEditor perms={createPerms} setPerms={setCreatePerms} stores={stores} />
        )}
      </Modal>

      <Modal title="编辑用户" open={editOpen} onOk={handleUpdate} onCancel={() => setEditOpen(false)} width={600}
        styles={{ body: { maxHeight: '60vh', overflow: 'auto' } }}>
        <div style={{ marginBottom: 12 }}>
          <span>用户名：</span>
          <strong>{editUser?.username}</strong>
        </div>
        <div style={{ marginBottom: 12 }}>
          <span>新密码（留空不修改）：</span>
          <Input.Password value={editPassword} onChange={e => setEditPassword(e.target.value)} placeholder="留空则不修改" />
        </div>
        <div style={{ marginBottom: 12 }}>
          <span>管理员权限：</span>
          <Switch checked={editIsAdmin} onChange={setEditIsAdmin} disabled={editUser?.username === 'admin'} style={{ marginLeft: 8 }} />
        </div>
        {!editIsAdmin && editPerms && (
          <PermissionEditor perms={editPerms} setPerms={setEditPerms} stores={stores} />
        )}
      </Modal>
    </Card>
  )
}
