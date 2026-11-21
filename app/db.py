"""booklab 数据层：书籍 / 章节 / 拆书任务 / 单元结果，全部落 SQLite。"""
import sqlite3
import time
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'booklab.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    author      TEXT DEFAULT '',
    source      TEXT DEFAULT 'import',   -- import / paste / fanqie
    source_ref  TEXT DEFAULT '',         -- 原始路径或 bookId
    created_at  REAL NOT NULL,
    chapter_cnt INTEGER DEFAULT 0,
    char_cnt    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chapters (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id  INTEGER NOT NULL,
    idx      INTEGER NOT NULL,           -- 从 0 开始的顺序号
    title    TEXT DEFAULT '',
    content  TEXT NOT NULL,
    UNIQUE(book_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, idx);

CREATE TABLE IF NOT EXISTS analyses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/error
    stage       TEXT DEFAULT '',        -- 当前阶段
    progress    REAL DEFAULT 0,
    message     TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL,
    seq        INTEGER NOT NULL,        -- 单元序号，从 1 开始
    name       TEXT DEFAULT '',
    chap_from  INTEGER DEFAULT 0,       -- 起始章节序号(1-based)
    chap_to    INTEGER DEFAULT 0,
    summary    TEXT DEFAULT '',
    characters TEXT DEFAULT '',         -- JSON
    relations  TEXT DEFAULT '',         -- JSON / 文本
    detail     TEXT DEFAULT '',         -- 完整 markdown 输出
    UNIQUE(analysis_id, seq)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- 后台任务表。任务跑在独立子进程里，靠这张表回传进度与结果，
-- 这样即使子进程内存爆掉，主服务也不受影响。
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'running',  -- running/done/error
    progress   REAL DEFAULT 0,
    stage      TEXT DEFAULT '',
    message    TEXT DEFAULT '',
    error      TEXT DEFAULT '',
    result     TEXT DEFAULT '',
    book_id    INTEGER,
    grabbed    INTEGER DEFAULT 0,
    total      INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
    return DB_PATH


def now():
    return time.time()


# ---------------- books ----------------

def add_book(title, author='', source='import', source_ref=''):
    with conn() as c:
        cur = c.execute(
            'INSERT INTO books(title,author,source,source_ref,created_at) VALUES(?,?,?,?,?)',
            (title, author, source, source_ref, now()))
        return cur.lastrowid


def add_chapters(book_id, chapters):
    """chapters: [(title, content), ...]"""
    rows = [(book_id, i, t or f'第{i+1}章', ct) for i, (t, ct) in enumerate(chapters)]
    with conn() as c:
        c.executemany('INSERT OR REPLACE INTO chapters(book_id,idx,title,content) VALUES(?,?,?,?)', rows)
        c.execute('UPDATE books SET chapter_cnt=?, char_cnt=? WHERE id=?',
                  (len(rows), sum(len(r[3]) for r in rows), book_id))


def list_books():
    with conn() as c:
        rs = c.execute('''SELECT b.*,
            (SELECT COUNT(*) FROM analyses a WHERE a.book_id=b.id) AS analysis_cnt
            FROM books b ORDER BY b.created_at DESC''').fetchall()
        return [dict(r) for r in rs]


def get_book(book_id):
    with conn() as c:
        r = c.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        return dict(r) if r else None


def delete_book(book_id):
    with conn() as c:
        ids = [r[0] for r in c.execute('SELECT id FROM analyses WHERE book_id=?', (book_id,))]
        for aid in ids:
            c.execute('DELETE FROM units WHERE analysis_id=?', (aid,))
        c.execute('DELETE FROM analyses WHERE book_id=?', (book_id,))
        c.execute('DELETE FROM chapters WHERE book_id=?', (book_id,))
        c.execute('DELETE FROM books WHERE id=?', (book_id,))


def get_chapters(book_id, start=None, end=None):
    """start/end 为 0-based 索引，左闭右开。"""
    q = 'SELECT idx,title,content FROM chapters WHERE book_id=?'
    args = [book_id]
    if start is not None:
        q += ' AND idx>=?'
        args.append(start)
    if end is not None:
        q += ' AND idx<?'
        args.append(end)
    q += ' ORDER BY idx'
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_chapter(book_id, idx):
    with conn() as c:
        r = c.execute('SELECT idx,title,content FROM chapters WHERE book_id=? AND idx=?',
                      (book_id, idx)).fetchone()
        return dict(r) if r else None


# ---------------- analyses ----------------

def create_analysis(book_id, model=''):
    t = now()
    with conn() as c:
        cur = c.execute(
            'INSERT INTO analyses(book_id,status,created_at,updated_at,model) VALUES(?,?,?,?,?)',
            (book_id, 'pending', t, t, model))
        return cur.lastrowid


def update_analysis(aid, **kw):
    if not kw:
        return
    kw['updated_at'] = now()
    cols = ','.join(f'{k}=?' for k in kw)
    with conn() as c:
        c.execute(f'UPDATE analyses SET {cols} WHERE id=?', (*kw.values(), aid))


def get_analysis(aid):
    with conn() as c:
        r = c.execute('SELECT * FROM analyses WHERE id=?', (aid,)).fetchone()
        return dict(r) if r else None


def list_analyses(book_id=None):
    q = '''SELECT a.*, b.title AS book_title FROM analyses a
           JOIN books b ON b.id=a.book_id'''
    args = []
    if book_id is not None:
        q += ' WHERE a.book_id=?'
        args.append(book_id)
    q += ' ORDER BY a.created_at DESC'
    with conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def delete_analysis(aid):
    with conn() as c:
        c.execute('DELETE FROM units WHERE analysis_id=?', (aid,))
        c.execute('DELETE FROM analyses WHERE id=?', (aid,))


# ---------------- units ----------------

def clear_units(aid):
    with conn() as c:
        c.execute('DELETE FROM units WHERE analysis_id=?', (aid,))


def save_unit(aid, seq, **kw):
    cols = ['analysis_id', 'seq'] + list(kw.keys())
    vals = [aid, seq] + list(kw.values())
    marks = ','.join('?' * len(cols))
    with conn() as c:
        c.execute(f'INSERT OR REPLACE INTO units({",".join(cols)}) VALUES({marks})', vals)


def list_units(aid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM units WHERE analysis_id=? ORDER BY seq', (aid,)).fetchall()]


def get_unit(aid, seq):
    with conn() as c:
        r = c.execute('SELECT * FROM units WHERE analysis_id=? AND seq=?', (aid, seq)).fetchone()
        return dict(r) if r else None


# ---------------- kv ----------------

def kv_get(k, default=None):
    with conn() as c:
        r = c.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
        return r[0] if r else default


def kv_set(k, v):
    with conn() as c:
        c.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)', (k, str(v)))


# ---------------- jobs（独立进程任务） ----------------

def job_create(job_id, kind):
    t = now()
    with conn() as c:
        c.execute('INSERT OR REPLACE INTO jobs(id,kind,status,created_at,updated_at) '
                  'VALUES(?,?,?,?,?)', (job_id, kind, 'running', t, t))
    return job_id


def job_update(job_id, **kw):
    if not kw:
        return
    kw['updated_at'] = now()
    cols = ','.join(f'{k}=?' for k in kw)
    with conn() as c:
        c.execute(f'UPDATE jobs SET {cols} WHERE id=?', (*kw.values(), job_id))


def job_get(job_id):
    with conn() as c:
        r = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        # result 存的是 JSON 字符串，这里还原
        if d.get('result'):
            try:
                d['result'] = json.loads(d['result'])
            except Exception:
                pass
        return d


def job_reap(stale_seconds=90):
    """把长时间没有心跳的 running 任务标记为失败。

    任务跑在子进程里，如果子进程被 OOM kill 或异常退出，
    主服务无法感知，只能靠心跳时间判断。
    """
    cutoff = now() - stale_seconds
    with conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status='error', error='任务进程已退出（可能内存不足），请重试', "
            "updated_at=? WHERE status='running' AND updated_at < ?",
            (now(), cutoff))
        return cur.rowcount


def job_cleanup(days=1):
    """清理陈旧的已完成任务，避免表无限增长。"""
    cutoff = now() - days * 86400
    with conn() as c:
        c.execute("DELETE FROM jobs WHERE updated_at < ? AND status != 'running'", (cutoff,))


if __name__ == '__main__':
    print('db ready at', init())
