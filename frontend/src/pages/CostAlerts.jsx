// 成本/运费缺失预警 — 待补录队列（一键补录 / 忽略）
import React, { useState, useEffect } from 'react'
import {
  Card, Table, Button, Select, Space, message, Tag, Modal, Form, InputNumber,
  Input, Alert, Row, Col, Statistic, Popconfirm,
} from 'antd'
import { WarningOutlined, CheckOutlined, StopOutlined, ReloadOutlined } from '@ant-design/icons'
import { getStores, getCostAlerts, createCostEntry, ignoreCostAlert } from '../api'

export default function CostAlerts() {
  const [stores, setStores] = useState([])
  const [store, setStore] = useState()
  const [status, setStatus] = useState('pending')
  const [period, setPeriod] = useState('')
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form] = Form.useForm()

  useEffect(() => {
    getStores().then((r) => {
      setStores(r.data || [])
      if (r.data?.[0]) setStore(r.data[0].store_code)
    }).catch(() => {})
  }, [])

  const load = async () => {
    setLoading(true)
    try {
      const params = { status }
      if (store) params.store = store
      if (period) params.period_month = period
      const res = await getCostAlerts(params)
      setAlerts(res.data.items || [])
    } catch (e) {
      message.error('加载失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (store) load() }, [store, status, period])

  const openEdit = (row) => {
    setEditing(row)
    form.setFieldsValue({ cost_rmb: undefined, freight: undefined })
  }

  const submitEntry = async () => {
    const vals = await form.validateFields()
    try {
      const res = await createCostEntry({
        store: editing.store_code,
        asin: editing.asin,
        sku: editing.sku,
        period: editing.period,
        cost_rmb: vals.cost_rmb ?? null,
        freight: vals.freight ?? null,
      })
      message.success(`补录成功，已关闭 ${res.data.closed_alerts} 条预警。请到「核算快照」重新核算以更新利润。`)
      setEditing(null)
      load()
    } catch (e) {
      message.error('补录失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const doIgnore = async (row) => {
    try {
      await ignoreCostAlert(row.id)
      message.success('已忽略该预警')
      load()
    } catch (e) {
      message.error('操作失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const pendingCount = alerts.filter((a) => a.status === 'pending').length

  return (
    <div style={{ padding: 4 }}>
      <Alert
        type="warning" showIcon icon={<WarningOutlined />} style={{ marginBottom: 16 }}
        message="成本/运费缺失预警"
        description="报表产品表可能缺少新品的成本或运费。缺失时系统不会静默按 0 计算，而是标记「成本不完整」并在此排队待补录；补录后请重新核算。"
      />

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <span>店铺</span>
          <Select style={{ width: 130 }} value={store} onChange={setStore}
            options={stores.map((s) => ({ value: s.store_code, label: s.store_code }))} />
          <span>月份</span>
          <Input style={{ width: 120 }} value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="全部 / YYYY-MM" />
          <span>状态</span>
          <Select style={{ width: 130 }} value={status} onChange={setStatus}
            options={[
              { value: 'pending', label: '待补录' },
              { value: 'resolved', label: '已补录' },
              { value: 'ignored', label: '已忽略' },
              { value: 'all', label: '全部' },
            ]} />
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
        </Space>
      </Card>

      <Row gutter={12} style={{ marginBottom: 16 }}>
        <Col span={6}><Card size="small"><Statistic title="当前清单条数" value={alerts.length} /></Card></Col>
        <Col span={6}>
          <Card size="small">
            <Statistic title="待补录" value={pendingCount}
              valueStyle={{ color: pendingCount > 0 ? '#cf1322' : '#3f8600' }} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="缺失清单">
        <Table
          size="small" rowKey="id" loading={loading} dataSource={alerts}
          pagination={{ pageSize: 15 }}
          columns={[
            { title: '店铺', dataIndex: 'store_code', width: 80 },
            { title: 'ASIN', dataIndex: 'asin', width: 130 },
            { title: 'SKU', dataIndex: 'sku', width: 150 },
            { title: '站点', dataIndex: 'country', width: 70, render: (v) => v || '—' },
            { title: '月份', dataIndex: 'period', width: 90 },
            {
              title: '缺失项', dataIndex: 'missing_type', width: 100,
              render: (v) => v === 'cost' ? <Tag color="red">成本</Tag>
                : v === 'freight' ? <Tag color="orange">运费</Tag>
                  : <Tag color="red">成本+运费</Tag>,
            },
            {
              title: '状态', dataIndex: 'status', width: 100,
              render: (v) => v === 'pending' ? <Tag color="red">待补录</Tag>
                : v === 'resolved' ? <Tag color="green">已补录</Tag> : <Tag>已忽略</Tag>,
            },
            {
              title: '检出时间', dataIndex: 'detected_at', width: 170,
              render: (v) => (v || '').replace('T', ' ').slice(0, 19),
            },
            {
              title: '操作', width: 150,
              render: (_, r) => r.status === 'pending' ? (
                <Space size={4}>
                  <Button size="small" type="primary" ghost icon={<CheckOutlined />} onClick={() => openEdit(r)}>
                    补录
                  </Button>
                  <Popconfirm title="确定忽略此预警？" onConfirm={() => doIgnore(r)}>
                    <Button size="small" danger icon={<StopOutlined />}>忽略</Button>
                  </Popconfirm>
                </Space>
              ) : <span style={{ color: '#bbb' }}>—</span>,
            },
          ]}
        />
      </Card>

      <Modal
        open={!!editing} title="成本补录" onCancel={() => setEditing(null)} onOk={submitEntry}
        okText="保存补录" width={520}
      >
        {editing && (
          <>
            <Alert type="info" showIcon style={{ marginBottom: 12 }}
              message={`${editing.store_code} / ${editing.asin} / ${editing.sku} / ${editing.period}`}
              description="补录值写入成本版本表（来源=人工补录），并自动关闭对应预警。" />
            <Form form={form} layout="vertical">
              <Form.Item name="cost_rmb" label="采购成本（RMB / 单件）">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} placeholder="留空表示不补录该项" />
              </Form.Item>
              <Form.Item name="freight" label="头程运费（RMB / 单件）">
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} placeholder="留空表示不补录该项" />
              </Form.Item>
            </Form>
          </>
        )}
      </Modal>
    </div>
  )
}
