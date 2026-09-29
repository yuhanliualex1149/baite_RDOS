# RDOS 云端测试部署与恢复

目标：`https://yjmt.cn/baite-rdos/`（2026-09-29 用户选择改用官网子路径，暂不变更 DNS）。云端只运行 Panel/API/SQLite；Mac 运行 Runner、Workspace、Agent。是否上线以验收记录为准，模板存在不代表部署成功。

## 发布边界

- 云端全新数据库，不复制本地凭证、数据库、测试记录或 Workspace。
- Human 使用 `shared_admin`；本轮仅显示名称为 `yuhan` 的测试节点，Runner ID 动态生成，独立 Token。
- Python 3.12 独立环境，单 worker，监听 `127.0.0.1:8010`；独立 Nginx 子路径配置，沿用官网 HTTPS。
- 子路径与官网共用浏览器 Origin，Cookie Path 不是安全隔离边界。官网同源脚本也能发起 RDOS 请求；本轮仅测试，独立子域的隔离方案延后。
- 外部同步先关闭。日报只写用户指定测试文件夹的 `RDOS 项目进展/` 子目录。
- 本次 schema 4：旧回执没有内容 Hash 时，重试返回 409。Server 和 Runner 必须一起更新到新协议。
- 不改官网、YXXZ、福雀来的路由，不关已有维护入口；不包含 Windows、OSS 或正式人员接入。

## 1. 只读预检

运行 `bash deploy/preflight.sh` 并保存带时间的输出。核对系统、资源、8010、sudo、Nginx 全部 include 和现有证书。8010 占用则停止，不抢占端口。

现有 Alibaba Linux 3 / systemd 239 可用本模板；日志通过 shell append，未使用需要 systemd 240 的 `StandardOutput=append:`。不替换系统 Python，单独安装 3.12；依据实际官方软件仓库选择安装方式，不升级整机。

保存并在发布后重复检查状态码、跳转和页面关键内容：

- `https://yjmt.cn/`
- `https://yjmt.cn/yxxz/`
- `https://yjmt.cn/fuquelai/`
- `https://yjmt.cn/fuquelai/api/health`（按真实既有基线判断，不假定必须 200）

## 2. 发布准备与首次启动

在审核过的发布代码目录运行，使用指定 GitHub 仓库的完整提交 SHA：

```bash
sudo bash deploy/prepare_release.sh <40位提交SHA>
```

脚本创建系统用户、固定 SHA 的 release 和虚拟环境，运行全部单元测试及 E2E。成功才写 `.verified`；只准备代码，不切换服务或修改 Nginx。

若服务器无法稳定连接 GitHub，可在已核实远端 SHA 的本地 checkout 使用 `git archive <SHA>`，通过 SSH 传输并比对两端 SHA-256，再在目标 release 创建独立环境、运行相同测试；不能上传工作目录或凭证代替发布归档。

```text
/opt/baite-rdos/releases/<SHA>/   代码、.venv、release.env，root 所有
/opt/baite-rdos/current          当前 release 的符号链接
/opt/baite-rdos/previous         上次 release 的符号链接
/opt/baite-rdos/tools/bin/       已验证并固定版本的 lark-cli
/etc/baite-rdos/rdos.env         root:baite-rdos 0640
/var/lib/baite-rdos/baite.db     持久数据库
/var/lib/baite-rdos/backups/     0700 目录，0600 备份
/var/log/baite-rdos/             0700 日志目录
```

把 `deploy/server.env.example` 复制到 `/etc/baite-rdos/rdos.env`。使用安全终端录入独立随机 Session Secret（至少 32 字节）和初始管理员密码，不能进入 Git、聊天记录或命令历史。保持外部同步关闭、Secure Cookie 开启、Offline 窗口 60 秒。

```bash
sudo chown root:baite-rdos /etc/baite-rdos/rdos.env
sudo chmod 0640 /etc/baite-rdos/rdos.env
sudo install -m 0644 deploy/baite-rdos.service /etc/systemd/system/
sudo install -m 0644 deploy/baite-rdos-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/baite-rdos-backup.timer /etc/systemd/system/
sudo install -m 0644 deploy/rdos.logrotate /etc/logrotate.d/baite-rdos
sudo systemctl daemon-reload
sudo bash deploy/activate_release.sh <40位提交SHA>
```

激活检查 `/api/health` 的 `app_release`。更新前脚本先停服，使用旧 release 的配置额外备份，再原子切换 `current`。健康检查失败会停服并保留旧版本/备份，避免直接用旧代码打开不兼容的新库。

首次登录后修改密码，确认旧会话失效，移除环境文件中的 `BAITE_ADMIN_PASSWORD` 并重启。Session Secret 必须保留，改变它也会使 Runner Token 的校验失效。

## 3. 官网子路径与 HTTPS

本轮不添加 DNS、不申请新证书，沿用 `yjmt.cn` 的有效证书及其既有续期方式。先核实证书有效期与续期任务，不改其他站点的 TLS 配置。

两个配置文件分开安装，安装前确认目标尚不存在并保存 Nginx 基线：

- `deploy/rdos-rate-limit.nginx.conf` → `/www/server/panel/vhost/nginx/00-baite-rdos-rate-limit.conf`，位于 http 级，只声明限速区。
- `deploy/rdos-subpath.nginx.conf` → `/www/server/panel/vhost/nginx/proxy/www.yjmt.cn/baite-rdos.conf`，位于已有 server 内，只添加 `/baite-rdos` 相关 location。不覆盖主站配置或 `location /`。
- Uvicorn `--root-path /baite-rdos`，Nginx 去掉前缀后转发；环境 `BAITE_PUBLIC_URL=https://yjmt.cn/baite-rdos`。这三处必须一致。
- 无尾斜杠入口、HTTP、www 主机统一跳到规范 HTTPS 地址。现有其他路径保持原样。

```bash
sudo /www/server/nginx/sbin/nginx -t
sudo /www/server/nginx/sbin/nginx -s reload
curl -fsS https://yjmt.cn/baite-rdos/api/health
```

不新增监听端口；已有网站端口保持原样。8010 不加入安全组，必须公网实测不可达。代理头仅信任本机 Nginx；Nginx 覆写客户端转发头。

Nginx 登录限速为每 IP 每分钟 5 次、额外突发 5 次、超限 429。所有浏览器管理/认证写操作要求 Origin 精确匹配公开 URL 的 scheme/host/port（本轮 `https://yjmt.cn`，不含路径），不同子域也拒绝；维护脚本调用也要带正确 Origin 和 Human Cookie。Cookie Path 为 `/baite-rdos/`，退出及改密使用相同路径清除。

`deploy/rdos.nginx.conf` 仅保留为未来独立子域的备选模板，不与本轮限速配置重复安装。迁移子域需单独确认 DNS、证书、PUBLIC_URL、root-path 和 Mac 配置。

## 4. Mac Runner

新建 `/Users/alexliu/Documents/baite-rdos-cloud-test`：

```text
releases/<SHA>/   与云端一致的代码和 .venv
current          当前发布符号链接
config/yuhan.json 一次性节点配置，0600
workspace/       全新 Workspace
logs/            按日轮转，最多 30 份历史日志
```

云端注册显示名称 `yuhan`，Workspace 填上述绝对路径；下载一次性 JSON，核实 `control_url=https://yjmt.cn/baite-rdos`，不用旧 Token。安装代码固定 SHA，Python 3.12 独立环境。

```bash
python3 deploy/install_macos_runner.py \
  --python /Users/alexliu/Documents/baite-rdos-cloud-test/current/.venv/bin/python \
  --code /Users/alexliu/Documents/baite-rdos-cloud-test/current \
  --config /Users/alexliu/Documents/baite-rdos-cloud-test/config/yuhan.json \
  --logs /Users/alexliu/Documents/baite-rdos-cloud-test/logs
```

安装器生成 `~/Library/LaunchAgents/cn.yjmt.rdos.runner.yuhan.plist`，仅引用配置路径，不嵌入 Token。用户登录启动、异常退出重启、每 5 秒轮询。`--render-only` 只生成和校验，不启动。

```bash
launchctl print gui/$(id -u)/cn.yjmt.rdos.runner.yuhan
launchctl kickstart -k gui/$(id -u)/cn.yjmt.rdos.runner.yuhan
launchctl bootout gui/$(id -u)/cn.yjmt.rdos.runner.yuhan
```

分别检查、重启、停止。再次启动用 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/cn.yjmt.rdos.runner.yuhan.plist`。若 macOS 要求 Documents 访问授权，由用户允许该 Python，不能关闭系统隐私保护。

合盖/休眠超过 60 秒 Offline 是预期，唤醒自动重连。轮换 Token 后旧凭证应 401，替换 JSON 并重启。不要托管与手动运行同一 Workspace，Runner 使用排他锁。

共享和项目的 `shared`、`control` 通过单一 `.runner/current` 指针切换。完整快照和本地文件清单在启动时复核，损坏回到可验证的上一版。自动日报先 fsync 到 outbox 再联网；ACK 必须为 true 且 event_id 匹配才归档。网络失败保留待传，400/409/422 进入 rejected。

## 5. 飞书闭环

1. 云端服务用户单独安装已验证的固定版本 `lark-cli`，root 管理二进制；不复制 Mac OAuth 缓存。
2. 以 `baite-rdos` 身份及独立 HOME，由雨寒完成 OAuth。核实 headless 凭证存储路径和权限 0700/0600。
3. 用与 systemd 一致的身份/HOME/PATH 只读列目录，确认重启后授权可用，再设置 `BAITE_DISABLE_EXTERNAL_SYNC=false` 并重启。禁用期间后台和人工触发都不访问飞书。
4. Selected RAG 只读已确认文件夹第一层 Markdown/docx，依据实际清单与内容 Hash 验收，不假设固定 6 个文件。
5. 测试项目路径解析为唯一 folder token 后才创建项目、分配 yuhan；日报仅其 `RDOS 项目进展/`，不改源资料。
6. 用明确标注“测试”的 Proposal 验证 Shared Skill 发布；正式共享内容不预置。
7. 受控模拟授权/写回失败，保留旧快照和 DB 报告，恢复后继续同步。不要为测试删除真实 OAuth 或破坏正式目录权限。
8. Gate 确认和退回后都可继续上报。

## 6. 备份、恢复与回滚

每天北京时间 03:00 一致性备份，错过后启动补跑，保留 14 天；更新前额外备份也按 14 天保留，因此回滚窗口最长 14 天。使用 [SQLite Online Backup API](https://www.sqlite.org/backup.html)，包含已提交 WAL，不复制正在使用的单个 DB 文件。

```bash
sudo systemctl start baite-rdos-backup.service
sudo systemctl list-timers baite-rdos-backup.timer
sudo journalctl -u baite-rdos-backup.service --since today
```

每份 DB 旁 JSON 记录 UTC 时间、schema_version、app_release。同机备份不能抵御整机/磁盘丢失，本测试阶段接受重建。logrotate 每日轮转保留 30 天，copytruncate 的小窗口日志丢失不能作为事务凭据。

### 隔离恢复演练

1. 选择具体 DB 备份及匹配 JSON，核实 SHA/版本，复制到新的私有临时演练目录，绝不指向 live DB。
2. 用对应 release 和原 Session Secret；覆盖 `BAITE_DB_PATH` 为临时 DB、外部同步为 true 禁用、`BAITE_PUBLIC_URL=http://127.0.0.1:18010`、`BAITE_COOKIE_SECURE=false`。
3. 只监听本机 18010，不加公网代理。验收 integrity_check、管理员登录、已有测试 Token 认证、共享/项目 snapshot_hash。
4. 用临时 Workspace 拉取快照并比较备份前 Hash。停止演练进程，保留结果，不把演练 DB 切回在线环境。

自动测试覆盖 WAL 恢复及保留 Session Secret 后的身份/快照；不能替代云主机真实演练。

### 回滚操作

1. 明确旧 SHA、匹配备份、时间窗口；告知恢复点之后的新记录可能需要补传/补录，暂停新提交。
2. 停服务与备份 timer；再备份当前库留作恢复窗口内数据的依据。
3. schema 向后兼容时可只切代码；不兼容则恢复备份到 **新文件名**（如 `/var/lib/baite-rdos/restore-<时间>.db`），不覆盖原 DB/WAL，检查 integrity、所有者和 0600。
4. 安全编辑环境中 DB 路径，保留原 Session Secret，先禁用外部同步防止旧状态写回飞书。
5. 临时符号链接加 `mv -Tf` 原子切回旧 release。启动并核实 app_release、认证、快照后恢复 timer；核对远端日报及 outbox 重试影响后再启用外部同步。
6. 不删除原库和旧 release；记录数据影响与补传结果。每次恢复演练用新的临时 DB。

## 7. 必须逐项记录的验收

每项保存时间、发布 SHA、动作、预期/实际结果；未执行写“未验收”。

- [ ] HTTPS、既有证书续期、子路径资源/API、Cookie Path、公网 8010 不通。
- [ ] 未登录拒绝、Cookie 属性、改密使旧会话失效、异源拒绝、限速 429。
- [ ] Runner 不能管理、停用/轮换失效、跨节点事件冲突 409。
- [ ] Cold Start、Panel 重启、Runner 重启、断网恢复、重复 Event。
- [ ] 提交后 ACK 丢失不重复写、错误 ACK 不归档、日报离线持久化。
- [ ] 共享/项目快照被改或缺文件不替换好版本，启动重新校验。
- [ ] Mac 不依赖终端、登录启动、崩溃重启、休眠唤醒。
- [ ] 飞书清单/Hash/快照/Mac 一致，日报仅指定目录，失败可恢复。
- [ ] Gate 退回不阻断继续上报。
- [ ] 定时备份、真实恢复、上一版本回滚；首次无旧云版本须受控演练，不能当作通过。
- [ ] 官网、YXXZ、福雀来页面和既有 API 回归。

交付地址、已部署 SHA、CLI 版本、Mac 维护说明、逐项证据和未完成项。全部通过后才称“云端＋Mac 测试闭环已验收”。

参考：[FastAPI 反向代理和路径前缀](https://fastapi.tiangolo.com/advanced/behind-a-proxy/)。
