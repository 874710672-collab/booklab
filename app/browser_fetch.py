"""通过浏览器会话抓取番茄章节（Python 直连会被风控）。

用法：
    python3 browser_fetch.py <chapterId>          # 抓单章，打印解码后正文
    python3 browser_fetch.py <chapterId> --json   # 输出完整 JSON

说明：
    Python 直接请求 fanqienovel.com 会拿到"验证码中间页"（针对 IP 的风控），
    所以这里调用 minis-browser-use，借用已通过风控的浏览器会话取数据，
    再用 fontdec 把字体反爬的 PUA 字符还原成正常汉字。
"""
import os
import re
import sys
import json
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fontdec

JS = r'''
var st = window.__INITIAL_STATE__ || {};
var cd = ((st.reader||{}).chapterData) || {};
var css = ((st.common||{}).css) || "";
return JSON.stringify({
  title: cd.title || "",
  bookName: cd.bookName || "",
  bookId: cd.bookId || "",
  author: cd.author || "",
  content: cd.content || "",
  chapterWordNumber: cd.chapterWordNumber || 0,
  nextItemId: cd.nextItemId || "",
  preItemId: cd.preItemId || "",
  serialCount: cd.serialCount || 0,
  realChapterOrder: cd.realChapterOrder || 0,
  needPay: cd.needPay || 0,
  isChapterLock: !!cd.isChapterLock,
  fontCss: css
});
'''


def _extract_json_obj(s):
    i = s.find('{')
    if i < 0:
        return None
    depth, instr, esc = 0, False, False
    for k in range(i, len(s)):
        ch = s[k]
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
                return s[i:k + 1]
    return None


def fetch_chapter(chapter_id, tab_id=None, timeout=90):
    """用浏览器会话抓一章，返回 dict。

    tab_id：指定在哪个标签页抓。批量采集时应传入一个专属标签页，
    否则会把用户正在看的 APP 界面顶掉。
    """
    url = f'https://fanqienovel.com/reader/{chapter_id}'
    nav = ['minis-browser-use', 'navigate', '--url', url]
    js = ['minis-browser-use', 'execute_js', '--script', JS]
    if tab_id is not None:
        nav += ['--tab-id', str(tab_id)]
        js += ['--tab-id', str(tab_id)]

    subprocess.run(nav, capture_output=True, timeout=timeout)
    r = subprocess.run(js, capture_output=True, timeout=timeout, text=True)
    out = r.stdout or ''

    try:
        env = json.loads(out)
        inner = (env.get('data') or {}).get('text', '')
    except Exception:
        inner = out

    seg = _extract_json_obj(inner)
    if not seg:
        raise RuntimeError('未能取得章节数据（可能被风控或页面结构变化）')

    d = json.loads(seg)
    d['id'] = str(chapter_id)
    d['url'] = url
    return d


def open_worker_tab(timeout=60):
    """开一个专用标签页给采集用，返回 tab_id。"""
    r = subprocess.run(['minis-browser-use', 'new_tab', '--url', 'about:blank'],
                       capture_output=True, timeout=timeout, text=True)
    out = r.stdout or ''
    try:
        env = json.loads(out)
        data = env.get('data') or {}
        tid = data.get('tab_id')
        if tid is None:
            # 文本形如 "Opened new tab 2 at about:blank. ... Use tab_id: 2"
            m = re.search(r'tab_id:\s*(\d+)', data.get('text', ''))
            if m:
                tid = m.group(1)
        return int(tid) if tid is not None else None
    except Exception:
        return None


def decode_chapter(d):
    """把章节里的 PUA 字符还原成正常汉字。"""
    raw = d.get('content', '') or ''
    decoded = False
    err = ''
    if fontdec.has_pua(raw):
        try:
            urls = fontdec.extract_font_urls(d.get('fontCss', '') or '')
            if urls:
                fp = fontdec.download_font(urls[0])
                mp = fontdec.build_mapping(fp)
                raw = fontdec.decode_text(raw, mp)
                decoded = True
            else:
                err = '页面未包含字体 CSS'
        except Exception as e:
            err = f'{type(e).__name__}: {e}'
    text = fontdec.html_to_text(raw)
    text = fontdec.correct_confusions(text)
    d['text'] = text
    d['font_decoded'] = decoded
    if err:
        d['decode_error'] = err
    return d


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cid = sys.argv[1]
    d = decode_chapter(fetch_chapter(cid))

    if '--json' in sys.argv:
        print(json.dumps({k: d[k] for k in
                          ('id', 'title', 'bookName', 'author', 'chapterWordNumber',
                           'nextItemId', 'serialCount', 'font_decoded', 'text')},
                         ensure_ascii=False, indent=2))
        return

    print(f"《{d['bookName']}》 {d['title']}")
    print(f"声明字数 {d['chapterWordNumber']}｜实得 {len(d['text'])} 字"
          f"｜字体解码 {'成功' if d['font_decoded'] else '无需/失败'}")
    print('-' * 50)
    print(d['text'])


if __name__ == '__main__':
    main()
