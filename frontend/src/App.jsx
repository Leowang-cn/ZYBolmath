import { useDeferredValue, useEffect, useRef, useState } from 'react'
import { Virtuoso } from 'react-virtuoso'
import {
  ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, ChevronLeft, ChevronRight,
  Clipboard, Edit3, Expand, ImagePlus, LockKeyhole, LogOut, RotateCcw, Save,
  Search, Trash2, Upload, X,
} from 'lucide-react'

const FILTER_COLUMNS = ['A', 'B', 'C', 'D', 'G', 'H', 'L', 'M', 'N', 'O', 'P']
const optionCollator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: options.body instanceof FormData ? options.headers : { 'Content-Type': 'application/json', ...options.headers },
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || '请求失败')
  return payload
}

function App() {
  const [data, setData] = useState(null)
  const [authenticated, setAuthenticated] = useState(false)
  const [filters, setFilters] = useState({})
  const [query, setQuery] = useState('')
  const [loginOpen, setLoginOpen] = useState(false)
  const [viewer, setViewer] = useState(null)
  const [notice, setNotice] = useState('')
  const deferredQuery = useDeferredValue(query.trim().toLowerCase())

  const load = async () => {
    try {
      const [records, auth] = await Promise.all([api('/api/records'), api('/api/auth/status')])
      setData(records)
      setAuthenticated(auth.authenticated)
    } catch (error) {
      setNotice(error.message)
    }
  }

  useEffect(() => { load() }, [])

  const records = (data?.records || []).filter((record) => {
    if (deferredQuery && !`${record.searchText} ${record.values.I} ${record.values.J} ${record.values.K}`.toLowerCase().includes(deferredQuery)) return false
    return FILTER_COLUMNS.every((column) => !filters[column] || record.values[column] === filters[column])
  })

  const options = Object.fromEntries(FILTER_COLUMNS.map((column) => [
    column,
    [...new Set((data?.records || []).map((record) => record.values[column]).filter((value) => value && value !== data?.headers[column]))]
      .sort(optionCollator.compare),
  ]))

  const updateRecord = (rowNumber, updater) => {
    setData((current) => ({ ...current, records: current.records.map((record) => record.rowNumber === rowNumber ? updater(record) : record) }))
  }

  const openViewer = (rowNumber, imageIndex = 0) => {
    const recordIndex = records.findIndex((record) => record.rowNumber === rowNumber)
    if (recordIndex >= 0) setViewer({ recordIndex, imageIndex })
  }

  if (!data) return <div className="loading"><span className="loading-mark">π</span><p>{notice || '正在载入题库…'}</p></div>

  return (
    <main className="app-shell">
      <FilterBar
        headers={data.headers} filters={filters} setFilters={setFilters} options={options}
        query={query} setQuery={setQuery} total={data.records.length} shown={records.length}
        authenticated={authenticated} onLogin={() => setLoginOpen(true)}
        onLogout={async () => { await api('/api/auth/logout', { method: 'POST' }); setAuthenticated(false) }}
      />
      {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice('')} aria-label="关闭"><X size={16} /></button></div>}
      <section className="list-area">
        {records.length ? (
          <Virtuoso
            data={records}
            overscan={700}
            increaseViewportBy={{ top: 300, bottom: 700 }}
            itemContent={(_, record) => (
              <RecordCard
                record={record} headers={data.headers} columns={data.visibleColumns}
                authenticated={authenticated} onUpdate={updateRecord} onOpenViewer={openViewer}
                onNotice={setNotice}
              />
            )}
          />
        ) : <div className="empty"><Search size={28} /><p>没有符合当前条件的数据</p><button onClick={() => { setFilters({}); setQuery('') }}>清除筛选</button></div>}
      </section>
      {loginOpen && <LoginDialog onClose={() => setLoginOpen(false)} onSuccess={() => { setAuthenticated(true); setLoginOpen(false) }} />}
      {viewer && <Viewer records={records} viewer={viewer} setViewer={setViewer} headers={data.headers} />}
    </main>
  )
}

function FilterBar({ headers, filters, setFilters, options, query, setQuery, total, shown, authenticated, onLogin, onLogout }) {
  const active = Object.values(filters).filter(Boolean).length + (query ? 1 : 0)
  return (
    <header className="filter-bar">
      <div className="brand"><div className="brand-mark">π</div><div><strong>奥数大纲题库</strong><span>{shown} / {total} 条</span></div></div>
      <div className="filters">
        <label className="search-field"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索题目或字段" /></label>
        {FILTER_COLUMNS.map((column) => (
          <label className="select-field" key={column}>
            <span>{headers[column]}</span>
            <select value={filters[column] || ''} onChange={(event) => setFilters((value) => ({ ...value, [column]: event.target.value }))}>
              <option value="">全部</option>
              {options[column].map((option) => <option key={option} value={option}>{option}</option>)}
            </select>
          </label>
        ))}
      </div>
      <div className="header-actions">
        {active > 0 && <button className="icon-command" onClick={() => { setFilters({}); setQuery('') }} title="清除筛选"><RotateCcw size={17} /><span>{active}</span></button>}
        <button className={authenticated ? 'auth-button active' : 'auth-button'} onClick={authenticated ? onLogout : onLogin}>
          {authenticated ? <LogOut size={16} /> : <LockKeyhole size={16} />}{authenticated ? '退出编辑' : '编辑登录'}
        </button>
      </div>
    </header>
  )
}

function RecordCard({ record, headers, columns, authenticated, onUpdate, onOpenViewer, onNotice }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(record.values)
  const [uploadTarget, setUploadTarget] = useState(null)
  const fileInput = useRef(null)

  useEffect(() => { if (!editing) setDraft(record.values) }, [record.values, editing])

  const saveFields = async () => {
    try {
      await api(`/api/records/${record.rowNumber}`, { method: 'PATCH', body: JSON.stringify({ values: draft }) })
      onUpdate(record.rowNumber, (value) => ({ ...value, values: draft }))
      setEditing(false)
      onNotice('修改已保存')
    } catch (error) { onNotice(error.message) }
  }

  const upload = async (file, action = 'append', index = -1) => {
    if (!file?.type.startsWith('image/')) return onNotice('剪贴板或文件中没有可用图片')
    const form = new FormData()
    form.append('image', file, file.name || `clipboard-${Date.now()}.png`)
    form.append('action', action)
    form.append('index', String(index))
    try {
      const result = await api(`/api/records/${record.rowNumber}/images`, { method: 'POST', body: form })
      onUpdate(record.rowNumber, (value) => ({ ...value, images: result.images }))
      onNotice(action === 'replace' ? '图片已替换' : '图片已添加')
    } catch (error) { onNotice(error.message) }
  }

  const saveImages = async (images) => {
    try {
      const result = await api(`/api/records/${record.rowNumber}/images`, {
        method: 'PUT', body: JSON.stringify({ assetHashes: images.map((image) => image.hash) }),
      })
      onUpdate(record.rowNumber, (value) => ({ ...value, images: result.images }))
    } catch (error) { onNotice(error.message) }
  }

  const moveImage = (index, direction) => {
    const images = [...record.images]
    const target = index + direction
    if (target < 0 || target >= images.length) return
    ;[images[index], images[target]] = [images[target], images[index]]
    saveImages(images)
  }

  const handlePaste = (event) => {
    if (!authenticated) return
    const image = [...event.clipboardData.items].find((item) => item.type.startsWith('image/'))?.getAsFile()
    if (image) { event.preventDefault(); upload(image) }
  }

  return (
    <article className="record" onPaste={handlePaste} tabIndex={authenticated ? 0 : undefined}>
      <aside className="meta-panel">
        <div className="record-heading"><span className="row-index">#{record.rowNumber}</span><h2>{record.title}</h2></div>
        <div className="field-list">
          {columns.map((column) => (
            <label className="field" key={column}>
              <span>{headers[column]}</span>
              {editing ? <textarea rows={Math.min(4, Math.max(1, Math.ceil((draft[column]?.length || 0) / 22)))} value={draft[column] || ''} onChange={(event) => setDraft((value) => ({ ...value, [column]: event.target.value }))} /> : <p>{record.values[column] || <i>—</i>}</p>}
            </label>
          ))}
        </div>
        {authenticated && <div className="edit-actions">
          {editing ? <><button className="primary" onClick={saveFields}><Save size={15} />保存</button><button onClick={() => setEditing(false)}><X size={15} />取消</button></> : <button onClick={() => setEditing(true)}><Edit3 size={15} />编辑字段</button>}
        </div>}
      </aside>
      <section className="examples">
        <div className="examples-head"><div><span>示例题目</span><small>{record.images.length ? `${record.images.length} 张图片` : '文本内容'}</small></div>
          {authenticated && <div className="image-actions">
            <button onClick={() => { setUploadTarget(null); fileInput.current.click() }}><Upload size={15} />上传</button>
            <span title="聚焦本区域后，可直接粘贴剪贴板图片"><Clipboard size={15} />可粘贴</span>
            <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden onChange={(event) => { upload(event.target.files[0], uploadTarget === null ? 'append' : 'replace', uploadTarget ?? -1); event.target.value = '' }} />
          </div>}
        </div>
        {record.values.K && <button className="example-text" onClick={() => onOpenViewer(record.rowNumber)} title="全屏查看文本示例">{record.values.K}<Expand size={17} /></button>}
        <div className="image-stack">
          {record.images.map((image, index) => <div className="image-frame" key={`${image.hash}-${index}`}>
            <button className="open-image" onClick={() => onOpenViewer(record.rowNumber, index)} title="全屏放映"><img src={image.url} loading="lazy" alt={`${record.title} 示例题 ${index + 1}`} /><span><Expand size={17} /></span></button>
            {authenticated && <div className="image-toolbar">
              <button disabled={index === 0} onClick={() => moveImage(index, -1)} title="上移"><ArrowUp size={16} /></button>
              <button disabled={index === record.images.length - 1} onClick={() => moveImage(index, 1)} title="下移"><ArrowDown size={16} /></button>
              <button onClick={() => { setUploadTarget(index); fileInput.current.click() }} title="替换图片"><ImagePlus size={16} /></button>
              <button className="danger" onClick={() => saveImages(record.images.filter((_, itemIndex) => itemIndex !== index))} title="删除图片"><Trash2 size={16} /></button>
            </div>}
          </div>)}
          {!record.images.length && !record.values.K && <div className="no-example">当前没有示例题目</div>}
        </div>
        <footer className="attachments"><InfoField label={headers.I} value={record.values.I} /><InfoField label={headers.J} value={record.values.J} /></footer>
      </section>
    </article>
  )
}

function InfoField({ label, value }) {
  return <div><span>{label}</span><p>{value || '—'}</p></div>
}

function LoginDialog({ onClose, onSuccess }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const submit = async (event) => {
    event.preventDefault()
    try { await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ password }) }); onSuccess() }
    catch (reason) { setError(reason.message) }
  }
  return <div className="dialog-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="login-dialog" onSubmit={submit}>
    <button type="button" className="dialog-close" onClick={onClose} aria-label="关闭"><X size={18} /></button>
    <LockKeyhole size={26} /><h2>进入编辑模式</h2><p>输入编辑密码后可修改字段和示例题图片。</p>
    <input autoFocus type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="编辑密码" />
    {error && <div className="form-error">{error}</div>}<button className="primary" type="submit"><Check size={16} />确认</button>
  </form></div>
}

function Viewer({ records, viewer, setViewer, headers }) {
  const record = records[viewer.recordIndex]
  const images = record.images
  const hasTextOnly = !images.length && record.values.K
  const currentImage = images[viewer.imageIndex]

  const shiftRecord = (direction) => {
    const nextIndex = viewer.recordIndex + direction
    if (nextIndex >= 0 && nextIndex < records.length) setViewer({ recordIndex: nextIndex, imageIndex: 0 })
  }
  const shiftImage = (direction) => {
    const next = viewer.imageIndex + direction
    if (next >= 0 && next < images.length) setViewer({ ...viewer, imageIndex: next })
    else shiftRecord(direction)
  }
  useEffect(() => {
    const keydown = (event) => {
      if (event.key === 'Escape') setViewer(null)
      if (event.key === 'ArrowLeft') shiftImage(-1)
      if (event.key === 'ArrowRight') shiftImage(1)
      if (event.key === 'ArrowUp') shiftRecord(-1)
      if (event.key === 'ArrowDown') shiftRecord(1)
    }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  })

  return <div className="viewer" role="dialog" aria-modal="true">
    <div className="viewer-top"><div><strong>{record.title}</strong><span>第 {viewer.recordIndex + 1} / {records.length} 条 · {images.length ? `图片 ${viewer.imageIndex + 1} / ${images.length}` : '文本示例'}</span></div><button onClick={() => setViewer(null)} aria-label="退出放映"><X size={24} /></button></div>
    <aside className="viewer-meta">
      <span className="viewer-row">原表第 {record.rowNumber} 行</span>
      {Object.entries(record.values).filter(([column, value]) => value && column !== 'K').map(([column, value]) => <div key={column}><span>{headers[column]}</span><p>{value}</p></div>)}
    </aside>
    <section className="viewer-stage">
      {currentImage ? <img src={currentImage.url} alt={`${record.title} 示例题`} /> : hasTextOnly ? <div className="viewer-text">{record.values.K}</div> : <div className="viewer-empty">当前数据没有示例题目</div>}
      <button className="viewer-arrow left" onClick={() => shiftImage(-1)} disabled={viewer.recordIndex === 0 && viewer.imageIndex === 0} aria-label="上一个"><ChevronLeft size={34} /></button>
      <button className="viewer-arrow right" onClick={() => shiftImage(1)} disabled={viewer.recordIndex === records.length - 1 && viewer.imageIndex >= images.length - 1} aria-label="下一个"><ChevronRight size={34} /></button>
    </section>
    <div className="viewer-nav"><button onClick={() => shiftRecord(-1)} disabled={viewer.recordIndex === 0}><ArrowLeft size={17} />上一条数据</button><button onClick={() => shiftRecord(1)} disabled={viewer.recordIndex === records.length - 1}>下一条数据<ArrowRight size={17} /></button></div>
  </div>
}

export default App