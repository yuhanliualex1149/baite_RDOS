# 百特 RDOS — 中央 Agent 试用版

本轮供百特三个人试用研发协作与方法改进：管理员与一个中央 Agent 讨论真实工作，独立 Local Agent 直接从飞书获取任务、写入反馈。先跑通“任务—实际反馈—有依据的判断—补充要求”，再接“方法草稿—人工确认—发布—后续采用反馈”。首个项目为 **RDOS 自身改造**。线上页面、账号、飞书及模型须分别核验；代码上线不自动证明后面三项可用。

2026-10-13 的 A → B → C 工作台修订仍在本地分支，未推送或部署；本次实现、测试、两轮页面审核及待发布项集中记录在 [工作台改进验收记录](docs/workbench_improvements_20261012_review.md)。下方较早日期的验收数字和远端操作仅描述当时版本。

## 本地启动与角色测试

复用当前虚拟环境和 `.env.local`，不要覆盖既有配置。中央 Agent 使用已确认的 Agent Plan 接入；代码默认模型为 `deepseek-v4-pro`，`thinking=enabled`、`reasoning_effort=high`。对方舟 Agent Plan 的该模型，请求显式发送 `max_completion_tokens=384000`，推理与回答共用这份额度；这是模型表的最大回答额，并非承诺每次请求都能用满。已有配置以本机实际值为准，不能仅凭代码默认值宣称运行模型已经切换。从项目目录启动：

```bash
.venv/bin/python start_local.py
```

首次配置或更换专属 API Key 才运行 `.venv/bin/python configure_agent_plan.py`；输入不回显，配置文件权限为 `600`，现有其他配置保留。已有凭据可用时不重复填写。历史 Flash 超时与后续低推理验收属于下方历史证据，不代表新版 DeepSeek 已验收，也不触发自动降档。

打开 [本地 Panel](http://127.0.0.1:8000)，使用已有账号登录。新版分为 **Agent 接入、项目、中央 Agent、任务与反馈、全局设置** 五个入口；研究员使用接入与项目，后三个管理入口由管理员操作。旧管理台保留在 [旧版管理界面](http://127.0.0.1:8000/legacy)。默认启动不依赖 Runner 或 outbox；历史后台同步仅显式启用 `BAITE_ENABLE_LEGACY_SYNC=true` 时运行。

本轮本地角色账号与初始密码在 **`data/local-trial-access.json`**，该文件被 Git 忽略，仅在本机查看，勿复制到聊天或共享资料。按文件中的角色分别登录；切换角色时先退出，再用另一个账号进入。管理员可讨论和管理任务，研究员可登记自己的接入范围、查看相关工作，双角色账号具备两者操作。使用测试身份产生的记录会标记为角色测试，不能当成三位真实成员已经试用。

线上从 Git `main` 的自动部署见 [部署说明](deploy/PRODUCTION_AUTODEPLOY.md)。代码默认沿用本地正式飞书任务表、机构反馈和三份接入材料的对象标识；`BAITE_CENTRAL_*`、`BAITE_LLM_*` 等覆盖值以服务器 `/etc/baite-rdos/rdos.env` 为准。本机 `.env.local`、飞书 CLI 的用户授权、本地 SQLite 与测试账号密码不会随 Git 推送。线上模型调用须在服务器安全配置 Agent Plan 密钥；飞书读写须由 Linux 服务账号使用已授权的 CLI 实测。首版数据库为空时先由服务器现有引导管理员启动，再保留雨寒、潇斐、多多各自成员标识与角色；已建项目应关联原反馈文档，不能重新创建同名文档。每项分别验证，不把健康检查通过写成真实联调通过。

2026-10-11 发布候选做了两轮页面体验审核，按实际浏览器观察修复了对话定位与阅读、项目指令和过期方法草稿等问题。干净的 Python 3.12 打包环境中，294 项程序测试通过（含 6 项跳过），原有 E2E 演示通过；新版前端 26 项、旧版前端 10 项隔离检查通过。这些验证不代替线上账号、飞书读写和模型调用的实测。

飞书沿用已有 `lark-cli` 授权与组织共享范围。原接入材料及历史记录保留；当前机构接入与项目分配独立。项目关联既有飞书文件夹，准备独立项目反馈入口；接入反馈写机构入口，实际项目进展写对应项目入口。没有项目或任务时不伪造归属，也不写没有变化的空日报。正式方法资料仍须逐份草稿人工确认发布。密钥、密码和授权令牌不得进入共享文档或日志。

每位成员在 Agent 接入页自选“首次接入”或“更新已有接入”，不按账号硬编码。当前流程为 **连接飞书 → 确认工作范围 → 设置定时同步**；旧内部阶段编号 0、1、3 和旧工作阶段历史保留，完成一次项目工作不再是接入门槛。更新接入在飞书旧反馈可读、身份与范围仍适用时复用已通过的连接和范围，缺什么只补什么。“重新接入”显式开启新轮，旧轮指引随即失效。已有定时任务按成员和实际分配项目更新，不固定接入测试项目；计划配置与普通 Agent 对话中的一次手动读写回收通过即可进入项目，首次真正按时触发另记且不阻挡。进入新项目只补项目范围、资料和反馈入口，不重复机构接入。

Panel 生成可复制的短指令和 **24 小时受限只读说明链接**。链接只提供当前本人、当前步骤或项目说明，不是 Panel 登录或管理凭据；链接本身需要妥善保管。当前以同一台电脑的 Local Agent 读取 localhost 为验收范围，尚未验证跨电脑访问；其他电脑无法访问 localhost 时不能宣称接入成功。过期后从 Panel 重新生成，不把受限链接贴入共享文档。

接入后续步骤只给当前正式材料、反馈位置和写回字段。飞书安装与排障独立放在“飞书连接帮助”；已有连接直接复用，实际失效时再排查。项目指令另行给出当前项目文件夹、任务表和项目反馈入口。多多的角色测试曾使用已有个人飞书授权完成读写，这不证明多多拥有独立飞书账号授权。

2026-10-11 已按本轮确认将组织通用旧任务 `ONB-FRAME-01` 和 `ONB-REVIEW-01` 从已发布 v1 撤回为停用 v2；飞书远端逐条读回，原要求与反馈、SQLite v1 历史保留。[查看任务表](https://scn33386s7ui.feishu.cn/base/E34dbYDEXaCRnDsMQDBcMvyMnjb?table=tblvJU3CwPt6eAaw)。本轮接入逻辑改变不否定历史任务当时的工作反馈。

2026-10-11 多多的独立 DeepSeek Harness 读取新版限时指引后，在原有两条本地定时任务 ID 上修正每日读取与每周反馈规则，并在普通对话中手动完成读取、写入、读回。本机再次读取定时配置，确认不再只筛“第三阶段”，普通任务按适用成员及实际分配项目判断；[飞书反馈原文](https://scn33386s7ui.feishu.cn/docx/RoxidRoZXocXSnxR0tlc4aCMnof) revision 28 中更正标识 `onb_D1fcflie2FB56jfa-stage3-20261011-1817` 仅一处。Panel 以 `deepseek-v4-pro-ga-260813` 真模型重新核对该反馈后显示接入完成，旧连接与范围记录按远端证据复用。此为本机**角色测试**：所用飞书授权来自已有账号，不能证明多多已取得独立授权；实际业务项目尚未分配，首次真实定时触发尚未发生，均不得写成已验证。

账号不绑定固定姓名；`role_test` 与 `actual` 明确区分。成员标记确认归属，**不表示研究员之间严格隔离**。开发者内部能力、同一人模拟多个角色不能作为真实团队试用证据；本轮工具与成员对应关系只记在测试操作单，不进入通用 onboarding。

## 本轮已确定的边界

- 复用 FastAPI、SQLite 和静态页面，保留当前分支、原有工作和历史功能；新主流程取消 Runner、固定 outbox、强制文件迁移。研究员保留自己的文件夹习惯，具体研发工作可以在会议、线下或其他工具中进行。
- 飞书是共享任务版本、状态、资料及反馈正文的来源；SQLite 保存关联、已处理内容版本、运行、草稿和发布依据。中央 Agent 根据材料自主选择工具分析、追问和写回；不以关键词分支或固定答案代替模型。
- 管理员管理项目、任务、成员、运行和方法发布。新工作由 Agent 形成任务草稿，再由管理员确认；对原项目、原成员、原任务的必要补充按工具边界直接写回。日常工作任务没有接入阶段。角色由后端校验，外部材料不能授予权限或绕过方法发布确认；改稿或来源变化后须重新审阅。
- 普通操作使用稳定标识去重，远端结果不确定时先核对再重试；项目反馈创建也先查目录与正文标识。中央周期检查每五分钟运行，不启动外部 Agent，也不替代其本地同步计划。服务停止或电脑关闭时不会执行，错过检查如实显示。
- 判断区分自述、可核验结果、尚未验证；没有反馈不等于没有工作，任务发布不表示 Local Agent 已读取或执行。重要资料变化需读取正文判断，不能只依据修改时间。

## 运行预算与续跑

首轮默认总预算 **180 秒、6 次模型调用、12 次工具调用**，包含前置资料读取；单个模型请求默认最多 90 秒，实际请求与重试按剩余总时间收缩。高推理保持启用，不会因超时偷偷切模型或降低推理。运行记录展示总耗时、阶段耗时、实际模型、调用量及失败；达到限制保留已完成步骤，可核对远端结果后重试原运行，继续未完成工作，不能承诺自动完成全部后续事项。

本机配置可显式调整：`BAITE_AGENT_MAX_SECONDS`（1–600）、`BAITE_AGENT_MAX_MODEL_CALLS`（1–12）、`BAITE_AGENT_MAX_TOOL_CALLS`（1–24）、`BAITE_LLM_REQUEST_TIMEOUT`（1–180）、`BAITE_LLM_MAX_COMPLETION_TOKENS`（方舟 Agent Plan `deepseek-v4-pro` 为 1024–384000，其他模型沿用 1024–32768）。未配置时，前者使用 384000，其他模型沿用 8192；已有环境变量仍优先于代码默认值，配置工具不再为新配置写入 4096。`BAITE_LLM_MODEL`、`BAITE_LLM_THINKING`、`BAITE_LLM_REASONING_EFFORT` 决定实际模型参数。越界会报错，不静默截断配置；增加预算意味着更长等待及更多模型使用，应根据实际失败记录调整。正式服务若已显式配置 8192，本次代码修改不会自动更改该配置，需在之后受控上线时单独核对。[方舟模型表](https://docs.volcengine.com/docs/ark/model-list?lang=en)给出的模型回答上限与[通用 Chat API 参数说明](https://docs.volcengine.com/docs/ark/chat-api?lang=en)范围尚不完全一致，正式接口接受 384000 仍需验证；[上下文管理说明](https://docs.volcengine.com/docs/ark/context-management?lang=zh)明确 `max_completion_tokens` 包含思维与回答。

## 验收记录怎么读

Panel 的“最近运行”保留输入、工具结果、来源、失败原因；飞书正文保留实际任务及反馈。自动测试只验证程序风险，不能替代真实飞书、真实模型、独立 Local Agent 的端到端验收。真实模型未配置时显示阻塞，不启用默认模拟。

首次手动执行、真实定时执行、独立 Agent 读取与反馈、方法确认发布及后续采用必须分别核对；“已配置”不代表已验收，手动成功不代表定时成功，角色模拟不代表真实团队试用。未运行或失败的环节保留为未验证，不填入模拟成功结果。

## 前端体验修订：2026-10-11

本轮修订新版、旧版及人类界面错误，保留业务能力与角色边界。方法建议移入“任务与反馈”；指令全文、核验报告和运行过程按需展开。项目、账号和本次操作内容绑定；过期会话、乱序返回、未保存表单和不确定发布保留可恢复状态。刷新失败保留上次内容，不显示成空数据或成功；旧版规则的整份未保存表单受到保护。

后续文案精简：删除重复页头说明、常态徽标、空记录占位和技术标识，复制指令仅保留用途、按钮与说明入口；身份、异常、未验证项与发布影响仍保留，Agent 指令全文不作删减。新版 13 项和旧版 10 项前端回归检查再次通过。

本地访问仍为 `http://127.0.0.1:8000`。同数据的改前/改后截图和验证清单见本机 [页面对照与验收记录](data/ux-review-20261011/index.html)，截图与冻结数据只在 Git 忽略的本机目录。桌面 1280×800、手机 390×844；覆盖研究员两页、管理员三页及关键弹窗。真实本地三个账号的页面与权限已核对；异常、编辑、发布及恢复在显式隔离的浏览器 fixture 验证，不写正式飞书。Python 回归 254 项（248 通过、6 跳过），新版 13 项、旧版 10 项前端检查通过。

本轮前端修订时未重新验收真实飞书发布、模型任务闭环、外部 Agent 定时和三位成员真实采用；历史真实结果仍以下方记录为准。是否直观由用户打开实物判断。`tests/ux_fixture_server.py` 仅供本机故障验收，需要私有冻结数据，不是默认启动模式。

## 新版实测：2026-10-10 晚间

这是同一人操作既有三个账号的本地角色测试，不是三位真实成员试用结论。真实远端位置：[任务表](https://scn33386s7ui.feishu.cn/base/E34dbYDEXaCRnDsMQDBcMvyMnjb?table=tblvJU3CwPt6eAaw)、[本轮反馈](https://scn33386s7ui.feishu.cn/docx/RoxidRoZXocXSnxR0tlc4aCMnof)。保留历史正文，不把旧错误覆盖成成功。

| 项目 | 实际结果与位置 |
| --- | --- |
| 五入口及角色 | 浏览器分别登录雨寒、潇斐、多多；前两者可见五入口，多多仅接入和项目。正常认证 HTTP 核对多多仅收到本人／共享任务和本人反馈，管理来源及运行详情返回 403，潇斐双角色返回 200。后端检查不充当员工工作证据。 |
| 复制短指令 → 独立 Agent 第 0 步 → 下一步 | 真正的 DeepSeek Harness 直接读取受限 localhost 说明，复用已有 CLI 授权，读取阶段一、最小追加并读回。反馈标识 `onb_Bjiznzpx43zCJKRI-stage0-20261010-2220`，revision 14→15；Panel 22:21 自动核对并显示第一阶段指令。未传递 Panel 密码或模型 Key。 |
| 正式第一阶段 | DeepSeek 实际重读任务 v2、材料，报告持久 goal 保存／读回，追加 `onb_Bjiznzpx43zCJKRI-stage1-20261010-2246`（revision 16→17）。本地动作与远端正文分别记录；最新核对仍在第一阶段，只缺本人范围确认或具体早前确认来源。DeepSeek 23:09 已向本人提出这一项，等待确认或更正，不能据 Agent 自述宣布三阶段完成。 |
| 真实模型准备任务 → Panel 确认发布 | `run_Z8VKsfJ-8D27MIMm` 用 `deepseek-v4-pro-ga-260813` enabled/high，27.6 秒、3 次模型、2 次工具。管理员在 Panel 确认后把 `LOCAL-DEEPSEEK-ONB-01` 原 record `reczz28LfXA6XFbL` 修正为第一阶段 v2，保留其余要求。DeepSeek 随后实际读到 v2。 |
| 普通项目任务及实际开发反馈 | Panel 发布 `RDOS-UX-REVIEW-20261010 v1`，record `reczz28Lk7zXiv0o`，仅适用雨寒，类别 work、阶段空。Codex 用 CLI 真实读取后写开发工作反馈 `codex-ux-review-20261010-actual`，revision 15→16，读回标识仅一处且多多原文保留。它是开发工作记录，不是额外员工试用。 |
| 真实反馈读取与分析 | `run_-eFajrW7uHnLcAkb` 实际读取多多新反馈和当前任务，36.3 秒、2 次模型、2 次工具；识别旧权限报错已不代表当前状态。阶段核对曾误要求新会话、重复读回凭证；已按当前阶段规则对原反馈重评，目前只需本人范围确认或具体早前确认来源，新会话恢复仍属第二阶段。中央后续写回仍受旧对话影响，误发 `LOCAL-DEEPSEEK-ONB-01-S1` 新会话要求（`run_4CycxEWY3MG6DWoM`，61.5 秒）；该错误补充已停用为 v2，未让独立 Agent 执行，保留历史。修正后 `run_d7Z00hl_5fU2kp_K` 88.1 秒、4 次模型／4 次工具，真实发布 `LOCAL-DEEPSEEK-ONB-01-S2 v1`（record `reczz28LkdiPvw6E`），仅补本人范围确认或具体早前确认来源；任务发布尚不代表缺项已补齐。独立 DeepSeek 23:09 自行读取当前说明和 S2 v1，确认没有重做或跳阶段；目前向本人询问身份、项目、读取及共享范围，尚未取得回答，未伪造确认或补充反馈。 |
| 重复、不确定写入及重启 | 任务首次远端核对未立即读到新值，保留失败提示；使用原操作标识 `07daea1a-8a98-4a02-9490-a976253e27db` 先核对，确认已有同 record/v2 后成功，没有第二次写入。新增有限只读回查，不重发写请求。重启后接入进度、任务版本及草稿确认记录保留。 |
| 真实中央周期 | `run_cPTpj3ZsBuh1-o1b` 22:25:54–22:25:58 为实际五分钟触发读取；`run_QuesmjJtZOO6VGc3` 22:45:56 实际周期读取并用 DeepSeek 分析，48.1 秒。中央周期不能证明外部 Agent 已执行本地计划。 |
| 模型失败可见与预算 | `run_Ro-yVax9oyBn-ssO` 实际周期在 64.6 秒收到不完整模型回复，明确记失败，保留工具结果；未降档或假报成功。已有成功样本在三分钟内，不代表所有长任务稳定。 |
| 项目创建／独立反馈 | UI、后端及程序验证已具备；尚缺用户指定的可写项目文件夹，未向 RAG/Skill 目录擅建测试项目反馈。真实创建待验证。 |
| 完整接入与方法采用 | Codex/WorkBuddy 增量更新与本地同步计划，DeepSeek 正式第二、三阶段、新会话恢复及 Local Agent 首次真实定时仍待实际使用验证。方法草稿未获负责人发布确认，正式资料未改，后续采用未验证。 |

程序回归覆盖真实风险，均以测试桩隔离外部服务：本轮完整组合 160 项通过；收尾阶段规则、角色及当前上下文修改另做 68 项组合回归通过；当前步骤补缺说明随后通过 41 项接入定向检查；上下文修正通过 35 项项目／运行检查和 23 项模型检查（均有重叠，不相加）。页面独立交互检查通过，但不代替上述真实链路或用户对体验的判断。受限链接的过期／错误标识拒绝、空资料／接口失败等由程序测试覆盖；尚未逐项用真实失效账号破坏性复现。

## 历史验收：2026-10-10 早前 Flash 版本

以下保留当时实际结果，不能作为当前新 UI、DeepSeek 或完整三阶段接入的成功证据。

- **真实主链已完成（角色模拟）**：管理员从 Panel 发布 `RDOS-TRIAL-01 v1`（飞书 record `reczz28LXrynEGIL`）；独立开发子 Agent 用普通研究员身份直接读取飞书、保存自身接入范围并写反馈；中央真实模型读回、核对归属及证据，发布关联补充 `RDOS-TRIAL-01-S1 v1`（record `reczz28LYUFDuOsq`）。协作者又写回补充反馈，原任务和原文保留。
- **结果位置**：[任务表](https://scn33386s7ui.feishu.cn/base/E34dbYDEXaCRnDsMQDBcMvyMnjb?table=tblvJU3CwPt6eAaw)、[反馈文档](https://scn33386s7ui.feishu.cn/docx/RoxidRoZXocXSnxR0tlc4aCMnof)。反馈运行标识 `external-local-trial-20261010`、`external-local-supplement-20261010`；当时正文 revision 5→6→7。原两项 ONB 保留；当时新增四个归属字段，新版另补任务类别，不据此推断当前远端字段或正文版本。
- **手动与周期分别验证**：Panel 手动运行 `run_AU3OhTjMlfZhI1rD` 真实模型 `glm-5-3-flash-260828`，约一分钟完成，4 次模型调用、3 次工具调用；首次任务回读未通过后，同标识核对成功。自动周期运行 `run_PLnMsvSoFXu5om2l` 于北京时间 11:18:03–11:18:33 真实读取并分析补充反馈（2 次模型、2 次工具），没有再发无必要补充。电脑或服务停止期间不会运行；后续读取失败也保留在历史记录。
- **角色与归属**：三个本地账号均标记 `role_test`；研究员的管理动作返回 403，管理员单角色不能登记研究员接入。将双角色临时改为研究员进行真实路由检查时，它只看到通用 ONB 要求，未收到多多的专属任务或反馈；双角色已恢复。管理员的监督视图可以查看其他成员记录，这是原有共享范围，不是安全隔离。
- **去重与恢复**：重复首任务发布返回同 record 和 v1；两次反馈使用相同正文与写回标识重读均返回 duplicate，revision 不增加。最终重启前后四条任务、两条反馈、一份草稿、角色及周期验证记录逐项一致，失败历史保留。105 项程序测试通过，覆盖发布边界、模型与读取失败、旧版本反馈、权限、来源变化及不确定写入；模拟测试不替代真实验收。
- **凭据到期处理**：曾在后续周期读取中返回授权失败；核验原授权并刷新凭据后真实读取恢复。工具现在仅在只读请求遇到授权错误时验证现有授权，验证成功才重读一次，写入不盲重试。无新增授权范围；无法恢复时仍显示失败。最终服务已恢复每五分钟检查。
- **方法草稿已就绪，尚未发布**：`prop_68a10b0365a604a68a0d` 基于实际反馈和阶段二 revision 5 生成，已在 Panel 审阅时收窄为保留原段落，仅补当前任务适用范围核对说明。正式方法发布与发布后的采用反馈，须负责人确认后再验证。
- **仍待实际使用验证**：用户自行运行的外部 Local Agent、三位真实成员试用、平台原生持久记忆/新会话恢复、研究员每周真实调度及方法采用效果。开发子 Agent 共享一个人类飞书授权完成的角色模拟、尚未执行的验证计划，均不作为这些事项的成功证据。

## 三个本地 Agent 的用户测试流程

完整操作单及当前界面功能说明见 [本地三 Agent 测试流程](docs/本地三Agent测试流程.md)。每一步注明账号、界面、可复制指令与期待结果，并附结果记录表。

沿用已有账号及本机安全文件中的凭据，不新增员工身份、不迁移原工作。工具映射、短提示词、正式第二阶段成果检查、新会话恢复、实际周期记录与旧任务阶段误标风险集中保留在测试操作单；不把测试安排写入通用 onboarding。

## 历史原型说明（保留用于回看）

下方为此前 Runner 管理台与部署说明。本轮中央 Agent 的要求与入口以上文为准，历史说明不作为新主流程的前置条件。

### Baite AI R&D OS — 系统管理员工作台原型

这是一个支持单机云端测试部署的轻量协作系统。它验证系统管理员发布组织共享信息、任意数量的 Local Runner 安全同步、Local Agent 通过 outbox 回传结构化事件，以及项目资料与研发进度的双向协作。云端与 Mac 服务安装、HTTPS、备份恢复和逐项验收见 [部署手册](DEPLOYMENT.md)；代码就绪不代表云端已经验收。

当前不是 Workflow Engine，也不会远程操纵 Agent。

Windows / 自助接入候选版：见 [跨平台 Runner 接入说明](docs/runner_onboarding.md)。新增统一安装器、runtime.json 入口、可选 Workspace、心跳运行信息，以及节点交付窗口的安全接入指令；仍需 Windows 10/11 和 WorkBuddy 实机验收，未发布正式云端。

下一阶段候选分支依次加入 Operating Contract v1、Runner 内置 Agent 入口和稳定 Manager 托管的 Runtime 自动更新；设计、测试证据和未验收项分别见 [WP1 Review](docs/WP1_OPERATING_CONTRACT_REVIEW.md)、[WP2 Review](docs/WP2_AGENT_INTEGRATION_REVIEW.md)、[WP3 Review](docs/WP3_RUNNER_AUTO_UPDATE_REVIEW.md)。正式云端和现有真实节点没有随候选代码切换；`runtime-state.json` 是安装目录内的程序版本状态，Workspace 内的 `runtime.json` 仍只表示资料快照。

双平台自动测试已通过，证据与实机待验收边界见 [候选版本验证报告](docs/windows_candidate_verification.md)。

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

仅接入云端的伙伴不需要安装 Panel 或完整仓库：使用 [Runner 通用安装与 Agent 接入说明](docs/runner_onboarding.md)，由 Local Agent 完成下载、配置和验证。下方环境安装主要供 Control Server 与本地开发使用。

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
