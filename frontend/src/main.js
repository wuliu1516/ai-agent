import './style.css'

const number = new Intl.NumberFormat('zh-CN')
const splitLabels = {
  development: '开发集',
  validation: '验证集',
  test: '测试集',
}
const splitDescriptions = {
  development: '模型开发与调试',
  validation: '模型选择与验证',
  test: '基准评测',
}

const state = {
  view: 'samples',
  split: 'development',
  overview: null,
  samplePage: 1,
  samplePageSize: 25,
  sampleQuery: '',
  databaseFilter: '',
  resultStatus: 'all',
  sampleResponse: null,
  selectedSampleIndex: null,
  sample: null,
  databaseSearch: '',
  databases: [],
  selectedDatabaseId: '',
  database: null,
  selectedTable: '',
  tableRows: null,
  tableOffset: 0,
  databaseCount: 0,
  resultQuery: '',
  resultDatabaseFilter: '',
  resultSplit: '',
  resultPage: 1,
  resultPageSize: 30,
  results: null,
  experimentConfig: null,
  experiments: [],
  selectedRunId: '',
  experiment: null,
  experimentItems: null,
  experimentItemsKey: '',
  experimentItemStatus: 'all',
  experimentItemQuery: '',
  experimentItemDatabase: '',
  experimentItemPage: 1,
  experimentItemPageSize: 30,
  selectedExperimentItemId: null,
  experimentItem: null,
  fullRunSplit: 'test',
  fullRunAId: '',
  fullRunBId: '',
}

const app = document.querySelector('#app')
let sampleSearchTimer
let databaseSearchTimer
let resultSearchTimer
let experimentSearchTimer
let experimentStartPending = false
let experimentHistoryLoading = false
let experimentHistoryLoaded = false
const requestSequence = { samples: 0, sample: 0, databases: 0, database: 0, table: 0, results: 0, experiments: 0, experiment: 0, experimentItems: 0, experimentItem: 0 }
let experimentPollTimer
let experimentPollBusy = false
let experimentPollFailures = 0
let fullRunCategoryRequestId = 0

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[char])
}

function fmt(value) {
  return number.format(Number(value || 0))
}

function icon(name, size = 18) {
  const paths = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    rows: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>',
    chart: '<path d="M4 19V5M4 19h17"/><path d="m7 15 4-4 3 2 6-7"/><path d="M17 6h3v3"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    chevron: '<path d="m9 18 6-6-6-6"/>',
    down: '<path d="m7 10 5 5 5-5"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    refresh: '<path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.55-2.6L20 12M4 12l2.85 5.6A7 7 0 0 0 18.4 15"/>',
    arrow: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.07 0l3-3A5 5 0 0 0 13 2.93l-1.72 1.72"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3A5 5 0 0 0 11 21.07l1.72-1.72"/>',
    table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16M15 10v10"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
  }
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.grid}</svg>`
}

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  let payload
  try {
    payload = await response.json()
  } catch {
    payload = {}
  }
  if (!response.ok) {
    throw new Error(payload.detail || `请求失败（${response.status}）`)
  }
  return payload
}

function renderShell() {
  app.innerHTML = `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark"><span></span><span></span><span></span></div>
          <div><strong>QueryLab</strong><small>NL2SQL WORKSPACE</small></div>
        </div>
        <div class="side-label">工作台</div>
        <nav class="primary-nav">
          <button class="nav-item" data-view="samples">${icon('grid')}<span>样本与金标</span></button>
          <button class="nav-item" data-view="databases">${icon('database')}<span>数据库浏览</span></button>
          <button class="nav-item" data-view="results">${icon('chart')}<span>批量运行审阅</span><span id="result-nav-count" class="nav-count"></span></button>
        </nav>
        <div class="side-divider"></div>
        <div class="dataset-heading"><div class="side-label">数据集划分</div><span class="dataset-count">03</span></div>
        <nav id="dataset-navigation" class="dataset-nav"></nav>
        <div class="sidebar-bottom">
          <div class="source-badge"><span class="source-dot"></span><div><strong>本地数据源</strong><small>SQLite · 只读浏览</small></div></div>
          <div class="sidebar-footnote">CSpider · Chinese Text-to-SQL</div>
        </div>
      </aside>
      <main class="main-shell">
        <header class="topbar">
          <div class="breadcrumb"><span>工作台</span>${icon('chevron', 14)}<strong id="breadcrumb-current">样本与金标</strong></div>
          <div class="top-actions">
            <div id="backend-status" class="connection-pill is-loading"><span></span><label>连接数据源…</label></div>
            <button class="icon-button" data-action="refresh" aria-label="刷新数据" title="刷新数据">${icon('refresh')}</button>
            <div class="avatar">QL</div>
          </div>
        </header>
        <div class="main-scroll"><div class="page-wrap">
          <section id="overview-stats" class="stats-grid"></section>
          <section id="page-content"></section>
          <footer class="page-footer"><span>CSpider NL2SQL 工作台</span><span>数据文件只读 · 生成结果单独保存</span></footer>
        </div></div>
      </main>
    </div>
    <div id="toast" class="toast" role="status"></div>
  `
}

function updateShell() {
  const activeView = state.view
  const viewNames = { samples: '样本与金标', databases: '数据库浏览', results: '批量运行审阅' }
  document.querySelector('#breadcrumb-current').textContent = viewNames[activeView]
  document.querySelectorAll('.nav-item[data-view]').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === activeView)
  })
  document.querySelector('#dataset-navigation').innerHTML = (state.overview?.datasets || []).map((dataset) => `
    <button class="dataset-nav-item ${state.split === dataset.key ? 'active' : ''}" data-split="${esc(dataset.key)}">
      <span class="dataset-marker marker-${esc(dataset.key)}"></span>
      <span class="dataset-nav-copy"><strong>${esc(dataset.label)}</strong><small>${esc(dataset.description)}</small></span>
      <span class="dataset-nav-total">${fmt(dataset.sample_count)}</span>
    </button>
  `).join('')
  document.querySelector('#result-nav-count').textContent = state.overview?.result_count ? fmt(state.overview.result_count) : ''
  const stats = state.overview
  document.querySelector('#overview-stats').innerHTML = stats ? `
    <article class="stat-card stat-purple"><div class="stat-top"><span>数据集样本</span><span class="stat-icon">${icon('rows')}</span></div><strong>${fmt(stats.total_samples)}</strong><small>3 个划分 · ${fmt(stats.datasets.length)} 个数据集</small></article>
    <article class="stat-card stat-blue"><div class="stat-top"><span>数据库</span><span class="stat-icon">${icon('database')}</span></div><strong>${fmt(stats.database_count)}</strong><small>跨域 SQLite 数据库</small></article>
    <article class="stat-card stat-teal"><div class="stat-top"><span>数据表</span><span class="stat-icon">${icon('table')}</span></div><strong>${fmt(stats.table_count)}</strong><small>可查看 schema 与表数据</small></article>
    <article class="stat-card stat-orange"><div class="stat-top"><span>生成记录</span><span class="stat-icon">${icon('chart')}</span></div><strong>${fmt(stats.result_count)}</strong><small>已保存的 NL2SQL 输出</small></article>
  ` : ''
}

function setConnection(online, label = '') {
  const element = document.querySelector('#backend-status')
  if (!element) return
  element.classList.toggle('is-online', online)
  element.classList.toggle('is-offline', !online)
  element.classList.remove('is-loading')
  element.querySelector('label').textContent = label || (online ? '后端 API 已连接' : '后端 API 未连接')
}

function showToast(message, kind = 'success') {
  const toast = document.querySelector('#toast')
  toast.textContent = message
  toast.className = `toast is-visible ${kind}`
  clearTimeout(showToast.timer)
  showToast.timer = setTimeout(() => { toast.className = 'toast' }, 2600)
}

function setPageLoading(message = '正在读取数据…') {
  document.querySelector('#page-content').innerHTML = `<div class="loading-state"><span class="spinner"></span><span>${esc(message)}</span></div>`
}

function renderView() {
  if (state.view !== 'results') clearTimeout(experimentPollTimer)
  updateShell()
  if (state.view === 'samples') renderSamplesView()
  if (state.view === 'databases') renderDatabasesView()
  if (state.view === 'results') renderResultsView()
}

function renderSamplesView() {
  const dataset = state.overview?.datasets.find((item) => item.key === state.split)
  document.querySelector('#page-content').innerHTML = `
    <div class="page-heading">
      <div><div class="eyebrow">CSPIDER DATASET</div><h1>样本与金标</h1><p>参考 SQL 取自样本 JSON.query；本划分独立 gold 文件的 DB ID 同序号匹配 ${fmt(dataset?.gold_file_alignment?.database_id_matches)} / ${fmt(dataset?.gold_file_alignment?.rows)} 行。</p></div>
      <div class="heading-chip"><span class="dataset-marker marker-${esc(state.split)}"></span><div><strong>${esc(splitLabels[state.split])}</strong><small>${fmt(dataset?.sample_count)} 条样本</small></div>${icon('down', 16)}</div>
    </div>
    <div class="toolbar sample-toolbar">
      <label class="search-field">${icon('search')}<input id="sample-search" type="search" placeholder="搜索问题、SQL 或数据库…" value="${esc(state.sampleQuery)}" autocomplete="off" /><kbd>⌕</kbd></label>
      <label class="filter-field"><span>数据库</span><input id="database-filter" type="search" placeholder="例如 concert_singer" value="${esc(state.databaseFilter)}" autocomplete="off" /></label>
      <label class="select-field"><span>结果状态</span><select id="result-status"><option value="all" ${state.resultStatus === 'all' ? 'selected' : ''}>全部样本</option><option value="pending" ${state.resultStatus === 'pending' ? 'selected' : ''}>待生成</option><option value="generated" ${state.resultStatus === 'generated' ? 'selected' : ''}>已有生成结果</option></select></label>
    </div>
    <div class="sample-workspace">
      <section class="sample-list-panel panel">
        <div class="panel-heading list-heading"><div><strong>样本列表</strong><span id="sample-count-label" class="subtle-count"></span></div><span class="list-sort">按原始顺序</span></div>
        <div id="sample-list" class="sample-list-body"><div class="list-loading"><span class="spinner small"></span>读取样本中</div></div>
        <div id="sample-pagination" class="pagination"></div>
      </section>
      <section id="sample-detail" class="sample-detail-column"><div class="detail-placeholder panel"><span class="placeholder-icon">${icon('rows', 24)}</span><strong>选择一条样本</strong><span>问题、金标 SQL 与生成结果会显示在这里</span></div></section>
    </div>
  `
  loadSamples().catch((error) => showLoadError('#sample-list', error))
}

function renderSampleList() {
  const response = state.sampleResponse
  const list = document.querySelector('#sample-list')
  const pagination = document.querySelector('#sample-pagination')
  const count = document.querySelector('#sample-count-label')
  if (!list || !response) return
  count.textContent = `${fmt(response.total)} 条`
  if (!response.items.length) {
    list.innerHTML = `<div class="empty-inline"><span>${icon('search', 20)}</span><strong>没有找到匹配样本</strong><small>调整搜索词或筛选条件后再试。</small></div>`
    pagination.innerHTML = ''
    return
  }
  list.innerHTML = response.items.map((item) => `
    <button class="sample-row ${item.index === state.selectedSampleIndex ? 'active' : ''}" data-sample-index="${item.index}">
      <div class="sample-row-top"><span class="sample-number">#${String(item.sample_no).padStart(4, '0')}</span><span class="db-mini">${esc(item.db_id)}</span></div>
      <span class="sample-question">${esc(item.question)}</span>
      <span class="sample-row-bottom"><span>${item.latest_result ? '<i class="tiny-status has-result"></i>已生成' : '<i class="tiny-status"></i>待生成'}</span><span>${icon('chevron', 14)}</span></span>
    </button>
  `).join('')
  const lastPage = Math.max(response.pages, 1)
  pagination.innerHTML = `
    <span>第 ${response.page} / ${lastPage} 页</span>
    <div><button data-sample-page="prev" ${response.page <= 1 ? 'disabled' : ''} aria-label="上一页">‹</button><button data-sample-page="next" ${response.page >= lastPage ? 'disabled' : ''} aria-label="下一页">›</button></div>
  `
}

function renderSqlPanel(title, content, kind, action = '') {
  return `
    <section class="sql-card ${kind}">
      <div class="sql-card-heading"><div><span class="sql-dot"></span><strong>${esc(title)}</strong></div>${action}</div>
      <pre><code>${esc(content || '暂无 SQL')}</code></pre>
    </section>
  `
}

function renderSampleDetail() {
  const sample = state.sample
  const container = document.querySelector('#sample-detail')
  if (!container || !sample) return
  const latest = sample.latest_result
  const history = sample.result_history || []
  container.innerHTML = `
    <article class="detail-header panel">
      <div class="detail-title-row"><div><div class="eyebrow">${esc(splitLabels[sample.split])} / SAMPLE #${String(sample.sample_no).padStart(4, '0')}</div><h2>样本详情</h2></div><span class="result-pill ${latest ? 'generated' : 'pending'}"><i></i>${latest ? '已有生成结果' : '待生成'}</span></div>
      <div class="detail-meta"><button class="meta-chip database-chip" data-open-database="${esc(sample.db_id)}">${icon('database', 15)}<span>${esc(sample.db_id)}</span>${icon('arrow', 14)}</button><span class="meta-chip">${icon('layers', 15)}${esc(splitLabels[sample.split])}</span><span class="meta-text">原始序号 ${sample.index}</span></div>
      <div class="question-box"><span class="question-label">自然语言问题</span><p>${esc(sample.question)}</p></div>
      ${renderSqlPanel('金标 SQL', sample.gold_sql, 'gold-sql', `<span class="sql-source-tag" title="${esc(sample.gold_sql_source)}">JSON.query · Spider gold</span><button class="small-action" data-copy-target="gold">${icon('copy', 14)}复制</button>`)}
      <details class="query-source"><summary>查看结构化 SQL 标注</summary><pre>${esc(JSON.stringify(sample.sql_structure || {}, null, 2))}</pre></details>
    </article>
    <article class="prediction-card panel">
      <div class="prediction-heading"><div><span class="prediction-icon">${icon('chart', 18)}</span><div><strong>NL2SQL 生成结果</strong><small>${latest ? `最近保存于 ${esc(formatDate(latest.created_at))}` : '等待模型输出，可先粘贴 SQL 进行记录'}</small></div></div><span class="history-count">${fmt(history.length)} 条记录</span></div>
      ${latest ? `<div class="latest-result-bar"><span class="latest-dot"></span><span>最近一次输出</span>${latest.model ? `<strong>${esc(latest.model)}</strong>` : ''}${latest.latency_ms != null ? `<small>${esc(latest.latency_ms)} ms</small>` : ''}</div>` : ''}
      <textarea id="generated-sql" class="sql-editor" spellcheck="false" placeholder="将 NL2SQL 模型生成的 SQL 粘贴到这里…">${esc(latest?.generated_sql || '')}</textarea>
      <div class="prediction-form-footer"><label class="model-field"><span>模型 / 来源</span><input id="generated-model" type="text" value="${esc(latest?.model || '')}" placeholder="例如 GPT-4.1 / local-model" maxlength="250" /></label><label class="model-field run-id-field"><span>运行标识（可选）</span><input id="generated-run-id" type="text" value="" placeholder="run-2026-01" maxlength="250" /></label><button class="primary-button" data-action="save-result">${icon('check', 16)}保存生成结果</button></div>
      ${history.length > 1 ? `<details class="history-details"><summary>查看历史输出（${history.length}）</summary><div class="history-list">${history.slice(1).map((item) => `<article><div><strong>${esc(item.model || '未标注模型')}</strong><span>${esc(formatDate(item.created_at))}</span></div><pre>${esc(item.generated_sql)}</pre></article>`).join('')}</div></details>` : ''}
    </article>
  `
}

function formatDate(value) {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function showLoadError(selector, error) {
  const container = document.querySelector(selector)
  if (container) container.innerHTML = `<div class="empty-inline"><strong>读取失败</strong><small>${esc(error.message)}</small></div>`
  showToast(error.message, 'error')
}

function showDatabaseLoadError(error) {
  for (const selector of ['#database-list', '#database-main']) {
    const container = document.querySelector(selector)
    if (container) container.innerHTML = `<div class="empty-inline"><strong>读取失败</strong><small>${esc(error.message)}</small></div>`
  }
  showToast(error.message, 'error')
}

async function loadOverview() {
  state.overview = await request('/datasets')
  updateShell()
}

async function loadSamples({ skipDetail = false } = {}) {
  const sequence = ++requestSequence.samples
  const requestedSplit = state.split
  const requestedPage = state.samplePage
  const requestedQuery = state.sampleQuery
  const requestedDatabase = state.databaseFilter
  const requestedStatus = state.resultStatus
  const params = new URLSearchParams({
    split: requestedSplit,
    page: String(requestedPage),
    page_size: String(state.samplePageSize),
    q: requestedQuery,
    db_id: requestedDatabase,
    result_status: requestedStatus,
  })
  let response
  try {
    response = await request(`/samples?${params}`)
  } catch (error) {
    if (sequence === requestSequence.samples) throw error
    return
  }
  if (sequence !== requestSequence.samples || requestedSplit !== state.split || requestedPage !== state.samplePage || requestedQuery !== state.sampleQuery || requestedDatabase !== state.databaseFilter || requestedStatus !== state.resultStatus || state.view !== 'samples') return
  state.sampleResponse = response
  const stillVisible = response.items.some((item) => item.index === state.selectedSampleIndex)
  if (!stillVisible) state.selectedSampleIndex = response.items[0]?.index ?? null
  renderSampleList()
  if (!skipDetail && state.selectedSampleIndex != null) {
    if (state.sample?.split !== state.split || state.sample?.index !== state.selectedSampleIndex) {
      await loadSampleDetail(state.selectedSampleIndex)
    } else {
      renderSampleDetail()
    }
  } else if (!response.items.length) {
    state.sample = null
    document.querySelector('#sample-detail').innerHTML = `<div class="detail-placeholder panel"><span class="placeholder-icon">${icon('rows', 24)}</span><strong>暂无匹配详情</strong><span>当前筛选条件下没有样本</span></div>`
  }
}

async function loadSampleDetail(index) {
  const sequence = ++requestSequence.sample
  const requestedSplit = state.split
  const requestedIndex = Number(index)
  state.selectedSampleIndex = requestedIndex
  state.sample = null
  const detail = document.querySelector('#sample-detail')
  if (detail) detail.innerHTML = `<div class="detail-placeholder panel"><span class="spinner"></span><strong>读取样本详情…</strong></div>`
  let sample
  try {
    sample = await request(`/samples/${encodeURIComponent(requestedSplit)}/${requestedIndex}`)
  } catch (error) {
    if (sequence === requestSequence.sample) showLoadError('#sample-detail', error)
    return
  }
  if (sequence !== requestSequence.sample || requestedSplit !== state.split || requestedIndex !== state.selectedSampleIndex || state.view !== 'samples') return
  state.sample = sample
  renderSampleList()
  renderSampleDetail()
}

function renderDatabasesView() {
  document.querySelector('#page-content').innerHTML = `
    <div class="page-heading">
      <div><div class="eyebrow">DATABASE EXPLORER</div><h1>数据库浏览</h1><p>从 ${esc(splitLabels[state.split])} 覆盖的库开始，也可以搜索全部 166 个数据库。</p></div>
      <div class="heading-chip compact-chip"><span class="dataset-marker marker-${esc(state.split)}"></span><div><strong>${esc(splitLabels[state.split])}</strong><small>覆盖数据库筛选</small></div>${icon('down', 16)}</div>
    </div>
    <div class="database-browser panel">
      <aside class="database-list-column">
        <div class="database-search-wrap"><label class="search-field compact-search">${icon('search')}<input id="database-search" type="search" placeholder="搜索数据库…" value="${esc(state.databaseSearch)}" autocomplete="off" /></label><div id="database-list-count" class="database-list-count">读取数据库中…</div></div>
        <div id="database-list" class="database-list"><div class="list-loading"><span class="spinner small"></span>读取数据库中</div></div>
      </aside>
      <section id="database-main" class="database-main"><div class="detail-placeholder"><span class="placeholder-icon">${icon('database', 24)}</span><strong>选择一个数据库</strong><span>查看 schema、字段关系和实际数据</span></div></section>
    </div>
  `
  loadDatabases().catch(showDatabaseLoadError)
}

async function loadDatabases() {
  const sequence = ++requestSequence.databases
  const requestedSplit = state.split
  const requestedSearch = state.databaseSearch
  const params = new URLSearchParams({ split: requestedSplit, q: requestedSearch, page_size: '500' })
  let response
  try {
    response = await request(`/databases?${params}`)
  } catch (error) {
    if (sequence === requestSequence.databases) throw error
    return
  }
  if (sequence !== requestSequence.databases || requestedSplit !== state.split || requestedSearch !== state.databaseSearch || state.view !== 'databases') return
  state.databases = response.items
  state.databaseCount = response.total
  document.querySelector('#database-list-count').textContent = `${fmt(response.total)} 个数据库`
  renderDatabaseList()
  const visible = response.items.some((item) => item.db_id === state.selectedDatabaseId)
  if (!visible) state.selectedDatabaseId = response.items[0]?.db_id || ''
  if (state.selectedDatabaseId) {
    if (state.database?.db_id !== state.selectedDatabaseId) await loadDatabaseDetail(state.selectedDatabaseId)
    else renderDatabaseMain()
  } else {
    state.database = null
    document.querySelector('#database-main').innerHTML = `<div class="empty-inline"><strong>没有匹配的数据库</strong><small>试试缩短搜索词。</small></div>`
  }
}

function renderDatabaseList() {
  const container = document.querySelector('#database-list')
  if (!container) return
  container.innerHTML = state.databases.map((database) => `
    <button class="database-row ${database.db_id === state.selectedDatabaseId ? 'active' : ''}" data-db-select="${esc(database.db_id)}">
      <span class="db-icon">${icon('database', 16)}</span><span class="database-row-copy"><strong>${esc(database.db_id)}</strong><small>${fmt(database.table_count)} 张表 · ${fmt(database.sample_count)} 条样本</small></span>${icon('chevron', 14)}
    </button>
  `).join('') || `<div class="empty-inline small-empty"><strong>没有匹配的数据库</strong></div>`
}

async function loadDatabaseDetail(dbId) {
  const sequence = ++requestSequence.database
  const changedDatabase = state.database?.db_id !== dbId
  state.selectedDatabaseId = dbId
  if (changedDatabase) {
    state.selectedTable = ''
    state.tableOffset = 0
    state.tableRows = null
  }
  const databaseMain = document.querySelector('#database-main')
  if (databaseMain) databaseMain.innerHTML = `<div class="loading-state"><span class="spinner"></span><span>正在读取数据库 schema…</span></div>`
  let database
  try {
    database = await request(`/databases/${encodeURIComponent(dbId)}`)
  } catch (error) {
    if (sequence === requestSequence.database) throw error
    return
  }
  if (sequence !== requestSequence.database || state.selectedDatabaseId !== dbId || state.view !== 'databases') return
  state.database = database
  const selectedStillExists = state.database.tables.some((table) => table.name === state.selectedTable)
  if (!selectedStillExists) {
    state.selectedTable = state.database.tables[0]?.name || ''
    state.tableOffset = 0
  }
  renderDatabaseList()
  await loadTableRows()
  if (sequence !== requestSequence.database || state.selectedDatabaseId !== dbId || state.view !== 'databases') return
  renderDatabaseMain()
}

async function loadTableRows() {
  const sequence = ++requestSequence.table
  if (!state.database || !state.selectedTable) {
    state.tableRows = null
    return
  }
  const dbId = state.selectedDatabaseId
  const tableName = state.selectedTable
  const offset = state.tableOffset
  const params = new URLSearchParams({ limit: '50', offset: String(state.tableOffset) })
  let result
  try {
    result = await request(`/databases/${encodeURIComponent(dbId)}/tables/${encodeURIComponent(tableName)}/rows?${params}`)
  } catch (error) {
    if (sequence === requestSequence.table) throw error
    return
  }
  if (sequence !== requestSequence.table || dbId !== state.selectedDatabaseId || tableName !== state.selectedTable || offset !== state.tableOffset || state.view !== 'databases') return
  state.tableRows = result
}

function renderDatabaseMain() {
  const database = state.database
  const container = document.querySelector('#database-main')
  if (!database || !container) return
  const selected = database.tables.find((table) => table.name === state.selectedTable)
  container.innerHTML = `
    <div class="db-detail-top"><div><div class="eyebrow">DATABASE SCHEMA</div><h2>${esc(database.db_id)}</h2><p>跨三个数据集划分关联 ${fmt(database.sample_count)} 条样本</p></div><button class="outline-button" data-action="samples-for-database" data-db="${esc(database.db_id)}">${icon('rows', 15)}查看相关样本</button></div>
    <div class="db-metrics"><div><span>数据表</span><strong>${fmt(database.tables.length)}</strong></div><div><span>关联样本</span><strong>${fmt(database.sample_count)}</strong></div>${Object.entries(database.split_counts).map(([split, count]) => `<div><span>${esc(splitLabels[split])}</span><strong>${fmt(count)}</strong></div>`).join('')}</div>
    <section class="schema-section"><div class="section-heading"><div><h3>表结构</h3><span>选择一张表查看字段与行数据</span></div><span class="schema-source">SQLite · read-only</span></div>
      <div class="schema-grid">${database.tables.map((table) => `
        <button class="schema-card ${table.name === state.selectedTable ? 'active' : ''}" data-table-select="${esc(table.name)}">
          <span class="schema-card-top">${icon('table', 16)}<strong>${esc(table.name)}</strong><small>${fmt(table.row_count)} 行</small></span>
          <span class="column-pills">${table.columns.slice(0, 5).map((column) => `<i>${esc(column.name)}</i>`).join('')}${table.columns.length > 5 ? `<i class="more-pill">+${table.columns.length - 5}</i>` : ''}</span>
        </button>
      `).join('')}</div>
    </section>
    ${selected ? renderSelectedTable(selected) : `<div class="empty-inline"><strong>这个数据库没有可浏览的用户表</strong></div>`}
  `
}

function renderSelectedTable(table) {
  const data = state.tableRows
  return `
    <section class="table-inspector">
      <div class="section-heading table-inspector-heading"><div><h3>${esc(table.name)} <span class="table-tag">${fmt(table.row_count)} 行</span></h3><span>${fmt(table.columns.length)} 个字段 · 字段类型与主外键关系</span></div><span class="preview-label">数据预览</span></div>
      <div class="column-schema-table"><div class="column-schema-head"><span>字段名</span><span>类型</span><span>约束</span><span>允许为空</span></div>${table.columns.map((column) => `
        <div class="column-schema-row"><code>${esc(column.name)}</code><span>${esc(column.type)}</span><span class="constraint-cell">${column.primary_key_order ? '<b class="key-tag pk">PK</b>' : ''}${column.foreign_key ? `<b class="key-tag fk" title="${esc(column.foreign_key.table)}.${esc(column.foreign_key.referenced_column)}">FK</b>` : ''}${!column.primary_key_order && !column.foreign_key ? '<span class="dash">—</span>' : ''}</span><span>${column.nullable ? '是' : '否'}</span></div>
      `).join('')}</div>
      <div class="data-preview-heading"><div><strong>表数据</strong><span>${data ? `展示 ${fmt(data.rows.length)} / ${fmt(data.total)} 行` : '读取中…'}</span></div><span>LIMIT 50</span></div>
      <div class="data-table-wrap">${data ? renderDataTable(data) : `<div class="loading-state compact-loading"><span class="spinner small"></span><span>读取表数据…</span></div>`}</div>
    </section>
  `
}

function renderDataTable(data) {
  if (!data.columns.length) return `<div class="empty-inline small-empty"><strong>表中没有字段</strong></div>`
  const body = data.rows.length ? data.rows.map((row) => `<tr>${row.map((value) => `<td title="${esc(value == null ? 'NULL' : value)}">${value == null ? '<span class="null-value">NULL</span>' : esc(value)}</td>`).join('')}</tr>`).join('') : `<tr><td class="no-rows" colspan="${data.columns.length}">这张表目前没有数据行</td></tr>`
  const prevOffset = Math.max(data.offset - data.limit, 0)
  const nextOffset = data.offset + data.limit
  return `<table><thead><tr>${data.columns.map((column) => `<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table>
    <div class="table-pagination"><span>偏移 ${fmt(data.offset)} · 共 ${fmt(data.total)} 行</span><div><button data-table-page="${prevOffset}" ${data.offset <= 0 ? 'disabled' : ''}>上一页</button><button data-table-page="${nextOffset}" ${nextOffset >= data.total ? 'disabled' : ''}>下一页</button></div></div>`
}

function renderResultsView() {
  document.querySelector('#page-content').innerHTML = `
    <div class="page-heading">
      <div><div class="eyebrow">BATCH REVIEW</div><h1>批量运行审阅</h1><p>每轮固定记录模型和提示词；正确、错误、无法判定的样本都能逐条查看。</p></div>
      <div class="heading-summary"><strong id="experiment-run-count-summary">${fmt(state.experiments.length)}</strong><span>个运行批次</span></div>
    </div>
    <section class="experiment-create panel">
      <div class="experiment-create-heading"><div><strong>启动批量运行</strong><small id="experiment-run-description">每条样本可选择是否提供目标题 Schema；最多 ${fmt(state.experimentConfig?.max_concurrency ?? 6)} 路并发生成；金标只用于运行后的评测。</small></div><span class="experiment-seed">固定抽样种子 42</span></div>
      <div class="experiment-create-fields">
        <label class="select-field"><span>数据集</span><select id="experiment-split">${Object.entries(splitLabels).map(([key, label]) => `<option value="${key}" ${key === 'development' ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
        <label class="model-field"><span>模型</span><input id="experiment-model" type="text" value="${esc(state.experimentConfig?.default_model || '')}" placeholder="填写已配置服务中的模型 ID" maxlength="250" /></label>
        <label class="model-field sample-limit-field"><span>样本数</span><input id="experiment-sample-limit" type="number" min="0" max="${state.experimentConfig?.max_samples || 10000}" value="20" /><small>0 表示整个划分</small></label>
        <label class="few-shot-option"><input id="experiment-few-shot" type="checkbox" checked /><span><strong>相似示例（Few-shot）</strong><small>最多附 2 个 development 示例（只含问题 + SQL）；运行 development 时自动关闭</small></span></label>
        <label class="few-shot-option"><input id="experiment-target-schema" type="checkbox" checked /><span><strong>提供目标题 Schema</strong><small>关闭后生成模型看不到目标表名、字段和外键；Few-shot 示例只有问题 + SQL</small></span></label>
        <label class="few-shot-option"><input id="experiment-query-planning" type="checkbox" /><span><strong>复杂问题先生成查询计划</strong><small>聚合/分组、比较/排名、多条件或嵌套问题触发；计划节点多调用一次模型</small></span></label>
      <button class="primary-button experiment-start-button" data-action="start-experiment" ${experimentStartPending || experimentHistoryLoading || !experimentHistoryLoaded || state.experiments.some((run) => ['queued', 'running'].includes(run.status)) || !state.experimentConfig?.provider_ready || !state.experimentConfig?.default_model ? 'disabled' : ''}>${experimentStartPending ? '<span class="spinner small"></span>正在创建批次' : `${icon('arrow', 16)}开始批量运行`}</button>
      </div>
      <div class="quality-scoring-controls">
        <label class="few-shot-option quality-scoring-toggle"><input id="experiment-quality-scoring" type="checkbox" checked /><span><strong>语义评分与低分修订</strong><small>评分器只看问题、schema、SQL 和执行证据，不看金标；低于阈值时带反馈重试</small></span></label>
        <label class="quality-number-field"><span>重试阈值</span><input id="experiment-quality-threshold" type="number" min="0" max="100" value="70" /><small>分（0–100）</small></label>
        <label class="quality-number-field"><span>最多修订次数</span><input id="experiment-quality-retries" type="number" min="0" max="2" value="1" /><small>每题最多 2 次</small></label>
        <label class="quality-model-field"><span>评分模型</span><input id="experiment-quality-judge-model" type="text" value="" placeholder="留空时使用生成模型" maxlength="250" /><small>空值默认与生成模型相同</small></label>
      </div>
      <div id="experiment-provider-note" class="experiment-provider-note ${state.experimentConfig?.provider_ready ? 'ready' : ''}">${state.experimentConfig?.provider_ready ? `模型服务地址已配置 · ${esc(state.experimentConfig.base_url)} · 实际可用性以批次结果为准` : state.experimentConfig?.configuration_error ? `模型服务配置错误：${esc(state.experimentConfig.configuration_error)}` : '请在 backend/.env 中配置 NL2SQL_API_BASE_URL；服务启用鉴权时再填写 NL2SQL_API_KEY。'}</div>
      <label class="experiment-optimization-note"><span>本轮优化备注</span><textarea id="experiment-optimization-note" maxlength="2000" placeholder="例如：本轮新增 Few-shot；从 development 检索最多 2 个可执行示例。"></textarea><small>备注与自动记录的 Prompt、Few-shot、SQL 输出清理设置会随批次保存。</small></label>
      <div class="experiment-method-note">Few-shot 示例只从 development 集检索，并跳过无法执行或无有效结果的 SQL；测试集可单独运行。若根据测试集错误反复调整 Prompt，这组数据也会参与调参。</div>
      <details class="experiment-prompt-editor"><summary>本轮 System Prompt（可编辑，逐条保存实际 Prompt）</summary><textarea id="experiment-system-prompt" spellcheck="false">${esc(state.experimentConfig?.default_system_prompt || '')}</textarea></details>
    </section>
    <section class="experiment-history panel"><div class="panel-heading"><div><strong>运行批次</strong><span id="experiment-runs-count" class="subtle-count"></span></div><span class="list-sort">最近运行在前</span></div><div id="experiment-run-list" class="experiment-run-list"><div class="list-loading"><span class="spinner small"></span>读取批次中</div></div></section>
    <section id="experiment-review" class="experiment-review"></section>
    <section class="full-test-problem-categories panel"><div class="panel-heading"><div><strong id="full-run-comparison-title">最近两轮全量测试 · 问题类别统计</strong><span class="subtle-count">默认比较同一数据集最近两轮，可自行选择批次</span></div></div><div id="full-test-problem-category-content"><div class="list-loading"><span class="spinner small"></span>统计中</div></div></section>
  `
  loadExperiments().catch(showExperimentHistoryError)
  loadFullTestProblemCategories()
}

const experimentStatusLabels = {
  queued: '排队中', running: '运行中', completed: '已完成', cancelled: '已停止', failed: '运行失败',
}
const evaluationStatusLabels = {
  correct: '结果一致', incorrect: '结果不一致', unjudged: '无法判定', generation_failed: '生成失败', interrupted: '运行中断', not_run: '未运行', pending: '待运行', queued: '排队中', generating: '生成中',
}

function renderExperimentRunList() {
  const container = document.querySelector('#experiment-run-list')
  const count = document.querySelector('#experiment-runs-count')
  const headingCount = document.querySelector('#experiment-run-count-summary')
  if (!container) return
  if (count) count.textContent = `${fmt(state.experiments.length)} 个`
  if (headingCount) headingCount.textContent = fmt(state.experiments.length)
  if (!state.experiments.length) {
    container.innerHTML = `<div class="empty-inline small-empty"><strong>还没有运行批次</strong><small>配置模型服务后，从开发集启动第一轮基线。</small></div>`
    return
  }
  container.innerHTML = state.experiments.map((run) => `
    <button class="experiment-run-card ${run.run_id === state.selectedRunId ? 'active' : ''}" data-experiment-run="${esc(run.run_id)}">
      <span class="run-status-dot status-${esc(run.status)}"></span>
      <span class="experiment-run-copy"><strong>${esc(splitLabels[run.split] || run.split)} · ${esc(run.model)}</strong><small>${esc(formatDate(run.created_at))} · ${esc(run.prompt_version)}</small></span>
      <span class="experiment-run-score"><strong>${fmt(run.counts.correct)} / ${fmt(run.total_count)}</strong><small>${esc(experimentStatusLabels[run.status] || run.status)}</small></span>
    </button>
  `).join('')
}

function renderFullTestProblemCategories(data) {
  const container = document.querySelector('#full-test-problem-category-content')
  if (!container) return
  const runs = Array.isArray(data.runs) ? data.runs : []
  const availableRuns = Array.isArray(data.available_runs) ? data.available_runs : []
  state.fullRunSplit = data.split || state.fullRunSplit
  state.fullRunAId = data.selected_run_ids?.[0] || ''
  state.fullRunBId = data.selected_run_ids?.[1] || ''
  const title = document.querySelector('#full-run-comparison-title')
  if (title) title.textContent = `最近两轮全量${state.fullRunSplit === 'test' ? '测试' : '验证'} · 问题类别统计`
  const optionLabel = (run) => `${formatDate(run.created_at)} · ${run.model} · ${run.run_id}`
  const runSelect = (label, id, selectedId, otherSelectedId) => {
    const selectableRuns = availableRuns.filter((run) => run.run_id !== otherSelectedId)
    const options = selectableRuns.length
      ? selectableRuns.map((run) => `<option value="${esc(run.run_id)}" ${run.run_id === selectedId ? 'selected' : ''}>${esc(optionLabel(run))}</option>`).join('')
      : '<option value="">暂无可比较批次</option>'
    return `<label class="category-run-select"><span>${label}</span><select id="${id}" ${selectableRuns.length ? '' : 'disabled'}>${options}</select></label>`
  }
  const controls = `<div class="category-comparison-controls"><label class="category-run-select category-dataset-select"><span>数据集</span><select id="full-run-split"><option value="test" ${state.fullRunSplit === 'test' ? 'selected' : ''}>测试集</option><option value="validation" ${state.fullRunSplit === 'validation' ? 'selected' : ''}>验证集</option></select></label>${runSelect('批次 A', 'full-run-a', state.fullRunAId, state.fullRunBId)}${runSelect('批次 B', 'full-run-b', state.fullRunBId, state.fullRunAId)}</div>`
  if (!runs.length) {
    const splitLabel = splitLabels[state.fullRunSplit] || state.fullRunSplit
    container.innerHTML = `${controls}<div class="empty-inline small-empty"><strong>${esc(splitLabel)}还没有全量测评批次</strong><small>完成一轮覆盖全部样本的测评后，这里会显示诊断统计。</small></div>`
    return
  }
  const [runA, runB] = runs
  const categoryRows = (data.categories || []).map((category) => {
    const current = category.runs?.[0] || {}
    const older = category.runs?.[1] || {}
    const delta = Number(category.delta || 0)
    const deltaLabel = delta > 0 ? `+${fmt(delta)}` : delta < 0 ? `−${fmt(Math.abs(delta))}` : '0'
    const deltaClass = delta > 0 ? 'category-delta-up' : delta < 0 ? 'category-delta-down' : ''
    const cell = (counts) => `${fmt(counts.incorrect || 0)} / ${fmt(counts.unjudged || 0)} / ${fmt(counts.generation_failed || 0)}`
    return `<tr><th scope="row">${esc(category.title || category.category)}</th><td>${cell(current)}</td>${runB ? `<td>${cell(older)}</td><td class="${deltaClass}">${deltaLabel}</td>` : ''}</tr>`
  }).join('')
  const runHeader = (run) => `<strong>${esc(run.run_id)}</strong><small>${esc(run.model)} · ${esc(formatDate(run.created_at))}</small>`
  const runMetric = (run, label) => `<div class="category-run-metric"><span>${label} · 执行一致率</span><strong>${run.accuracy == null ? '—' : `${(run.accuracy * 100).toFixed(2)}%`}</strong><small>${fmt(run.counts?.incorrect || 0)} 错误 · ${fmt(run.counts?.unjudged || 0)} 无法判定 · ${fmt(run.counts?.generation_failed || 0)} 生成失败</small></div>`
  const tableEmpty = runB ? '这两个批次都没有诊断到问题类别。' : '这一批次没有诊断到问题类别。'
  const tableColumns = `<th>问题类别</th><th>${runHeader(runA)}<small>错误 / 无法判定 / 生成失败</small></th>${runB ? `<th>${runHeader(runB)}<small>错误 / 无法判定 / 生成失败</small></th><th>批次 A − 批次 B</th>` : ''}`
  const emptyRow = `<tr><td colspan="${runB ? 4 : 2}">${tableEmpty}</td></tr>`
  const comparisonNote = runB ? '变化值为批次 A 各类别样本数减去批次 B。' : '选择第二个全量批次后即可比较两轮变化。'
  container.innerHTML = `
    ${controls}
    <div class="category-run-overview">${runMetric(runA, '批次 A')}${runB ? runMetric(runB, '批次 B') : ''}</div>
    <div class="category-comparison-scroll"><table class="category-comparison-table"><thead><tr>${tableColumns}</tr></thead><tbody>${categoryRows || emptyRow}</tbody></table></div>
    <p class="category-comparison-note">${esc(data.category_method || '每个诊断类别按受影响样本数计数；同一样本可能属于多个类别。')} ${comparisonNote}</p>
  `
}

async function loadFullTestProblemCategories() {
  const container = document.querySelector('#full-test-problem-category-content')
  if (!container) return
  const requestId = ++fullRunCategoryRequestId
  try {
    const params = new URLSearchParams({ split: state.fullRunSplit })
    if (state.fullRunAId) params.set('run_a_id', state.fullRunAId)
    if (state.fullRunBId) params.set('run_b_id', state.fullRunBId)
    const data = await request(`/experiment-analytics/full-test-problem-categories?${params}`)
    if (container.isConnected && requestId === fullRunCategoryRequestId) renderFullTestProblemCategories(data)
  } catch (error) {
    if (container.isConnected && requestId === fullRunCategoryRequestId) container.innerHTML = `<div class="empty-inline small-empty"><strong>问题类别统计读取失败</strong><small>${esc(error.message)}</small></div>`
  }
}

function renderExperimentSummary() {
  const container = document.querySelector('#experiment-review')
  const run = state.experiment
  if (!container) return
  if (!run) {
    container.innerHTML = `<div class="detail-placeholder panel"><span class="placeholder-icon">${icon('chart', 24)}</span><strong>选择或启动一个批次</strong><span>批次完成后，这里会展示全量结果和逐条审阅内容。</span></div>`
    return
  }
  const counts = run.counts || {}
  const processed = (counts.correct || 0) + (counts.incorrect || 0) + (counts.unjudged || 0) + (counts.generation_failed || 0) + (counts.interrupted || 0)
  const progress = run.total_count ? Math.min(100, Math.round(processed / run.total_count * 100)) : 0
  const accuracy = run.accuracy == null ? '—' : `${(run.accuracy * 100).toFixed(1)}%`
  const fewShotLabel = run.parameters?.few_shot_enabled
    ? `最多 ${fmt(run.parameters.few_shot_count)} 个示例（${esc(run.parameters.few_shot_source_split || 'development')}）`
    : run.parameters?.few_shot_requested ? '开发集批次自动关闭' : '关闭'
  const targetSchemaLabel = run.parameters?.target_schema_enabled === false ? '关闭' : '开启'
  const optimizationLog = Array.isArray(run.parameters?.optimization_log) ? run.parameters.optimization_log : []
  const optimizationNote = String(run.parameters?.optimization_note || '').trim()
  const quality = run.quality_summary || {}
  const qualityReport = run.parameters?.quality_scoring
    ? '<section class="quality-run-summary"><div class="quality-run-summary-heading"><strong>Rubric 评分区分能力</strong><small>初始 SQL 分数与执行正确/错误标签比较</small></div><div class="quality-run-summary-metrics">' +
      '<div><span>AUC</span><strong>' + (quality.rubric_auc == null ? '样本不足' : Number(quality.rubric_auc).toFixed(3)) + '</strong><small>' + fmt(quality.rubric_correct_samples || 0) + ' 条正确 · ' + fmt(quality.rubric_incorrect_samples || 0) + ' 条错误</small></div>' +
      '<div><span>初始 Rubric 均分</span><strong>' + (quality.rubric_mean_correct == null || quality.rubric_mean_incorrect == null ? '—' : Number(quality.rubric_mean_correct).toFixed(1) + ' / ' + Number(quality.rubric_mean_incorrect).toFixed(1)) + '</strong><small>正确 / 错误；AUC 只计 Rubric 评分样本</small></div>' +
      '<div><span>低分修订</span><strong>' + fmt(quality.retry_samples || 0) + '</strong><small>修复 ' + fmt(quality.retry_recovered || 0) + ' 条 · 退化 ' + fmt(quality.retry_regressed || 0) + ' 条</small></div>' +
      '<div><span>确定性拦截</span><strong>' + fmt(quality.deterministic_rejects || 0) + '</strong><small>' + fmt(quality.judge_errors || 0) + ' 条评分调用失败</small></div>' +
      '</div><p>AUC 约 0.5 表示和随机排序接近，1.0 表示区分更强；小样本结果仅作试验参考。</p></section>'
    : ''
  const markup = `
    <section class="experiment-summary panel">
      <div class="experiment-summary-top"><div><div class="eyebrow">${esc(splitLabels[run.split] || run.split)} · ${esc(run.run_id)}</div><h2>${esc(run.model)}</h2><p>Prompt ${esc(run.prompt_version)} · Few-shot ${fewShotLabel} · 目标题 Schema ${targetSchemaLabel} · ${run.full_dataset ? '全量样本' : `${fmt(run.total_count)} 条固定抽样`} · 并发 ${fmt(run.parameters?.concurrency ?? 1)} 路 · temperature=${esc(run.parameters?.temperature ?? 0)} · max_tokens=${esc(run.parameters?.max_tokens ?? 2048)} · ${esc(formatDate(run.created_at))}</p></div><div class="experiment-summary-actions"><span class="run-state state-${esc(run.status)}">${esc(experimentStatusLabels[run.status] || run.status)}</span>${['queued', 'running'].includes(run.status) ? `<button class="outline-button" data-action="cancel-experiment" data-run-id="${esc(run.run_id)}">停止运行</button>` : ''}</div></div>
      <div class="experiment-progress"><span style="width:${progress}%"></span></div><div class="experiment-progress-caption"><span>${fmt(processed)} / ${fmt(run.total_count)} 条已处理</span><span class="experiment-progress-live"><span>排队中 ${fmt(counts.queued)}</span><span>生成中 ${fmt(counts.generating)}</span></span><span>${progress}%</span></div>
      <div class="experiment-metrics"><div><span>结果一致</span><strong>${fmt(counts.correct)}</strong></div><div><span>结果不一致</span><strong>${fmt(counts.incorrect)}</strong></div><div><span>无法判定</span><strong>${fmt(counts.unjudged)}</strong></div><div><span>生成失败</span><strong>${fmt(counts.generation_failed)}</strong></div><div><span>运行中断</span><strong>${fmt(counts.interrupted)}</strong></div><div><span>未运行</span><strong>${fmt(counts.not_run)}</strong></div><div><span>待运行</span><strong>${fmt(counts.pending)}</strong></div><div><span>执行准确率</span><strong>${accuracy}</strong><small>仅在可判定样本中计算</small></div></div>
      ${qualityReport}
      <section class="experiment-optimization-log"><div class="optimization-log-heading"><strong>本轮优化记录</strong><span>随批次保存</span></div>${optimizationLog.length ? `<ul>${optimizationLog.map((entry) => `<li class="${entry.applied ? 'applied' : 'not-applied'}"><span class="optimization-log-state">${entry.applied ? '已启用' : '未启用'}</span><div><strong>${esc(entry.title || entry.id || '优化项')}</strong><small>${esc(entry.detail || '')}</small></div></li>`).join('')}</ul>` : '<p class="optimization-log-empty">该历史批次未保存结构化优化记录。</p>'}${optimizationNote ? `<div class="optimization-log-note"><strong>备注</strong><p>${esc(optimizationNote)}</p></div>` : ''}</section>
      ${run.error_message ? `<div class="experiment-run-error">${esc(run.error_message)}</div>` : ''}
      <details class="experiment-system-snapshot"><summary>查看本轮 System Prompt</summary><pre>${esc(run.system_prompt || '')}</pre></details>
    </section>
    <div class="experiment-workspace">
      <section class="experiment-items-panel panel">
        <div class="panel-heading"><div><strong>运行样本</strong><span id="experiment-items-count" class="subtle-count"></span></div><span class="list-sort">正确与错误均展示</span></div>
        <div class="toolbar experiment-items-toolbar">
          <label class="search-field">${icon('search')}<input id="experiment-item-search" type="search" placeholder="搜索问题、SQL 或原因…" value="${esc(state.experimentItemQuery)}" autocomplete="off" /></label>
          <label class="filter-field"><span>数据库</span><input id="experiment-item-database" type="search" placeholder="database id" value="${esc(state.experimentItemDatabase)}" autocomplete="off" /></label>
          <label class="select-field"><span>结果</span><select id="experiment-item-status">${[['all', '全部'], ['correct', '正确'], ['incorrect', '错误'], ['unjudged', '无法判定'], ['generation_failed', '生成失败'], ['interrupted', '运行中断'], ['not_run', '未运行'], ['pending', '待运行']].map(([key, label]) => `<option value="${key}" ${state.experimentItemStatus === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
        </div>
        <div id="experiment-items-table" class="experiment-items-table"><div class="list-loading"><span class="spinner small"></span>读取运行结果中</div></div>
        <div id="experiment-items-pagination" class="pagination"></div>
      </section>
      <section id="experiment-item-detail" class="experiment-item-detail"><div class="detail-placeholder panel"><span class="placeholder-icon">${icon('rows', 24)}</span><strong>选择一条运行样本</strong><span>问题、金标、实际 Prompt、生成 SQL 与判断依据显示在这里。</span></div></section>
    </div>
  `
  const existingWorkspace = container.querySelector('.experiment-workspace')
  if (existingWorkspace) {
    const template = document.createElement('template')
    template.innerHTML = markup
    const nextSummary = template.content.querySelector('.experiment-summary')
    const currentSummary = container.querySelector('.experiment-summary')
    if (currentSummary) currentSummary.replaceWith(nextSummary)
    else container.prepend(nextSummary)
  } else {
    container.innerHTML = markup
  }
  renderExperimentRunList()
}

function renderExperimentItems({ preserveScroll = true } = {}) {
  const response = state.experimentItems
  const table = document.querySelector('#experiment-items-table')
  const count = document.querySelector('#experiment-items-count')
  const pagination = document.querySelector('#experiment-items-pagination')
  if (!table || !response) return
  const previousScrollTop = preserveScroll ? table.querySelector('.experiment-items-scroll')?.scrollTop || 0 : 0
  if (count) count.textContent = `${fmt(response.total)} 条`
  if (!response.items.length) {
    table.innerHTML = `<div class="empty-inline small-empty"><strong>没有匹配样本</strong><small>调整结果或搜索条件后再试。</small></div>`
    if (pagination) pagination.innerHTML = ''
    return
  }
  table.innerHTML = `<div class="experiment-items-scroll"><table class="experiment-items-table-grid"><thead><tr><th>样本</th><th>状态</th><th>问题与初步判断</th><th>耗时</th></tr></thead><tbody>${response.items.map((item) => {
    const status = item.evaluation_status === 'pending' ? item.item_status : item.evaluation_status
    const label = item.evaluation_status === 'pending'
      ? evaluationStatusLabels[item.item_status] || '待运行'
      : evaluationStatusLabels[item.evaluation_status] || evaluationStatusLabels[status] || status
    const scoreLabel = item.quality_score == null ? '' : ` · 质量分 ${esc(item.quality_score)}`
    return `<tr class="${item.item_id === state.selectedExperimentItemId ? 'active' : ''}" data-experiment-item="${item.item_id}"><td><code>#${String(item.sample_index + 1).padStart(4, '0')}</code><small>${esc(item.db_id)}</small></td><td><span class="evaluation-badge eval-${esc(status)}">${esc(label)}</span></td><td class="experiment-list-question"><strong>${esc(item.question)}</strong><small>${esc(item.reason_title || (item.item_status === 'queued' ? '等待运行' : '初步判断待生成'))}${scoreLabel}</small></td><td>${item.latency_ms == null ? '—' : `${esc(item.latency_ms)} ms`}</td></tr>`
  }).join('')}</tbody></table></div>`
  const nextScrollContainer = table.querySelector('.experiment-items-scroll')
  if (nextScrollContainer) nextScrollContainer.scrollTop = previousScrollTop
  const pages = Math.max(response.pages, 1)
  pagination.innerHTML = `<span>第 ${response.page} / ${pages} 页</span><div><button data-experiment-page="prev" ${response.page <= 1 ? 'disabled' : ''}>‹</button><button data-experiment-page="next" ${response.page >= pages ? 'disabled' : ''}>›</button></div>`
}

function renderResultPreview(title, summary) {
  if (!summary || !Object.keys(summary).length) return ''
  if (!summary.ok) return `<section class="evaluation-preview"><div><strong>${esc(title)}</strong><span>无法比较</span></div><p>${esc(summary.error || '结果不可用')}</p></section>`
  const body = summary.rows?.length
    ? `<div class="evaluation-preview-table"><table><thead><tr>${(summary.columns || []).map((column) => `<th>${esc(column)}</th>`).join('')}</tr></thead><tbody>${summary.rows.map((row) => `<tr>${row.map((value) => `<td>${esc(value == null ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : value)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<p>查询返回 ${fmt(summary.row_count)} 行。</p>`
  return `<section class="evaluation-preview"><div><strong>${esc(title)}</strong><span>${fmt(summary.row_count)} 行${summary.truncated_preview ? ' · 仅展示前 12 行' : ''}</span></div>${body}</section>`
}

function renderQualityAssessment(assessment) {
  if (!assessment?.enabled) return ''
  const statusLabels = { pass: '通过', fail: '失败', unknown: '未判定', partial: '未完整执行' }
  const attempts = Array.isArray(assessment.attempts) ? assessment.attempts : []
  const attemptCards = attempts.map((attempt) => {
    const validation = attempt.validation?.checks || {}
    const checks = Object.entries(validation).map(([key, check]) => {
      const labels = { read_only: '只读', syntax: '语法', schema: 'Schema', execution: '执行' }
      return `<span class="quality-check quality-check-${esc(check.status)}"><strong>${esc(labels[key] || key)}：${esc(statusLabels[check.status] || check.status)}</strong><small>${esc(check.evidence || '')}</small></span>`
    }).join('')
    const issues = (attempt.issues || []).map((issue) => `<li>${esc(issue)}</li>`).join('')
    const source = attempt.score_source === 'deterministic' ? '确定性校验分' : attempt.score_source === 'llm_rubric' ? 'Rubric 评分' : '评分不可用'
    const benchmark = attempt.benchmark_status ? evaluationStatusLabels[attempt.benchmark_status] || attempt.benchmark_status : '评测中'
    const dimensions = Object.entries(attempt.dimensions || {}).map(([key, value]) => `<span>${esc(key.replaceAll('_', ' '))} <b>${esc(value)}</b></span>`).join('')
    return `<article class="quality-attempt"><div class="quality-attempt-heading"><strong>第 ${fmt(attempt.attempt)} 次 · ${attempt.score == null ? '评分失败' : `${fmt(attempt.score)} / 100`}</strong><span>${esc(source)} · ${esc(benchmark)}${assessment.selected_attempt === attempt.attempt ? ' · 采用' : ''}</span></div><div class="quality-checks">${checks}</div>${dimensions ? `<div class="quality-dimensions">${dimensions}</div>` : ''}${attempt.grader_error ? `<p class="quality-grader-error">${esc(attempt.grader_error)}</p>` : ''}${issues ? `<ul class="quality-issues">${issues}</ul>` : ''}${attempt.feedback ? `<p class="quality-feedback">${esc(attempt.feedback)}</p>` : ''}<pre class="review-sql generated">${esc(attempt.sql || '')}</pre></article>`
  }).join('')
  return `<article class="review-section panel quality-assessment"><div class="review-section-heading"><strong>SQL 校验与质量评分</strong><small>阈值 ${fmt(assessment.threshold)} · 已修订 ${fmt(assessment.retries_used)} 次 · 评分器 ${esc(assessment.judge_model || '')}</small></div><div class="quality-attempt-list">${attemptCards || '<div class="empty-inline small-empty">暂无评分结果。</div>'}</div>${assessment.retry_generation_errors?.length ? `<p class="quality-grader-error">修订生成错误：${esc(assessment.retry_generation_errors.join('；'))}</p>` : ''}</article>`
}

function renderExperimentItemDetail() {
  const container = document.querySelector('#experiment-item-detail')
  const item = state.experimentItem
  if (!container) return
  if (!item) {
    container.innerHTML = `<div class="detail-placeholder panel"><span class="placeholder-icon">${icon('rows', 24)}</span><strong>选择一条运行样本</strong><span>问题、金标、实际 Prompt、生成 SQL 与判断依据显示在这里。</span></div>`
    return
  }
  const status = item.evaluation_status === 'pending' ? item.item_status : item.evaluation_status
  const messages = item.prompt?.messages || []
  const systemPrompt = messages.find((message) => message.role === 'system')?.content || ''
  const userPrompt = messages.find((message) => message.role === 'user')?.content || ''
  const queryPlan = item.prompt?.query_plan
  const queryPlanStatus = !queryPlan?.enabled
    ? '本轮未启用'
    : !queryPlan.triggered
      ? '已启用，本题未命中复杂问题规则'
      : `已触发 · ${(queryPlan.trigger_reasons || []).map(esc).join('、')}${queryPlan.status === 'generated_unstructured' ? ' · 计划未遵循 JSON 格式' : ''}`
  const queryPlanContent = queryPlan ? `<article class="review-section panel"><div class="review-section-heading"><strong>查询计划节点</strong><small>${queryPlanStatus}</small></div>${queryPlan.plan ? `<pre class="review-sql">${esc(JSON.stringify(queryPlan.plan, null, 2))}</pre>` : ''}${queryPlan.error ? `<p class="quality-grader-error">计划失败，已回退直接生成：${esc(queryPlan.error)}</p>` : ''}${queryPlan.planner_prompt ? `<details class="experiment-system-snapshot"><summary>查看计划节点 Prompt</summary><div class="actual-prompt"><div><span>system</span><pre>${esc(queryPlan.planner_prompt.system || '')}</pre></div><div><span>user</span><pre>${esc(queryPlan.planner_prompt.user || '')}</pre></div></div></details>` : ''}</article>` : ''
  const diagnosis = item.diagnosis || []
  const diagnosisContent = diagnosis.length ? diagnosis.map((reason) => `<article class="diagnosis-card diagnosis-${esc(reason.category)}"><strong>${esc(reason.title)}</strong><p>${esc(reason.evidence)}</p></article>`).join('') : `<div class="empty-inline small-empty"><strong>${item.item_status === 'queued' ? '等待模型运行' : '暂无判断依据'}</strong></div>`
  const diff = item.diff || {}
  const diffContent = Object.keys(diff).length ? `<div class="evaluation-diff-note">生成结果 ${fmt(diff.predicted_row_count || 0)} 行 · 金标结果 ${fmt(diff.gold_row_count || 0)} 行${diff.row_match?.row_index != null ? ` · 第 ${fmt(diff.row_match.row_index + 1)} 行首次不同` : ''}</div>` : ''
  const generatedSqlContent = item.generated_sql || item.generation_error || (item.item_status === 'not_run' ? '该样本未运行' : item.item_status === 'interrupted' ? '运行中断' : item.item_status === 'queued' || item.item_status === 'generating' ? '等待生成 SQL' : '模型未返回 SQL')
  const qualityAssessmentContent = renderQualityAssessment(item.quality_assessment)
  container.innerHTML = `
    <article class="review-detail-header panel"><div class="detail-title-row"><div><div class="eyebrow">${esc(splitLabels[item.split] || state.experiment?.split || '')} / SAMPLE #${String(item.sample_index + 1).padStart(4, '0')}</div><h2>${esc(item.db_id)}</h2></div><span class="evaluation-badge eval-${esc(status)}">${esc(evaluationStatusLabels[status] || (item.item_status === 'generating' ? '生成中' : status))}</span></div><div class="review-question"><span>自然语言问题</span><p>${esc(item.question)}</p></div></article>
    <article class="review-section panel"><div class="review-section-heading"><strong>初步原因与证据</strong><small>规则依据执行错误和结果差异生成，供人工确认</small></div><div class="diagnosis-list">${diagnosisContent}</div>${diffContent}</article>
    <article class="review-section panel"><div class="review-section-heading"><strong>金标 SQL</strong><small>来自对应样本 JSON.query</small></div><pre class="review-sql">${esc(item.gold_sql || '暂无金标 SQL')}</pre></article>
    ${queryPlanContent}
    <article class="review-section panel"><div class="review-section-heading"><strong>生成 SQL</strong><small>${item.model ? esc(item.model) : '模型未返回 SQL'}${item.latency_ms == null ? '' : ` · ${esc(item.latency_ms)} ms`}</small></div><pre class="review-sql generated">${esc(generatedSqlContent)}</pre></article>
    ${qualityAssessmentContent}
    <article class="review-section panel"><div class="review-section-heading"><strong>本次实际发送的 Prompt</strong><small>包含 system 与 user 消息，不含金标</small></div><div class="actual-prompt"><div><span>system</span><pre>${esc(systemPrompt || '暂无')}</pre></div><div><span>user</span><pre>${esc(userPrompt || '暂无')}</pre></div></div></article>
    ${item.prediction_summary?.ok || item.prediction_summary?.error ? `<article class="review-section panel"><div class="review-section-heading"><strong>执行结果对照</strong><small>单个数据库上的执行结果比较</small></div>${diffContent}${renderResultPreview('生成 SQL 结果', item.prediction_summary)}${renderResultPreview('金标 SQL 结果', item.gold_summary)}</article>` : ''}
  `
}

async function loadExperimentConfig() {
  if (!state.experimentConfig) state.experimentConfig = await request('/experiment-config')
  return state.experimentConfig
}

function updateExperimentStartButton() {
  const button = document.querySelector('[data-action="start-experiment"]')
  if (!button || button.dataset.submitting === 'true') return
  const model = document.querySelector('#experiment-model')?.value || state.experimentConfig?.default_model || ''
  const hasActiveRun = state.experiments.some((run) => ['queued', 'running'].includes(run.status))
  button.disabled = experimentStartPending || experimentHistoryLoading || !experimentHistoryLoaded || !state.experimentConfig?.provider_ready || !String(model).trim() || hasActiveRun
}

function showExperimentReviewError(error) {
  const container = document.querySelector('#experiment-review')
  if (!container) return
  container.innerHTML = `<div class="api-error panel"><span class="error-icon">!</span><h2>批次结果读取失败</h2><p>${esc(error.message)}</p><button class="outline-button" data-action="retry-experiment">${icon('refresh', 15)}重试读取</button></div>`
}

function showExperimentHistoryError(error) {
  const container = document.querySelector('#experiment-run-list')
  if (!container || state.view !== 'results') return
  container.innerHTML = `<div class="empty-inline small-empty"><strong>批次列表读取失败</strong><small>${esc(error.message)}</small><button class="outline-button" data-action="retry-experiment-history">重试读取</button></div>`
}

function upsertExperimentListItem(run) {
  if (!run?.run_id) return
  const index = state.experiments.findIndex((item) => item.run_id === run.run_id)
  if (index === -1) state.experiments = [run, ...state.experiments].slice(0, 30)
  else state.experiments[index] = { ...state.experiments[index], ...run }
  renderExperimentRunList()
  updateExperimentStartButton()
}

async function loadExperiments() {
  const sequence = ++requestSequence.experiments
  experimentHistoryLoading = true
  updateExperimentStartButton()
  try {
    const config = await loadExperimentConfig()
    if (sequence !== requestSequence.experiments || state.view !== 'results') return
    const modelInput = document.querySelector('#experiment-model')
    const promptInput = document.querySelector('#experiment-system-prompt')
    if (modelInput && !modelInput.value && config.default_model) modelInput.value = config.default_model
    if (promptInput && !promptInput.value) promptInput.value = config.default_system_prompt || ''
    const runDescription = document.querySelector('#experiment-run-description')
    if (runDescription) runDescription.textContent = `每条样本可选择是否提供目标题 Schema；最多 ${fmt(config.max_concurrency ?? 6)} 路并发生成；金标只用于运行后的评测。`
    const note = document.querySelector('#experiment-provider-note')
    if (note) {
      note.classList.toggle('ready', config.provider_ready)
      note.innerHTML = config.provider_ready ? `模型服务地址已配置 · ${esc(config.base_url)} · 实际可用性以批次结果为准` : config.configuration_error ? `模型服务配置错误：${esc(config.configuration_error)}` : '请在 backend/.env 中配置 NL2SQL_API_BASE_URL；服务启用鉴权时再填写 NL2SQL_API_KEY。'
    }
    const response = await request('/experiments?limit=30')
    if (sequence !== requestSequence.experiments || state.view !== 'results') return
    state.experiments = response.items || []
    experimentHistoryLoaded = true
    if (!state.selectedRunId || !state.experiments.some((run) => run.run_id === state.selectedRunId)) state.selectedRunId = state.experiments[0]?.run_id || ''
    renderExperimentRunList()
    updateExperimentStartButton()
    if (state.selectedRunId) await loadSelectedExperiment().catch((error) => showExperimentReviewError(error))
    else {
      state.experiment = null
      renderExperimentSummary()
    }
  } catch (error) {
    if (sequence !== requestSequence.experiments || state.view !== 'results') return
    throw error
  } finally {
    if (sequence === requestSequence.experiments) {
      experimentHistoryLoading = false
      updateExperimentStartButton()
    }
  }
}

async function loadSelectedExperiment() {
  if (!state.selectedRunId) return
  const runId = state.selectedRunId
  const sequence = ++requestSequence.experiment
  try {
    const run = await request(`/experiments/${encodeURIComponent(runId)}`)
    if (sequence !== requestSequence.experiment || runId !== state.selectedRunId || state.view !== 'results') return
    state.experiment = run
    upsertExperimentListItem(run)
    renderExperimentSummary()
    await loadExperimentItems()
    renderExperimentRunList()
    scheduleExperimentPoll()
  } catch (error) {
    if (sequence === requestSequence.experiment && runId === state.selectedRunId && state.view === 'results') {
      showExperimentReviewError(error)
      throw error
    }
  }
}

async function loadExperimentItems({ refreshSelected = true, silent = false } = {}) {
  if (!state.selectedRunId) return
  const runId = state.selectedRunId
  const sequence = ++requestSequence.experimentItems
  const params = new URLSearchParams({
    status: state.experimentItemStatus,
    q: state.experimentItemQuery,
    db_id: state.experimentItemDatabase,
    page: String(state.experimentItemPage),
    page_size: String(state.experimentItemPageSize),
  })
  const requestedFilters = `${state.experimentItemStatus}|${state.experimentItemQuery}|${state.experimentItemDatabase}|${state.experimentItemPage}`
  const requestedViewKey = `${runId}|${requestedFilters}`
  let response
  try {
    response = await request(`/experiments/${encodeURIComponent(runId)}/items?${params}`)
  } catch (error) {
    const currentFilters = `${state.experimentItemStatus}|${state.experimentItemQuery}|${state.experimentItemDatabase}|${state.experimentItemPage}`
    if (sequence !== requestSequence.experimentItems || runId !== state.selectedRunId || requestedFilters !== currentFilters) return
    const table = document.querySelector('#experiment-items-table')
    if (table) table.innerHTML = `<div class="empty-inline small-empty"><strong>运行结果读取失败</strong><small>${esc(error.message)}</small><button class="outline-button" data-action="retry-experiment-items">重试</button></div>`
    if (!silent) showToast(error.message, 'error')
    return
  }
  const currentFilters = `${state.experimentItemStatus}|${state.experimentItemQuery}|${state.experimentItemDatabase}|${state.experimentItemPage}`
  if (requestedFilters !== currentFilters) return
  if (sequence !== requestSequence.experimentItems || runId !== state.selectedRunId || state.view !== 'results') return
  const preserveListScroll = state.experimentItemsKey === requestedViewKey
  state.experimentItemsKey = requestedViewKey
  state.experimentItems = response
  const previousSelectedId = state.selectedExperimentItemId
  const selectedStillVisible = response.items.some((item) => item.item_id === state.selectedExperimentItemId)
  if (!selectedStillVisible) state.selectedExperimentItemId = response.items[0]?.item_id ?? null
  renderExperimentItems({ preserveScroll: preserveListScroll })
  const selectedNeedsRefresh = refreshSelected || previousSelectedId !== state.selectedExperimentItemId || !state.experimentItem || ['queued', 'generating'].includes(state.experimentItem.item_status)
  if (state.selectedExperimentItemId != null && selectedNeedsRefresh) await loadExperimentItemDetail(state.selectedExperimentItemId, { silent })
  else {
    if (state.selectedExperimentItemId == null) {
      state.experimentItem = null
      renderExperimentItemDetail()
    }
  }
}

async function loadExperimentItemDetail(itemId, { silent = false } = {}) {
  const runId = state.selectedRunId
  state.selectedExperimentItemId = Number(itemId)
  state.experimentItem = null
  renderExperimentItems()
  const detailContainer = document.querySelector('#experiment-item-detail')
  if (detailContainer) detailContainer.innerHTML = `<div class="detail-placeholder panel"><span class="spinner"></span><strong>读取这条运行样本…</strong></div>`
  const sequence = ++requestSequence.experimentItem
  let detail
  try {
    detail = await request(`/experiments/${encodeURIComponent(runId)}/items/${Number(itemId)}`)
  } catch (error) {
    if (sequence === requestSequence.experimentItem && runId === state.selectedRunId) {
      if (detailContainer) detailContainer.innerHTML = `<div class="empty-inline panel"><strong>读取样本失败</strong><small>${esc(error.message)}</small><button class="outline-button" data-action="retry-experiment-item" data-item-id="${Number(itemId)}">重试</button></div>`
      if (!silent) showToast(error.message, 'error')
    }
    return
  }
  if (sequence !== requestSequence.experimentItem || runId !== state.selectedRunId || state.view !== 'results') return
  state.experimentItem = detail
  renderExperimentItems()
  renderExperimentItemDetail()
}

function scheduleExperimentPoll(delay = 2500) {
  clearTimeout(experimentPollTimer)
  if (state.view !== 'results' || !state.experiment || !['queued', 'running'].includes(state.experiment.status)) return
  experimentPollTimer = setTimeout(async () => {
    if (state.view !== 'results' || !state.selectedRunId) return
    if (experimentPollBusy) {
      scheduleExperimentPoll(2500)
      return
    }
    experimentPollBusy = true
    try {
      const response = await request(`/experiments/${encodeURIComponent(state.selectedRunId)}`)
      if (state.view !== 'results' || response.run_id !== state.selectedRunId) return
      state.experiment = response
      upsertExperimentListItem(response)
      renderExperimentSummary()
      await loadExperimentItems({ refreshSelected: false, silent: true })
      const history = await request('/experiments?limit=30')
      if (state.view !== 'results' || response.run_id !== state.selectedRunId) return
      state.experiments = history.items || state.experiments
      renderExperimentRunList()
      updateExperimentStartButton()
      if (response.status === 'completed') await loadFullTestProblemCategories()
      experimentPollFailures = 0
      scheduleExperimentPoll()
    } catch (error) {
      experimentPollFailures += 1
      if (experimentPollFailures === 1) showToast('批次状态暂时无法刷新，正在重试。', 'error')
      if (state.view === 'results') scheduleExperimentPoll(Math.min(30_000, 2500 * (2 ** Math.min(experimentPollFailures - 1, 4))))
    } finally {
      experimentPollBusy = false
    }
  }, delay)
}

async function startExperiment() {
  if (experimentStartPending || experimentHistoryLoading || !experimentHistoryLoaded || state.experiments.some((run) => ['queued', 'running'].includes(run.status))) return
  experimentStartPending = true
  const button = document.querySelector('[data-action="start-experiment"]')
  const originalContent = button?.innerHTML || ''
  if (button) {
    button.dataset.submitting = 'true'
    button.disabled = true
    button.innerHTML = `<span class="spinner small"></span>正在创建批次`
  }
  try {
    const config = await loadExperimentConfig()
    if (!config.provider_ready) throw new Error('模型服务地址未配置或格式无效，请检查 backend/.env 中的 NL2SQL_API_BASE_URL。')
    const model = document.querySelector('#experiment-model')?.value.trim() || ''
    if (!model) throw new Error('请填写模型 ID。')
    const systemPrompt = document.querySelector('#experiment-system-prompt')?.value || ''
    if (!systemPrompt.trim()) throw new Error('System Prompt 不能为空。')
    const sampleLimit = Number(document.querySelector('#experiment-sample-limit')?.value || 20)
    const response = await request('/experiments', {
      method: 'POST',
      body: JSON.stringify({
        split: document.querySelector('#experiment-split')?.value || 'development',
        model,
        sample_limit: sampleLimit,
        sample_seed: 42,
        use_few_shot: document.querySelector('#experiment-few-shot')?.checked ?? true,
        use_target_schema: document.querySelector('#experiment-target-schema')?.checked ?? true,
        use_query_planning: document.querySelector('#experiment-query-planning')?.checked ?? false,
        use_quality_scoring: document.querySelector('#experiment-quality-scoring')?.checked ?? false,
        quality_score_threshold: Number(document.querySelector('#experiment-quality-threshold')?.value || 70),
        quality_retry_limit: Number(document.querySelector('#experiment-quality-retries')?.value || 0),
        quality_judge_model: document.querySelector('#experiment-quality-judge-model')?.value.trim() || '',
        optimization_note: document.querySelector('#experiment-optimization-note')?.value.trim() || '',
        system_prompt: systemPrompt,
      }),
    })
    state.selectedRunId = response.run_id
    state.experiment = response
    upsertExperimentListItem(response)
    state.experimentItemStatus = 'all'
    state.experimentItemQuery = ''
    state.experimentItemDatabase = ''
    state.experimentItemPage = 1
    state.selectedExperimentItemId = null
    state.experimentItem = null
    renderExperimentSummary()
    await loadSelectedExperiment()
    showToast('批量运行已启动')
  } finally {
    experimentStartPending = false
    if (button?.isConnected) {
      button.innerHTML = originalContent
      button.dataset.submitting = 'false'
    }
    updateExperimentStartButton()
  }
}

async function loadResults() {
  const sequence = ++requestSequence.results
  const requestedSplit = state.resultSplit
  const requestedDatabase = state.resultDatabaseFilter
  const requestedQuery = state.resultQuery
  const requestedPage = state.resultPage
  const params = new URLSearchParams({
    split: requestedSplit,
    db_id: requestedDatabase,
    q: requestedQuery,
    page: String(requestedPage),
    page_size: String(state.resultPageSize),
  })
  let results
  try {
    results = await request(`/results?${params}`)
  } catch (error) {
    if (sequence === requestSequence.results) throw error
    return
  }
  if (sequence !== requestSequence.results || requestedSplit !== state.resultSplit || requestedDatabase !== state.resultDatabaseFilter || requestedQuery !== state.resultQuery || requestedPage !== state.resultPage || state.view !== 'results') return
  state.results = results
  renderResultsTable()
}

function renderResultsTable() {
  const response = state.results
  if (!response) return
  const count = document.querySelector('#results-count')
  const table = document.querySelector('#results-table-wrap')
  const pagination = document.querySelector('#results-pagination')
  if (!table) return
  count.textContent = `${fmt(response.total)} 条`
  if (!response.items.length) {
    const hasSavedResults = Number(state.overview?.result_count || 0) > 0
    table.innerHTML = `<div class="empty-inline results-empty"><span>${icon('chart', 22)}</span><strong>${hasSavedResults ? '没有匹配的生成记录' : '还没有生成结果'}</strong><small>${hasSavedResults ? '调整搜索词或筛选条件后再试。' : '在样本详情里保存生成 SQL 后，记录会显示在这里。'}</small></div>`
    pagination.innerHTML = ''
    return
  }
  table.innerHTML = `<div class="results-table-scroll"><table class="results-table"><thead><tr><th>样本</th><th>数据库</th><th>自然语言问题</th><th>模型</th><th>生成 SQL</th><th>时间</th><th></th></tr></thead><tbody>${response.items.map((item) => `
    <tr><td><span class="result-split">${esc(splitLabels[item.split])}</span><code>#${String(item.sample_index + 1).padStart(4, '0')}</code></td><td><code class="db-code">${esc(item.db_id)}</code></td><td class="result-question">${esc(item.question)}</td><td>${item.model ? `<span class="model-tag">${esc(item.model)}</span>` : '<span class="dash">未标注</span>'}</td><td><pre class="result-sql-preview">${esc(item.generated_sql)}</pre></td><td class="date-cell">${esc(formatDate(item.created_at))}</td><td>${item.sample_available ? `<button class="text-button" data-open-sample="${esc(item.split)}:${item.sample_index}">查看样本 ${icon('arrow', 13)}</button>` : `<span class="dash">${item.sample_status === 'legacy_unlinked' ? '旧版结果无法关联' : '源样本已变更'}</span>`}</td></tr>
  `).join('')}</tbody></table></div>`
  const pages = Math.max(response.pages, 1)
  pagination.innerHTML = `<span>第 ${response.page} / ${pages} 页</span><div><button data-result-page="prev" ${response.page <= 1 ? 'disabled' : ''}>‹</button><button data-result-page="next" ${response.page >= pages ? 'disabled' : ''}>›</button></div>`
}

async function refresh() {
  try {
    await request('/health')
    setConnection(true)
    await loadOverview()
    renderView()
    showToast('数据已刷新')
  } catch (error) {
    setConnection(false)
    showToast(error.message, 'error')
  }
}

function debounce(callback, delay, key) {
  const timers = { sample: sampleSearchTimer, database: databaseSearchTimer, result: resultSearchTimer }
  clearTimeout(timers[key])
  const timer = setTimeout(callback, delay)
  if (key === 'sample') sampleSearchTimer = timer
  if (key === 'database') databaseSearchTimer = timer
  if (key === 'result') resultSearchTimer = timer
}

document.addEventListener('click', async (event) => {
  const viewButton = event.target.closest('[data-view]')
  const splitButton = event.target.closest('[data-split]')
  const sampleButton = event.target.closest('[data-sample-index]')
  const databaseButton = event.target.closest('[data-db-select]')
  const tableButton = event.target.closest('[data-table-select]')
  const sampleLink = event.target.closest('[data-open-sample]')
  const databaseLink = event.target.closest('[data-open-database]')
  const experimentRunButton = event.target.closest('[data-experiment-run]')
  const experimentItemButton = event.target.closest('[data-experiment-item]')
  const action = event.target.closest('[data-action]')

  try {
    if (viewButton) {
      state.view = viewButton.dataset.view
      renderView()
      return
    }
    if (experimentRunButton) {
      clearTimeout(experimentPollTimer)
      state.selectedRunId = experimentRunButton.dataset.experimentRun
      state.experiment = null
      state.experimentItems = null
      state.experimentItem = null
      state.experimentItemStatus = 'all'
      state.experimentItemQuery = ''
      state.experimentItemDatabase = ''
      state.experimentItemPage = 1
      state.selectedExperimentItemId = null
      state.experimentItem = null
      renderExperimentRunList()
      renderExperimentSummary()
      await loadSelectedExperiment()
      return
    }
    if (experimentItemButton) {
      await loadExperimentItemDetail(experimentItemButton.dataset.experimentItem)
      return
    }
    if (splitButton) {
      const nextSplit = splitButton.dataset.split
      if (nextSplit !== state.split) {
        state.split = nextSplit
        state.samplePage = 1
        state.selectedSampleIndex = null
        state.sample = null
        state.sampleQuery = ''
        state.databaseFilter = ''
        state.resultStatus = 'all'
      }
      state.view = 'samples'
      renderView()
      return
    }
    if (sampleButton) {
      await loadSampleDetail(sampleButton.dataset.sampleIndex)
      return
    }
    if (databaseButton) {
      try {
        await loadDatabaseDetail(databaseButton.dataset.dbSelect)
      } catch (error) {
        showDatabaseLoadError(error)
      }
      return
    }
    if (tableButton) {
      const previousTable = state.selectedTable
      const previousOffset = state.tableOffset
      state.selectedTable = tableButton.dataset.tableSelect
      state.tableOffset = 0
      try {
        await loadTableRows()
      } catch (error) {
        state.selectedTable = previousTable
        state.tableOffset = previousOffset
        throw error
      }
      renderDatabaseMain()
      return
    }
    if (sampleLink) {
      const [split, index] = sampleLink.dataset.openSample.split(':')
      state.split = split
      state.selectedSampleIndex = Number(index)
      state.samplePage = Math.floor(state.selectedSampleIndex / state.samplePageSize) + 1
      state.sampleQuery = ''
      state.databaseFilter = ''
      state.resultStatus = 'all'
      state.sample = null
      state.view = 'samples'
      renderView()
      return
    }
    if (databaseLink) {
      state.selectedDatabaseId = databaseLink.dataset.openDatabase
      state.view = 'databases'
      state.databaseSearch = ''
      renderView()
      return
    }
    if (action?.dataset.action === 'refresh') {
      await refresh()
      return
    }
    if (action?.dataset.action === 'start-experiment') {
      await startExperiment()
      return
    }
    if (action?.dataset.action === 'cancel-experiment') {
      await request(`/experiments/${encodeURIComponent(action.dataset.runId)}/cancel`, { method: 'POST' })
      await loadSelectedExperiment()
      showToast('已发送停止请求')
      return
    }
    if (action?.dataset.action === 'retry-experiment') {
      await loadSelectedExperiment()
      return
    }
    if (action?.dataset.action === 'retry-experiment-history') {
      await loadExperiments()
      return
    }
    if (action?.dataset.action === 'retry-experiment-items') {
      await loadExperimentItems()
      return
    }
    if (action?.dataset.action === 'retry-experiment-item') {
      await loadExperimentItemDetail(action.dataset.itemId)
      return
    }
    if (action?.dataset.action === 'save-result') {
      const sql = document.querySelector('#generated-sql')?.value || ''
      if (!sql.trim()) {
        showToast('请先填写生成 SQL', 'error')
        return
      }
      await request(`/samples/${encodeURIComponent(state.split)}/${state.selectedSampleIndex}/results`, {
        method: 'POST',
        body: JSON.stringify({
          generated_sql: sql,
          model: document.querySelector('#generated-model')?.value || '',
          run_id: document.querySelector('#generated-run-id')?.value || '',
          sample_id: state.sample?.sample_id,
        }),
      })
      await loadOverview()
      state.sample = null
      await loadSampleDetail(state.selectedSampleIndex)
      await loadSamples({ skipDetail: true })
      showToast('生成结果已保存')
      return
    }
    if (action?.dataset.action === 'samples-for-database') {
      const dbId = action.dataset.db
      if (!state.database?.split_counts?.[state.split]) {
        state.split = Object.keys(splitLabels).find((split) => state.database?.split_counts?.[split] > 0) || state.split
      }
      state.databaseFilter = dbId
      state.sampleQuery = ''
      state.resultStatus = 'all'
      state.samplePage = 1
      state.selectedSampleIndex = null
      state.view = 'samples'
      renderView()
      return
    }
    const samplePageButton = event.target.closest('[data-sample-page]')
    if (samplePageButton && !samplePageButton.disabled) {
      const previousPage = state.samplePage
      state.samplePage += samplePageButton.dataset.samplePage === 'next' ? 1 : -1
      try {
        await loadSamples()
      } catch (error) {
        state.samplePage = previousPage
        throw error
      }
      return
    }
    const resultPageButton = event.target.closest('[data-result-page]')
    if (resultPageButton && !resultPageButton.disabled) {
      const previousPage = state.resultPage
      state.resultPage += resultPageButton.dataset.resultPage === 'next' ? 1 : -1
      try {
        await loadResults()
      } catch (error) {
        state.resultPage = previousPage
        throw error
      }
      return
    }
    const experimentPageButton = event.target.closest('[data-experiment-page]')
    if (experimentPageButton && !experimentPageButton.disabled) {
      state.experimentItemPage += experimentPageButton.dataset.experimentPage === 'next' ? 1 : -1
      await loadExperimentItems()
      return
    }
    const tablePageButton = event.target.closest('[data-table-page]')
    if (tablePageButton && !tablePageButton.disabled) {
      const previousOffset = state.tableOffset
      state.tableOffset = Number(tablePageButton.dataset.tablePage)
      try {
        await loadTableRows()
      } catch (error) {
        state.tableOffset = previousOffset
        throw error
      }
      renderDatabaseMain()
      return
    }
    const copyButton = event.target.closest('[data-copy-target]')
    if (copyButton) {
      await navigator.clipboard.writeText(state.sample?.gold_sql || '')
      showToast('金标 SQL 已复制')
    }
  } catch (error) {
    showToast(error.message, 'error')
  }
})

document.addEventListener('input', (event) => {
  const target = event.target
  if (target.id === 'experiment-model') {
    updateExperimentStartButton()
  }
  if (target.id === 'experiment-item-search') {
    state.experimentItemQuery = target.value
    state.experimentItemPage = 1
    requestSequence.experimentItems += 1
    clearTimeout(experimentSearchTimer)
    experimentSearchTimer = setTimeout(() => loadExperimentItems().catch((error) => showToast(error.message, 'error')), 300)
  }
  if (target.id === 'experiment-item-database') {
    state.experimentItemDatabase = target.value
    state.experimentItemPage = 1
    requestSequence.experimentItems += 1
    clearTimeout(experimentSearchTimer)
    experimentSearchTimer = setTimeout(() => loadExperimentItems().catch((error) => showToast(error.message, 'error')), 300)
  }
  if (target.id === 'sample-search') {
    state.sampleQuery = target.value
    state.samplePage = 1
    debounce(() => loadSamples().catch((error) => showToast(error.message, 'error')), 300, 'sample')
  }
  if (target.id === 'database-filter') {
    state.databaseFilter = target.value
    state.samplePage = 1
    debounce(() => loadSamples().catch((error) => showToast(error.message, 'error')), 300, 'sample')
  }
  if (target.id === 'database-search') {
    state.databaseSearch = target.value
    debounce(() => loadDatabases().catch(showDatabaseLoadError), 260, 'database')
  }
  if (target.id === 'results-search') {
    state.resultQuery = target.value
    state.resultPage = 1
    debounce(() => loadResults().catch((error) => showToast(error.message, 'error')), 300, 'result')
  }
  if (target.id === 'results-database') {
    state.resultDatabaseFilter = target.value
    state.resultPage = 1
    debounce(() => loadResults().catch((error) => showToast(error.message, 'error')), 300, 'result')
  }
})

document.addEventListener('change', (event) => {
  const target = event.target
  if (target.id === 'full-run-split') {
    state.fullRunSplit = target.value
    state.fullRunAId = ''
    state.fullRunBId = ''
    loadFullTestProblemCategories()
  }
  if (target.id === 'full-run-a') {
    state.fullRunAId = target.value
    loadFullTestProblemCategories()
  }
  if (target.id === 'full-run-b') {
    state.fullRunBId = target.value
    loadFullTestProblemCategories()
  }
  if (target.id === 'experiment-split' && target.value === 'development' && document.querySelector('#experiment-few-shot')?.checked) {
    showToast('development 集用作 Few-shot 示例库；该划分运行会自动关闭 Few-shot。')
  }
  if (target.id === 'experiment-item-status') {
    state.experimentItemStatus = target.value
    state.experimentItemPage = 1
    requestSequence.experimentItems += 1
    loadExperimentItems().catch((error) => showToast(error.message, 'error'))
  }
  if (target.id === 'result-status') {
    state.resultStatus = target.value
    state.samplePage = 1
    loadSamples().catch((error) => showToast(error.message, 'error'))
  }
  if (target.id === 'results-split') {
    state.resultSplit = target.value
    state.resultPage = 1
    loadResults().catch((error) => showToast(error.message, 'error'))
  }
})

async function init() {
  renderShell()
  try {
    await request('/health')
    setConnection(true)
    await loadOverview()
    renderView()
  } catch (error) {
    setConnection(false)
    document.querySelector('#page-content').innerHTML = `<div class="api-error panel"><span class="error-icon">!</span><h2>无法连接后端 API</h2><p>${esc(error.message)}</p><div>先安装后端依赖，然后在仓库根目录启动 Python API：<code>python -m pip install -r backend/requirements.txt<br>python -m uvicorn backend.main:app --reload --port 8000</code></div><button class="outline-button" data-action="refresh">${icon('refresh', 15)}重试</button></div>`
  }
}

init()
