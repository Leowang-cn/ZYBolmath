import { useDeferredValue, useEffect, useRef, useState } from 'react'
import { Virtuoso } from 'react-virtuoso'
import { LEVEL_OPTIONS, matchesLevels, toggleLevel } from './levelFilter'
import {
  ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, ChevronLeft, ChevronRight,
  Clipboard, Edit3, Expand, ImagePlus, LockKeyhole, LogOut, RotateCcw, Save,
  Search, Trash2, Upload, X,
} from 'lucide-react'

const FILTER_COLUMNS = ['A', 'B', 'C', 'D', 'G', 'H', 'L', 'M']
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
  const [loading, setLoading] = useState(true)
  const [importNeeded, setImportNeeded] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [filters, setFilters] = useState({})
  const [query, setQuery] = useState('')
  const [loginOpen, setLoginOpen] = useState(false)
  const [dataImportOpen, setDataImportOpen] = useState(false)
  const [viewer, setViewer] = useState(null)
  const [notice, setNotice] = useState('')
  const deferredQuery = useDeferredValue(query.trim().toLowerCase())

  const load = async () => {
    setLoading(true)
    try {
      const [health, auth] = await Promise.all([api('/api/health'), api('/api/auth/status')])
      setAuthenticated(auth.authenticated)
      setImportNeeded(!health.dataReady)
      if (health.dataReady) {
        setData(await api('/api/records'))
      } else {
        setData(null)
      }
      setNotice('')
    } catch (error) {
      setNotice(error.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const records = (data?.records || []).filter((record) => {
    if (deferredQuery && !`${record.searchText} ${record.values.I} ${record.values.J} ${record.values.K}`.toLowerCase().includes(deferredQuery)) return false
    return matchesLevels(record.values, filters.levels) && FILTER_COLUMNS.every((column) => !filters[column] || record.values[column] === filters[column])
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

  if (loading) return <div className="loading"><span className="loading-mark">π</span><p>正在载入题库…</p></div>
  if (importNeeded) return <DataImport authenticated={authenticated} onAuthenticated={() => setAuthenticated(true)} onComplete={load} />
  if (!data) return <div className="loading"><span className="loading-mark">π</span><p>{notice || '题库载入失败'}</p><button onClick={load}>重新加载</button></div>

  return (
    <main className="app-shell">
      <FilterBar
        headers={data.headers} filters={filters} setFilters={setFilters} options={options}
        query={query} setQuery={setQuery} total={data.records.length} shown={records.length}
        authenticated={authenticated} onLogin={() => setLoginOpen(true)}
        onDataImport={() => setDataImportOpen(true)}
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
      {dataImportOpen && <div className="dialog-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setDataImportOpen(false)}>
        <DataImport
          authenticated
          modal
          onClose={() => setDataImportOpen(false)}
          onComplete={async () => { setDataImportOpen(false); await load(); setNotice('题库更新完成') }}
        />
      </div>}
      {viewer && <Viewer records={records} viewer={viewer} setViewer={setViewer} headers={data.headers} />}
    </main>
  )
}

function DataImport({ authenticated, onAuthenticated = () => {}, onComplete, modal = false, onClose }) {
  const [password, setPassword] = useState('')
  const [file, setFile] = useState(null)
  const [progress, setProgress] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const authenticate = async () => {
    if (authenticated) return true
    await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ password }) })
    onAuthenticated()
    return true
  }

  const uploadArchive = (archive) => new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    const form = new FormData()
    form.append('archive', archive)
    request.open('POST', '/api/data/import')
    request.withCredentials = true
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) setProgress(Math.round((event.loaded / event.total) * 100))
    }
    request.onload = () => {
      let payload = {}
      try { payload = JSON.parse(request.responseText) } catch { /* 后端异常页 */ }
      if (request.status >= 200 && request.status < 300) resolve(payload)
      else reject(new Error(payload.error || `上传失败（HTTP ${request.status}）`))
    }
    request.onerror = () => reject(new Error('网络中断，数据包上传失败'))
    request.send(form)
  })

  const submit = async (event) => {
    event.preventDefault()
    if (!file) return setError('请选择 ZYBolmath-data.tar.gz')
    setBusy(true)
    setError('')
    setProgress(0)
    try {
      await authenticate()
      await uploadArchive(file)
      await onComplete()
    } catch (reason) {
      setError(reason.message)
    } finally {
      setBusy(false)
    }
  }

  const panel = <form className="data-import-panel" onSubmit={submit}>
      {modal && <button className="dialog-close" type="button" onClick={onClose} aria-label="关闭"><X size={19} /></button>}
      <div className="brand-mark">π</div>
      <div><span className="setup-label">{modal ? '管理员操作' : '首次初始化'}</span><h1>{modal ? '更新奥数题库' : '导入奥数题库'}</h1></div>
      <p>{modal ? '导入新版数据包。网页中修改的字段和图片会继续保留。' : '当前服务器尚无题库数据。使用管理员密码验证后，导入完整数据压缩包。'}</p>
      {!authenticated && <label className="setup-field"><span>管理员密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>}
      <label className="archive-picker">
        <Upload size={22} />
        <span>{file ? file.name : '选择数据压缩包'}</span>
        <small>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : '仅支持 .tar.gz，最大 256 MB'}</small>
        <input type="file" accept=".gz,.tgz,application/gzip" onChange={(event) => setFile(event.target.files[0] || null)} disabled={busy} />
      </label>
      {busy && <div className="upload-progress"><div style={{ width: `${progress}%` }} /><span>{progress < 100 ? `正在上传 ${progress}%` : '正在校验并安装数据…'}</span></div>}
      {error && <div className="form-error" role="alert">{error}</div>}
      <button className="primary import-submit" type="submit" disabled={busy || !file}><Upload size={17} />{busy ? '正在导入' : modal ? '验证并更新' : '验证并导入'}</button>
    </form>
  return modal ? panel : <main className="data-import-page">{panel}</main>
}

function FilterBar({ headers, filters, setFilters, options, query, setQuery, total, shown, authenticated, onLogin, onDataImport, onLogout }) {
  const active = Object.values(filters).filter((value) => Array.isArray(value) ? value.length > 0 : Boolean(value)).length + (query ? 1 : 0)
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
        <div className="level-filter" role="group" aria-label="层级">
          <span className="level-filter-title">层级</span>
          {LEVEL_OPTIONS.map((option) => <label key={option}>
            <input type="checkbox" checked={option === '不限' ? !filters.levels?.length : (filters.levels || []).includes(option)} onChange={() => setFilters((value) => ({ ...value, levels: toggleLevel(value.levels, option) }))} />
            <span>{option}</span>
          </label>)}
        </div>
      </div>
      <div className="header-actions">
        {active > 0 && <button className="icon-command" onClick={() => { setFilters({}); setQuery('') }} title="清除筛选"><RotateCcw size={17} /><span>{active}</span></button>}
        {authenticated && <button className="auth-button" onClick={onDataImport}><Upload size={16} />更新题库</button>}
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
          <div className="knowledge-details"><InfoField label={headers.I} value={record.values.I} /><InfoField label={headers.J} value={record.values.J} /></div>
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