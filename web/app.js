/* 书析 · 前端逻辑 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  books: [],
  bookId: null,
  book: null,
  analysisId: null,
  pollTimer: null,
  activeTab: 'units',
  viewChapter: 0,
  pendingFile: null,
};

/* ---------------- 工具 ---------------- */
async function api(path, opts = {}) {
  const res = await fetch('/api/' + path, opts);
  const txt = await res.text();
  let d;
  try { d = JSON.parse(txt); } catch { throw new Error(txt.slice(0, 200) || '响应解析失败'); }
  if (d.error) throw new Error(d.error);
  return d;
}

function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = 'toast' + (isErr ? ' err' : ''); }, 3000);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtNum(n) {
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
  return String(n);
}

function setStatus(text, kind = '') {
  $('#statusText').textContent = text;
  $('#statusDot').className = 'dot ' + kind;
}

/* ---------------- 极简 Markdown ---------------- */
function mdToHtml(md) {
  if (!md) return '';
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  let html = '', inUl = false, inOl = false, inQuote = false;

  const closeList = () => {
    if (inUl) { html += '</ul>'; inUl = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  };
  const closeQuote = () => { if (inQuote) { html += '</blockquote>'; inQuote = false; } };

  const inline = (s) => {
    s = esc(s);
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, '<em>$1</em>');
    s = s.replace(/`([^`\n]+?)`/g, '<code>$1</code>');
    return s;
  };

  for (let raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { closeList(); closeQuote(); continue; }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList(); closeQuote();
      const lv = Math.min(4, h[1].length + 1);
      html += `<h${lv}>${inline(h[2])}</h${lv}>`;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { closeList(); closeQuote(); html += '<hr>'; continue; }

    const q = line.match(/^>\s?(.*)$/);
    if (q) {
      closeList();
      if (!inQuote) { html += '<blockquote>'; inQuote = true; }
      html += `<p>${inline(q[1])}</p>`;
      continue;
    } else closeQuote();

    const ul = line.match(/^\s*[-*+•]\s+(.*)$/);
    if (ul) {
      if (inOl) { html += '</ol>'; inOl = false; }
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${inline(ul[1])}</li>`;
      continue;
    }
    const ol = line.match(/^\s*\d+[.)、]\s*(.*)$/);
    if (ol) {
      if (inUl) { html += '</ul>'; inUl = false; }
      if (!inOl) { html += '<ol>'; inOl = true; }
      html += `<li>${inline(ol[1])}</li>`;
      continue;
    }

    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  closeList(); closeQuote();
  return html;
}

/* ---------------- 书籍列表 ---------------- */
async function loadBooks(selectId) {
  try {
    const d = await api('books');
    state.books = d.books;
    renderBooks();
    if (selectId) selectBook(selectId);
  } catch (e) {
    toast('加载书架失败：' + e.message, true);
  }
}

function renderBooks() {
  const el = $('#bookList');
  if (!state.books.length) {
    el.innerHTML = '<div class="empty-hint">还没有书</div>';
    return;
  }
  el.innerHTML = state.books.map(b => `
    <div class="bookitem ${b.id === state.bookId ? 'active' : ''}" data-id="${b.id}">
      <div class="bi-title">${esc(b.title)}</div>
      <div class="bi-meta">
        <span>${b.chapter_cnt} 章</span>
        <span>${fmtNum(b.char_cnt)} 字</span>
        ${b.analysis_cnt ? `<span>· ${b.analysis_cnt} 次拆解</span>` : ''}
      </div>
    </div>`).join('');

  $$('.bookitem').forEach(e => e.onclick = () => selectBook(+e.dataset.id));
}

/* ---------------- 选中书籍 ---------------- */
async function selectBook(id) {
  stopPolling();
  state.bookId = id;
  $$('.bookitem').forEach(e => e.classList.toggle('active', +e.dataset.id === id));
  $('#placeholder').hidden = true;
  $('#bookView').hidden = false;
  $('#bkTitle').textContent = '加载中…';
  $('#bkMeta').innerHTML = '';
  $('#analyseStatus').innerHTML = '';
  $('#paneUnits').innerHTML = '<div class="pane-empty">加载中…</div>';
  $('#paneChapters').innerHTML = '';
  $('#paneRaw').innerHTML = '';

  try {
    const b = await api('book?id=' + id);
    state.book = b;
    if (!b.chapters.some(c => c.idx === state.viewChapter)) state.viewChapter = 0;
    renderBook(b);
    await loadAnalysis(id);
  } catch (e) {
    toast('加载书籍失败：' + e.message, true);
  }
}

function renderBook(b) {
  $('#bkTitle').textContent = b.title;
  const srcLabel = { import: '本地导入', paste: '粘贴文本', fanqie: '番茄采集' }[b.source] || b.source;
  $('#bkMeta').innerHTML = `
    <span>${b.chapter_cnt} 章</span>
    <span>${fmtNum(b.char_cnt)} 字</span>
    <span>${srcLabel}</span>`;

  // 章节列表
  $('#paneChapters').innerHTML = `<div class="chap-list">${b.chapters.map(c => `
    <div class="chap-row ${c.idx === state.viewChapter ? 'active' : ''}" data-idx="${c.idx}">
      <span class="chap-no">${c.idx + 1}</span>
      <span class="chap-name">${esc(c.title)}</span>
      <span class="chap-size">${fmtNum(c.chars)} 字</span>
    </div>`).join('')}</div>`;

  $$('#paneChapters .chap-row').forEach(r => r.onclick = () => {
    state.viewChapter = +r.dataset.idx;
    $$('#paneChapters .chap-row').forEach(x => x.classList.toggle('active', x === r));
    switchTab('raw');
    renderRaw();
  });
}

async function renderRaw() {
  const pane = $('#paneRaw');
  pane.innerHTML = '<div class="pane-empty">加载中…</div>';
  try {
    const c = await api(`chapter?book=${state.bookId}&idx=${state.viewChapter}`);
    const paras = c.content.split('\n').filter(p => p.trim()).map(p => `<p>${esc(p.trim())}</p>`).join('');
    pane.innerHTML = `<div class="reader"><h3>${esc(c.title)}</h3>${paras || '<p>（本章无内容）</p>'}</div>`;
  } catch (e) {
    pane.innerHTML = `<div class="pane-empty">${esc(e.message)}</div>`;
  }
}

/* ---------------- 拆解结果 ---------------- */
async function loadAnalysis(bookId) {
  const d = await api('analyses?book=' + bookId);
  const list = d.analyses;
  if (!list.length) {
    state.analysisId = null;
    renderNoAnalysis();
    return;
  }
  const a = list[0];
  state.analysisId = a.id;
  await refreshAnalysis();
}

function renderNoAnalysis() {
  $('#analyseStatus').innerHTML = '';
  $('#paneUnits').innerHTML = `
    <div class="pane-empty">
      <b>还没有拆解结果</b>
      点右上角「开始拆书」，系统会先划分故事单元，再逐个单元做深度拆解。
    </div>`;
}

async function refreshAnalysis() {
  if (!state.analysisId) return;
  try {
    const a = await api('analysis?id=' + state.analysisId);
    renderStatus(a);
    renderUnits(a);
    if (a.status === 'running' || a.status === 'pending') {
      setStatus('拆书中…', 'busy');
      startPolling();
    } else {
      stopPolling();
      setStatus(a.status === 'error' ? '出错' : '就绪', a.status === 'error' ? 'err' : '');
    }
  } catch (e) {
    toast('刷新失败：' + e.message, true);
  }
}

function renderStatus(a) {
  const el = $('#analyseStatus');
  if (a.status === 'done') {
    el.innerHTML = `<div class="statusbar done">
      <div class="sb-top"><span class="sb-stage">拆解完成</span>
      <span class="sb-pct">${a.units.length} 个单元</span></div>
      <div class="progress"><i style="width:100%"></i></div>
      <div class="sb-msg">${esc(a.message || '')}</div></div>`;
    return;
  }
  if (a.status === 'error') {
    el.innerHTML = `<div class="statusbar error">
      <div class="sb-top"><span class="sb-stage">拆解中断</span>
      <button class="btn ghost" id="btnResume" style="padding:5px 12px;font-size:12.5px">重试</button></div>
      <div class="sb-msg">${esc(a.message || '未知错误')}</div></div>`;
    const r = $('#btnResume');
    if (r) r.onclick = resumeAnalysis;
    return;
  }
  const pct = Math.round((a.progress || 0) * 100);
  el.innerHTML = `<div class="statusbar">
    <div class="sb-top"><span class="sb-stage">${esc(a.stage || '准备中')}</span>
    <span class="sb-pct">${pct}%</span></div>
    <div class="progress"><i style="width:${pct}%"></i></div>
    <div class="sb-msg">${esc(a.message || '')}</div></div>`;
}

function renderUnits(a) {
  const pane = $('#paneUnits');
  const units = a.units || [];
  if (!units.length) {
    if (a.status === 'running' || a.status === 'pending')
      pane.innerHTML = '<div class="pane-empty">正在划分故事单元…</div>';
    else renderNoAnalysis();
    return;
  }
  const prevOpen = new Set($$('#paneUnits .unit-card.open').map(e => e.dataset.seq));

  pane.innerHTML = units.map(u => {
    const open = prevOpen.has(String(u.seq)) || u.detail;
    const hasDetail = !!(u.detail && u.detail.trim());
    return `<div class="unit-card ${open ? 'open' : ''}" data-seq="${u.seq}">
      <div class="uc-head">
        <span class="uc-seq">${u.seq}</span>
        <div class="uc-main">
          <div class="uc-name">${esc(u.name)}</div>
          <div class="uc-range">第 ${u.chap_from} — ${u.chap_to} 章${hasDetail ? '' : ' · 待分析'}</div>
        </div>
        <span class="uc-arrow">▶</span>
      </div>
      <div class="uc-body">
        ${hasDetail ? `<div class="md">${mdToHtml(u.detail)}</div>`
                    : `<div class="pane-empty" style="padding:26px">该单元正在分析或尚未生成内容</div>`}
      </div>
    </div>`;
  }).join('');

  $$('#paneUnits .unit-card .uc-head').forEach(h => h.onclick = () => {
    h.parentElement.classList.toggle('open');
  });
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(refreshAnalysis, 2500);
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

/* ---------------- 操作 ---------------- */
async function startAnalyse() {
  if (!state.bookId) return;
  const btn = $('#btnAnalyse');
  btn.disabled = true;
  try {
    const d = await api('analyse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: state.bookId }),
    });
    state.analysisId = d.id;
    toast(d.existing ? '已有拆书任务在进行' : '已开始拆书');
    await refreshAnalysis();
  } catch (e) {
    toast('启动失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function resumeAnalysis() {
  if (!state.analysisId) return;
  try {
    await api('resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: state.analysisId }),
    });
    toast('已重新排队');
    await refreshAnalysis();
  } catch (e) { toast('重试失败：' + e.message, true); }
}

async function doEstimate() {
  if (!state.bookId) return;
  try {
    const d = await api('estimate?book=' + state.bookId);
    toast(`约需 输入 ${fmtNum(d.est_input_tokens)} / 输出 ${fmtNum(d.est_output_tokens)} tokens`);
  } catch (e) { toast(e.message, true); }
}

async function deleteBook() {
  if (!state.bookId) return;
  if (!confirm('确定删除这本书及其全部拆解结果？此操作不可撤销。')) return;
  try {
    await api('delete_book', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: state.bookId }),
    });
    stopPolling();
    state.bookId = null; state.book = null; state.analysisId = null;
    $('#bookView').hidden = true;
    $('#placeholder').hidden = false;
    await loadBooks();
    toast('已删除');
  } catch (e) { toast('删除失败：' + e.message, true); }
}

/* ---------------- 导入 ---------------- */
function openModal() {
  $('#modalMask').hidden = false;
  $('#importHint').textContent = '';
  state.pendingFile = null;
  const fp = $('#fqProgress');
  if (fp) fp.hidden = true;
}
function closeModal() { $('#modalMask').hidden = true; }

async function doImport() {
  if (!$('#ipaneFanqie').hidden) {
    return startFanqieImport();
  }
  const isFile = !$('#ipaneFile').hidden;
  const btn = $('#btnDoImport');
  btn.disabled = true;
  $('#importHint').textContent = '正在解析…';
  try {
    let d;
    if (isFile) {
      if (!state.pendingFile) { toast('请先选择文件', true); btn.disabled = false; $('#importHint').textContent = ''; return; }
      const buf = await state.pendingFile.arrayBuffer();
      const nameB64 = btoa(String.fromCharCode(...new TextEncoder().encode(state.pendingFile.name)));
      d = await api('import', {
        method: 'POST',
        headers: { 'X-Filename-B64': nameB64 },
        body: buf,
      });
    } else {
      const text = $('#pasteText').value;
      if (!text.trim()) { toast('内容为空', true); btn.disabled = false; $('#importHint').textContent = ''; return; }
      d = await api('paste', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, title: $('#pasteTitle').value.trim() }),
      });
    }
    closeModal();
    toast(`已导入《${d.title}》，${d.chapters} 章 / ${fmtNum(d.chars)} 字`);
    $('#pasteText').value = ''; $('#pasteTitle').value = '';
    await loadBooks(d.id);
  } catch (e) {
    $('#importHint').textContent = '导入失败：' + e.message;
    toast('导入失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 标签切换 ---------------- */
function switchTab(name) {
  state.activeTab = name;
  $$('#bookTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('#paneUnits').hidden = name !== 'units';
  $('#paneChapters').hidden = name !== 'chapters';
  $('#paneRaw').hidden = name !== 'raw';
  if (name === 'raw') renderRaw();
}

/* ---------------- 番茄采集 ---------------- */
let fqTimer = null;

async function startFanqieImport() {
  const cid = ($('#fqChapter').value || '').trim();
  const maxc = parseInt($('#fqMax').value, 10) || 10;
  if (!/^\d{5,}$/.test(cid)) {
    toast('请填写正确的章节 ID（一串数字）', true);
    return;
  }
  const btn = $('#btnDoImport');
  btn.disabled = true;
  $('#fqProgress').hidden = false;
  $('#fqBar').style.width = '0%';
  $('#fqMsg').textContent = '正在启动采集…';

  try {
    const d = await api(`fanqie_fetch?chapter=${cid}&max=${maxc}`);
    const tid = d.task;
    pollFanqie(tid, btn);
  } catch (e) {
    $('#fqMsg').textContent = '启动失败：' + e.message;
    toast('启动失败：' + e.message, true);
    btn.disabled = false;
  }
}

function pollFanqie(tid, btn) {
  clearInterval(fqTimer);
  fqTimer = setInterval(async () => {
    try {
      const j = await api('fanqie_task?task=' + tid);
      const pct = Math.round((j.progress || 0) * 100);
      $('#fqBar').style.width = pct + '%';
      $('#fqMsg').textContent = j.message || '采集中…';

      if (j.status === 'done') {
        clearInterval(fqTimer);
        btn.disabled = false;
        $('#fqMsg').textContent = j.message;
        toast('采集完成，已加入书架');
        closeModal();
        await loadBooks(j.book_id);
      } else if (j.status === 'error') {
        clearInterval(fqTimer);
        btn.disabled = false;
        $('#fqMsg').textContent = '出错：' + (j.error || '未知');
        toast('采集失败：' + (j.error || ''), true);
      }
    } catch (e) {
      clearInterval(fqTimer);
      btn.disabled = false;
      $('#fqMsg').textContent = '查询失败：' + e.message;
    }
  }, 2500);
}

/* ---------------- 初始化 ---------------- */
function init() {
  $('#btnImport').onclick = openModal;
  $('#btnImport2').onclick = openModal;
  $('#btnCloseModal').onclick = closeModal;
  $('#btnDoImport').onclick = doImport;
  $('#btnAnalyse').onclick = startAnalyse;
  $('#btnEstimate').onclick = doEstimate;
  $('#btnDeleteBook').onclick = deleteBook;

  $('#modalMask').onclick = (e) => { if (e.target.id === 'modalMask') closeModal(); };

  // 导入弹窗标签
  $$('.modal .tab').forEach(t => t.onclick = () => {
    $$('.modal .tab').forEach(x => x.classList.toggle('active', x === t));
    const k = t.dataset.itab;
    $('#ipaneFile').hidden = k !== 'file';
    $('#ipanePaste').hidden = k !== 'paste';
    $('#ipaneFanqie').hidden = k !== 'fanqie';
  });

  // 书籍详情标签
  $$('#bookTabs .tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

  // 文件选择
  const fi = $('#fileInput'), dz = $('#dropzone');
  dz.onclick = () => fi.click();
  fi.onchange = () => {
    if (fi.files[0]) {
      state.pendingFile = fi.files[0];
      $('.dz-title', dz).textContent = fi.files[0].name;
      $('.dz-sub', dz).textContent = (fi.files[0].size / 1024).toFixed(0) + ' KB';
    }
  };
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('drag'); };
  dz.ondragleave = () => dz.classList.remove('drag');
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove('drag');
    if (e.dataTransfer.files[0]) {
      state.pendingFile = e.dataTransfer.files[0];
      $('.dz-title', dz).textContent = e.dataTransfer.files[0].name;
      $('.dz-sub', dz).textContent = (e.dataTransfer.files[0].size / 1024).toFixed(0) + ' KB';
    }
  };

  // iOS 返回手势 / 页面隐藏时停轮询
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else if (state.analysisId) refreshAnalysis();
  });

  loadBooks();
}

document.addEventListener('DOMContentLoaded', init);
