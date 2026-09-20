# 配置与部署

## 主要环境变量

| 变量 | 默认 / 示例 | 作用 |
| --- | --- | --- |
| `APP_ENV` | `development` | 生产设为 `production` |
| `AUTH_REQUIRED` | `0` | 本地开发可关闭；生产要求启用 |
| `DY_COOKIES` / `DY_LIVE_COOKIES` | 本地填写 | 抖音主站 / 直播 Cookie |
| `MAX_ROOMS` | `10` | 同时监听上限 |
| `DEFAULT_WATCH_IDS` | 空 | 常驻抖音号，英文逗号分隔 |
| `PORT` | `5000` | Web 端口 |
| `SQLITE_DB_PATH` | 项目内数据库 | 持久化位置 |
| `ONLINE_SYNC_INTERVAL` | `3` | 在线同步间隔，秒 |
| `MIC_TICKET_REFRESH_INTERVAL` | `3` | 麦位与票数刷新间隔，秒 |
| `RESPONSE_CACHE_TTL` | `1.0` | 只读缓存有效期，秒；0 关闭 |
| `SILENCE_THRESHOLD_SECONDS` | `900` | 文字静默阈值，秒 |
| `SILENCE_SCAN_INTERVAL` | `60` | 静默检查间隔，秒 |
| `SUPABASE_URL` | 空 | 自有 Supabase 地址 |
| `SUPABASE_SECRET_KEY` | 空 | 仅服务端使用的密钥 |
| `SUPABASE_SERVICE_ROLE_KEY` | 空 | 旧密钥兼容入口，新密钥优先 |
| `REGISTRATION_INVITE_CODE` | 空 | 注册邀请码 |
| `APP_SESSION_SECRET` | 空 | 会话等服务端校验密钥 |
| `SUPABASE_RESET_REDIRECT_URL` | 自有域名 `/reset-password` | 密码恢复跳转地址 |
| `DEPLOY_BASE_URL` | 空 | 版本核对目标地址 |

可选旧 IM 客户端使用 `DY_IM_APP_KEY`；可选浏览器 Cookie 辅助工具使用 `DY_PROFILE_URL`。Web 直播面板不依赖这两个扩展入口。

## Docker / Railway

1. 设置 `APP_ENV=production`、`AUTH_REQUIRED=1`，并填写自有 Cookie、Supabase 地址、服务端密钥、邀请码和会话密钥。
2. 在自有 Supabase 数据库检查并执行 `supabase/migrations/` 中的迁移。
3. 持久卷挂载 `/app/data`，设置 `SQLITE_DB_PATH=/app/data/mystery_history.db`。
4. 使用根目录 `Dockerfile`，单个 Gunicorn worker、384 个线程，避免多进程重复监听。
5. Railway 读取注入的 `PORT`，通过 `/healthz` 检查存活；部署后另行检查注册、权限及实际直播数据。

```bash
docker build -t kekemi .
docker run --env-file .env -p 5000:5000 -v kekemi-data:/app/data kekemi
```

`.env` 须提前填入生产配置和持久化路径。远程部署使用 HTTPS 并保持认证开启。

## 首位超级管理员

在服务器运行，交互输入密码：

```bash
python -m scripts.bootstrap_super_admin \
  --email owner@example.com \
  --username owner \
  --reason "Initial administrator setup"
```

示例邮箱需替换为自己控制的地址。脚本也支持从指定环境变量读取密码；不要把密码写入命令参数或 Git。现有普通账号不会默认自动提权。

## 部署版本核对

```bash
python scripts/verify_deploy.py origin/main https://your-app.example
```

脚本只有在提供目标地址后才发出请求。版本与存活检查不能替代真实直播验证。

## 常见问题

- 解析或监听失败：检查 Cookie 完整性、有效期与直播间可访问性。
- 在线名单为空：检查开播状态、主播身份及上游接口返回。
- 礼物价格未知：确认数量、单价来源后再修正，不强行估算。
- 容器重建丢数据：核对持久卷及 `SQLITE_DB_PATH`。
- 本地旧依赖兼容问题：Dockerfile 包含 `protobuf-to-dict` 的 Python 兼容处理，可用于复现容器环境。

仓库配置本身不会创建云服务、迁移生产数据或录入凭证。
