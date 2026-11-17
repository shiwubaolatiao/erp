// 数据溯源 — 汇总数字 → fact 记录 → 原始行 → 批次 → 文件
// 核心验收能力：任意数字可下钻还原到 Excel 原始行与单元格值
import React, { useState, useEffect } from 'react'
import {
  Card, Table, Input, Button, Descriptions, Tag, Space, message, Tabs,
  Breadcrumb, Row, Col, Statistic, Alert, Divider, Modal,
} from 'antd'
import { SearchOutlined, FileSearchOutlined, DownloadOutlined } from '@ant-design/icons'
import { listBatches, traceBatch, traceRawRow, searchRaw } from '../api'

const { Search } = Input

export default function Trace() {
  const [batches, setBatches] = useState([])
  const [loadingBatches, setLoadingBatches] = useState(false)
  const [batchDetail, setBatchDetail] = useState(null)
  const [rawRow, setRawRow] = useState(null)
  const [searchItems, setSearchItems] = useState([])
  const [searching, setSearching] = useState(false)

  const loadBatches = async () => {
    setLoadingBatches(true)
    try {
      const res = await listBatches()
      setBatches(res.data.items || [])
    } catch (e) {
      message.error('加载批次失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setLoadingBatches(false)
    }
  }

  useEffect(() => { loadBatches() }, [])

  const openBatch = async (batchId) => {
    try {
      const res = await traceBatch(batchId)
      setBatchDetail(res.data)
    } catch (e) {
      message.error('加载批次详情失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const openRawRow = async (rawRowId) => {
    try {
      const res = await traceRawRow(rawRowId)
      setRawRow(res.data)
    } catch (e) {
      message.error('加载原始行失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const doSearch = async (kw) => {
    if (!kw) return
    setSearching(true)
    try {
      const res = await searchRaw(kw)
      setSearchItems(res.data.items || [])
      if (!res.data.items?.length) message.info('未找到匹配的原始行')
    } catch (e) {
      message.error('搜索失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setSearching(false)
    }
  }

  const batchColumns = [
    { title: '批次', dataIndex: 'id', width: 80 },
    { title: '文件名', dataIndex: 'file_name', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: (v) => (
        <Tag color={v === 'success' ? 'green' : v === 'partial' ? 'orange' : v === 'failed' ? 'red' : 'blue'}>
          {v === 'success' ? '成功' : v === 'partial' ? '部分失败' : v === 'failed' ? '失败' : v}
        </Tag>
      ),
    },
    { title: '成功行', dataIndex: 'success_rows', width: 90 },
    { title: '失败行', dataIndex: 'failed_rows', width: 90, render: (v) => v > 0 ? <span style={{ color: '#cf1322' }}>{v}</span> : v },
    { title: '锁结', dataIndex: 'is_locked', width: 80, render: (v) => v ? <Tag color="gold">已锁</Tag> : '—' },
    { title: '导入时间', dataIndex: 'imported_at', width: 170, render: (v) => v ? v.replace('T', ' ').slice(0, 19) : '' },
    {
      title: '操作', width: 100,
      render: (_, r) => <Button size="small" type="link" onClick={() => openBatch(r.id)}>详情</Button>,
    },
  ]

  return (
    <div style={{ padding: 4 }}>
      <Alert
        type="info" showIcon style={{ marginBottom: 16 }}
        message="数据溯源"
        description="任意汇总数字都可沿 dws快照 → fact 记录 → 原始行(ODS) → 批次/文件 链路下钻，核对到 Excel 的具体 sheet、行号与单元格原值。"
      />

      <Card title="按订单号 / SKU / ASIN 搜索原始行" size="small" style={{ marginBottom: 16 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Search
            placeholder="输入订单号、SKU、ASIN 或任意单元格文本"
            enterButton={<><SearchOutlined /> 搜索原始数据</>}
            loading={searching}
            onSearch={doSearch}
            allowClear
          />
        </Space.Compact>
        {searchItems.length > 0 && (
          <>
            <Divider style={{ margin: '12px 0' }} />
            <Table
              size="small" rowKey="raw_row_id"
              dataSource={searchItems}
              pagination={{ pageSize: 10 }}
              columns={[
                { title: '原始行ID', dataIndex: 'raw_row_id', width: 110 },
                { title: 'Sheet', dataIndex: 'sheet_name', width: 200 },
                { title: '行号', dataIndex: 'row_no', width: 80 },
                { title: '业务日期', dataIndex: 'biz_date', width: 180, render: (v) => v ? v.replace('T', ' ').slice(0, 19) : '—' },
                { title: '批次', dataIndex: 'batch_id', width: 90 },
                {
                  title: '操作', width: 100,
                  render: (_, r) => <Button size="small" type="link" icon={<FileSearchOutlined />} onClick={() => openRawRow(r.raw_row_id)}>查看原值</Button>,
                },
              ]}
            />
          </>
        )}
      </Card>

      <Card title="导入批次" size="small" extra={<Button size="small" onClick={loadBatches}>刷新</Button>}>
        <Table
          size="small" rowKey="id" loading={loadingBatches}
          dataSource={batches} columns={batchColumns}
          pagination={{ pageSize: 10 }}
        />
      </Card>

      {/* 批次详情 */}
      <Modal
        open={!!batchDetail} onCancel={() => setBatchDetail(null)} footer={null}
        width={1000} title={`批次详情 #${batchDetail?.batch?.id || ''}`}
      >
        {batchDetail && (
          <>
            <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="文件名" span={2}>{batchDetail.batch.file_name}</Descriptions.Item>
              <Descriptions.Item label="文件哈希" span={2}>
                <code style={{ fontSize: 11 }}>{batchDetail.batch.file_hash}</code>
              </Descriptions.Item>
              <Descriptions.Item label="状态">{batchDetail.batch.status}</Descriptions.Item>
              <Descriptions.Item label="导入时间">{(batchDetail.batch.imported_at || '').replace('T', ' ').slice(0, 19)}</Descriptions.Item>
              <Descriptions.Item label="成功/失败行">{batchDetail.batch.success_rows} / {batchDetail.batch.failed_rows}</Descriptions.Item>
              <Descriptions.Item label="归档路径" span={2}>
                <span style={{ fontSize: 11 }}>{batchDetail.batch.original_path}</span>
              </Descriptions.Item>
            </Descriptions>

            <div style={{ fontWeight: 600, margin: '10px 0 6px' }}>Sheet 识别结果</div>
            <Table
              size="small" rowKey="id" pagination={false}
              dataSource={batchDetail.sheets}
              columns={[
                { title: 'Sheet 名', dataIndex: 'sheet_name', width: 180 },
                { title: '识别类型', dataIndex: 'report_type', width: 120, render: (v) => v === 'unrecognized' ? <Tag color="red">未识别</Tag> : <Tag>{v}</Tag> },
                { title: '国家', dataIndex: 'country', width: 70 },
                { title: '币种', dataIndex: 'currency', width: 70 },
                { title: '表头行', dataIndex: 'detected_header_row', width: 80 },
                { title: '数据行', dataIndex: 'row_count', width: 80 },
              ]}
            />

            {batchDetail.errors?.length > 0 && (
              <>
                <div style={{ fontWeight: 600, margin: '14px 0 6px', color: '#cf1322' }}>
                  异常行（{batchDetail.errors.length}）
                </div>
                <Table
                  size="small" rowKey={(r, i) => `${r.sheet_name}-${r.row_no}-${i}`}
                  dataSource={batchDetail.errors} pagination={{ pageSize: 5 }}
                  columns={[
                    { title: 'Sheet', dataIndex: 'sheet_name', width: 160 },
                    { title: '行号', dataIndex: 'row_no', width: 70 },
                    { title: '类型', dataIndex: 'error_type', width: 130 },
                    { title: '原因', dataIndex: 'error_message', ellipsis: true },
                  ]}
                />
              </>
            )}
          </>
        )}
      </Modal>

      {/* 原始行单元格级详情 */}
      <Modal
        open={!!rawRow} onCancel={() => setRawRow(null)} footer={null}
        width={860} title={rawRow ? `原始行 #${rawRow.raw_row_id} — ${rawRow.sheet_name} 第 ${rawRow.row_no} 行` : ''}
      >
        {rawRow && (
          <>
            <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="来源文件">{rawRow.batch?.file_name}</Descriptions.Item>
              <Descriptions.Item label="导入时间">{(rawRow.batch?.imported_at || '').replace('T', ' ').slice(0, 19)}</Descriptions.Item>
              <Descriptions.Item label="Sheet 类型">{rawRow.sheet?.report_type}</Descriptions.Item>
              <Descriptions.Item label="国家/币种">{rawRow.sheet?.country} / {rawRow.sheet?.currency}</Descriptions.Item>
              <Descriptions.Item label="业务日期" span={2}>
                {(rawRow.biz_date || '').replace('T', ' ').slice(0, 19) || '—'}
              </Descriptions.Item>
            </Descriptions>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>单元格原值（{rawRow.cells.length} 列）</div>
            <Table
              size="small" rowKey={(r, i) => i} pagination={false}
              scroll={{ y: 400 }}
              dataSource={rawRow.cells.map((c, i) => ({ key: i, idx: i + 1, ...c }))}
              columns={[
                { title: '#', dataIndex: 'idx', width: 50 },
                { title: '原始列名', dataIndex: 'header', width: 260 },
                { title: '原始值', dataIndex: 'value', ellipsis: true, render: (v) => v === null || v === undefined ? <span style={{ color: '#bbb' }}>(空)</span> : String(v) },
              ]}
            />
          </>
        )}
      </Modal>
    </div>
  )
}
