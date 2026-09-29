# RDOS Web Server 部署说明

本文用于把当前 RDOS Control Panel 部署到一台 Linux Web Server 做实际测试。推荐拓扑：

```text
管理员浏览器 / 各地 Local Runner
              ↓ HTTPS
            Nginx
              ↓ 127.0.0.1:8000
      FastAPI Control Panel
              ↓
     SQLite 持久目录 + 飞书 OAuth
```

Web Server 只运行 Control Panel。Local Runner 应继续运行在各自的研发电脑上，不要把 Runner 和研发 Workspace 一起迁到服务器。

## 1. 部署前确认

- Linux 服务器可使用 Python 3.9 或更高版本、Git、Nginx 和 systemd。
- 已准备 HTTPS 域名，例如 `rdos.example.com`。
- 防火墙只向公网开放 80/443；不要直接暴露 8000。
- 服务器具有持久磁盘和 SQLite 备份目录。
- 如需真实飞书同步，运行 RDOS 的系统用户必须已经安装并完成 `lark-cli` 个人 OAuth；Runner 不持有飞书 OAuth。
- 不要把本机的 `.env.local`、数据库、Runner 配置或 `workspace/` 上传到服务器。

## 2. 创建独立系统用户与目录

以下命令以 Ubuntu 为例：

```bash
sudo useradd --system --create-home \
  --home-dir /var/lib/baite-rdos \
  --shell /usr/sbin/nologin baite-rdos

sudo install -d -o baite-rdos -g baite-rdos /opt/baite-rdos
sudo install -d -o baite-rdos -g baite-rdos /var/lib/baite-rdos/backups
sudo install -d -o root -g baite-rdos -m 0750 /etc/baite-rdos
```

## 3. 拉取代码并安装依赖

```bash
sudo -u baite-rdos git clone \
  https://github.com/yuhanliualex1149/baite_RDOS.git \
  /opt/baite-rdos/app

sudo -u baite-rdos python3 -m venv /opt/baite-rdos/app/.venv
sudo -u baite-rdos /opt/baite-rdos/app/.venv/bin/pip install \
  -r /opt/baite-rdos/app/requirements.txt
```

部署前运行测试：

```bash
cd /opt/baite-rdos/app
sudo -u baite-rdos .venv/bin/python -m unittest \
  tests.test_api tests.test_projects tests.test_runner tests.test_rag_sync -v
```

## 4. 创建服务器环境配置

先生成 Session Secret：

```bash
openssl rand -hex 32
```

创建 `/etc/baite-rdos/rdos.env`，权限必须为 `0640 root:baite-rdos`：

```text
BAITE_DB_PATH=/var/lib/baite-rdos/baite.db
BAITE_SESSION_SECRET=<粘贴上一步生成的随机值>
BAITE_ADMIN_USERNAME=admin
BAITE_ADMIN_PASSWORD=<首次部署使用的长随机密码>
BAITE_COOKIE_SECURE=true
BAITE_PUBLIC_URL=https://rdos.example.com
BAITE_HOST=127.0.0.1
BAITE_PORT=8000
BAITE_SELECTED_RAG_FOLDER_TOKEN=KQjyf6KrxlZ24MdkHirc9TP0n2d
BAITE_TIMEZONE=Asia/Shanghai
BAITE_SESSION_HOURS=12
BAITE_ONLINE_WINDOW_SECONDS=10
```

```bash
sudo chown root:baite-rdos /etc/baite-rdos/rdos.env
sudo chmod 0640 /etc/baite-rdos/rdos.env
```

注意：

- 不要把真实密码或 Session Secret 写回 Git 仓库。
- `BAITE_ADMIN_PASSWORD` 只用于空数据库首次创建管理员。首次登录成功后，从服务器环境文件中删除这一行，再重启服务。
- 正式 HTTPS 环境必须保持 `BAITE_COOKIE_SECURE=true`。
- 如暂时没有配置飞书 OAuth，部署测试阶段可以临时加入 `BAITE_DISABLE_EXTERNAL_SYNC=true`；这只能用于隔离测试，配置 OAuth 后必须移除。

## 5. 配置 systemd

创建 `/etc/systemd/system/baite-rdos.service`：

```ini
[Unit]
Description=Baite AI RDOS Control Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=baite-rdos
Group=baite-rdos
WorkingDirectory=/opt/baite-rdos/app
EnvironmentFile=/etc/baite-rdos/rdos.env
ExecStart=/opt/baite-rdos/app/.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now baite-rdos
sudo systemctl status baite-rdos --no-pager
curl -fsS http://127.0.0.1:8000/api/health
```

预期健康检查返回：

```json
{"status":"ok"}
```

## 6. 配置 Nginx 与 HTTPS

在现有 HTTPS `server` 块中增加：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    client_max_body_size 2m;
}
```

应用配置前必须先检查：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS https://rdos.example.com/api/health
```

不要直接复制覆盖服务器现有 Nginx 配置。若服务器还承载其他网站或路径，应先读取当前配置，增加独立域名或独立 `location`，并回归检查原有应用。

## 7. 首次登录与 Runner 接入

1. 打开 `https://rdos.example.com`，使用环境文件中的初始管理员账号登录。
2. 修改管理员密码。
3. 从 `/etc/baite-rdos/rdos.env` 删除 `BAITE_ADMIN_PASSWORD`，执行 `sudo systemctl restart baite-rdos`。
4. 在 Runners 页面创建节点并下载一次性配置。
5. 把配置安全地放到对应研发电脑，确认其中的 `control_url` 是公网 HTTPS 地址。
6. 在研发电脑运行：

```bash
python runner/runner.py --config /安全路径/runner-config.json
```

7. 在工作台确认 Runner Online、心跳和项目同步正常。

## 8. 飞书同步边界

- Control Server 使用自己的飞书 OAuth 读取 Selected RAG 和项目文件夹。
- 项目正文只读；Server 只能在项目文件夹的 `RDOS 项目进展/` 子目录重建 Runner 日报。
- Local Runner 不持有飞书 OAuth。
- 首次真实写入测试必须使用管理员明确指定的测试项目文件夹，不能使用 Workflow 来源文件或 Selected RAG 正式文件夹。

如果飞书认证尚未完成，先验收登录、Runner 注册、HTTPS、数据库持久化和项目本地上报，不要把“Control Panel 已上线”误认为“飞书同步已验收”。

## 9. 数据备份、更新与回滚

SQLite 备份应使用 SQLite 自带备份命令，不要在运行中只复制单个 `.db` 文件：

```bash
sudo -u baite-rdos sqlite3 /var/lib/baite-rdos/baite.db \
  ".backup '/var/lib/baite-rdos/backups/baite-$(date +%Y%m%d-%H%M%S).db'"
```

更新代码：

```bash
cd /opt/baite-rdos/app
sudo -u baite-rdos git fetch origin
sudo -u baite-rdos git checkout <已确认的提交 SHA>
sudo -u baite-rdos .venv/bin/pip install -r requirements.txt
sudo systemctl restart baite-rdos
curl -fsS http://127.0.0.1:8000/api/health
```

数据库迁移在启动时自动执行，并在 schema 变化前生成备份。仍建议每次更新前另做一次上述人工备份。

回滚时切回上一提交并重启；如果新版本已经改变数据库且无法向后兼容，再停止服务并恢复更新前的 SQLite 备份。

## 10. 部署验收清单

- [ ] 本机 `127.0.0.1:8000/api/health` 返回 200。
- [ ] 公网 HTTPS `/api/health` 返回 200，HTTP 自动跳转 HTTPS。
- [ ] 8000 端口未向公网开放。
- [ ] 未登录无法访问管理 API。
- [ ] 管理员可以登录、改密码和重新登录。
- [ ] 重启服务后管理员、Runner、项目和记录仍存在。
- [ ] 创建一个测试 Runner，公网心跳正常。
- [ ] 创建测试项目后，只有项目成员能获得项目资料。
- [ ] Gate 确认或退回不会阻断 Runner 后续上报。
- [ ] 停止 Runner 超过在线窗口后显示 Offline，恢复后自动补传。
- [ ] 如启用飞书：只读资料同步正常，日报只写入保留子目录。
- [ ] Nginx 日志与 `journalctl -u baite-rdos` 没有持续错误。
- [ ] 已完成一次备份和恢复演练。
