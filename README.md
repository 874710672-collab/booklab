# 书析 · 网文拆书工作台

把小说按「故事单元」拆解成结构化分析报告。

## 🚀 在线使用（推荐）

**https://874710672-collab.github.io/booklab/**

不需要安装，不需要启动任何服务，24 小时可用。

- 第一次打开会要求填 DeepSeek API Key（`sk-` 开头），填一次就记住
- 支持导入 txt / epub，或直接粘贴正文
- 所有数据存在你自己的浏览器里

## 两个版本

| | 在线版（docs/） | 本地版（app/ + web/） |
|---|---|---|
| 入口 | GitHub Pages | `./run.sh` |
| 后端 | 无，全在浏览器 | Python |
| 数据 | 浏览器 IndexedDB | SQLite |
| txt / epub 导入 | ✅ | ✅ |
| 拆书 | ✅ | ✅ |
| 番茄小说采集 | ❌ | ✅（仅片段） |
| 依赖设备在线 | ❌ 不依赖 | ✅ 依赖 |

**日常使用推荐在线版。** 只有需要番茄采集时才用本地版。

## 拆书逻辑

两段式，按固定提示词执行：

1. **分单元**：通读全书，按故事节点（而非固定章数）划分单元
2. **逐单元深度分析**，每个单元输出：
   - 核心设定与世界观
   - 故事梗概
   - 登场角色（个人信息背景 / 所属阵营 / 作用）
   - 人物关系（逐对说明，含变化）

支持**断点续拆**：中断后重新点「开始拆书」会跳过已完成单元。

## 本地版

```sh
cd /var/minis/shared/booklab
./run.sh              # 启动
./run.sh status       # 查看状态与地址
./run.sh stop         # 停止
```

需要 `DEEPSEEK_API_KEY` 环境变量。

### 本地版的真实限制

它跑在 iSH（Minis 内置 Linux）里，受 iOS 后台策略约束：

- **服务会随 Minis 退出而停止**，重新打开后需执行一次 `./run.sh`
- **长时间挂起后进程会被冻结**，表现为"进程在但不响应"，需 `./run.sh stop && ./run.sh` 重启
- IP 由 DHCP 分配，换网络后会变

这些是 iOS 沙箱的固有行为，不是代码问题。**所以日常用在线版。**

## 目录结构

```
booklab/
├── docs/                  在线版（GitHub Pages 托管）
│   ├── index.html
│   └── app.js
├── standalone/            纯前端版源码（docs 的来源）
├── app/                   本地版后端
│   ├── server.py          HTTP 服务 + API
│   ├── worker.py          独立子进程任务执行器
│   ├── db.py              SQLite 数据层
│   ├── importer.py        txt/epub 解析
│   ├── engine.py          拆书引擎
│   ├── fontdec.py         番茄字体反爬解码器
│   ├── fanqie.py          番茄采集
│   └── browser_fetch.py   浏览器会话抓取
├── web/                   本地版前端
└── run.sh                 本地版启动脚本
```

## 关于番茄小说

实测结论（2026-11）：

1. **目录**可以完整拿到（沿 `nextItemId` 链遍历）
2. **正文**每章被服务端强制截断到约 200 字，**登录也无法解除**
3. 网页版还有**字体反爬**——部分汉字被替换成私用区码位，用动态字体渲染

本项目实现了字体解码器，能把片段还原成可读文本：

```
原始： 犹豫，调半元，猛刀携旨"昊"朝玄黄钟劈斩。
解码：没有任何犹豫，许太平调动出体内大半道元，猛然一刀携着法旨之力"太昊"朝下方玄黄钟劈斩下去。
```

但拿不到全文。完整正文只在番茄 App 内提供。**要完整拆一本书，请用 txt / epub 导入。**

## 开发

在线版没有构建步骤，直接改 `docs/` 下的两个文件，推送到 `main` 分支后 GitHub Pages 会自动更新（约 1 分钟）。

本地版依赖：

```sh
apk add python3 py3-numpy py3-pillow font-noto-cjk
pip3 install --break-system-packages fonttools brotli
```
