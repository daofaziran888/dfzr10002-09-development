import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataset'
const columns = [
  '数据集编号',
  '数据集名称',
  '来源表名',
  '字段列表',
  '行数上限',
  '刷新频率',
  '最近刷新',
  '是否启用'
]
const actions = [
  '提交校验',
  '启用配置',
  '停用配置'
]

type Check = {
  key: string
  label: string
  passed: boolean
  detail: string
}
type Profile = {
  id?: number
  dataset_code: string
  dataset_name: string
  version: number
  submitted_by: string
  submitted_at: string
  is_current: boolean
  qualified: boolean
  reasons: string[]
  checks: Check[]
  snapshot: { 来源表名: string; 最近刷新: string; 刷新频率: string }
}
type BatchItem = {
  dataset_code: string
  state: 'profiled' | 'conflict' | 'not_found'
  qualified: boolean
  version?: number
  message: string
  profile: Profile | null
}
type BatchResult = {
  total: number
  qualified_count: number
  failed_count: number
  conflict_count: number
  not_found_count: number
  all_qualified: boolean
  items: BatchItem[]
}
type Row = Record<string, string | number | null> & {
  启用资格?: string
  当前画像版本?: number | null
  停用原因?: string | null
}

function Badge({ text, tone }: { text: string; tone: 'ok' | 'fail' | 'warn' | 'muted' }) {
  return <span className={`badge ${tone}`}>{text}</span>
}

export default function DatasetView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [batchResult, setBatchResult] = useState<BatchResult | null>(null)
  const [historyCode, setHistoryCode] = useState('')
  const [historyItems, setHistoryItems] = useState<Profile[]>([])
  const [submitting, setSubmitting] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据集列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setSelected((prev) => {
        const valid = new Set((payload.items ?? []).map((row: Row) => String(row['数据集编号'])))
        return new Set([...prev].filter((code) => valid.has(code)))
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集列表读取失败')
    }
  }, [filters])

  const reloadProfiles = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/quality/profiles`)
      if (response.ok) {
        const payload = await response.json()
        setProfiles(payload.items ?? [])
      }
    } catch {
      // 画像区读取失败不阻塞主列表
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadProfiles()
  }, [reloadProfiles])

  const profileMap = useMemo(() => {
    const map = new Map<string, Profile>()
    for (const profile of profiles) {
      map.set(profile.dataset_code, profile)
    }
    return map
  }, [profiles])

  function toggleSelect(code: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(code)) {
        next.delete(code)
      } else {
        next.add(code)
      }
      return next
    })
  }

  function toggleSelectAll() {
    setSelected((prev) => {
      if (prev.size === rows.length && rows.length > 0) {
        return new Set()
      }
      return new Set(rows.map((row) => String(row['数据集编号'])))
    })
  }

  async function submitProfiles() {
    if (selected.size === 0) {
      setErrorMessage('请先勾选至少一个分析数据集')
      return
    }
    setSubmitting(true)
    setErrorMessage('')
    setInfoMessage('')
    setBatchResult(null)
    try {
      const response = await request(`${ENDPOINT}/quality/profiles`, {
        method: 'POST',
        body: JSON.stringify({ dataset_codes: [...selected], operator: '值班管理员' }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? '质量画像提交失败')
      }
      setBatchResult(payload as BatchResult)
      await Promise.all([reload(), reloadProfiles()])
      if (payload.conflict_count > 0) {
        setInfoMessage(`有 ${payload.conflict_count} 条提交因版本过期冲突作废，仅保留后一个有效结论`)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量画像提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function enableSelected() {
    if (selected.size === 0) {
      setErrorMessage('请先勾选至少一个分析数据集')
      return
    }
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/quality/enable-batch`, {
        method: 'POST',
        body: JSON.stringify({ dataset_codes: [...selected] }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? '整组启用失败')
      }
      const blocked = payload.items.filter((item: { enabled: boolean }) => !item.enabled)
      if (blocked.length > 0) {
        setErrorMessage(blocked.map((item: { message: string }) => item.message).join('；'))
      }
      if (payload.all_enabled) {
        setInfoMessage(`整组 ${payload.enabled_count} 个数据集已全部启用`)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '整组启用失败')
    }
  }

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
        throw new Error(payload?.message ?? '分析数据集动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析数据集操作失败')
    }
  }

  async function openHistory(code: string) {
    if (historyCode === code) {
      setHistoryCode('')
      setHistoryItems([])
      return
    }
    setHistoryCode(code)
    try {
      const response = await request(`${ENDPOINT}/quality/profiles/${encodeURIComponent(code)}/history`)
      const payload = await response.json()
      setHistoryItems(payload.items ?? [])
    } catch {
      setHistoryItems([])
    }
  }

  const qualifiedTotal = profiles.filter((profile) => profile.qualified).length

  return (
    <section className="page" data-module="dataset">
      <header className="page-head">
        <div>
          <h2>分析数据集管理</h2>
          <p className="page-desc">
            维护分析可用的数据集定义、字段范围与刷新策略；启用前先做多选质量画像，未通过画像的数据集不能启用。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析数据集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">分析数据集总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前画像合格</span>
          <strong className="stat-value">{qualifiedTotal}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前画像不合格</span>
          <strong className="stat-value">{profiles.length - qualifiedTotal}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已勾选待提交</span>
          <strong className="stat-value">{selected.size}</strong>
        </article>
      </div>

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

      <div className="quality-panel">
        <h3>数据集质量画像（多选整组提交）</h3>
        <div className="quality-toolbar">
          <button
            className="btn primary"
            type="button"
            disabled={submitting || selected.size === 0}
            onClick={() => void submitProfiles()}
          >
            {submitting ? '画像生成中…' : `生成质量画像（已选 ${selected.size} 个）`}
          </button>
          <button
            className="btn"
            type="button"
            disabled={selected.size === 0}
            onClick={() => void enableSelected()}
          >
            整组启用（须全部合格）
          </button>
          <span className="profile-meta">
            校验项：字段完整性（对照数据源默认库表结构逐项比对）、行数上限、空值、刷新时效（48 小时）
          </span>
        </div>

        {batchResult && (
          <div className="profile-list">
            <div className="profile-meta">
              本批共 {batchResult.total} 条：合格 {batchResult.qualified_count} 条、
              未通过 {batchResult.failed_count} 条、冲突 {batchResult.conflict_count} 条、
              不存在 {batchResult.not_found_count} 条。失败项不计入合格数，也不会进入启用状态。
            </div>
            {batchResult.items.map((item) => (
              <article
                key={item.dataset_code}
                className={`profile-card ${item.state === 'profiled' && item.qualified ? 'ok' : 'fail'}`}
              >
                <div className="profile-head">
                  <span className="profile-title">{item.dataset_code}</span>
                  {item.state === 'profiled' && (
                    <Badge
                      text={item.qualified ? `合格 v${item.version}` : `不合格 v${item.version}`}
                      tone={item.qualified ? 'ok' : 'fail'}
                    />
                  )}
                  {item.state === 'conflict' && <Badge text="版本冲突作废" tone="warn" />}
                  {item.state === 'not_found' && <Badge text="数据集不存在" tone="muted" />}
                  <span className="profile-meta">{item.message}</span>
                  {item.profile && (
                    <button className="link" type="button" onClick={() => void openHistory(item.dataset_code)}>
                      {historyCode === item.dataset_code ? '收起历史版本' : '查看历史版本'}
                    </button>
                  )}
                </div>
                {item.profile && (
                  <>
                    <ul className="check-list">
                      {item.profile.checks.map((check) => (
                        <li key={check.key} className={check.passed ? 'check-pass' : 'check-fail'}>
                          {check.passed ? '✓ ' : '✗ '}{check.detail}
                        </li>
                      ))}
                    </ul>
                    <p className="profile-meta" style={{ marginTop: 6 }}>
                      来源表名：{item.profile.snapshot['来源表名']} ｜
                      刷新时间：{item.profile.snapshot['最近刷新']} ｜
                      提交人：{item.profile.submitted_by} ｜ 提交时间：{item.profile.submitted_at}
                    </p>
                  </>
                )}
                {historyCode === item.dataset_code && (
                  <div className="history-box">
                    {historyItems.map((history) => (
                      <div key={history.version} className="history-item">
                        v{history.version} · {history.qualified ? '合格' : '不合格'} ·
                        {' '}{history.submitted_by} · {history.submitted_at}
                        {history.is_current ? '（当前版本）' : '（历史版本）'}
                        {!history.qualified && history.reasons.length > 0 ? ` · 原因：${history.reasons.join('；')}` : ''}
                      </div>
                    ))}
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </div>

      <table className="data-table">
        <thead>
          <tr>
            <th className="checkbox-cell">
              <input
                type="checkbox"
                checked={rows.length > 0 && selected.size === rows.length}
                onChange={() => toggleSelectAll()}
                aria-label="全选"
              />
            </th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>启用资格（当前画像）</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const code = String(row['数据集编号'])
            const profile = profileMap.get(code)
            return (
              <tr key={String(row.id)}>
                <td className="checkbox-cell">
                  <input
                    type="checkbox"
                    checked={selected.has(code)}
                    onChange={() => toggleSelect(code)}
                    aria-label={`选择 ${code}`}
                  />
                </td>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td className="dep-cell">
                  {profile ? (
                    <>
                      <Badge
                        text={profile.qualified ? `合格 v${profile.version}` : `不合格 v${profile.version}`}
                        tone={profile.qualified ? 'ok' : 'fail'}
                      />
                      {!profile.qualified && (
                        <button className="link" type="button" style={{ marginLeft: 6 }} onClick={() => void openHistory(code)}>
                          原因
                        </button>
                      )}
                      {row['停用原因'] && !profile.qualified && (
                        <p className="reason-text">{String(row['停用原因'])}</p>
                      )}
                    </>
                  ) : (
                    <Badge text="未画像" tone="muted" />
                  )}
                  {historyCode === code && (
                    <div className="history-box">
                      {historyItems.map((history) => (
                        <div key={history.version} className="history-item">
                          v{history.version} · {history.qualified ? '合格' : '不合格'} · {history.submitted_at}
                          {history.is_current ? '（当前版本）' : '（历史版本）'}
                        </div>
                      ))}
                    </div>
                  )}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无分析数据集数据</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析数据集记录，已勾选 {selected.size} 条</span>
        <span>
          {infoMessage && <span className="success-text" style={{ marginRight: 12 }}>{infoMessage}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
