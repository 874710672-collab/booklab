"""文本导入：txt / epub 解析 + 章节智能切分。"""
import os
import re
import zipfile
import html
from xml.etree import ElementTree as ET


# ---------- 章节标题识别 ----------

# 常见章节行模式（中文网文）
CHAP_PATTERNS = [
    re.compile(r'^\s*第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷集部篇]\b.*$'),
    re.compile(r'^\s*(?:正文\s*)?第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷集部篇]'),
    re.compile(r'^\s*(?:Chapter|CHAPTER)\s+[\dIVXLC]+.*$'),
    re.compile(r'^\s*[【\[]?\s*第[0-9]+[章节回]\s*[】\]]?'),
]
# 明显的卷标题
VOL_PATTERN = re.compile(r'^\s*(?:第\s*[0-9零一二三四五六七八九十百千万]+\s*[卷部]\b.*|卷[一二三四五六七八九十].*)$')

MAX_TITLE_LEN = 40


def _looks_like_title(line):
    s = line.strip()
    if not s or len(s) > MAX_TITLE_LEN:
        return False
    # 去掉常见装饰
    core = s.strip('　 \t#=-—*_')
    if not core or len(core) > MAX_TITLE_LEN:
        return False
    return any(p.match(core) for p in CHAP_PATTERNS)


def split_chapters(text):
    """把整本 txt 切成 [(title, content), ...]。

    策略：逐行扫描，命中章节标题行则开新章。
    若识别不到任何章节标题，退化为按长度均分。
    """
    text = normalize_text(text)
    lines = text.split('\n')

    chapters = []
    cur_title = None
    buf = []

    def flush():
        nonlocal cur_title, buf
        body = '\n'.join(buf).strip()
        if cur_title is None and not body:
            return
        chapters.append((cur_title or '开篇', body))
        buf = []

    for line in lines:
        if _looks_like_title(line):
            flush()
            cur_title = line.strip().strip('　 \t#=-—*_')
        else:
            buf.append(line)
    flush()

    # 过滤空章
    chapters = [(t, c) for t, c in chapters if c.strip()]

    if len(chapters) <= 1:
        # 没有识别到章节结构，按字数硬切
        return _chunk_by_size(text)

    # 若首章内容极少且不是真章节，合并进下一章
    if len(chapters) >= 2 and len(chapters[0][1]) < 200:
        t0, c0 = chapters[0]
        t1, c1 = chapters[1]
        chapters = [(t1, (c0 + '\n' + c1).strip())] + chapters[2:]

    return chapters


def _chunk_by_size(text, target=3000):
    text = text.strip()
    if not text:
        return []
    n = max(1, (len(text) + target - 1) // target)
    size = (len(text) + n - 1) // n
    out = []
    for i in range(n):
        seg = text[i * size:(i + 1) * size].strip()
        if seg:
            out.append((f'片段 {i+1}', seg))
    return out


def normalize_text(t):
    if not isinstance(t, str):
        t = t.decode('utf-8', errors='ignore')
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    t = t.replace('\u3000', ' ')
    # 压缩 3+ 连续空行为 1 空行
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t


def read_txt(path):
    """自动猜编码读取。"""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk', 'big5', 'utf-16'):
        try:
            s = raw.decode(enc)
            # gbk 误判保护：若出现大量替换字符则跳过
            if s.count('\ufffd') > len(s) * 0.01:
                continue
            return s
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='ignore')


# ---------- epub ----------

NS = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container',
      'opf': 'http://www.idpf.org/2007/opf',
      'xhtml': 'http://www.w3.org/1999/xhtml'}


def read_epub(path):
    """返回 (title, [(chapter_title, text), ...])。"""
    zf = zipfile.ZipFile(path)
    names = zf.namelist()

    # 找 opf
    opf_path = None
    if 'META-INF/container.xml' in names:
        root = ET.fromstring(zf.read('META-INF/container.xml'))
        for rf in root.iter():
            if rf.tag.endswith('rootfile'):
                opf_path = rf.attrib.get('full-path')
                break
    if not opf_path:
        opf_path = next((n for n in names if n.endswith('.opf')), None)
    if not opf_path:
        raise ValueError('epub 中未找到 opf 文件')

    base = os.path.dirname(opf_path)
    opf = ET.fromstring(zf.read(opf_path))

    title = ''
    author = ''
    for m in opf.iter():
        tag = m.tag.split('}')[-1]
        if tag == 'title' and not title:
            title = (m.text or '').strip()
        elif tag == 'creator' and not author:
            author = (m.text or '').strip()

    # manifest: id -> href
    manifest = {}
    for m in opf.iter():
        if m.tag.split('}')[-1] == 'item':
            manifest[m.attrib.get('id')] = m.attrib.get('href', '')

    spine_files = []
    for m in opf.iter():
        if m.tag.split('}')[-1] == 'itemref':
            idref = m.attrib.get('idref')
            if idref in manifest:
                href = manifest[idref]
                full = os.path.normpath(os.path.join(base, href)).replace('\\', '/')
                if full in names:
                    spine_files.append(full)

    chapters = []
    for f in spine_files:
        try:
            raw = zf.read(f).decode('utf-8', errors='ignore')
        except Exception:
            continue
        ct, txt = _epub_doc_to_text(raw)
        if txt.strip():
            chapters.append((ct, txt))

    return title, author, chapters


def _epub_doc_to_text(raw_html):
    # 取 title
    m = re.search(r'<title[^>]*>(.*?)</title>', raw_html, re.S | re.I)
    doc_title = html.unescape(m.group(1)).strip() if m else ''

    # 优先取 body
    bm = re.search(r'<body[^>]*>(.*?)</body>', raw_html, re.S | re.I)
    body = bm.group(1) if bm else raw_html

    # 段落分割
    body = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', body, flags=re.S | re.I)
    body = re.sub(r'<br\s*/?>', '\n', body, flags=re.I)
    body = re.sub(r'</(p|div|h\d|li|tr)>', '\n', body, flags=re.I)
    body = re.sub(r'<[^>]+>', '', body)
    txt = html.unescape(body)
    txt = normalize_text(txt).strip()

    # 若 <title> 不像「第X章」，尝试从正文首行取
    if doc_title and not re.match(r'^(第|Chapter|CHAPTER)', doc_title):
        first_line = txt.split('\n', 1)[0].strip()
        if _looks_like_title(first_line):
            doc_title = first_line.strip()
    return doc_title, txt


def load_document(path, logical_name=None):
    """统一入口：返回 (title, author, [(chap_title, text)])。

    logical_name: 用于推断书名的原始文件名（path 可能带时间戳前缀）。
    """
    stem = os.path.splitext(os.path.basename(logical_name or path))[0]
    ext = os.path.splitext(path)[1].lower()
    if ext == '.epub':
        title, author, chapters = read_epub(path)
        if not chapters:
            raise ValueError('epub 未解析出章节')
        return title or stem, author, chapters
    else:
        text = read_txt(path)
        title = stem
        # 尝试从首行猜书名
        first = text.strip().split('\n', 1)[0].strip() if text.strip() else ''
        if 0 < len(first) <= 30 and not _looks_like_title(first):
            title = first
        return title, '', split_chapters(text)


def load_text(text, title='粘贴文本'):
    return title, '', split_chapters(text)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        t, a, chs = load_document(sys.argv[1])
        print('title:', t, '| author:', a, '| chapters:', len(chs))
        for ct, c in chs[:5]:
            print(' -', ct, len(c), 'chars')
