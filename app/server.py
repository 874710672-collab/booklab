"""booklab 后端：极简 HTTP 服务（无第三方依赖），提供 API + 静态前端。"""
import os
import sys
import json
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'app'))

import db
import importer
import engine

WEB_DIR = os.path.join(BASE, 'web')
UPLOAD_DIR = os.path.join(BASE, 'data', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
}

# ---------------- 后台任务（跑在独立子进程里） ----------------
# 为什么用子进程：字体解码要加载几十 MB 的字形模板，iSH 容器内存紧张，
# 一旦在主进程里 OOM，整个 APP 就打不开了。放子进程后，最坏情况只是
# 该任务失败，主服务依然正常响应。

import subprocess
import uuid

APP_DIR = os.path.join(BASE, 'app')


def _spawn(kind, job_id, *args):
    """拉起 worker 子进程。"""
    cmd = [sys.executable, os.path.join(APP_DIR, 'worker.py'), kind, job_id, *map(str, args)]
    logf = open(os.path.join(BASE, 'data', 'worker.log'), 'ab')
    subprocess.Popen(cmd, stdout=logf, stderr=logf,
                     cwd=BASE, start_new_session=True)
    return job_id


def _new_job(kind):
    jid = f'{kind[:3]}{uuid.uuid4().hex[:10]}'
    db.job_create(jid, kind)
    return jid


def _start_catalog_job(chapter_id):
    jid = _new_job('catalog')
    return _spawn('catalog', jid, chapter_id)


def _start_fetch_job(chapter_id, max_chapters=30):
    jid = _new_job('fanqie')
    return _spawn('fanqie', jid, chapter_id, max_chapters)


def _start_analyse_job(analysis_id):
    jid = _new_job('analyse')
    return _spawn('analyse', jid, analysis_id)


def json_resp(handler, obj, status=200):
    data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(data)


def read_body(handler):
    n = int(handler.headers.get('Content-Length') or 0)
    if n <= 0:
        return b''
    return handler.rfile.read(n)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass  # 静默

    # ---------- GET ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)

        try:
            if path.startswith('/api/'):
                return self.api_get(path[5:], qs)
            return self.serve_static(path)
        except Exception as e:
            return json_resp(self, {'error': f'{type(e).__name__}: {e}'}, 500)

    def _404(self):
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _redirect(self, to):
        self.send_response(302)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def serve_static(self, path):
        if path in ('/', ''):
            path = '/index.html'

        # standalone/ 是纯前端版本，同样对外提供
        if path in ('/standalone', '/standalone/'):
            return self._redirect('/standalone/index.html')
        if path.startswith('/standalone/'):
            root = os.path.join(BASE, 'standalone')
            fp = os.path.normpath(os.path.join(root, path[len('/standalone/'):]))
            if not fp.startswith(root):
                return self._404()
        else:
            fp = os.path.normpath(os.path.join(WEB_DIR, path.lstrip('/')))
            if not fp.startswith(WEB_DIR):
                return self._404()

        if not os.path.isfile(fp):
            return self._404()
        ext = os.path.splitext(fp)[1].lower()
        data = open(fp, 'rb').read()
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def api_get(self, route, qs):
        if route == 'books':
            return json_resp(self, {'books': db.list_books()})

        if route == 'book':
            bid = int(qs.get('id', [0])[0])
            b = db.get_book(bid)
            if not b:
                return json_resp(self, {'error': 'not found'}, 404)
            chs = db.get_chapters(bid)
            b['chapters'] = [{'idx': c['idx'], 'title': c['title'],
                              'chars': len(c['content'])} for c in chs]
            return json_resp(self, b)

        if route == 'chapter':
            bid = int(qs.get('book', [0])[0])
            idx = int(qs.get('idx', [0])[0])
            c = db.get_chapter(bid, idx)
            if not c:
                return json_resp(self, {'error': 'not found'}, 404)
            return json_resp(self, c)

        if route == 'analyses':
            bid = qs.get('book', [None])[0]
            return json_resp(self, {'analyses': db.list_analyses(int(bid) if bid else None)})

        if route == 'analysis':
            aid = int(qs.get('id', [0])[0])
            a = db.get_analysis(aid)
            if not a:
                return json_resp(self, {'error': 'not found'}, 404)
            b = db.get_book(a['book_id'])
            a['book_title'] = b['title'] if b else ''
            a['book_id'] = a['book_id']
            a['units'] = db.list_units(aid)
            return json_resp(self, a)

        if route == 'estimate':
            bid = int(qs.get('book', [0])[0])
            return json_resp(self, engine.estimate_cost(bid))

        if route == 'fanqie_catalog':
            cid = (qs.get('chapter', [''])[0] or '').strip()
            if not cid.isdigit():
                return json_resp(self, {'error': '请提供章节 ID（阅读页 URL 末尾的数字）'}, 400)
            tid = _start_catalog_job(cid)
            return json_resp(self, {'ok': True, 'task': tid})

        if route == 'fanqie_fetch':
            cid = (qs.get('chapter', [''])[0] or '').strip()
            if not cid.isdigit():
                return json_resp(self, {'error': '请提供章节 ID'}, 400)
            maxc = int((qs.get('max', ['30'])[0] or 30))
            maxc = max(1, min(200, maxc))
            tid = _start_fetch_job(cid, maxc)
            return json_resp(self, {'ok': True, 'task': tid})

        if route == 'fanqie_task':
            db.job_reap()
            tid = (qs.get('task', [''])[0] or '').strip()
            job = db.job_get(tid)
            if not job:
                return json_resp(self, {'error': '任务不存在'}, 404)
            return json_resp(self, job)

        if route == 'fanqie_chapter':
            cid = (qs.get('chapter', [''])[0] or '').strip()
            if not cid.isdigit():
                return json_resp(self, {'error': '章节 ID 无效'}, 400)
            import browser_fetch as bf
            d = bf.decode_chapter(bf.fetch_chapter(cid))
            return json_resp(self, {k: d.get(k) for k in
                                    ('id', 'title', 'bookName', 'author',
                                     'chapterWordNumber', 'nextItemId',
                                     'serialCount', 'font_decoded', 'text')})

        return json_resp(self, {'error': 'unknown route'}, 404)

    # ---------- POST ----------
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            raw = read_body(self)

            if path == '/api/import':
                return self.api_import(raw)
            if path == '/api/paste':
                return self.api_paste(raw)
            if path == '/api/analyse':
                return self.api_analyse(raw)
            if path == '/api/delete_book':
                d = json.loads(raw or b'{}')
                db.delete_book(int(d.get('id', 0)))
                return json_resp(self, {'ok': True})
            if path == '/api/delete_analysis':
                d = json.loads(raw or b'{}')
                db.delete_analysis(int(d.get('id', 0)))
                return json_resp(self, {'ok': True})
            if path == '/api/resume':
                d = json.loads(raw or b'{}')
                aid = int(d.get('id', 0))
                a = db.get_analysis(aid)
                if not a:
                    return json_resp(self, {'error': 'not found'}, 404)
                db.update_analysis(aid, status='pending', message='已排入队列')
                _start_analyse_job(aid)
                return json_resp(self, {'ok': True, 'id': aid})

            return json_resp(self, {'error': 'unknown route'}, 404)
        except Exception as e:
            return json_resp(self, {'error': f'{type(e).__name__}: {e}'}, 500)

    def _save_upload(self, filename, data):
        """落盘时加时间戳前缀避免冲突，但返回逻辑文件名供解析书名。"""
        safe = os.path.basename(filename).replace('/', '_') or 'book.txt'
        fp = os.path.join(UPLOAD_DIR, f'{int(__import__("time").time()*1000)}_{safe}')
        open(fp, 'wb').write(data)
        return fp, safe

    def api_import(self, raw):
        """raw 请求体即为文件内容；文件名走 X-Filename-B64（Base64 UTF-8），
        因为 HTTP 头只支持 latin-1，直接塞中文会乱码。"""
        import base64
        b64 = self.headers.get('X-Filename-B64')
        if b64:
            fn = base64.b64decode(b64).decode('utf-8', errors='replace')
        else:
            fn = urllib.parse.unquote(self.headers.get('X-Filename', 'book.txt'))
        fp, logical_name = self._save_upload(fn, raw)
        title, author, chapters = importer.load_document(fp, logical_name)
        if not chapters:
            return json_resp(self, {'error': '未能解析出任何章节'}, 400)
        bid = db.add_book(title, author, 'import', fp)
        db.add_chapters(bid, chapters)
        return json_resp(self, {'ok': True, 'id': bid, 'title': title,
                                'chapters': len(chapters),
                                'chars': sum(len(c[1]) for c in chapters)})

    def api_paste(self, raw):
        d = json.loads(raw or b'{}')
        text = d.get('text', '')
        title = d.get('title') or '粘贴文本'
        if not text.strip():
            return json_resp(self, {'error': '内容为空'}, 400)
        _, _, chapters = importer.load_text(text, title)
        bid = db.add_book(title, '', 'paste', '')
        db.add_chapters(bid, chapters)
        return json_resp(self, {'ok': True, 'id': bid, 'title': title,
                                'chapters': len(chapters),
                                'chars': sum(len(c[1]) for c in chapters)})

    def api_analyse(self, raw):
        d = json.loads(raw or b'{}')
        bid = int(d.get('book', 0))
        if not db.get_book(bid):
            return json_resp(self, {'error': 'book not found'}, 404)
        running = [a for a in db.list_analyses(bid) if a['status'] in ('running', 'pending')]
        if running:
            return json_resp(self, {'ok': True, 'id': running[0]['id'], 'existing': True})
        aid = db.create_analysis(bid, model=engine.MODEL)
        db.update_analysis(aid, status='pending', message='已排入队列')
        _start_analyse_job(aid)
        return json_resp(self, {'ok': True, 'id': aid})


def main():
    port = int(os.environ.get('PORT', '8787'))
    db.init()
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'booklab running on http://0.0.0.0:{port}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
