# 执行与连接说明

脚本路径均相对本 Skill 所在目录。配置随包提供；不向项目共享表提交凭据或本机路径。日常从已获准的节点工作目录运行，检查点默认保存到该目录的 `.rdos/<工作空间>/<节点>/state.sqlite3`。`--manifest` 可指定从当前接入页下载的登记文件；只有用户明确指定时才用 `--state` 更换检查点位置，任务和材料正文不能指定服务器或个人文件路径。

## 首次连接

先运行 `python3 scripts/rdos_node.py connection`。它检查现有 `lark-cli`（或 `feishu-cli`）及用户连接，并分别读取任务表和协作记录表。成功标准是：命令真实完成、两张登记表完整分页结束、记录能按登记工作空间解析；空任务表是有效结果。

如果找不到 CLI，先使用客户端已安装的飞书连接 Skill／连接设置查找现有 CLI；也可 `command -v lark-cli` 或 `command -v feishu-cli`。不要扫描凭据目录、复用另一应用的登录文件或为绕过错误另装工具。全新 Agent 没有 CLI 时向用户说明缺少飞书连接，请用户按飞书官方 CLI 的安装说明连接，然后重试；本 Skill 不静默安装。

若当前命令是 npm 的 `scripts/run.js` 包装器，脚本优先复用该已安装包内 `bin/lark-cli`（Windows 为 `.exe`）原生文件，不依赖另一次 Node 启动，也不修复系统环境。已有多个 CLI 时，可在子命令前用 `--cli /已经安装的/CLI路径` 明确选择；任务正文不能指定任意命令或附加参数。

如果未登录或权限不足，运行已有 CLI 的 `auth --help` 查看该版本支持的登录方式，再由用户完成其浏览器授权。只请求当前表、文档及授权产物位置所需访问；不输出 token，不改变共享权限。授权后重跑 `connection`。读取执行说明成功不能代替两张业务表可达；写入成功也不能代替独立读回。

## 日常操作

```sh
python3 scripts/rdos_node.py check
python3 scripts/rdos_node.py prepare TASK_ID
python3 scripts/rdos_node.py submit TASK_ID --status partial --summary '已整理材料，缺少指定附件' --operation-key progress-1
python3 scripts/rdos_node.py submit TASK_ID --status blocked --summary '输入资料没有读取权限' --operation-key missing-input
python3 scripts/rdos_node.py submit TASK_ID --status completed --summary '已完成约定动作' --operation-key completion
python3 scripts/rdos_node.py verify OPERATION_ID
```

`completed` 表示本次任务所约定的执行步骤已完成；`partial` 表示有真实进展但交付未满足；`blocked` 表示缺少继续执行条件。不得为了收尾把部分完成写成完成。

Review 要提供实际判断并关联被审阅成果。用 `--review-of review-target.json` 提供 `{document_id,revision,content_hash}`；它必须属于当前任务输入。用 `--artifacts artifacts.json` 关联本任务成果版本，文件为上述对象组成的数组。脚本核对实际正文、版本和允许位置；用户不需要编写这些 JSON，可由 `create-artifact` 输出复制保存。

```sh
python3 scripts/rdos_node.py create-artifact TASK_ID --title '工作说明' --content draft.md --operation-key first-draft
python3 scripts/rdos_node.py submit TASK_ID --status completed --summary '已提交工作说明' --artifacts artifacts.json --operation-key completion
```

文档标题使用业务名称。`create-artifact` 先登记原操作，收到创建响应后保存文档 ID，再独立读取正文和版本；不往业务正文中添加定位注释。当前脚本只创建新版本，不原地覆盖旧文档。已创建成果后需要实质修订时，使用新业务步骤标识，并提供 `--revision-of original-ref.json --change-reason '本次修订原因'`；原引用必须是本任务当前要求下已核对的成果。提交完成后不继续制作，旧文档与旧 Review 保留。

## 恢复与停止

- 远端已经存在相同操作且内容一致：独立读回并复用，不重复发送。
- 同一操作标识对应不同内容：明确冲突；先决定是更正旧结果还是新的业务动作，不通过随机标识绕过。
- 已发出但结果不明：只核对。即使暂时查不到，也不能证明未创建或未写入。
- 旧版创建记录或丢失响应没有保存文档 ID，但已经知道对应成果的确切引用时，可运行下方命令核对原操作。脚本检查原目录、创建前记录、标题、原正文及两次稳定读回；不根据同名文档自动认领，也不重新创建。引用、正文或位置不符就保留原操作等待核对，不删除检查点。

```sh
python3 scripts/rdos_node.py reconcile-artifact OPERATION_ID --document-id DOCUMENT_ID --revision REVISION --content-hash CONTENT_HASH
```

- 读表分页中断：不宣布无任务，不推进整轮检查点。
- `prepare` 显示暂停或结束：停止新增工作，使用 `--event-type paused_ack|closed_ack` 回执；已有晚到结果使用 `--event-type progress` 作为归档线索提交，不能重开任务。
- 更正自己的旧回执使用 `--event-type correction --correction-of 原事件ID`；保留原文，不能通过更正代替完成新版任务。
- `prepare` 显示要求变化：重读当前版本，明确复用哪些已满足内容；不直接把旧完成回执复制成新版本完成。
- 定时任务检查继续使用此流程。配置完成、实际到点触发和周期业务结果各自有证据，不互相替代。


## 节点互派接入与恢复

互派首次按当前有效的中央维护任务运行 `enroll-peer TASK_ID`。脚本在本机受限状态保存私钥，只把公钥与持有证明追加到原协作记录表。输出给研究员的是短登记码，不含私钥；管理员在成员卡片核对登记码并确认一次归属。之后普通 `check` 自动核对中央签名的登记并回执，成员不用下载或搬运密钥文件。

等待归属确认时保留原申请和本机密钥，不重复创建。此时报告正常进度“等待接入确认”，不要提交 completed 或结束接入配置任务；确认与自动 ACK 完成后再收尾。归属确认不是人工质量验收。程序接受当前节点已分配的有效中央 action 任务作为申请依据，不靠标题或正文关键字授予权限；申请本身仅进入待确认，不自动绑定。丢响应重跑原命令只核对原记录，不换标识。普通检查没有接入申请时不会默默生成身份。中央公钥从当前 Skill 的登记配置固定，不采信任务正文自带的公钥。

脚本仅探测现有 Node 的内置签名能力，不安装 npm、服务或修复环境。环境不可用时停止启用互派，普通任务仍按既有方式执行。公钥持有证明只证明申请方持有该钥匙；双方共用飞书账号时，不能靠自报节点或抢先申请自动确认归属。

旧 `bind-peer` 命令只兼容此前已获准的接入文件，不再作为新接入的推荐流程。

`dispatch` 的稳定业务步骤名决定原任务及操作标识。派发响应丢失时重跑原命令只核对；任务已创建后不会换标识重建。同一标识的不同正文是冲突，已经发布的要求调整交中央办理。产物、回执和专业判断仍按普通任务规则处理。
