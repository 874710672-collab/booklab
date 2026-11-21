# 书析 · 网文拆书工作台

一个本地运行的深色网页 APP，用于把小说按「故事单元」拆解成结构化分析报告。

## 快速开始

```sh
cd /var/minis/shared/booklab
./run.sh
```

启动后访问 `http://127.0.0.1:8787`。

需要环境变量 `DEEPSEEK_API_KEY`（拆书功能依赖）。

## 三种导入方式

| 方式 | 说明 |
|---|---|
| **文件** | 上传 `.txt` / `.epub`，自动按「第X章」切分，支持 GBK/UTF-8 自动识别 |
| **粘贴文本** | 直接贴正文，同样自动切分 |
| **番茄** | 填章节 ID 从番茄小说网页端采集 |

### 关于番茄采集的限制（重要）

实测结论：

- **目录**可以完整拿到（沿 `nextItemId` 链遍历，4051 章也能全取）
- **正文**每章被服务端强制截断到约 **200 字**，**登录也无法解除**
  （已验证：登录前后 DOM 长度、`content` 字段长度完全一致）
- 网页版正文还有**字体反爬**：部分汉字被替换成私用区码位，用动态字体渲染
- Python 直连会被风控拦截（返回验证码页），采集走浏览器会话

本工具已实现**字体解码器**，能把片段里的乱码还原成正常汉字：

```
原始： 犹豫，调半元，猛刀携旨“昊”朝玄黄钟劈斩。
解码：没有任何犹豫，许太平调动出体内大半道元，猛然一刀携着法旨之力“太昊”朝下方玄黄钟劈斩下去。
```

但**拿不到全文**。要完整拆一本书，请用 txt / epub 导入。

## 拆书逻辑

按给定的提示词两段式执行：

1. **分单元**：通读全书，按故事节点（而非固定章数）划分单元，
   输出单元名与章节范围
2. **逐单元深度分析**：对每个单元输出
   - 核心设定与世界观
   - 故事梗概
   - 登场角色（个人信息背景 / 所属阵营 / 作用）
   - 人物关系（逐对说明，含变化）

结果落 SQLite，支持**断点续拆**——中断后重新点「开始拆书」会跳过已完成单元。

## 目录结构

```
booklab/
├── run.sh                  启动脚本
├── app/
│   ├── server.py           HTTP 服务 + API
│   ├── db.py               SQLite 数据层
│   ├── importer.py         txt/epub 解析 + 章节切分
│   ├── engine.py           两段式拆书引擎
│   ├── fontdec.py          番茄字体反爬解码器
│   ├── fanqie.py           番茄采集（目录 / 片段）
│   └── browser_fetch.py    通过浏览器会话抓番茄章节
├── web/                    深色前端
│   ├── index.html
│   ├── style.css
│   └── app.js
└── data/
    ├── booklab.db          数据库
    ├── uploads/            导入的原始文件
    └── server.log          运行日志
```

## API

| 端点 | 说明 |
|---|---|
| `GET /api/books` | 书架列表 |
| `GET /api/book?id=N` | 书籍详情（含章节列表） |
| `GET /api/chapter?book=N&idx=M` | 单章正文 |
| `POST /api/import` | 上传文件（文件名走 `X-Filename-B64`） |
| `POST /api/paste` | 粘贴文本 |
| `POST /api/analyse` | 开始拆书 |
| `GET /api/analysis?id=N` | 拆书结果与进度 |
| `GET /api/fanqie_chapter?chapter=ID` | 抓单章（含字体解码） |
| `GET /api/fanqie_fetch?chapter=ID&max=N` | 后台批量采集 |

## 依赖

```sh
apk add python3 py3-numpy py3-pillow font-noto-cjk
pip3 install --break-system-packages fonttools brotli
```

字体解码器首次运行会构建字形模板缓存（约 1-2 分钟），之后秒级。
