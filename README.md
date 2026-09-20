# 克克咪 · 抖音直播监听与数据面板

**多直播间监听、弹幕公屏、在线观众、礼物统计、日榜周榜与主持管理。**

克克咪（Kekemi）是一个可自行部署的 Douyin Live Dashboard。输入抖音号、直播间链接或用户主页链接，在同一个网页里查看直播互动、在线名单、麦位和礼物数据。数据按直播间隔离，支持成员、管理员和超级管理员分工使用。

[English](README.en.md) · [完整功能](docs/FEATURES.md) · [配置与部署](docs/DEPLOYMENT.md) · [隐私与数据](docs/PRIVACY.md) · [更新记录](CHANGELOG.md)

## 现有功能

| 模块 | 能做什么 |
| --- | --- |
| 多直播间监听 | 输入抖音号、主页链接或直播间链接；默认同时监听 10 个房间，上限可配置；按房间切换数据 |
| 自动监听 | 用环境变量设置常驻房间，轮询开播状态；下播后等待下一场；另支持临时房间 |
| 在线观众与麦位 | 展示接口可获取的在线观众、麦上主持和本轮上麦票数，结合本厅当日累计票数展示 |
| 神秘人 / 匿名用户 | 展示当前在厅的匿名用户；依据可用标识与已有身份记录匹配，可解析时补充昵称和公开资料；证据不足时保持匿名 |
| 弹幕公屏 | 展示文字聊天、送礼人、礼物及可确定的收礼主持；不展示进场刷屏；支持去重、翻看冻结与新消息提示 |
| 日榜 | 按北京时间统计本厅当日游客送出票数、主持收到票数；默认展示前 20 名，可展开 |
| 主持周榜 / 游客周榜 | 按房间查看周榜、可用历史时段及明细；同一已匹配身份的不同标识合并统计 |
| 礼物库 | 搜索礼物、查看价格来源和未定价名称；维护礼物及皮肤名称对应单价，保留修改历史 |
| 礼物归属修正 | 按送礼人或收礼主持搜索，修正归属及备注；保留原始互动记录 |
| 主持文字静默提醒 | 按厅订阅、铃铛面板、未读计数及浏览器通知；默认 15 分钟无文字发言提醒，按 2 小时档计数并形成聊天快照 |
| 账号与权限 | 邀请码注册、管理员审核、登录、密码找回；暂停 / 恢复账号、角色管理、强制下线及受限删除 |
| 用户备注 | 超级管理员可管理账号备注；备注不向普通成员公开 |
| 性能与运维 | SSE、增量拉取、gzip、ETag / 304、短时缓存、后台标签页暂停常规轮询；健康检查与版本指纹 |

**静默提醒依据公屏文字消息，不采集或分析麦克风音频，不能据此认定主持没有说话。**

### 权限分工

| 角色 | 主要权限 |
| --- | --- |
| 已审核成员 | 查看允许访问的直播间、神秘人、在线名单、公屏和日榜 |
| 管理员 | 成员能力，以及周榜、礼物库、归属修正、静默提醒和账号审核管理 |
| 超级管理员 | 管理员能力，以及解析 / 启动 / 停止监听、临时房间、角色调整、强制下线、账号删除和私人备注 |

权限由后端检查。正式部署启用登录；本地免登录模式仅用于开发。

## 快速开始

需要 Python 3.10+、Node.js 20+ 和本人有权使用的抖音网页 Cookie。

```bash
git clone https://github.com/kkray983681062-bit/douyin-kekemi.git
cd douyin-kekemi
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

macOS / Linux：

```bash
source .venv/bin/activate
cp .env.example .env
```

安装依赖：

```bash
pip install -r requirements.txt
npm ci
```

在本机 `.env` 中填写配置：

```dotenv
APP_ENV=development
AUTH_REQUIRED=0
DY_COOKIES=在本机填写抖音主站Cookie
DY_LIVE_COOKIES=在本机填写抖音直播Cookie
MAX_ROOMS=10
PORT=5000
DEFAULT_WATCH_IDS=
SQLITE_DB_PATH=
```

运行 `python web_listener.py`，访问 [本地页面](http://127.0.0.1:5000)。需要常驻监听时，在 `DEFAULT_WATCH_IDS` 填入自己有权访问的抖音号，多个账号用英文逗号分隔。

仓库不提供真实账号清单、Cookie 或线上演示地址。生产环境使用 [Docker / Railway 部署说明](docs/DEPLOYMENT.md)。仓库已有 Supabase 账号表迁移文件，部署者需自行配置数据库并初始化首位管理员。

## 工作方式

```text
抖音直播间与当前在线接口
          │ WebSocket / HTTP
          ▼
     Python + Flask
          ├── 在线观众、麦位与身份记录
          ├── 聊天与礼物 → SQLite → 日榜 / 周榜
          ├── 文字静默检查 → 提醒与聊天快照
          └── SSE + 增量查询 → 浏览器数据面板

Supabase Auth / PostgreSQL → 注册、审核、角色与会话
```

普通观众聊天通过公屏约 3 秒增量拉取补齐；神秘人等相关事件仍通过 SSE 推送。并非所有事件都逐条即时推送。

## 统计口径

- 榜单统计本工具**实际监听并捕获**、数量和单价可核算的礼物；不是完整平台历史流水，也不是结算收入。
- 价格或数量不明确时保留“未知”，不强行估算票数。
- 日榜采用北京时间；“本轮上麦票数”和“本厅当日累计票数”分别展示。
- 当前在线名单受上游接口可用性影响，不等于完整观众历史。
- 聊天、礼物明细及普通用户资料默认保留 7 天；身份档案、部分归档和静默快照另有保留规则，见[隐私说明](docs/PRIVACY.md)。

## 项目结构

```text
web_listener.py          Flask、房间监听、API 与事件推送
online_audience.py       在线观众和麦位
identity_registry.py     身份标识关联与匿名别名
gift_analytics.py        礼物数量和票数计算
gift_catalog.py          礼物、名称定价和修改历史
leaderboards.py          日榜、周榜、礼物归属
mic_silence.py           文字静默状态、告警与快照
kekemi_auth/             注册、登录、审核和权限
user_notes.py            超级管理员账号备注
templates/ + static/     页面、交互、样式和资源
builder/ + dy_apis/      抖音请求、协议和接口封装
scripts/                管理员初始化、部署版本核对
supabase/migrations/     账号系统数据库定义
tests/                  Python 与 JavaScript 回归测试
docs/                   功能、部署与隐私说明
```

## 测试与维护

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
node --test tests/*.test.js
```

测试应使用虚构 Cookie、空的 `DEFAULT_WATCH_IDS` 和临时 SQLite 路径。测试通过不代表当前抖音接口、生产部署或真实直播间已验证。

Cookie 会过期，抖音接口、消息结构与风控策略也可能变化。仅处理自己有权访问的数据；发布问题报告前清除 Cookie、账号标识、聊天内容和部署信息。

## 检索关键词

中文：克克咪、抖音直播监听、抖音弹幕采集、直播公屏、在线观众、连麦主持、礼物统计、礼物库、日榜、周榜、匿名用户、神秘人、主持文字静默提醒、直播数据面板。

English: Douyin live dashboard, livestream monitoring, live chat, danmaku, audience analytics, gift analytics, leaderboards, multi-room monitoring, Python, Flask, SQLite, WebSocket, Server-Sent Events, Supabase, Railway.
