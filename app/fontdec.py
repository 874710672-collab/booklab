"""番茄小说字体反爬解码器。

原理
----
番茄网页端把正文里的一部分汉字替换成私用区（PUA）码位，
再用一个动态生成的字体把 PUA 码位映射到"长得像真字"的字形上。
所以 DOM 里读到的是一串乱码，但屏幕上显示的是正常文字。

还原方法
--------
1. 从页面 __INITIAL_STATE__.common.css 里找到动态字体 URL，下载 woff2。
2. 用 fontTools 把字体里每个 PUA 码位对应的字形渲染成位图。
3. 用标准中文字体（Noto Sans CJK）把候选汉字也渲染成位图。
4. 逐像素比对（归一化相关系数），取最相似的字作为该 PUA 码位的真值。
5. 建立映射表后，把正文里的 PUA 字符全部替换回来。

性能
----
候选字模板库与每个字体文件的渲染结果都缓存到磁盘（.npz），
首次约 2-3 分钟，之后每章解码只需秒级。
"""
import os
import re
import json
import hashlib
import tempfile
import urllib.request

import numpy as np
from PIL import Image, ImageFont, ImageDraw, ImageFilter

FONT_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'fanqie_fonts')
os.makedirs(FONT_CACHE_DIR, exist_ok=True)

CJK_FONTS = [
    '/usr/share/fonts/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/noto/NotoSerifCJK-Regular.ttc',
]

RENDER_SIZE = 72
# 32x32 已足够区分绝大多数汉字，内存占用是 48x48 的 44%
GRID = 32
UA = ('Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')


# ---------------------------------------------------------------- 字体获取

def extract_font_urls(css_or_html):
    urls = re.findall(r'url\(["\']?([^"\')]+\.(?:woff2|woff|otf|ttf))["\']?\)', css_or_html)
    urls = [u.replace('\\u002F', '/').replace('\\/', '/') for u in urls]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    out.sort(key=lambda u: (0 if u.endswith('.woff2') else 1))
    return out


def download_font(url, timeout=30):
    name = re.sub(r'[^A-Za-z0-9._-]', '_', url.split('/')[-1])
    if not name.endswith(('.woff2', '.woff', '.ttf', '.otf')):
        name += '.woff2'
    fp = os.path.join(FONT_CACHE_DIR, name)
    if os.path.exists(fp) and os.path.getsize(fp) > 1000:
        return fp
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://fanqienovel.com/'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    open(fp, 'wb').write(data)
    return fp


# ---------------------------------------------------------------- 字形渲染

def _render_glyph(font_path, char, size=RENDER_SIZE):
    """按字身框（em box）渲染字符，不做墨迹裁剪。

    用固定字身框而不是裁剪到墨迹边界：汉字的位置和留白本身就是字形特征，
    裁剪会把「门」和「闩」这类只差一笔的字变得难以区分。
    """
    try:
        font = ImageFont.truetype(font_path, size)
    except Exception:
        return None
    canvas = size * 2
    img = Image.new('L', (canvas, canvas), 0)
    d = ImageDraw.Draw(img)
    try:
        # 以字身框中心定位，保证所有字在同一坐标系
        d.text((size / 2, size / 2), char, font=font, fill=255, anchor='mm')
    except Exception:
        try:
            d.text((size // 2, size // 2), char, font=font, fill=255)
        except Exception:
            return None
    arr = np.array(img)
    if arr.max() == 0:
        return None
    # 按墨迹裁剪再缩放：让不同字号的字形可比，同时保留笔画结构
    ys, xs = np.where(arr > 20)
    if len(ys) == 0:
        return None
    crop = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    # 等比缩放后居中补边：直接拉成正方形会把「一」「二」这种
    # 极扁的字形纵向拉伸到糊成一片，丢失笔画特征
    h, w = crop.shape
    scale = (GRID * 0.86) / max(h, w)
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    resized = np.array(Image.fromarray(crop).resize((nw, nh), Image.BICUBIC), dtype=np.float32)

    canvas = np.zeros((GRID, GRID), dtype=np.float32)
    top = (GRID - nh) // 2
    left = (GRID - nw) // 2
    canvas[top:top + nh, left:left + nw] = resized

    raw = canvas / 255.0
    ink = float((raw > 0.35).mean())
    a = (raw - raw.mean()) / (raw.std() + 1e-6)
    return a, ink


def _to_ttf(font_path):
    """PIL 不认 woff2，统一转成 ttf 并缓存。"""
    if font_path.endswith(('.ttf', '.otf', '.ttc')):
        return font_path
    out = font_path + '.ttf'
    if os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    from fontTools.ttLib import TTFont
    f = TTFont(font_path, fontNumber=0)
    f.flavor = None
    f.save(out)
    return out


def _font_cmap(font_path):
    from fontTools.ttLib import TTFont
    f = TTFont(font_path, fontNumber=0)
    return f.getBestCmap()


def render_font_glyphs(font_path):
    """渲染字体里所有 PUA 码位的字形，结果按字体内容哈希缓存。"""
    key = hashlib.md5(open(font_path, 'rb').read()).hexdigest()[:16]
    cache = os.path.join(FONT_CACHE_DIR, f'glyphs_{key}.npz')
    if os.path.exists(cache):
        try:
            z = np.load(cache, allow_pickle=True)
            codes = list(z['codes'])
            mats = z['mats']
            inks = z['inks'] if 'inks' in z else np.ones(len(codes), dtype=np.float32) * 0.2
            return {int(c): mats[i] for i, c in enumerate(codes)}, codes, inks
        except Exception:
            pass

    cmap = _font_cmap(font_path)
    pua = sorted([c for c in cmap if 0xE000 <= c <= 0xF8FF])
    if not pua:
        return {}

    ttf = _to_ttf(font_path)
    codes, mats, inks = [], [], []
    for code in pua:
        r = _render_glyph(ttf, chr(code))
        if r is not None:
            codes.append(code)
            mats.append(r[0])
            inks.append(r[1])
    if not codes:
        return {}
    mats = np.stack(mats)
    try:
        np.savez_compressed(cache, codes=np.array(codes), mats=mats,
                            inks=np.array(inks, dtype=np.float32))
    except Exception:
        pass
    return {int(c): mats[i] for i, c in enumerate(codes)}, codes, np.array(inks, dtype=np.float32)


def build_candidate_pool(font_paths=None):
    """渲染候选汉字模板库（磁盘缓存，只算一次）。

    对每个候选字渲染多个字体，比对时取最佳匹配，可以显著提升形近字区分度。
    """
    cache = os.path.join(FONT_CACHE_DIR, f'pool_{GRID}_v2.npz')
    if os.path.exists(cache):
        try:
            z = np.load(cache, allow_pickle=True)
            return list(z['chars']), z['mats']
        except Exception:
            pass

    font_paths = font_paths or CJK_FONTS
    fonts = [p for p in font_paths if os.path.exists(p)]
    if not fonts:
        raise RuntimeError('缺少中文字体，请执行：apk add font-noto-cjk')

    chars, mats = [], []
    for ch in COMMON_CHARS:
        best = None
        for fp in fonts:
            r = _render_glyph(fp, ch)
            if r is not None:
                best = r[0]
                break
        if best is not None:
            chars.append(ch)
            mats.append(best)
    mats = np.stack(mats)
    try:
        np.savez_compressed(cache, chars=np.array(chars), mats=mats)
    except Exception:
        pass
    return chars, mats


# ---------------------------------------------------------------- 解码

def _pool_for_font(fp):
    """按字体从磁盘缓存加载候选模板库（不常驻内存）。

    服务是长跑进程，如果把 60MB+ 的模板库缓存在全局字典里，
    iSH 容器内存会逐渐吃紧并最终 MemoryError。
    """
    key = hashlib.md5((fp + str(os.path.getsize(fp))).encode()).hexdigest()[:12]
    cache = os.path.join(FONT_CACHE_DIR, f'pool_{GRID}_{key}.npz')
    if os.path.exists(cache):
        try:
            z = np.load(cache, allow_pickle=True)
            return list(z['chars']), z['mats']
        except Exception:
            pass
    chars, mats = [], []
    for ch in COMMON_CHARS:
        r = _render_glyph(fp, ch)
        if r is not None:
            chars.append(ch)
            mats.append(r[0])
    mats = np.stack(mats).astype(np.float32)
    try:
        np.savez_compressed(cache, chars=np.array(chars), mats=mats)
    except Exception:
        pass
    return chars, mats


def build_mapping(font_path, fonts=None, chunk=1024, min_sim=0.35):
    """PUA 码位 -> 真实汉字。

    两级精排，兼顾精度与内存：
      1. 32x32 粗筛，每字取前 K 个候选
      2. 对这 K 个候选放大回 48x48 精比，定最终结果

    低置信度保护：最佳相似度低于 min_sim 的码位不猜，保持原样。
    实测番茄字体里混有「空白/占位」字形（几乎全白），强行比对会被
    硬凑成某个汉字，还不如留空。

    内存控制：候选库按 chunk 分批参与矩阵乘，绝不一次性生成
    (6763 x N) 的中间矩阵——iSH 容器里那样会直接 MemoryError。
    """
    res = render_font_glyphs(font_path)
    if not res:
        return {}
    glyphs, all_codes, all_inks = res

    fonts = [p for p in (fonts or CJK_FONTS) if os.path.exists(p)]
    if not fonts:
        raise RuntimeError('缺少中文字体，请执行：apk add font-noto-cjk')

    # 剔除空/占位字形：用渲染时记录的原始墨迹占比。
    # 不能用归一化后的像素判断——「一」这种笔画极少的字会被误伤。
    ink_map = {int(c): float(all_inks[i]) for i, c in enumerate(all_codes)}
    usable = {}
    for code, g in glyphs.items():
        ink = ink_map.get(code, 0.2)
        if 0.015 <= ink <= 0.62:
            usable[code] = g
    if not usable:
        return {}

    codes = list(usable.keys())
    gly = np.stack([usable[c] for c in codes]).astype(np.float32).reshape(len(codes), -1)
    gly_n = gly / (np.linalg.norm(gly, axis=1, keepdims=True) + 1e-6)
    n_g = len(codes)

    chars, mats = _pool_for_font(fonts[0])
    K = 30

    # --- 第一级：粗筛 ---
    top_sim = np.full((K, n_g), -2.0, dtype=np.float32)
    top_idx = np.zeros((K, n_g), dtype=np.int32)

    for s in range(0, len(chars), chunk):
        part = mats[s:s + chunk].reshape(-1, gly.shape[1]).astype(np.float32)
        part_n = part / (np.linalg.norm(part, axis=1, keepdims=True) + 1e-6)
        sims = part_n @ gly_n.T
        merged_sim = np.vstack([top_sim, sims])
        merged_idx = np.vstack([top_idx,
                                np.arange(s, s + sims.shape[0])[:, None]
                                .repeat(n_g, axis=1).astype(np.int32)])
        order = np.argsort(-merged_sim, axis=0)[:K]
        top_sim = np.take_along_axis(merged_sim, order, axis=0)
        top_idx = np.take_along_axis(merged_idx, order, axis=0)
        del part, part_n, sims, merged_sim, merged_idx, order

    # --- 第二级：top-K 放大到 48x48 精排 ---
    mapping = {}
    for gi, code in enumerate(codes):
        g32 = usable[code]
        g_hi = np.array(Image.fromarray(
            ((g32 - g32.min()) / (np.ptp(g32) + 1e-6) * 255).astype(np.uint8)
        ).resize((48, 48), Image.BICUBIC), dtype=np.float32)
        g_hi = (g_hi - g_hi.mean()) / (g_hi.std() + 1e-6)
        g_norm = np.linalg.norm(g_hi)

        best_s, best_i = -2.0, None
        for cid in top_idx[:, gi]:
            c32 = mats[int(cid)]
            c_hi = np.array(Image.fromarray(
                ((c32 - c32.min()) / (np.ptp(c32) + 1e-6) * 255).astype(np.uint8)
            ).resize((48, 48), Image.BICUBIC), dtype=np.float32)
            c_hi = (c_hi - c_hi.mean()) / (c_hi.std() + 1e-6)
            s_val = float((c_hi * g_hi).sum() / (np.linalg.norm(c_hi) * g_norm + 1e-6))
            if s_val > best_s:
                best_s, best_i = s_val, int(cid)

        # 极简字形（如「一」「二」「十」）墨迹少、归一化后噪声大，
        # 相似度天然偏低，给它们放宽阈值，否则会被丢弃成空白
        ink = ink_map.get(code, 0.2)
        thresh = min_sim * 0.55 if ink < 0.09 else min_sim

        if best_i is not None and best_s >= thresh:
            mapping[code] = chars[best_i]

    return mapping


def decode_text(text, mapping):
    out = []
    for ch in text:
        o = ord(ch)
        if 0xE000 <= o <= 0xF8FF:
            out.append(mapping.get(o, ch))
        else:
            out.append(ch)
    return ''.join(out)


def has_pua(text):
    return any(0xE000 <= ord(c) <= 0xF8FF for c in text)


# 字形微扰导致的常见混淆对：错误字 -> 正确字
# 只收「在网文里几乎不会以错误字形态出现」的组合，避免反向误伤
CONFUSION_FIX = {
    '掌闩': '掌门', '山闩': '山门', '入闩': '入门', '看闩': '看门',
    '闩口': '门口', '闩派': '门派', '宗闩': '宗门', '闩外': '门外',
    '闩内': '门内', '闩下': '门下', '闩主': '门主', '一闩': '一门',
    '笔宣': '笔直', '正宣': '正直', '宣接': '直接', '宣线': '直线',
    '匀这': '与这', '匀其': '与其',
    '时寸会': '时才会', '寸能': '才能', '寸可': '才可',
    '一瞬寸': '一瞬才',
    # 闪 <- 门（等比缩放后仍偶发）
    '掌闪': '掌门', '山闪': '山门', '入闪': '入门', '宗闪': '宗门',
    '闪口': '门口', '闪派': '门派', '闪外': '门外', '闪内': '门内',
    '闪主': '门主', '一闪': '一门', '闪下': '门下', '大闪': '大门',
    # 寅 <- 直
    '笔寅': '笔直', '正寅': '正直', '寅接': '直接', '寅线': '直线',
    '寅到': '直到', '一寅': '一直', '寅属': '直属',
}


def correct_confusions(text):
    """修正常见的形近字误判。只做词典内的替换。"""
    for wrong, right in CONFUSION_FIX.items():
        text = text.replace(wrong, right)
    return text


def _extract_state(html):
    i = html.find('window.__INITIAL_STATE__=')
    if i < 0:
        return None
    start = i + len('window.__INITIAL_STATE__=')
    depth, end = 0, None
    instr = esc = False
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
    raw = re.sub(r'(?<=[:\[,])\s*undefined\s*(?=[,}\]])', 'null', raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


def decode_page(html, font_url=None):
    """给阅读页 HTML，返回 (解码后的正文 HTML, 映射表, 字体URL)。"""
    st = _extract_state(html) or {}
    cd = (st.get('reader', {}) or {}).get('chapterData', {}) or {}
    raw = cd.get('content', '') or ''
    css = (st.get('common', {}) or {}).get('css', '') or ''

    if not has_pua(raw):
        return raw, {}, None

    urls = extract_font_urls(css) or extract_font_urls(html)
    if not urls:
        return raw, {}, None
    url = font_url or urls[0]
    fp = download_font(url)
    mapping = build_mapping(fp)
    return decode_text(raw, mapping), mapping, url


def html_to_text(raw_html):
    """正文 HTML 转纯文本。"""
    body = re.sub(r'<br\s*/?>', '\n', raw_html)
    body = re.sub(r'</p>', '\n', body, flags=re.I)
    body = re.sub(r'<[^>]+>', '', body)
    body = (body.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
    body = re.split(r'更多免费小说|下载番茄小说APP', body)[0]
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    return body


# ---------------- 常用汉字表 ----------------

def _load_common_chars():
    chars = []
    for hi in list(range(0xB0, 0xD8)) + list(range(0xD8, 0xF8)):
        for lo in range(0xA1, 0xFF):
            try:
                chars.append(bytes([hi, lo]).decode('gb2312'))
            except Exception:
                pass
    seen, out = set(), []
    for c in chars:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


COMMON_CHARS = _load_common_chars()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        print(f'候选字库：{len(COMMON_CHARS)} 字')
        sys.exit(0)
    html = open(sys.argv[1], encoding='utf-8', errors='ignore').read()
    text, mp, url = decode_page(html)
    print('字体:', url)
    print('映射条数:', len(mp))
    print('--- 解码后 ---')
    print(html_to_text(text)[:500])
