import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/chart'
const columns = [
  '图表编号',
  '图表名称',
  '图表类型',
  '数据源绑定',
  '横轴字段',
  '纵轴字段',
  '筛选条件',
  '刷新间隔'
]
const actions = [
  '提交校验',
  '启用配置',
  '停用配置'
]

type Row = Record<string, string | number | null> & {
  依赖画像版本?: number | null
  依赖结论?: string
  依赖阻断原因?: string | null
}

function DependencyBadge({ row }: { row: Row }) {
  if (!row['依赖画像版本']) {
    return <span className="badge muted">未引用画像</span>
  }
  const ok = row['依赖结论'] === '合格'
  return (
    <div className="dep-cell">
      <span className={`badge ${ok ? 'ok' : 'fail'}`}>
        {row['依赖结论']} v{row['依赖画像版本']}
      </span>
      {!ok && row['依赖阻断原因'] && <p className="reason-text">{String(row['依赖阻断原因'])}</p>}
    </div>
  )
}

export default function ChartView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('图表配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '可视化图表配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload?.message ?? '可视化图表配置动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '可视化图表配置操作失败')
    }
  }

  async function syncDependency(row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/sync-dependency`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload?.message ?? '依赖同步失败')
      }
      setInfoMessage(payload.message ?? '依赖版本已同步')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '依赖同步失败')
    }
  }

  return (
    <section className="page" data-module="chart">
      <header className="page-head">
        <div>
          <h2>可视化图表配置管理</h2>
          <p className="page-desc">
            图表只是数据集的下游引用方：启用状态与实际引用的画像版本一致，数据集失去资格时图表会被同步阻断并自动停用。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出可视化图表配置清单
          </button>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>引用画像版本与结论</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td><DependencyBadge row={row} /></td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void syncDependency(row)}>
                  同步画像版本
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无可视化图表配置数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条可视化图表配置记录</span>
        <span>
          {infoMessage && <span className="success-text" style={{ marginRight: 12 }}>{infoMessage}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
