# Baite AI R&D OS — 系统管理员工作台原型

这是一个支持单机云端测试部署的轻量协作系统。它验证系统管理员发布组织共享信息、任意数量的 Local Runner 安全同步、Local Agent 通过 outbox 回传结构化事件，以及项目资料与研发进度的双向协作。云端与 Mac 服务安装、HTTPS、备份恢复和逐项验收见 [部署手册](DEPLOYMENT.md)；代码就绪不代表云端已经验收。

当前不是 Workflow Engine，也不会远程操纵 Agent。

## 系统边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| Control Panel | Runner Registry、Global Rules 发布、Shared Skill Proposal 审核、Selected RAG 只读同步、项目汇总、Gate 记录核实、异常与升级协作 | 选择 Local Skill、安排本地 Workflow、收集 Prompt 或思考过程 |
| Local Runner | Token 认证、心跳、共享及项目快照原子同步、日报与文件元数据上报、outbox 上传与 ACK 归档 | 理解任务、选择 Skill、执行研发工作、持有飞书 OAuth |
| Local Agent | 自行选择工作方法、Skill、Workflow 和本地文件操作 | 发布组织正式版本、修改飞书 RAG 正文 |

管理员待办只有三类：

- 待发布：Shared Skill Proposal，以及管理员正在编辑的 Global Rules；
- 待判断：重大风险、对外发布、不可逆操作、被节点升级的协作，以及项目 Gate 通过记录的真实性核实；
- 异常需处理：连续同步失败、内容损坏、重名冲突、认证失效。

日常 Progress 和 Activity 只展示。普通节点间的 Request / Confirm / Return 自行闭环，不进入管理员待办。

Gate 的“确认记录 / 退回核实”只改变记录可信状态，不批准或阻止项目继续工作。

## 目录

```text
server/                 Control API、SQLite 数据层、飞书只读同步、单页工作台
runner/runner.py        通用 Local Runner
runner-configs/         本地 Runner 配置；JSON 被 Git 忽略
workspace/<user>/       各节点的独立 Workspace
data/baite.db           SQLite；被 Git 忽略
simulate_agent.py       生成结构化 outbox 事件
start_local.py          启动 Panel，并可选启动任意数量 Runner
tests/                  API、快照、Runner 和 E2E 自测
```

旧版固定 Agent 数据迁移为 Legacy Audit，不会自动变成新 Runner。旧 Workspace 不删除、不覆盖。首次迁移默认数据库前会在 `data/backups/` 自动留备份。

## 安装

本次云端测试统一使用 Python 3.12。Selected RAG 真实同步需要 Panel 所在主机已安装并完成个人 OAuth 登录的 `lark-cli`。

```bash
git clone https://github.com/yuhanliualex1149/baite_RDOS.git
cd baite_RDOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local
```

编辑 `.env.local`。首次启动至少需要：

```text
BAITE_DB_PATH=/absolute/path/to/baite-ai-rd-os/data/baite.db
BAITE_SESSION_SECRET=至少24位随机字符
BAITE_ADMIN_USERNAME=admin
BAITE_ADMIN_PASSWORD=首次创建管理员使用的长密码
BAITE_COOKIE_SECURE=false
BAITE_PUBLIC_URL=http://127.0.0.1:8000
BAITE_SELECTED_RAG_FOLDER_TOKEN=KQjyf6KrxlZ24MdkHirc9TP0n2d
BAITE_TIMEZONE=Asia/Shanghai
```

管理员创建完成后，数据库只保存带盐密码 Hash。之后可从 `.env.local` 移除 `BAITE_ADMIN_PASSWORD`，并在工作台修改密码。

## 启动 Control Panel

```bash
source .venv/bin/activate
python start_local.py
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，使用系统管理员账号登录。

## 创建并启动 3 个测试节点

在 Runners 页面依次创建 `user1`、`user2`、`user3`，每次填写独立 Workspace 绝对路径，例如：

```text
/Users/your-name/path/to/baite-ai-rd-os/workspace/user1
```

创建后 Token 和配置只显示一次。下载 JSON，放入 `runner-configs/` 并限制权限：

```bash
chmod 600 runner-configs/user1.json runner-configs/user2.json runner-configs/user3.json
```

可以在三个终端分别启动：

```bash
python runner/runner.py --config runner-configs/user1.json
python runner/runner.py --config runner-configs/user2.json
python runner/runner.py --config runner-configs/user3.json
```

也可以由一次启动命令托管任意数量的配置：

```bash
python start_local.py \
  --runner-config runner-configs/user1.json \
  --runner-config runner-configs/user2.json \
  --runner-config runner-configs/user3.json
```

`start_local.py` 不包含固定节点名单；增加第 4 个节点无需修改代码。

## Workspace 与原子同步

Runner 成功同步后生成：

```text
workspace/user1/
  control/
    collaboration.md
    skill_proposals.md
    sync.json
  shared -> .runner/current/shared
  control -> .runner/current/control
  outbox/
    sent/
    rejected/
  work/
  .runner/
    state.json
    releases/
```

Runner 先校验完整 `snapshot_hash` 及各资料 Hash/大小，把内容和 control 写入新 release，再原子切换 `.runner/current`。启动时复核本地文件清单，损坏则回到 last-known-good。下载失败或内容冲突不替换可用 Runtime。

共享内容校验连续失败 10 次后暂停，管理员点击“重新同步”才继续。网络断开不计入这 10 次，Runner 持续重连。只有 ACK 为 true 且 event_id 匹配才进入 `sent/`；来源或内容冲突返回 409，格式/业务错误进入 `rejected/`。自动日报也先持久写入 outbox，重试不重复创建业务记录。

## Projects：立项、资料与日报

管理员在 `Projects` 页面填写项目周期、参与 Runner、工作日上报截止时刻、飞书项目文件夹，以及研发模式、产品类型和交付规模。生命周期只使用“进行中 / 暂停 / 已完成”；今日待报、逾期、待 Gate 核实和周期结束均为系统计算标签。

每个参与 Runner 获得独立项目空间：

```text
workspace/user1/projects/<project_id>/
  control/
    project.md
    project.json
    todos.md
    gate_records.md
  shared -> .runner/current/shared
  control -> .runner/current/control
  work/
    project_status.json
    files/
```

Local Agent 维护 `work/project_status.json`，描述摘要、C0–C7 节点、证据、Shared TODO、文件开发状态、问题和可选 Gate 声明。Runner 不解释这些内容，只做格式校验、成员身份校验、同步和上报。它仅扫描 `work/files/`，忽略符号链接，只上传相对路径、大小、时间和 Hash，不上传研发文件正文。

项目资料只读取飞书文件夹第一层：Markdown 和飞书文档进入原子快照，其他格式保留文件名、类型、更新时间和链接但不进入 Runtime。项目创建时、工作日 09:00 和管理员手动触发时同步；失败继续使用 last-known-good。

日报由 Server 写回项目文件夹的保留子目录 `RDOS 项目进展/`，每个 Runner 只对应 `<immutable-runner-id>.md`。Runner 不持有飞书 OAuth，Panel 也不提供项目正文编辑能力。写回失败不会丢失数据库报告，会自动重试；连续失败 10 次后进入异常待办。

## 模拟 Local Agent

模拟脚本只生成结构化事件，不做 AI 推理，也不修改研发文件。

```bash
python simulate_agent.py --config runner-configs/user1.json progress
python simulate_agent.py --config runner-configs/user1.json activity --kind method
python simulate_agent.py --config runner-configs/user1.json skill-proposal
```

模拟项目进度（项目 ID 可在 Projects 页面查看）：

```bash
python simulate_agent.py --config runner-configs/user1.json project-status \
  --project-id <project_id>

# 同时报告一个已通过的 Gate；管理员只核实记录，不控制 Runner 是否继续工作
python simulate_agent.py --config runner-configs/user1.json project-status \
  --project-id <project_id> --gate-code G2
```

普通协作：

```bash
python simulate_agent.py --config runner-configs/user1.json collaboration-request \
  --target-runner-id <user2 的 runner_id>

python simulate_agent.py --config runner-configs/user2.json collaboration-response \
  --collaboration-id <协作编号> --action confirm
```

升级给管理员：

```bash
python simulate_agent.py --config runner-configs/user2.json collaboration-response \
  --collaboration-id <协作编号> --action escalate
```

风险、对外发布、不可逆操作可在请求时使用 `--category risk`、`external_release` 或 `irreversible`，会直接进入管理员“待判断”。

## Selected RAG

正式范围由 `BAITE_SELECTED_RAG_FOLDER_TOKEN` 配置，代码不维护固定文件清单。

- 只读第一层，不递归子文件夹；
- `.md` 通过 Drive 下载；飞书 `docx` 转成 Markdown Runtime Copy；
- 其他类型明确显示为 unsupported，不会进入 Runtime；
- 完整读取和校验后才生成不可变 Snapshot；
- 新文件自动进入，下次成功同步时自动停用已移出文件；历史 Snapshot 保留；
- 内容无变化不增加共享 revision；
- 每周一 09:00（`Asia/Shanghai`）自动同步，也可由管理员立即同步；
- 临时失败每分钟重试，连续 10 次后进入“异常需处理”；
- OAuth 失效会提示管理员重新登录。

工作台不能编辑、添加或删除飞书正文，也没有 RAG Release 审批。

## 自测

```bash
source .venv/bin/activate
python -m unittest tests.test_api tests.test_projects tests.test_runner tests.test_rag_sync -v
python tests/e2e_demo.py
```

真实飞书 Smoke Test 是只读操作：

```bash
BAITE_SELECTED_RAG_FOLDER_TOKEN=KQjyf6KrxlZ24MdkHirc9TP0n2d \
python tests/feishu_smoke.py
```

完整自测清单见 [Codex 自测计划](docs/codex_self_test_plan.md)，手工步骤见 [本地试跑计划](docs/local_trial_plan.md)。

## 云端发布边界

面向 Web Server 的完整部署步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)。

当前代码已经把数据库路径、Session Secret、Cookie Secure、公开地址、管理员初始化、飞书文件夹和时区放到环境配置中。正式上云前仍需单独完成：

- HTTPS 域名与反向代理，并设置 `BAITE_COOKIE_SECURE=true`；
- 云端持久磁盘、SQLite 备份与恢复演练；
- 服务器上的飞书 OAuth 或等价只读凭证治理；
- Session Secret 与 Runner Token 的安全保管；
- 进程守护、日志、监控和防火墙；
- 实际外网 Runner 的网络连通与重连验收。

本轮不实施正式云服务器部署。

## 下一轮候选（本轮未开发）

- 正式云服务器和域名发布；
- 多管理员或完整 RBAC；
- 正式 Human Gate / decision owner；
- Selected RAG 正文写入和 RAG 人工 Release；
- PDF、Word、表格解析与子文件夹递归；
- Workflow Engine、Agent 自动调度；
- Prompt 或完整工作过程采集。
