"""后台任务子进程入口。

所有高内存任务（字体解码、拆书）都通过这里在**独立进程**里跑：
即使某个任务把内存吃光把自己撑死，主服务依然存活，
用户刷新页面能看到"该任务失败"，而不是整个 APP 打不开。

用法（由 server.py 调用）：
    python3 worker.py fanqie <chapterId> <maxChapters>
    python3 worker.py catalog <chapterId>
    python3 worker.py analyse <analysisId>

进度与结果都写进 SQLite 的 jobs 表，主服务轮询读取。
"""
import os
import sys
import json
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'app'))

import db


def _set(job_id, **kw):
    db.job_update(job_id, **kw)


def run_fanqie(job_id, chapter_id, max_chapters):
    import browser_fetch as bf

    rows = []
    name = author = ''
    cid = str(chapter_id)
    seen = set()

    worker_tab = bf.open_worker_tab()
    _set(job_id, message='已打开采集专用标签页')
    _set(job_id, message='已打开采集专用标签页')

    first = bf.decode_chapter(bf.fetch_chapter(cid, tab_id=worker_tab))
    if first.get('title') and not first.get('nextItemId') and first.get('preItemId'):
        cid = first['preItemId']

    for _ in range(max_chapters):
        if not cid or cid in seen:
            break
        seen.add(cid)
        d = bf.decode_chapter(bf.fetch_chapter(cid, tab_id=worker_tab))
        if not d.get('title'):
            break
        name = name or d.get('bookName', '')
        author = author or d.get('author', '')
        if d.get('text'):
            rows.append((d['title'], d['text']))
        _set(job_id,
             grabbed=len(rows),
             total=d.get('serialCount', 0),
             progress=round(len(rows) / max(1, max_chapters), 3),
             message=f"已抓 {len(rows)} 章：{d['title']}")
        cid = d.get('nextItemId') or ''

    if not rows:
        raise RuntimeError('未抓到任何章节内容')

    bid = db.add_book(name or f'番茄_{chapter_id}', author, 'fanqie', str(chapter_id))
    db.add_chapters(bid, rows)
    _set(job_id, book_id=bid, progress=1.0, status='done',
         message=f'完成：{len(rows)} 章已入库（每章为网页端预览片段）')


def run_catalog(job_id, chapter_id):
    import fanqie as fq

    def prog(p, m):
        _set(job_id, progress=round(p, 3), message=m)

    cat = fq.walk_catalog(chapter_id, on_progress=prog)
    _set(job_id, progress=1.0, status='done',
         result=json.dumps(cat, ensure_ascii=False),
         message=f"完成：{len(cat['chapters'])} 章")


def run_analyse(job_id, analysis_id):
    import engine

    def mirror():
        """把 engine 写进 analyses 表的进度同步到 jobs 表。"""
        a = db.get_analysis(analysis_id)
        if not a:
            return
        _set(job_id,
             progress=a.get('progress') or 0,
             stage=a.get('stage') or '',
             message=a.get('message') or '',
             status='running' if a.get('status') == 'running' else
                    ('done' if a.get('status') == 'done' else
                     ('error' if a.get('status') == 'error' else 'pending')))

    import threading
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                mirror()
            except Exception:
                pass
            stop.wait(1.2)

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        engine.run_analysis(analysis_id)
    finally:
        stop.set()
        time.sleep(0.3)
        mirror()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    kind = sys.argv[1]
    job_id = sys.argv[2]

    db.init()
    try:
        if kind == 'fanqie':
            run_fanqie(job_id, sys.argv[3], int(sys.argv[4]))
        elif kind == 'catalog':
            run_catalog(job_id, sys.argv[3])
        elif kind == 'analyse':
            run_analyse(job_id, int(sys.argv[3]))
        else:
            raise ValueError(f'未知任务类型: {kind}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.job_update(job_id, status='error', error=f'{type(e).__name__}: {e}'[:500])
        sys.exit(1)


if __name__ == '__main__':
    main()
