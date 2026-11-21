"""拆书引擎：两段式（先分单元 -> 逐单元深度拆解），按用户给定提示词实现。"""
import os
import re
import json
import time
import urllib.request
import urllib.error

import db

API_URL = os.environ.get('DEEPSEEK_API_URL', 'https://api.deepseek.com/chat/completions')
MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')

# 单次请求喂给模型的正文上限（字符），控制成本与超时
CHUNK_CHARS = 12000
# 分单元阶段每次喂入的最大章节数
PLAN_MAX_CHAPTERS = 60


class LLMError(Exception):
    pass


def chat(messages, temperature=0.3, max_tokens=4000, timeout=300, retries=3):
    if not API_KEY:
        raise LLMError('未设置 DEEPSEEK_API_KEY 环境变量')
    payload = {
        'model': MODEL,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'stream': False,
    }
    body = json.dumps(payload).encode('utf-8')
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API_URL, data=body,
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + API_KEY})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            return d['choices'][0]['message']['content']
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='ignore')[:300]
            last = f'HTTP {e.code}: {detail}'
            # 429 / 5xx 退避重试
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            raise LLMError(last)
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
            time.sleep(2 ** attempt)
    raise LLMError(last)


# ---------------- prompt ----------------

ANALYST_SYSTEM = """你是一个专业的网文文字分析智能助手，专门帮助用户拆解小说中的单元故事，提供分析内容的结构化输出。

注意事项：
1. 先通读文章故事架构，并将同一故事线的故事划分单元，每单元可以划分为多个章节为一个单元，并不是每章单独一个单元，每个单元章节必须根据内容的故事节点去划分的。
2. 单元划分完之后，根据每个单元的内容，提取并概括该单元中的核心设定与世界观细节。
3. 分析角色相关关系。

任务：
4. 根据故事情节，将文章尝试分成故事单元。
5. 总结每个单元的核心内容。
6. 总结每个单元出现的所有角色，梳理人物关系。

输出格式：按照划分好的单元，依次输出单元名、章节范围、故事梗概、登场角色、人物关系。
⚠️ 角色关系要特别详细，把该出场角色的所有相关信息，包括个人信息背景、所属阵营、文章中扮演的角色，并给出该角色所有的作用。"""

PLAN_SYSTEM = """你是网文结构分析师。下面会给你一本书的章节列表（只有章节号和标题，可能附带少量摘要）。
你的任务：根据章节标题与已知信息，**推测性地**把全书划分为若干个「故事单元」。

划分原则：
- 一个单元对应一条完整的故事线/一个大情节段落，通常包含多个连续章节。
- 依据故事节点（如开篇、转折、高潮、换地图、换主线）划分，不要按固定章数机械切分。
- 单元之间必须连续覆盖、不重叠、不留空。
- 书名/卷名暗示的新阶段要作为切分信号。

严格按 JSON 数组输出，不要任何解释、不要 markdown 代码块标记：
[{"name":"单元名(6-12字)","chap_from":起始章号,"chap_to":结束章号,"reason":"划分依据(30字内)"}]
章号使用给定的整数章号。"""

UNIT_SYSTEM = ANALYST_SYSTEM + """

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
关系要具体，不要只写"朋友/敌人"这样的标签。"""


def _clean_json(s):
    """从模型输出中抠出 JSON，并处理 JS 字面量 undefined。"""
    s = s.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    # 找第一个 [ 和最后的 ]
    i, j = s.find('['), s.rfind(']')
    if i != -1 and j != -1 and j > i:
        s = s[i:j + 1]
    # JS 的 undefined 不是合法 JSON
    s = re.sub(r'(?<=[:\[,])\s*undefined\s*(?=[,}\]])', 'null', s)
    return s


def plan_units(book, chapters, on_progress=None):
    """阶段一：分单元。chapters: [(idx, title, content)]。

    策略：先把章节标题打包成若干批（按章号 + 少量摘要），每批让模型划分，
    再合并相邻批次结果并做边界修正，保证连续不重叠。
    """
    # 分批
    batches = []
    for i in range(0, len(chapters), PLAN_MAX_CHAPTERS):
        batches.append(chapters[i:i + PLAN_MAX_CHAPTERS])

    all_units = []
    for bi, batch in enumerate(batches):
        lines = []
        for idx, title, content in batch:
            # 取章节开头一小段作为摘要线索
            snippet = re.sub(r'\s+', ' ', (content or ''))[:90]
            lines.append(f'第{idx+1}章 {title}｜{snippet}')
        listing = '\n'.join(lines)
        user = (f'书名：《{book["title"]}》\n'
                f'全书共 {len(chapters)} 章，本批为第 {batch[0][0]+1} — {batch[-1][0]+1} 章。\n'
                f'继续沿用前文单元划分的语境。\n\n章节列表：\n{listing}\n\n'
                f'请只输出 JSON 数组。章号必须在 {batch[0][0]+1} 到 {batch[-1][0]+1} 之间。')
        raw = chat([{'role': 'system', 'content': PLAN_SYSTEM},
                    {'role': 'user', 'content': user}],
                   temperature=0.2, max_tokens=3000)
        units = _parse_plan(raw, batch[0][0] + 1, batch[-1][0] + 1)
        all_units.extend(units)
        if on_progress:
            on_progress((bi + 1) / max(1, len(batches)) * 0.5)

    all_units = _normalize_units(all_units, len(chapters))
    return all_units


def _parse_plan(raw, lo, hi):
    s = _clean_json(raw)
    try:
        arr = json.loads(s)
    except Exception:
        return []
    out = []
    for u in arr:
        try:
            a = int(u.get('chap_from'))
            b = int(u.get('chap_to'))
        except Exception:
            continue
        a = max(lo, min(hi, a))
        b = max(lo, min(hi, b))
        if b < a:
            a, b = b, a
        out.append({'name': str(u.get('name', '')).strip()[:30] or f'第{a}-{b}章',
                    'chap_from': a, 'chap_to': b,
                    'reason': str(u.get('reason', ''))[:60]})
    return out


def _normalize_units(units, total):
    """修正重叠/空洞，保证 1..total 连续覆盖。"""
    if not units:
        # 兜底：每 10 章一个单元
        units = [{'name': f'第{i+1}-{min(i+10,total)}章段', 'chap_from': i + 1,
                  'chap_to': min(i + 10, total), 'reason': '自动兜底'} for i in range(0, total, 10)]

    units.sort(key=lambda u: (u['chap_from'], u['chap_to']))

    fixed = []
    cursor = 1
    for u in units:
        a, b = u['chap_from'], u['chap_to']
        if b < cursor:
            continue          # 完全被前面的覆盖，丢弃
        a = max(a, cursor)    # 消除重叠
        if b < a:
            continue
        fixed.append({'name': u['name'], 'chap_from': a, 'chap_to': b,
                      'reason': u.get('reason', '')})
        cursor = b + 1
        if cursor > total:
            break

    # 尾部补齐
    if cursor <= total:
        fixed.append({'name': f'第{cursor}-{total}章', 'chap_from': cursor,
                      'chap_to': total, 'reason': '尾部补齐'})

    # 重命名兜底名
    for i, u in enumerate(fixed, 1):
        if not u['name'].strip():
            u['name'] = f'单元{i}'
    return fixed


def build_unit_payload(book, chapters, unit):
    """把一个单元的章节正文拼成给模型的输入。

    控制总长度：超长章节取「开头 + 结尾」，超长单元按比例压缩每章预算，
    尽量让每个单元都能被完整覆盖而不是只看到前几章。
    """
    a, b = unit['chap_from'], unit['chap_to']
    sub = [c for c in chapters if a <= c[0] + 1 <= b]
    if not sub:
        return ''

    overhead = sum(len(f'\n【第{c[0]+1}章 {c[1]}】\n') for c in sub)
    budget = max(2000, CHUNK_CHARS - overhead)
    per = max(600, budget // len(sub))   # 每章预算

    parts = []
    for idx, title, content in sub:
        body = (content or '').strip()
        head = f'\n【第{idx+1}章 {title}】\n'
        if len(body) > per:
            # 两头都保留：开头交代场景，结尾通常有钩子
            keep_head = int(per * 0.62)
            keep_tail = per - keep_head
            body = body[:keep_head] + '\n……（中段略）……\n' + body[-keep_tail:]
        parts.append(head + body)
    return ''.join(parts)


def analyze_unit(book, chapters, unit):
    payload = build_unit_payload(book, chapters, unit)
    ctx = ''
    if unit.get('reason'):
        ctx = f'（结构分析提示：该单元划分依据是「{unit["reason"]}」）\n'
    user = (f'书名：《{book["title"]}》\n'
            f'章节范围：第{unit["chap_from"]}章 — 第{unit["chap_to"]}章\n'
            f'预估单元名：{unit["name"]}\n{ctx}\n'
            f'以下是该单元的全部章节正文：\n{payload}\n\n'
            f'请严格按格式输出该单元的分析。')
    return chat([{'role': 'system', 'content': UNIT_SYSTEM},
                 {'role': 'user', 'content': user}],
                temperature=0.4, max_tokens=6000, timeout=420)


# ---------------- 编排 ----------------

def run_analysis(analysis_id):
    """后台执行的完整拆书流程。可断点续跑。"""
    a = db.get_analysis(analysis_id)
    if not a:
        return
    book = db.get_book(a['book_id'])
    chapters = [(c['idx'], c['title'], c['content'])
                for c in db.get_chapters(book['id'])]
    if not chapters:
        db.update_analysis(analysis_id, status='error', message='该书没有章节')
        return

    try:
        db.update_analysis(analysis_id, status='running', stage='梳理整体架构',
                           progress=0.02, message='正在划分故事单元…')

        # 阶段一：分单元（若已存在则跳过，支持续跑）
        existing = db.list_units(analysis_id)
        if not existing:
            def prog(p):
                db.update_analysis(analysis_id, progress=0.02 + p * 0.28,
                                   message=f'正在划分故事单元… {int(p*100)}%')
            units = plan_units(book, chapters, on_progress=prog)
            db.clear_units(analysis_id)
            for i, u in enumerate(units, 1):
                db.save_unit(analysis_id, i, name=u['name'],
                             chap_from=u['chap_from'], chap_to=u['chap_to'],
                             summary=u.get('reason', ''))
            db.update_analysis(analysis_id, stage='划分完成', progress=0.30,
                               message=f'共划分 {len(units)} 个单元')
        else:
            units = [{'name': u['name'], 'chap_from': u['chap_from'], 'chap_to': u['chap_to']}
                     for u in existing]

        # 阶段二：逐单元深度分析
        total = len(units)
        for i, u in enumerate(units, 1):
            rec = db.get_unit(analysis_id, i)
            if rec and rec.get('detail'):
                continue  # 已完成，跳过（断点续拆）
            db.update_analysis(analysis_id, stage=f'分析单元 {i}/{total}',
                               progress=0.30 + (i - 1) / total * 0.68,
                               message=f'正在深度分析：{u["name"]}')
            detail = analyze_unit(book, chapters, u)
            db.save_unit(analysis_id, i, name=u['name'], chap_from=u['chap_from'],
                         chap_to=u['chap_to'], detail=detail,
                         summary=rec.get('summary', '') if rec else u.get('reason', ''))

        db.update_analysis(analysis_id, status='done', stage='完成', progress=1.0,
                           message=f'拆书完成，共 {total} 个单元')
    except LLMError as e:
        db.update_analysis(analysis_id, status='error', message=str(e)[:500])
    except Exception as e:
        db.update_analysis(analysis_id, status='error',
                           message=f'{type(e).__name__}: {e}'[:500])


def estimate_cost(book_id):
    """粗估 token。"""
    chs = db.get_chapters(book_id)
    chars = sum(len(c['content']) for c in chs)
    # 估算：分单元阶段 ≈ 每章 100 字线索；逐单元阶段 ≈ 正文 ×1.15（含 prompt 开销）
    plan_in = len(chs) * 110
    unit_in = chars * 1.15
    unit_out = max(1, len(chs) // 10) * 2200
    tin = plan_in + unit_in
    return {'chars': chars, 'chapters': len(chs),
            'est_input_tokens': int(tin / 1.6), 'est_output_tokens': int(unit_out)}
