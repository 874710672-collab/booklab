"""番茄小说采集器（实验性）。

实测结论（2026-11）
------------------
1. 【目录】完全可靠。每一页内嵌的 __INITIAL_STATE__ 里带 nextItemId /
   preItemId / serialCount，可沿链把整本目录串出来，4051 章也能拿全。
2. 【正文】被服务端强制截断到约 200 字，且与登录状态无关（已验证）。
   网页版只是用于引流和 SEO 的残缺预览，完整正文只在 App 内。
3. 【缺字】正文里的缺字是字体反爬：部分汉字被替换成 PUA 码位，
   再用动态字体渲染。本模块已集成 fontdec 解码器，可将片段还原成可读文本。

用法：
    python3 fanqie.py <chapterId> --one      # 抓单章（含字体解码）
    python3 fanqie.py <chapterId> --catalog  # 沿链抓全量目录
    python3 fanqie.py <chapterId> --save     # 抓整本入库（仅预览片段）
    python3 fanqie.py <chapterId> --save --max=50   # 限制章数

环境变量：
    FANQIE_DELAY  相邻请求间隔秒数，默认 1.2。过快会触发风控。
"""
import sys
import json
import re
import urllib.request
import urllib.parse
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

UA = ('Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')


def _load_cookie_header():
    """读取 cookie：优先环境变量 FANQIE_COOKIE，其次浏览器导出的 BROWSER_COOKIE_HEADER。

    番茄对无 cookie 的请求会返回验证码中间页，必须带上浏览器会话的 cookie。
    """
    c = os.environ.get('FANQIE_COOKIE', '').strip()
    if c:
        return c

    import glob
    files = sorted(glob.glob('/var/minis/offloads/env_cookies_fanqienovel_com_*.sh'))
    if not files:
        return ''
    raw = open(files[-1], encoding='utf-8', errors='ignore').read()

    # 导出脚本里通常直接给了完整的 Cookie 请求头，最省事也最准确
    m = re.search(r"export\s+BROWSER_COOKIE_HEADER=['\"]?([^'\"\n]+)", raw)
    if m:
        return m.group(1).strip()

    # 兜底：按 # cookie: 注释里的原始名称重建
    pairs = re.findall(r"export\s+COOKIE_(\w+)=['\"]?([^'\"\n]*)['\"]?", raw)
    names = re.findall(r'#\s*cookie:\s*(\S+)', raw)
    by_key = {re.sub(r'[^A-Za-z0-9]', '_', n).upper(): n for n in names}
    return '; '.join(f'{by_key.get(k, k.lower())}={v}' for k, v in pairs if v)


_COOKIE = [None]


def fetch(url, timeout=25):
    if _COOKIE[0] is None:
        _COOKIE[0] = _load_cookie_header()
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://fanqienovel.com/',
    }
    if _COOKIE[0]:
        headers['Cookie'] = _COOKIE[0]
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='ignore')


def parse_book_page(html):
    """从书籍详情页解析书名、作者、简介、章节列表。"""
    out = {'title': '', 'author': '', 'desc': '', 'chapters': []}

    m = re.search(r'<title>(.*?)</title>', html)
    if m:
        out['title'] = re.split(r'[_\-\|]', m.group(1))[0].strip()
        # 去掉「完整版在线免费阅读」「小说」「最新章节」等站点后缀
        out['title'] = re.sub(r'(完整版)?(在线)?(免费)?(阅读|看)$', '', out['title'])
        out['title'] = re.sub(r'(小说)?(最新章节)?$', '', out['title']).strip()

    # 作者：常见于 "XXX / 著" 或 author 字段
    m = re.search(r'([^<>"]{1,20})\s*/\s*著', html)
    if m:
        out['author'] = m.group(1).strip()

    # 章节链接
    seen = set()
    for m in re.finditer(r'href="(/reader/(\d+))"[^>]*>(.*?)</a>', html, re.S):
        cid = m.group(2)
        if cid in seen:
            continue
        seen.add(cid)
        title = re.sub(r'<[^>]+>', '', m.group(3)).strip()
        out['chapters'].append({'id': cid, 'title': title})
    return out


def _extract_state(html):
    """从页面提取 window.__INITIAL_STATE__ 并解析。"""
    i = html.find('window.__INITIAL_STATE__=')
    if i < 0:
        return None
    start = i + len('window.__INITIAL_STATE__=')
    depth = 0
    end = None
    instr = False
    esc = False
    for k in range(start, len(html)):
        ch = html[k]
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if not end:
        return None
    raw = html[start:end]
    # 页面里内嵌了 JS 字面量 undefined，不是合法 JSON
    raw = re.sub(r'(?<=[:\[,])\s*undefined\s*(?=[,}\]])', 'null', raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


def parse_reader_page(html, decode_font=True):
    """从阅读页解析章节元信息与正文。

    关键：内嵌的 chapterData 含 nextItemId / preItemId / serialCount，
    可以据此把整本目录串起来，不依赖懒加载的详情页。

    ⚠️ content 字段被服务端截断到约 200 字（实测与登录无关）。
    decode_font=True 时会把字体反爬的 PUA 字符还原成正常汉字，
    这样即使是片段也是可读的。
    """
    import fontdec

    st = _extract_state(html)
    cd = (st or {}).get('reader', {}).get('chapterData', {}) or {}

    title = cd.get('title', '')
    if not title:
        m = re.search(r'<title>(.*?)</title>', html)
        if m:
            title = re.split(r'[_\-\|]', m.group(1))[0].strip()

    raw = cd.get('content', '') or ''
    body = fontdec.html_to_text(raw)
    decoded = False

    if decode_font and fontdec.has_pua(body):
        try:
            css = (st.get('common', {}) or {}).get('css', '') or ''
            urls = fontdec.extract_font_urls(css) or fontdec.extract_font_urls(html)
            if urls:
                fp = fontdec.download_font(urls[0])
                mp = fontdec.build_mapping(fp)
                body = fontdec.decode_text(body, mp)
                decoded = True
        except Exception:
            pass

    return {
        'title': title,
        'content': body,
        'truncated': True,
        'font_decoded': decoded,
        'declared_chars': int(cd.get('chapterWordNumber') or 0),
        'book_name': cd.get('bookName', ''),
        'book_id': cd.get('bookId', ''),
        'author': cd.get('author', ''),
        'next_id': cd.get('nextItemId', ''),
        'prev_id': cd.get('preItemId', ''),
        'serial_count': int(cd.get('serialCount') or 0),
        'order': cd.get('realChapterOrder', 0),
        'need_pay': cd.get('needPay', 0),
        'locked': cd.get('isChapterLock', False),
    }


def get_book(book_id):
    url = f'https://fanqienovel.com/page/{book_id}'
    html = fetch(url)
    info = parse_book_page(html)
    info['book_id'] = book_id
    info['url'] = url
    return info


# 全局节流：相邻请求最小间隔（秒）。过快会触发风控返回验证码页。
_THROTTLE = float(os.environ.get('FANQIE_DELAY', '1.2'))
_last_req = [0.0]


def _throttle():
    import time as _t
    gap = _THROTTLE - (_t.time() - _last_req[0])
    if gap > 0:
        _t.sleep(gap)
    _last_req[0] = _t.time()


def get_chapter(chapter_id, decode_font=True, retries=3):
    """抓单章。带节流与退避重试，避免触发风控。"""
    url = f'https://fanqienovel.com/reader/{chapter_id}'
    last = None
    for attempt in range(retries):
        try:
            _throttle()
            html = fetch(url)
            if '验证码' in html[:2000] or '__INITIAL_STATE__' not in html:
                last = '触发风控/无数据'
                import time as _t
                _t.sleep(5 * (attempt + 1))
                continue
            c = parse_reader_page(html, decode_font=decode_font)
            if not c.get('title'):
                last = '解析为空'
                continue
            c['id'] = chapter_id
            c['url'] = url
            return c
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
            import time as _t
            _t.sleep(3 * (attempt + 1))
    raise RuntimeError(f'抓取失败 {chapter_id}: {last}')


def walk_catalog(any_chapter_id, max_steps=10000, on_progress=None):
    """从任意一章出发，沿 nextItemId 把整本目录串起来。

    这是采集器里最可靠的部分：目录信息（章节 id、标题、字数、连载数）
    完整内嵌在每一页的 __INITIAL_STATE__ 里，不受懒加载和反爬影响。

    返回 dict：{book_id, book_name, author, serial_count, chapters:[...]}
    """
    first = get_chapter(any_chapter_id)
    book_id = first['book_id']
    book_name = first['book_name']
    author = first['author']
    total = first['serial_count']

    # 先往回走到头
    cur = first
    guard = 0
    while cur.get('prev_id') and guard < max_steps:
        guard += 1
        try:
            prev = get_chapter(cur['prev_id'], decode_font=False)
            if not prev.get('title'):
                break
            cur = prev
        except Exception:
            break
        if on_progress and guard % 20 == 0:
            on_progress(0.05, f'向前定位起始章… {guard}')

    # 再往后正向遍历
    chapters = []
    seen = set()
    guard = 0
    while cur and cur.get('title') and guard < max_steps:
        cid = cur.get('id') or ''
        if cid in seen:
            break
        seen.add(cid)
        chapters.append({
            'id': cid,
            'title': cur['title'],
            'declared_chars': cur.get('declared_chars', 0),
            'order': cur.get('order', 0),
        })
        if on_progress:
            on_progress(0.05 + 0.95 * min(1, len(chapters) / max(1, total)),
                        f'采集目录 {len(chapters)}/{total}')
        nxt = cur.get('next_id')
        if not nxt:
            break
        guard += 1
        try:
            cur = get_chapter(nxt, decode_font=False)
        except Exception:
            break

    return {'book_id': book_id, 'book_name': book_name, 'author': author,
            'serial_count': total, 'chapters': chapters}


def import_book(any_chapter_id, max_chapters=0, with_content=True, on_progress=None):
    """从任意一章出发采集整本，存入库。

    with_content=True 时逐章抓取（含字体解码），但请注意：
    ⚠️ 番茄网页端服务端强制截断正文到约 200 字，实测与登录状态无关。
       所以抓到的每章只有开头片段。本功能主要价值在于【目录】与【追踪更新】。

    返回 (book_row_id, stats)
    """
    cat = walk_catalog(any_chapter_id, on_progress=on_progress)
    chapters = cat['chapters']
    if max_chapters > 0:
        chapters = chapters[:max_chapters]

    rows = []
    ok, fail = 0, 0

    if with_content:
        for i, c in enumerate(chapters):
            try:
                d = get_chapter(c['id'], decode_font=True)
                txt = (d.get('content') or '').strip()
                if txt:
                    rows.append((c['title'], txt))
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            if on_progress:
                on_progress(0.05 + 0.95 * (i + 1) / len(chapters),
                            f'抓取正文 {i+1}/{len(chapters)}')
    else:
        # 只要目录：用标题占位
        rows = [(c['title'], f'（仅目录，正文未采集：第{c["order"]}章）') for c in chapters]
        ok = len(rows)

    if not rows:
        return None, {'ok': 0, 'fail': fail, 'note': '未获取到任何内容'}

    title = cat['book_name'] or f'番茄_{cat["book_id"]}'
    bid = db.add_book(title, cat['author'], 'fanqie', cat['book_id'])
    db.add_chapters(bid, rows)

    return bid, {
        'ok': ok,
        'fail': fail,
        'title': title,
        'author': cat['author'],
        'total_chapters': cat['serial_count'],
        'with_content': with_content,
        'warning': ('网页端正文被截断为每章约 200 字预览；'
                    '完整正文仅在番茄 App 内提供。')
        if with_content else '仅采集了目录。',
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    arg = sys.argv[1]
    flags = sys.argv[2:]

    db.init()

    if '--one' in flags:
        c = get_chapter(arg)
        print(json.dumps({k: c[k] for k in
                          ('title', 'content', 'declared_chars',
                           'font_decoded', 'truncated')},
                         ensure_ascii=False, indent=2))
        return

    if '--save' in flags:
        maxc = 0
        for f in flags:
            if f.startswith('--max='):
                maxc = int(f.split('=')[1])
        bid, stats = import_book(arg, maxc, with_content=True)
        print(json.dumps({'book_id': bid, **stats}, ensure_ascii=False, indent=2))
        return

    if '--catalog' in flags:
        cat = walk_catalog(arg, on_progress=lambda p, m: print(m, file=sys.stderr))
        print(f"书名：{cat['book_name']}")
        print(f"作者：{cat['author']}")
        print(f"总章数：{cat['serial_count']}，已采集：{len(cat['chapters'])}")
        for c in cat['chapters'][:10]:
            print(f"  {c['title']}  ({c['declared_chars']}字, id={c['id']})")
        return

    info = get_book(arg)
    print(f"书名：{info['title']}")
    print(f"作者：{info['author']}")
    cid = info['chapters'][0]['id'] if info['chapters'] else None
    if cid:
        c = get_chapter(cid)
        print(f"总章数：{c['serial_count']}")
    print('前 10 章：')
    for c in info['chapters'][:10]:
        print(f"  {c['title']}  (id={c['id']})")
    print('\n提示：--catalog 只抓目录（快且可靠）；--save 抓正文（仅预览片段）。')


if __name__ == '__main__':
    main()
