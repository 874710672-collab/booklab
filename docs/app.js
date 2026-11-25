/* 书析 · 纯前端版
 * 所有计算在浏览器完成，数据存 IndexedDB，拆书直连 DeepSeek。
 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  books: [],
  bookId: null,
  book: null,
  activeTab: 'units',
  viewChapter: 0,
  pendingFile: null,
  cfg: { apiKey: '', model: 'deepseek-chat' },
  busy: false,
};

/* ============ 存储层（IndexedDB） ============ */
const DB_NAME = 'booklab';
const DB_VER = 1;
let _db = null;

function openDB() {
  if (_db) return Promise.resolve(_db);
  return new Promise((res, rej) => {
    const req = indexedDB.open(DB_NAME, DB_VER);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('books')) {
        const s = db.createObjectStore('books', { keyPath: 'id', autoIncrement: true });
        s.createIndex('created', 'created');
      }
      if (!db.objectStoreNames.contains('chapters')) {
        const s = db.createObjectStore('chapters', { keyPath: 'cid', autoIncrement: true });
        s.createIndex('book', 'bookId');
      }
      if (!db.objectStoreNames.contains('analyses')) {
        db.createObjectStore('analyses', { keyPath: 'bookId' });
      }
      if (!db.objectStoreNames.contains('meta')) {
        db.createObjectStore('meta', { keyPath: 'k' });
      }
    };
    req.onsuccess = () => { _db = req.result; res(_db); };
    req.onerror = () => rej(req.error);
  });
}

function tx(store, mode = 'readonly') {
  return openDB().then(db => db.transaction(store, mode).objectStore(store));
}

function reqP(req) {
  return new Promise((res, rej) => {
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

const store = {
  async allBooks() {
    const s = await tx('books');
    const list = await reqP(s.getAll());
    return (list || []).sort((a, b) => b.created - a.created);
  },
  async addBook(book) {
    const s = await tx('books', 'readwrite');
    return reqP(s.add(book));
  },
  async getBook(id) {
    const s = await tx('books');
    return reqP(s.get(id));
  },
  async delBook(id) {
    const db = await openDB();
    return new Promise((res, rej) => {
      const t = db.transaction(['books', 'chapters', 'analyses'], 'readwrite');
      t.objectStore('books').delete(id);
      const chs = t.objectStore('chapters').index('book').openCursor(IDBKeyRange.only(id));
      chs.onsuccess = (e) => {
        const c = e.target.result;
        if (c) { c.delete(); c.continue(); }
      };
      t.objectStore('analyses').delete(id);
      t.oncomplete = () => res(true);
      t.onerror = () => rej(t.error);
    });
  },
  async addChapters(bookId, chapters) {
    const db = await openDB();
    return new Promise((res, rej) => {
      const t = db.transaction('chapters', 'readwrite');
      const s = t.objectStore('chapters');
      chapters.forEach((c, i) => s.add({ bookId, idx: i, title: c[0], content: c[1] }));
      t.oncomplete = () => res(true);
      t.onerror = () => rej(t.error);
    });
  },
  async chapters(bookId) {
    const s = await tx('chapters');
    const idx = s.index('book');
    const list = await reqP(idx.getAll(IDBKeyRange.only(bookId)));
    return (list || []).sort((a, b) => a.idx - b.idx);
  },
  async getAnalysis(bookId) {
    const s = await tx('analyses');
    return reqP(s.get(bookId));
  },
  async saveAnalysis(a) {
    const s = await tx('analyses', 'readwrite');
    return reqP(s.put(a));
  },
  async cfg(key, val) {
    const s = await tx('meta', val === undefined ? 'readonly' : 'readwrite');
    if (val === undefined) {
      const r = await reqP(s.get(key));
      return r ? r.v : undefined;
    }
    return reqP(s.put({ k: key, v: val }));
  },
};

/* ============ 工具 ============ */
function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = 'toast' + (isErr ? ' err' : ''); }, 3200);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtNum(n) {
  n = Number(n) || 0;
  return n >= 10000 ? (n / 10000).toFixed(1) + ' 万' : String(n);
}

function setStatus(text, kind = '') {
  $('#statusText').textContent = text;
  $('#statusDot').className = 'dot ' + kind;
}

/* ============ Markdown 渲染 ============ */
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

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { closeList(); closeQuote(); continue; }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList(); closeQuote();
      const lv = h[1].length === 1 ? 2 : Math.min(3, h[1].length);
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

/* ============ 文本解析 ============ */
const CHAP_PATTERNS = [
  /^\s*第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷集部篇]/,
  /^\s*(?:正文\s*)?第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷集部篇]/,
  /^\s*(?:Chapter|CHAPTER)\s+[\dIVXLC]+/,
  /^\s*[【\[]?\s*第[0-9]+[章节回]\s*[】\]]?/,
];
const MAX_TITLE_LEN = 40;

function looksLikeTitle(line) {
  const s = line.trim();
  if (!s || s.length > MAX_TITLE_LEN) return false;
  const core = s.replace(/^[\s　#=\-—*_]+|[\s　#=\-—*_]+$/g, '');
  if (!core || core.length > MAX_TITLE_LEN) return false;
  return CHAP_PATTERNS.some(p => p.test(core));
}

function normalizeText(t) {
  return String(t)
    .replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    .replace(/\u3000/g, ' ')
    .replace(/\n{3,}/g, '\n\n');
}

function splitChapters(text) {
  text = normalizeText(text);
  const lines = text.split('\n');
  const chapters = [];
  let curTitle = null, buf = [];

  const flush = () => {
    const body = buf.join('\n').trim();
    if (curTitle === null && !body) return;
    chapters.push([curTitle || '开篇', body]);
    buf = [];
  };

  for (const line of lines) {
    if (looksLikeTitle(line)) {
      flush();
      curTitle = line.trim().replace(/^[\s　#=\-—*_]+|[\s　#=\-—*_]+$/g, '');
    } else {
      buf.push(line);
    }
  }
  flush();

  const cleaned = chapters.filter(c => c[1].trim());
  if (cleaned.length <= 1) return chunkBySize(text);

  // 只有当首章标题不像真章节、且内容极短时，才认为是书名/前言并入下一章
  const first = cleaned[0];
  const firstTitleReal = CHAP_PATTERNS.some(p => p.test(first[0]));
  if (!firstTitleReal && first[1].length < 120 && cleaned.length >= 2) {
    const [t0, c0] = cleaned[0], [t1, c1] = cleaned[1];
    return [[t1, (c0 + '\n' + c1).trim()], ...cleaned.slice(2)];
  }
  return cleaned;
}

function chunkBySize(text, target = 3000) {
  text = text.trim();
  if (!text) return [];
  const n = Math.max(1, Math.ceil(text.length / target));
  const size = Math.ceil(text.length / n);
  const out = [];
  for (let i = 0; i < n; i++) {
    const seg = text.slice(i * size, (i + 1) * size).trim();
    if (seg) out.push([`片段 ${i + 1}`, seg]);
  }
  return out;
}

function readTxtFile(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => {
      let text = r.result;
      // 粗判是否乱码（GBK 用 UTF-8 读会出现大量替换符）
      const bad = (text.match(/\ufffd/g) || []).length;
      if (bad > text.length * 0.01) {
        // 用 GBK 重读
        const r2 = new FileReader();
        r2.onload = () => res(r2.result);
        r2.onerror = rej;
        r2.readAsText(file, 'GBK');
      } else {
        res(text);
      }
    };
    r.onerror = rej;
    r.readAsText(file, 'UTF-8');
  });
}

async function readEpubFile(file) {
  if (typeof JSZip === 'undefined') throw new Error('JSZip 未加载，无法解析 epub');
  const zip = await JSZip.loadAsync(file);
  const names = Object.keys(zip.files);

  // 找 opf
  let opfPath = null;
  if (zip.file('META-INF/container.xml')) {
    const c = await zip.file('META-INF/container.xml').async('string');
    const m = c.match(/full-path="([^"]+\.opf)"/i);
    if (m) opfPath = m[1];
  }
  if (!opfPath) opfPath = names.find(n => n.endsWith('.opf'));
  if (!opfPath) throw new Error('epub 中未找到 opf 文件');

  const base = opfPath.includes('/') ? opfPath.slice(0, opfPath.lastIndexOf('/') + 1) : '';
  const opf = await zip.file(opfPath).async('string');

  const doc = new DOMParser().parseFromString(opf, 'application/xml');
  let title = '', author = '';
  const tEl = doc.querySelector('title'), aEl = doc.querySelector('creator');
  if (tEl) title = tEl.textContent.trim();
  if (aEl) author = aEl.textContent.trim();

  const manifest = {};
  doc.querySelectorAll('item').forEach(it => {
    manifest[it.getAttribute('id')] = it.getAttribute('href');
  });

  const spineFiles = [];
  doc.querySelectorAll('itemref').forEach(ir => {
    const id = ir.getAttribute('idref');
    if (manifest[id]) {
      const full = (base + manifest[id]).replace(/\\/g, '/');
      if (zip.file(full)) spineFiles.push(full);
    }
  });

  const chapters = [];
  for (const f of spineFiles) {
    const raw = await zip.file(f).async('string');
    const p = new DOMParser().parseFromString(raw, 'text/html');
    let docTitle = p.title ? p.title.trim() : '';

    const body = p.body || p.documentElement;
    let txt = '';
    if (body) {
      body.querySelectorAll('script,style').forEach(n => n.remove());
      body.querySelectorAll('br').forEach(n => n.replaceWith('\n'));
      body.querySelectorAll('p,div,h1,h2,h3,h4,h5,h6,li,tr').forEach(n => {
        n.appendChild(document.createTextNode('\n'));
      });
      txt = normalizeText(body.textContent || '').trim();
    }
    if (!txt) continue;
    if (docTitle && !/^第|^Chapter/.test(docTitle)) {
      const first = txt.split('\n')[0].trim();
      if (looksLikeTitle(first)) docTitle = first;
    }
    chapters.push([docTitle || `第${chapters.length + 1}节`, txt]);
  }
  if (!chapters.length) throw new Error('epub 未解析出章节');
  return { title: title || file.name.replace(/\.epub$/i, ''), author, chapters };
}

/* ============ 拆书引擎 ============ */
const PLAN_SYSTEM = `你是网文结构分析师。下面会给你一本书的章节列表（只有章节号和标题，可能附带少量摘要）。
你的任务：根据章节标题与已知信息，推测性地把全书划分为若干个「故事单元」。

划分原则：
- 一个单元对应一条完整的故事线/一个大情节段落，通常包含多个连续章节。
- 依据故事节点（如开篇、转折、高潮、换地图、换主线）划分，不要按固定章数机械切分。
- 单元之间必须连续覆盖、不重叠、不留空。
- 书名/卷名暗示的新阶段要作为切分信号。

严格按 JSON 数组输出，不要任何解释、不要 markdown 代码块标记：
[{"name":"单元名(6-12字)","chap_from":起始章号,"chap_to":结束章号,"reason":"划分依据(30字内)"}]
章号使用给定的整数章号。`;

const UNIT_SYSTEM = `你是一个专业的网文文字分析智能助手，专门帮助用户拆解小说中的单元故事，提供分析内容的结构化输出。

注意事项：
1. 先通读文章故事架构，并将同一故事线的故事划分单元，每单元可以划分为多个章节为一个单元，并不是每章单独一个单元，每个单元章节必须根据内容的故事节点去划分的。
2. 单元划分完之后，根据每个单元的内容，提取并概括该单元中的核心设定与世界观细节。
3. 分析角色相关关系。

任务：
4. 根据故事情节，将文章尝试分成故事单元。
5. 总结每个单元的核心内容。
6. 总结每个单元出现的所有角色，梳理人物关系。

输出格式：按照划分好的单元，依次输出单元名、章节范围、故事梗概、登场角色、人物关系。
⚠️ 角色关系要特别详细，把该出场角色的所有相关信息，包括个人信息背景、所属阵营、文章中扮演的角色，并给出该角色所有的作用。

本次你只需要分析【一个】单元。输出格式如下（Markdown）：

## 单元名
（单元名称）

## 章节范围
第 X 章 — 第 Y 章

## 核心设定与世界观
（本单元涉及的核心设定、力量体系、地理/势力格局等细节）

## 故事梗概
（按情节推进顺序，分点叙述，每点说清"谁做了什么、导致什么结果"）

## 登场角色
对每个角色：
- **角色名**（身份/阵营）
  - 个人信息背景：（年龄、出身、能力、性格、初始处境）
  - 所属阵营：
  - 在本单元中扮演的角色：（他做了什么、推动/阻碍了什么）
  - 作用：（对主线的作用、对他人物的作用、象征/伏笔作用）

## 人物关系
逐对说明关系：A → B：关系性质 + 具体表现 + 本单元中的变化。
关系要具体，不要只写"朋友/敌人"这样的标签。`;

async function llm(messages, { temperature = 0.3, maxTokens = 4000 } = {}) {
  if (!state.cfg.apiKey) throw new Error('未设置 API Key（导入弹窗 → 设置）');

  const res = await fetch('https://api.deepseek.com/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + state.cfg.apiKey,
    },
    body: JSON.stringify({
      model: state.cfg.model || 'deepseek-chat',
      messages,
      temperature,
      max_tokens: maxTokens,
      stream: false,
    }),
  });

  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.error?.message || msg; } catch {}
    throw new Error(msg);
  }
  const d = await res.json();
  return d.choices[0].message.content;
}

function cleanJson(s) {
  s = s.trim().replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
  const i = s.indexOf('['), j = s.lastIndexOf(']');
  if (i >= 0 && j > i) s = s.slice(i, j + 1);
  return s.replace(/(?<=[:\[,])\s*undefined\s*(?=[,}\]])/g, 'null');
}

const CHUNK_CHARS = 12000;
const PLAN_BATCH = 60;

async function planUnits(book, chapters, onProgress) {
  const batches = [];
  for (let i = 0; i < chapters.length; i += PLAN_BATCH) {
    batches.push(chapters.slice(i, i + PLAN_BATCH));
  }

  let all = [];
  for (let bi = 0; bi < batches.length; bi++) {
    const batch = batches[bi];
    const lines = batch.map(c =>
      `第${c.idx + 1}章 ${c.title}｜${(c.content || '').replace(/\s+/g, ' ').slice(0, 90)}`
    ).join('\n');

    const user = `书名：《${book.title}》\n全书共 ${chapters.length} 章，本批为第 ${batch[0].idx + 1} — ${batch[batch.length - 1].idx + 1} 章。\n\n章节列表：\n${lines}\n\n请只输出 JSON 数组。章号必须在 ${batch[0].idx + 1} 到 ${batch[batch.length - 1].idx + 1} 之间。`;

    const raw = await llm(
      [{ role: 'system', content: PLAN_SYSTEM }, { role: 'user', content: user }],
      { temperature: 0.2, maxTokens: 3000 }
    );

    let arr = [];
    try { arr = JSON.parse(cleanJson(raw)); } catch { arr = []; }
    for (const u of arr) {
      let a = parseInt(u.chap_from, 10), b = parseInt(u.chap_to, 10);
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
      const lo = batch[0].idx + 1, hi = batch[batch.length - 1].idx + 1;
      a = Math.max(lo, Math.min(hi, a));
      b = Math.max(lo, Math.min(hi, b));
      if (b < a) [a, b] = [b, a];
      all.push({ name: String(u.name || '').trim().slice(0, 30) || `第${a}-${b}章`,
                 chap_from: a, chap_to: b, reason: String(u.reason || '').slice(0, 60) });
    }
    if (onProgress) onProgress((bi + 1) / batches.length);
  }
  return normalizeUnits(all, chapters.length);
}

function normalizeUnits(units, total) {
  if (!units.length) {
    units = [];
    for (let i = 0; i < total; i += 10) {
      units.push({ name: `第${i + 1}-${Math.min(i + 10, total)}章段`,
                   chap_from: i + 1, chap_to: Math.min(i + 10, total), reason: '自动兜底' });
    }
  }
  units.sort((a, b) => a.chap_from - b.chap_from || a.chap_to - b.chap_to);

  const fixed = [];
  let cursor = 1;
  for (const u of units) {
    if (u.chap_to < cursor) continue;
    const a = Math.max(u.chap_from, cursor);
    if (u.chap_to < a) continue;
    fixed.push({ ...u, chap_from: a, chap_to: u.chap_to });
    cursor = u.chap_to + 1;
    if (cursor > total) break;
  }
  if (cursor <= total) {
    fixed.push({ name: `第${cursor}-${total}章`, chap_from: cursor, chap_to: total, reason: '尾部补齐' });
  }
  fixed.forEach((u, i) => { if (!u.name.trim()) u.name = `单元${i + 1}`; });
  return fixed;
}

function buildUnitPayload(chapters, unit) {
  const sub = chapters.filter(c => c.idx + 1 >= unit.chap_from && c.idx + 1 <= unit.chap_to);
  if (!sub.length) return '';

  const overhead = sub.reduce((s, c) => s + `\n【第${c.idx + 1}章 ${c.title}】\n`.length, 0);
  const budget = Math.max(2000, CHUNK_CHARS - overhead);
  const per = Math.max(600, Math.floor(budget / sub.length));

  return sub.map(c => {
    let body = (c.content || '').trim();
    if (body.length > per) {
      const head = Math.floor(per * 0.62);
      body = body.slice(0, head) + '\n……（中段略）……\n' + body.slice(-(per - head));
    }
    return `\n【第${c.idx + 1}章 ${c.title}】\n${body}`;
  }).join('');
}

async function analyzeUnit(book, chapters, unit) {
  const payload = buildUnitPayload(chapters, unit);
  const user = `书名：《${book.title}》\n章节范围：第${unit.chap_from}章 — 第${unit.chap_to}章\n预估单元名：${unit.name}\n\n以下是该单元的全部章节正文：\n${payload}\n\n请严格按格式输出该单元的分析。`;
  return llm(
    [{ role: 'system', content: UNIT_SYSTEM }, { role: 'user', content: user }],
    { temperature: 0.4, maxTokens: 6000 }
  );
}

/* ============ 界面：书架 ============ */
async function loadBooks(selectId) {
  state.books = await store.allBooks();
  renderBooks();
  if (selectId) await selectBook(selectId);
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
        <span>${b.chapterCnt} 章</span>
        <span>${fmtNum(b.charCnt)} 字</span>
        ${b.hasAnalysis ? '<span>· 已拆解</span>' : ''}
      </div>
    </div>`).join('');
  $$('.bookitem').forEach(e => e.onclick = () => selectBook(Number(e.dataset.id)));
}

/* ============ 界面：书籍详情 ============ */
async function selectBook(id) {
  state.bookId = id;
  $$('.bookitem').forEach(e => e.classList.toggle('active', Number(e.dataset.id) === id));

  $('#placeholder').hidden = true;
  $('#bookView').hidden = false;

  const book = await store.getBook(id);
  if (!book) { toast('书籍不存在', true); return; }
  state.book = book;

  const chapters = await store.chapters(id);
  state.chapters = chapters;
  if (!chapters.some(c => c.idx === state.viewChapter)) state.viewChapter = 0;

  renderBook(book, chapters);

  const analysis = await store.getAnalysis(id);
  renderAnalysis(analysis);

  switchTab('units');
}

function renderBook(book, chapters) {
  $('#bkTitle').textContent = book.title;
  const src = { file: '本地文件', paste: '粘贴文本' }[book.source] || '本地导入';
  $('#bkMeta').innerHTML = `
    <span>${book.chapterCnt} 章</span>
    <span>${fmtNum(book.charCnt)} 字</span>
    <span>${src}</span>`;

  $('#paneChapters').innerHTML = chapters.map(c => `
    <div class="chap-row ${c.idx === state.viewChapter ? 'active' : ''}" data-idx="${c.idx}">
      <span class="chap-no">${c.idx + 1}</span>
      <span class="chap-name">${esc(c.title)}</span>
      <span class="chap-size">${fmtNum(c.content.length)} 字</span>
    </div>`).join('');

  $$('#paneChapters .chap-row').forEach(r => r.onclick = () => {
    state.viewChapter = Number(r.dataset.idx);
    $$('#paneChapters .chap-row').forEach(x => x.classList.toggle('active', x === r));
    switchTab('raw');
    renderRaw();
  });
}

function renderRaw() {
  const c = state.chapters.find(x => x.idx === state.viewChapter);
  if (!c) { $('#paneRaw').innerHTML = '<div class="pane-empty">章节不存在</div>'; return; }
  const paras = c.content.split('\n').filter(p => p.trim())
    .map(p => `<p>${esc(p.trim())}</p>`).join('');
  $('#paneRaw').innerHTML =
    `<div class="reader"><h3>${esc(c.title)}</h3>${paras || '<p>（本章无内容）</p>'}</div>`;
}

/* ============ 界面：拆解结果 ============ */
function renderAnalysis(a) {
  if (!a) {
    $('#analyseStatus').innerHTML = '';
    $('#paneUnits').innerHTML = `
      <div class="pane-empty"><b>还没有拆解结果</b>
      点右上角「开始拆书」，系统会先划分故事单元，再逐个单元深度拆解。</div>`;
    return;
  }

  // 状态条
  const el = $('#analyseStatus');
  if (a.status === 'done') {
    el.innerHTML = `<div class="statusbar done">
      <div class="sb-top"><span class="sb-stage">拆解完成</span>
      <span class="sb-pct">${a.units.length} 个单元</span></div>
      <div class="progress"><i style="width:100%"></i></div>
      <div class="sb-msg">${esc(a.message || '')}</div></div>`;
  } else if (a.status === 'error') {
    el.innerHTML = `<div class="statusbar error">
      <div class="sb-top"><span class="sb-stage">拆解中断</span>
      <button class="btn ghost" id="btnResume" style="padding:5px 12px;font-size:12.5px">继续</button></div>
      <div class="sb-msg">${esc(a.message || '未知错误')}</div></div>`;
    const r = $('#btnResume');
    if (r) r.onclick = () => runAnalysis(true);
  } else {
    const pct = Math.round((a.progress || 0) * 100);
    el.innerHTML = `<div class="statusbar">
      <div class="sb-top"><span class="sb-stage">${esc(a.stage || '准备中')}</span>
      <span class="sb-pct">${pct}%</span></div>
      <div class="progress"><i style="width:${pct}%"></i></div>
      <div class="sb-msg">${esc(a.message || '')}</div></div>`;
  }

  // 单元
  const units = a.units || [];
  if (!units.length) {
    $('#paneUnits').innerHTML = a.status === 'running'
      ? '<div class="pane-empty">正在划分故事单元…</div>'
      : '<div class="pane-empty"><b>还没有拆解结果</b>点「开始拆书」开始。</div>';
    return;
  }

  const prevOpen = new Set($$('#paneUnits .unit-card.open').map(e => e.dataset.seq));
  $('#paneUnits').innerHTML = units.map(u => {
    const open = prevOpen.has(String(u.seq)) || !!u.detail;
    return `<div class="unit-card ${open ? 'open' : ''}" data-seq="${u.seq}">
      <div class="uc-head">
        <span class="uc-seq">${u.seq}</span>
        <div class="uc-main">
          <div class="uc-name">${esc(u.name)}</div>
          <div class="uc-range">第 ${u.chap_from} — ${u.chap_to} 章${u.detail ? '' : ' · 待分析'}</div>
        </div>
        <span class="uc-arrow">▶</span>
      </div>
      <div class="uc-body">
        ${u.detail ? `<div class="md">${mdToHtml(u.detail)}</div>`
                   : '<div class="pane-empty" style="padding:26px">该单元正在分析或尚未生成</div>'}
      </div>
    </div>`;
  }).join('');

  $$('#paneUnits .unit-card .uc-head').forEach(h =>
    h.onclick = () => h.parentElement.classList.toggle('open'));
}

/* ============ 执行拆书 ============ */
async function runAnalysis(resume = false) {
  if (state.busy) return;
  if (!state.cfg.apiKey) {
    toast('请先在「设置」里填 API Key', true);
    openModal('settings');
    return;
  }

  const bookId = state.bookId;
  const book = state.book;
  const chapters = state.chapters;
  if (!chapters || !chapters.length) { toast('这本书没有章节', true); return; }

  let a = resume ? await store.getAnalysis(bookId) : null;
  if (!a) {
    a = { bookId, status: 'running', stage: '准备中', progress: 0,
          message: '', units: [], createdAt: Date.now() };
  } else {
    a.status = 'running';
    a.message = '继续拆解…';
  }

  state.busy = true;
  setStatus('拆书中…', 'busy');
  $('#btnAnalyse').disabled = true;

  const save = async () => { await store.saveAnalysis(a); renderAnalysis(a); };

  try {
    await save();

    // 阶段一：分单元
    if (!a.units.length) {
      a.stage = '梳理整体架构';
      a.message = '正在划分故事单元…';
      await save();

      const planned = await planUnits(book, chapters, (p) => {
        a.progress = p * 0.30;
        a.message = `正在划分故事单元… ${Math.round(p * 100)}%`;
        store.saveAnalysis(a);
        renderAnalysis(a);
      });

      a.units = planned.map((u, i) => ({
        seq: i + 1, name: u.name, chap_from: u.chap_from, chap_to: u.chap_to, detail: '',
      }));
      a.progress = 0.30;
      a.stage = '划分完成';
      a.message = `共划分 ${a.units.length} 个单元`;
      await save();
    }

    // 阶段二：逐单元分析
    const total = a.units.length;
    for (let i = 0; i < total; i++) {
      const u = a.units[i];
      if (u.detail) continue;   // 断点续拆

      a.stage = `分析单元 ${i + 1}/${total}`;
      a.progress = 0.30 + (i / total) * 0.68;
      a.message = `正在深度分析：${u.name}`;
      await save();

      u.detail = await analyzeUnit(book, chapters, u);
      await save();
    }

    a.status = 'done';
    a.stage = '完成';
    a.progress = 1;
    a.message = `拆书完成，共 ${total} 个单元`;
    await save();

    // 标记这本书已拆解过（原地更新，不新建记录）
    const bk = await store.getBook(bookId);
    if (bk && !bk.hasAnalysis) {
      bk.hasAnalysis = true;
      const s = await tx('books', 'readwrite');
      await reqP(s.put(bk));
    }

    setStatus('就绪');
    toast('拆书完成');
  } catch (e) {
    a.status = 'error';
    a.message = e.message || String(e);
    await save();
    setStatus('出错', 'err');
    toast('拆书失败：' + a.message, true);
  } finally {
    state.busy = false;
    $('#btnAnalyse').disabled = false;
    await loadBooks();
    if (state.bookId) await selectBook(state.bookId);
  }
}

/* ============ 导入 ============ */
function openModal(tabName) {
  $('#modalMask').hidden = false;
  $('#importHint').textContent = '';
  state.pendingFile = null;
  const fi = $('#fileInput');
  if (fi) fi.value = '';
  $('.dz-title').textContent = '点击选择文件';
  $('.dz-sub').textContent = '支持 .txt / .epub，自动识别章节';

  // 未配置 Key 时默认停在设置页，其余情况一律回到「文件」
  const target = tabName || (state.cfg.apiKey ? 'file' : 'settings');
  const btn = $$('.modal .tab').find(t => t.dataset.itab === target);
  if (btn) btn.click();
}

function closeModal() { $('#modalMask').hidden = true; }

async function doImport() {
  if (!$('#ipaneSettings').hidden) {
    // 保存设置
    state.cfg.apiKey = $('#apiKey').value.trim();
    state.cfg.model = $('#apiKey').value.trim() ? ($('#apiModel').value.trim() || 'deepseek-chat') : '';
    await store.cfg('apiKey', state.cfg.apiKey);
    await store.cfg('model', state.cfg.model);
    toast('设置已保存');
    closeModal();
    return;
  }

  const btn = $('#btnDoImport');

  // 兜底：如果当前显示的既不是文件也不是粘贴面板，强制回到文件面板
  const fileVisible = !$('#ipaneFile').hidden;
  const pasteVisible = !$('#ipanePaste').hidden;
  if (!fileVisible && !pasteVisible) {
    const t = $$('.modal .tab').find(x => x.dataset.itab === 'file');
    if (t) t.click();
    $('#importHint').textContent = '已切到「文件」，请选择要导入的书';
    return;
  }

  // 前置校验：失败时不要锁住按钮，否则会永久变灰
  if (fileVisible && !state.pendingFile) {
    $('#importHint').textContent = '请先点击上方虚线框选择文件';
    toast('请先选择文件', true);
    return;
  }
  if (pasteVisible && !$('#pasteText').value.trim()) {
    $('#importHint').textContent = '粘贴框是空的';
    toast('内容为空', true);
    return;
  }

  btn.disabled = true;
  $('#importHint').textContent = '正在解析…';

  try {
    let title, author = '', chapters, source;

    if (fileVisible) {
      const f = state.pendingFile;
      if (!f) throw new Error('文件已丢失，请重新选择');
      if (/\.epub$/i.test(f.name)) {
        $('#importHint').textContent = '正在解析 epub…';
        const r = await readEpubFile(f);
        title = r.title; author = r.author; chapters = r.chapters;
      } else {
        const text = await readTxtFile(f);
        title = f.name.replace(/\.[^.]+$/, '');
        chapters = splitChapters(text);
      }
      source = 'file';
    } else {
      const text = $('#pasteText').value;
      title = $('#pasteTitle').value.trim() || '粘贴文本';
      chapters = splitChapters(text);
      source = 'paste';
    }

    if (!chapters.length) throw new Error('未能解析出任何章节');

    const charCnt = chapters.reduce((s, c) => s + c[1].length, 0);
    const bookId = await store.addBook({
      title, author, source, created: Date.now(),
      chapterCnt: chapters.length, charCnt, hasAnalysis: false,
    });
    await store.addChapters(bookId, chapters);

    closeModal();
    toast(`已导入《${title}》，${chapters.length} 章`);
    $('#pasteText').value = '';
    $('#pasteTitle').value = '';
    // 清空已选文件，避免下次导入带上一本书
    state.pendingFile = null;
    const fi = $('#fileInput');
    if (fi) fi.value = '';
    $('#importHint').textContent = '';
    $('.dz-title').textContent = '点击选择文件';
    $('.dz-sub').textContent = '支持 .txt / .epub，自动识别章节';
    await loadBooks(bookId);
  } catch (e) {
    $('#importHint').textContent = '导入失败：' + (e.message || e);
    toast('导入失败：' + (e.message || e), true);
  } finally {
    btn.disabled = false;
  }
}

/* ============ 删除 ============ */
async function deleteBook() {
  if (!state.bookId) return;
  if (!confirm('确定删除这本书及其拆解结果？')) return;
  await store.delBook(state.bookId);
  state.bookId = null; state.book = null; state.chapters = null;
  $('#bookView').hidden = true;
  $('#placeholder').hidden = false;
  await loadBooks();
  toast('已删除');
}


/* ============ 人物关系图 ============ */
/*
 * 数据来源：已有的拆书结果（每个单元的 detail markdown）。
 * 解析其中的「登场角色」和「人物关系」两节，抽成节点与边。
 * 不额外调用 LLM，省钱且不会引入新的不一致。
 */

const REL_PALETTE = [
  '#e8734a', '#4ec9a0', '#5b9bd5', '#b07cd8',
  '#e0b04a', '#e05c7a', '#56b8c4', '#8fa564',
];

/* ---- 从 markdown 里抽取角色与关系 ---- */

function splitSections(md) {
  // 只按二级标题（##）切分，三级标题（###）保留在 body 里，
  // 因为角色名和关系对经常写在 ### 里
  const secs = [];
  let cur = null;
  for (const line of md.split('\n')) {
    const m = line.match(/^##(?!#)\s+(.+?)\s*$/);
    if (m) {
      if (cur) secs.push(cur);
      cur = { title: m[1], body: [] };
    } else if (cur) {
      cur.body.push(line);
    }
  }
  if (cur) secs.push(cur);
  return secs;
}

function parseCharacters(md) {
  const sec = splitSections(md).find(s => /登场角色|角色/.test(s.title));
  if (!sec) return [];

  const chars = [];
  let cur = null;

  for (const raw of sec.body) {
    const line = raw.trim();
    if (!line) continue;

    // 角色行：- **名字**（身份/阵营）  或  - **名字**
    const head = line.match(/^[-*+]\s*\*\*(.+?)\*\*\s*[（(]?\s*([^）)]*)[）)]?\s*$/);
    if (head) {
      const name = head[1].trim().replace(/[：:].*$/, '');
      if (!name || name.length > 12) continue;
      cur = { name, role: (head[2] || '').trim(), info: '', camp: '', action: '', effect: '' };
      chars.push(cur);
      continue;
    }

    if (!cur) continue;

    // 子项：  - 个人信息背景：xxx
    const kv = line.match(/^[-*+]\s*(?:>\s*)?\*{0,2}([^：:]{2,12})[：:]\s*(.*)$/);
    if (kv) {
      const k = kv[1].replace(/\*/g, '').trim();
      const v = kv[2].trim();
      if (/背景|信息|出身|年龄/.test(k)) cur.info = v;
      else if (/阵营|势力|所属/.test(k)) cur.camp = v;
      else if (/扮演|角色/.test(k)) cur.action = v;
      else if (/作用/.test(k)) cur.effect = v;
      else if (!cur.info) cur.info = v;
      continue;
    }

    // 续行
    const t = line.replace(/^[-*+]\s*/, '').replace(/\*\*/g, '').trim();
    if (t && !cur.info) cur.info = t;
  }

  return chars.filter(c => c.name);
}

function parseRelations(md, knownNames) {
  const sec = splitSections(md).find(s => /人物关系|关系/.test(s.title));
  if (!sec) return [];

  const rels = [];
  const seen = new Set();

  // 统一清洗角色名：去加粗、去括号注释、去方括号、去列表符号
  const cleanName = (x) => String(x || '')
    .replace(/\*\*/g, '')
    .replace(/[（(][^）)]*[）)]/g, '')
    .replace(/[\[\]【】《》""'']/g, '')
    .replace(/^[\s\-—*+•\d.)、]+/, '')
    .replace(/[\s\-—*+•]+$/, '')
    .trim();

  const push = (a, b, desc) => {
    a = cleanName(a);
    b = cleanName(b);
    if (!a || !b || a === b) return;
    if (a.length > 12 || b.length > 12) return;
    // 只保留已知角色之间的边，避免把普通词当成角色
    if (knownNames.length && (!knownNames.includes(a) || !knownNames.includes(b))) return;
    const key = [a, b].sort().join('|');
    if (seen.has(key)) return;
    seen.add(key);
    rels.push({ a, b, desc: String(desc || '').replace(/\*\*/g, '').trim() });
  };

  // 预处理：去掉加粗标记与方括号标签，统一箭头符号
  const lines = sec.body.map(l =>
    l.replace(/\*\*/g, '')
     .replace(/【[^】]*】/g, '')       // 【主角】这类标签直接去掉
     .replace(/\[[^\]]*\]/g, '')
     .replace(/[➜➔⟶→]/g, '->')
  );

  const ARROW = /->/;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;

    // ---- 格式 A：A -> B：描述（也兼容 —— 破折号、~ 波浪线）----
    let m = line.match(/^[-*+•\d.)、\s]*(.+?)\s*(?:->|—>|-->|=>|——|~|～|＜|<>|↔|<->)\s*(.+?)\s*[：:]\s*(.*)$/);
    if (m) { push(m[1], m[2], m[3]); continue; }

    // ---- 格式 B：### A ↔ B ，下面几行是描述 ----
    const h = line.match(/^#{3,4}\s*(.+?)\s*(?:↔|->|<->|——|—|与|和|＆|&)\s*(.+?)\s*$/);
    if (h) {
      const desc = [];
      for (let j = i + 1; j < lines.length && desc.length < 3; j++) {
        const t = lines[j].trim();
        if (!t) { if (desc.length) break; else continue; }
        if (/^#{2,4}\s/.test(t)) break;
        desc.push(t);
      }
      push(h[1], h[2], desc.join(' '));
      continue;
    }

    // ---- 格式 C：A：描述（从描述里反查另一个角色）----
    m = line.match(/^[-*+•\d.)、\s]*([^\s，,、：:]{1,12})\s*[：:]\s*(.*)$/);
    if (m) {
      const a = cleanName(m[1]);
      const desc = m[2] || '';
      const others = knownNames.filter(n => n !== a && desc.includes(n));
      if (others.length) {
        others.forEach(o => push(a, o, desc));
      }
      continue;
    }

    // ---- 格式 D：一行里同时出现两个角色名 ----
    const mentioned = knownNames.filter(n => line.includes(n));
    if (mentioned.length >= 2) {
      push(mentioned[0], mentioned[1], line.replace(/^[-*+•\d.)、\s]+/, ''));
    }
  }

  return rels;
}

/* ---- 力导向布局（简易实现） ---- */

function layoutGraph(nodes, edges, width, height) {
  const cx = width / 2, cy = height / 2;
  const n = nodes.length;
  if (!n) return nodes;

  // 初始：环形分布，避免全部重叠
  nodes.forEach((nd, i) => {
    const ang = (i / n) * Math.PI * 2;
    const rad = Math.min(width, height) * 0.30;
    nd.x = cx + Math.cos(ang) * rad;
    nd.y = cy + Math.sin(ang) * rad;
    nd.vx = 0; nd.vy = 0;
  });

  const byName = {};
  nodes.forEach(nd => byName[nd.name] = nd);

  // 理想边长按画布尺寸和节点数估算，给足空间避免挤成一团
  const k = Math.sqrt((width * height) / Math.max(1, n)) * 1.05;
  const iterations = 420;

  for (let it = 0; it < iterations; it++) {
    const t = 1 - it / iterations;   // 温度，逐渐冷却

    // 斥力（所有节点两两）
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = (Math.random() - 0.5) * 2; dy = (Math.random() - 0.5) * 2; d2 = 4; }
        const d = Math.sqrt(d2);
        const f = (k * k) / d;
        const ux = dx / d, uy = dy / d;
        a.vx += ux * f * t * 0.05; a.vy += uy * f * t * 0.05;
        b.vx -= ux * f * t * 0.05; b.vy -= uy * f * t * 0.05;
      }
    }

    // 引力（边）
    for (const e of edges) {
      const a = byName[e.a], b = byName[e.b];
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d * d) / k * 0.045;
      const ux = dx / d, uy = dy / d;
      a.vx += ux * f * t; a.vy += uy * f * t;
      b.vx -= ux * f * t; b.vy -= uy * f * t;
    }

    // 向心力，防止飘散
    for (const nd of nodes) {
      nd.vx += (cx - nd.x) * 0.005 * t;
      nd.vy += (cy - nd.y) * 0.005 * t;
    }

    // 应用位移并限制边界
    const pad = 78;
    for (const nd of nodes) {
      nd.vx *= 0.82; nd.vy *= 0.82;
      nd.x += Math.max(-28, Math.min(28, nd.vx));
      nd.y += Math.max(-28, Math.min(28, nd.vy));
      nd.x = Math.max(pad, Math.min(width - pad, nd.x));
      nd.y = Math.max(pad, Math.min(height - pad, nd.y));
    }
  }

  return nodes;
}


/* ---- 关系图交互：拖动 / 缩放 / 点击 ---- */
/*
 * 之前 SVG 设了 touch-action:none 但没有实现任何手势，
 * 所以既不能拖动也影响了页面滚动。
 * 这里实现统一的指针事件处理：
 *   - 单指拖动：平移画布
 *   - 双指捏合：缩放
 *   - 点击节点（无位移）：选中角色
 */

function attachGraphInteraction(stage, svg, nodes, rels, onNodeClick) {
  const viewport = svg.querySelector('.rel-viewport') || svg;
  let tx = 0, ty = 0, scale = 1;
  const MIN_SCALE = 0.5, MAX_SCALE = 3;

  const applyTransform = () => {
    viewport.setAttribute('transform',
      `translate(${tx.toFixed(1)},${ty.toFixed(1)}) scale(${scale.toFixed(3)})`);
  };

  // ---- 指针状态 ----
  const pointers = new Map();
  let dragging = false;
  let moved = false;
  let startX = 0, startY = 0;
  let startTx = 0, startTy = 0;
  let pinchStartDist = 0, pinchStartScale = 1;

  const localToSvg = (clientX, clientY) => {
    const r = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const sx = vb.width / r.width;
    const sy = vb.height / r.height;
    return { x: (clientX - r.left) * sx, y: (clientY - r.top) * sy };
  };

  stage.addEventListener('pointerdown', (e) => {
    // 只处理落在 svg 上的
    if (!svg.contains(e.target)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.size === 1) {
      dragging = true;
      moved = false;
      startX = e.clientX; startY = e.clientY;
      startTx = tx; startTy = ty;
      // 记下按到的节点（可能是 circle/text，需要向上找 .rel-node）
      pressedNode = e.target.closest ? e.target.closest('.rel-node') : null;
      stage.setPointerCapture?.(e.pointerId);
    } else if (pointers.size === 2) {
      // 进入双指缩放
      dragging = false;
      const pts = [...pointers.values()];
      pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      pinchStartScale = scale;
    }
  });

  stage.addEventListener('pointermove', (e) => {
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.size === 2) {
      // 双指缩放
      const pts = [...pointers.values()];
      const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      const next = pinchStartScale * (dist / pinchStartDist);
      scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
      applyTransform();
      e.preventDefault();
      return;
    }

    if (!dragging) return;

    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!moved && Math.hypot(dx, dy) > 4) moved = true;
    if (!moved) return;
    svg.classList.add('dragging');

    const r = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    // 屏幕像素换算到 viewBox 坐标
    const kx = vb.width / r.width;
    const ky = vb.height / r.height;

    tx = startTx + dx * kx;
    ty = startTy + dy * ky;
    applyTransform();
    e.preventDefault();
  });

  // 记录按下时命中的节点，抬起时用它判断点击
  let pressedNode = null;

  const endPointer = (e) => {
    pointers.delete(e.pointerId);
    svg.classList.remove('dragging');
    if (pointers.size === 0) {
      dragging = false;
      // 没移动过，且按下时命中了节点 → 视为点击
      if (!moved && pressedNode) {
        onNodeClick(pressedNode);
      }
      pressedNode = null;
      moved = false;
    } else if (pointers.size === 1) {
      // 双指变单指：重置拖动起点，避免跳变
      const p = [...pointers.values()][0];
      dragging = true;
      startX = p.x; startY = p.y;
      startTx = tx; startTy = ty;
    }
  };
  stage.addEventListener('pointerup', endPointer);
  stage.addEventListener('pointercancel', endPointer);
  stage.addEventListener('pointerleave', (e) => {
    if (pointers.has(e.pointerId)) endPointer(e);
  });

  // ---- 鼠标滚轮缩放（桌面） ----
  stage.addEventListener('wheel', (e) => {
    if (!svg.contains(e.target)) return;
    e.preventDefault();
    const next = scale * (e.deltaY > 0 ? 0.92 : 1.08);
    scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
    applyTransform();
  }, { passive: false });

  // ---- 双击复位 ----
  stage.addEventListener('dblclick', (e) => {
    if (!svg.contains(e.target)) return;
    tx = 0; ty = 0; scale = 1;
    applyTransform();
  });

  return { applyTransform };
}

/* ---- 渲染 ---- */

function renderRelations(analysis) {
  const pane = $('#paneRelations');

  if (!analysis || !analysis.units || !analysis.units.length) {
    pane.innerHTML = `<div class="rel-empty"><b>还没有拆解结果</b>
      人物关系图依赖拆书结果，请先点「开始拆书」。</div>`;
    return;
  }

  const withDetail = analysis.units.filter(u => u.detail && u.detail.trim());
  if (!withDetail.length) {
    pane.innerHTML = `<div class="rel-empty"><b>拆解内容还在生成中</b>
      等单元分析完成后再来看关系图。</div>`;
    return;
  }

  // 记住上次选的单元
  const selIdx = Number(pane.dataset.sel || 0);
  const cur = withDetail[Math.min(selIdx, withDetail.length - 1)];

  const chars = parseCharacters(cur.detail);
  const names = chars.map(c => c.name);
  const rels = parseRelations(cur.detail, names);

  // 工具栏
  const options = withDetail.map((u, i) =>
    `<option value="${i}" ${u.seq === cur.seq ? 'selected' : ''}>第${u.seq}单元 · ${esc(u.name)}</option>`
  ).join('');

  pane.innerHTML = `
    <div class="rel-toolbar">
      <select class="rel-select" id="relUnitSel">${options}</select>
      <span class="rel-hint"><b>${names.length}</b> 个角色 · <b>${rels.length}</b> 条关系</span>
    </div>
    <div class="rel-stage" id="relStage"></div>
    <div class="rel-detail" id="relDetail" hidden></div>`;

  $('#relUnitSel').onchange = (e) => {
    pane.dataset.sel = e.target.value;
    renderRelations(analysis);
  };

  if (!names.length) {
    $('#relStage').innerHTML = `<div class="rel-empty"><b>本单元没有解析到角色</b>
      可能是该单元的「登场角色」格式与预期不符。</div>`;
    return;
  }

  drawGraph($('#relStage'), chars, rels, cur);

  function drawGraph(stage, chars, rels, unit) {
    // 画布宽度跟随可用区域。
    // 注意：面板可能是隐藏状态（clientWidth 为 0），
    // 所以用 window 宽度减去侧边栏和边距来估算，而不是依赖 clientWidth。
    const stageEl = $('#relStage');
    let availW = stageEl ? stageEl.clientWidth : 0;
    if (!availW || availW < 200) {
      const sidebar = $('#sidebar') ? $('#sidebar').offsetWidth : 272;
      const isNarrow = window.innerWidth < 700;
      availW = isNarrow ? window.innerWidth - 40
                        : window.innerWidth - sidebar - 76;
    }
    // 直接按可用宽度做画布，避免 viewBox 缩放导致字号变小
    const W = Math.max(300, Math.min(1000, Math.floor(availW)));

    // 高度按节点数估算，并保证每个节点有足够间距
    const perNode = Math.max(62, 340 / Math.max(1, Math.sqrt(chars.length)));
    const H = Math.max(360, Math.min(680, 180 + chars.length * perNode * 0.55));

    const nodes = chars.map((c, i) => ({
      name: c.name, role: c.role, info: c.info, camp: c.camp,
      action: c.action, effect: c.effect,
      // 出现次数多/关系多的节点画得更大
      weight: 1 + rels.filter(r => r.a === c.name || r.b === c.name).length,
      color: REL_PALETTE[i % REL_PALETTE.length],
    }));

    layoutGraph(nodes, rels, W, H);

    const byName = {};
    nodes.forEach(nd => byName[nd.name] = nd);

    // 边
    let edgeSvg = '';
    const seen = new Set();
    for (const e of rels) {
      const a = byName[e.a], b = byName[e.b];
      if (!a || !b) continue;
      const key = [e.a, e.b].sort().join('|');
      const dup = seen.has(key);
      seen.add(key);

      const strong = e.desc && e.desc.length > 0;
      edgeSvg += `<line class="rel-edge ${strong ? 'strong' : ''}" x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}"/>`;

      // 关系标签：沿边方向偏移放置，并避开节点中心，减少与角色名的重叠
      if (strong && !dup) {
        const dx = b.x - a.x, dy = b.y - a.y;
        const len = Math.hypot(dx, dy) || 1;
        // 放在靠 b 侧 62% 处，避开两端节点
        const mx = a.x + dx * 0.62;
        const my = a.y + dy * 0.62;
        // 垂直方向再推 12px，进一步错开
        const nx = -dy / len * 12, ny = dx / len * 12;
        const label = e.desc.length > 12 ? e.desc.slice(0, 12) + '…' : e.desc;
        edgeSvg += `<text class="rel-edge-label" x="${(mx + nx).toFixed(1)}" y="${(my + ny).toFixed(1)}">${esc(label)}</text>`;
      }
    }

    // 节点
    let nodeSvg = '';
    for (const nd of nodes) {
      const r = 20 + Math.min(14, nd.weight * 2.6);
      nodeSvg += `<g class="rel-node" data-name="${esc(nd.name)}" transform="translate(${nd.x.toFixed(1)},${nd.y.toFixed(1)})">
        <circle class="ring" r="${r + 4}"/>
        <circle r="${r}" fill="${nd.color}"/>
        <text y="${r + 17}">${esc(nd.name)}</text>
      </g>`;
    }

    stage.innerHTML = `
      <svg class="rel-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"
           style="width:100%;height:auto">
        <g class="rel-viewport">
          <g class="edges">${edgeSvg}</g>
          <g class="nodes">${nodeSvg}</g>
        </g>
      </svg>
      <div class="rel-legend">
        ${nodes.slice(0, 8).map(nd =>
          `<span><i style="background:${nd.color}"></i>${esc(nd.name)}</span>`).join('')}
      </div>`;

    // 点击节点：高亮并显示详情（由手势模块在「无位移」时回调）
    const handleNodeClick = (g) => {
        const name = g.dataset.name;
        const nd = nodes.find(x => x.name === name);
        if (!nd) return;

        const isSel = g.classList.contains('sel');
        $$('.rel-node', stage).forEach(x => x.classList.remove('sel', 'dim'));
        if (isSel) { $('#relDetail').hidden = true; return; }

        g.classList.add('sel');
        // 与它无关的节点淡化
        const related = new Set([name]);
        rels.forEach(r => { if (r.a === name) related.add(r.b); if (r.b === name) related.add(r.a); });
        $$('.rel-node', stage).forEach(x => {
          if (!related.has(x.dataset.name)) x.classList.add('dim');
        });

        showDetail(nd, rels.filter(r => r.a === name || r.b === name));
    };

    // 接入手势：拖动平移、双指缩放、点击选中
    attachGraphInteraction(stage, stage.querySelector('.rel-svg'), nodes, rels, handleNodeClick);

    function showDetail(nd, myRels) {
      const el = $('#relDetail');
      el.hidden = false;
      el.innerHTML = `
        <h3>${esc(nd.name)}${nd.role ? `　<span style="font-size:12.5px;color:var(--text3);font-weight:500">${esc(nd.role)}</span>` : ''}</h3>
        ${nd.info ? `<div class="kv"><b>背景</b>${esc(nd.info)}</div>` : ''}
        ${nd.camp ? `<div class="kv"><b>阵营</b>${esc(nd.camp)}</div>` : ''}
        ${nd.action ? `<div class="kv"><b>本单元</b>${esc(nd.action)}</div>` : ''}
        ${nd.effect ? `<div class="kv"><b>作用</b>${esc(nd.effect)}</div>` : ''}
        ${myRels.length ? `<ul class="rel-list">${myRels.map(r => {
          const other = r.a === nd.name ? r.b : r.a;
          return `<li><b style="color:var(--accent2)">${esc(other)}</b>：${esc(r.desc || '（无描述）')}</li>`;
        }).join('')}</ul>` : '<div class="kv" style="margin-top:10px;color:var(--text3)">本单元未记录与他人的关系</div>'}`;
    }
  }
}

/* ============ 标签切换 ============ */
function switchTab(name) {
  state.activeTab = name;
  $$('#bookTabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $('#paneUnits').hidden = name !== 'units';
  $('#paneRelations').hidden = name !== 'relations';
  $('#paneChapters').hidden = name !== 'chapters';
  $('#paneRaw').hidden = name !== 'raw';
  if (name === 'raw') renderRaw();
  if (name === 'relations') {
    store.getAnalysis(state.bookId).then(a => renderRelations(a));
  }
}

/* ============ 初始化 ============ */
/* 版本号：每次改动递增，用于确认线上是否为最新版 */
const APP_VERSION = '1.4.3';
const APP_BUILD = '2026-11-23';

async function init() {
  // 显示版本号
  const vl = $('#verLabel');
  if (vl) vl.textContent = `v${APP_VERSION}`;
  document.title = `书析 · 拆书 v${APP_VERSION}`;

  $('#btnImport').onclick = () => openModal();
  $('#btnImport2').onclick = () => openModal();
  $('#btnCloseModal').onclick = closeModal;
  $('#btnDoImport').onclick = doImport;
  $('#btnAnalyse').onclick = () => runAnalysis(false);
  $('#btnDeleteBook').onclick = deleteBook;
  $('#modalMask').onclick = (e) => { if (e.target.id === 'modalMask') closeModal(); };

  $$('.modal .tab').forEach(t => t.onclick = () => {
    $$('.modal .tab').forEach(x => x.classList.toggle('active', x === t));
    const k = t.dataset.itab;
    $('#ipaneFile').hidden = k !== 'file';
    $('#ipanePaste').hidden = k !== 'paste';
    $('#ipaneSettings').hidden = k !== 'settings';
    $('#btnDoImport').textContent = k === 'settings' ? '保存' : '导入';
  });

  $$('#bookTabs .tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

  // 文件选择
  const fi = $('#fileInput'), dz = $('#dropzone');
  // 不要在 label 上 preventDefault，也不要同时让 label 和 JS 各触发一次，
  // 否则 iOS 上会出现「点一下只高亮、选择器不弹」。
  dz.addEventListener('click', (e) => {
    e.stopPropagation();
    try { fi.click(); } catch (err) {
      $('#importHint').textContent = '无法打开文件选择器：' + (err.message || err);
    }
  });
  dz.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fi.click(); }
  });

  const pick = (f) => {
    state.pendingFile = f;
    $('.dz-title', dz).textContent = f.name;
    $('.dz-sub', dz).textContent = (f.size / 1024).toFixed(0) + ' KB';
    $('#importHint').textContent = '已选择：' + f.name + '，点「导入」开始';
  };

  // iOS Safari 对隐藏的 input 有时不触发 change，
  // 这里同时监听 change 和 input，并做一次去重
  let lastPicked = '';
  const onChange = () => {
    const f = fi.files && fi.files[0];
    if (!f) {
      // 有些机型选完文件后 files 会被后续渲染清空，给出提示而不是静默
      if (!state.pendingFile) {
        $('#importHint').textContent = '没有读到文件，请重新选择';
      }
      return;
    }
    const sig = f.name + ':' + f.size + ':' + f.lastModified;
    if (sig === lastPicked) return;
    lastPicked = sig;
    pick(f);
  };
  fi.addEventListener('change', onChange);
  fi.addEventListener('input', onChange);

  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('drag'); };
  dz.ondragleave = () => dz.classList.remove('drag');
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove('drag');
    if (e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);
  };

  // 读取配置
  try {
    state.cfg.apiKey = (await store.cfg('apiKey')) || '';
    state.cfg.model = (await store.cfg('model')) || 'deepseek-chat';
  } catch {}
  $('#apiKey').value = state.cfg.apiKey;
  $('#apiModel').value = state.cfg.model;

  await loadBooks();

  if (!state.cfg.apiKey) {
    setStatus('未配置 API Key', 'err');
    setTimeout(() => openModal('settings'), 400);
  }
}

document.addEventListener('DOMContentLoaded', init);
