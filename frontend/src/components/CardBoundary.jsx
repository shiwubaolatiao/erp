import React from 'react'
import { Button, Card, Result } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'

/**
 * CardBoundary 看板区块独立容错边界（组 9.1 / D-06）
 *
 * 覆盖两类失败，均只影响当前区块、不整页白屏：
 *   1. 接口请求失败 —— 父组件传入 error（字符串）时显示错误占位 + 重试按钮；
 *      children 以 display:none 保留在 DOM 中，重试成功后图表实例不丢失。
 *   2. 渲染期异常 —— ErrorBoundary 捕获子树抛错，显示占位；重试/筛选变化后恢复。
 *
 * @param {string} title    区块标题（错误占位时展示，保持版式一致）
 * @param {string|null} error  接口级错误信息（null 表示接口正常）
 * @param {function} [onRetry] 重试回调（通常是父组件的 fetch 函数）；不传则不显示重试按钮
 * @param {string} [retryKey] 筛选条件 key —— 变化时自动清除渲染期错误（换筛选=重新取数）
 * @param {React.ReactNode} children 正常内容
 */
export default class CardBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { renderError: null }
  }

  static getDerivedStateFromError() {
    return { renderError: true }
  }

  componentDidCatch(error, info) {
    // 渲染异常是代码级问题：完整记录便于定位，但不冒泡到整页
    console.error(`[CardBoundary] ${this.props.title || '区块'} 渲染异常:`, error,
      info && info.componentStack)
  }

  componentDidUpdate(prevProps) {
    // 筛选条件变化 → 清除渲染期错误，让新数据重新渲染区块
    if (prevProps.retryKey !== this.props.retryKey && this.state.renderError) {
      this.setState({ renderError: null })
    }
  }

  handleRetry = () => {
    this.setState({ renderError: null })
    if (typeof this.props.onRetry === 'function') this.props.onRetry()
  }

  render() {
    const { title, error, onRetry, children } = this.props
    const renderError = this.state.renderError

    if (renderError) {
      // 渲染期异常：子树不可再渲染（重试会再次抛错），只能整体换占位
      return (
        <Card title={title} size="small" style={{ marginBottom: 16 }}>
          <Result
            status="warning"
            title="区块渲染异常"
            subTitle="该区块暂时不可用，其余区块不受影响"
            extra={onRetry && <Button type="primary" icon={<ReloadOutlined />} onClick={this.handleRetry}>重试</Button>}
          />
        </Card>
      )
    }

    if (error) {
      // 接口失败：children 隐藏保留（echarts 实例不销毁），重试成功后直接恢复
      return (
        <>
          <div style={{ display: 'none' }}>{children}</div>
          <Card title={title} size="small" style={{ marginBottom: 16 }}>
            <Result
              status="warning"
              title="数据加载失败"
              subTitle={typeof error === 'string' && error ? error : '该区块暂时不可用，其余区块不受影响'}
              extra={onRetry && <Button type="primary" icon={<ReloadOutlined />} onClick={this.handleRetry}>重试</Button>}
            />
          </Card>
        </>
      )
    }

    return children
  }
}