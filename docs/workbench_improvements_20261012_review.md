# RDOS 工作台改进：实现、隔离验证与体验审核

记录时间：2026-10-13。工作分支：`codex/operating-contract-agent-update`。本轮只改本地代码和隔离测试；没有推送、部署或写入正式飞书。原有三份未跟踪说明文件保留，未作为测试数据使用。

## 已实现

| 顺序 | 实际变化 | 边界 |
| --- | --- | --- |
| A 接入 | 按单条反馈切分正文，校验指定机构反馈文档、成员、本轮、当前步骤、反馈 ID、写回标识与读取来源。第 0 步需要同条记录明确报告实际读取、追加和写后读回；模糊但可能成立的表述才调用受限模型核对。阶段判断保存缺项、依据、验证范围和规则版本；规则升级可重评原正文，同版本重复检查不重复记历史。前后端都绑定当前轮次和步骤，迟到响应不能覆盖新状态。通过后显示下一步指令；系统读取或解析失败只给重试，真实缺项才给补充指令。 | 反馈正文已读回不证明本地 Agent 拥有独立账号授权；工作效果仍须后续证据。旧工作阶段记录保留，但不再是接入门槛。 |
| B 项目反馈 | 解析反馈周期、报告时间、模板版本、更正关联、多任务和未关联任务的项目工作，保留原文及来源。六类栏目为当前进度、成果、卡点与协作、Human Gate 线索、Skill／RAG 使用、本地 Skill 更新；前四项优先展示，后两项按需展开。任务版本状态分清当前、旧版、停用和待核对；正文不变时重新读取不算新增成果，也不把系统读取时间当报告时间。中央上下文加入已授权 Workflow 参考，但人类决定仍须有可核验来源。项目指令与周期要求沿用同一反馈模板；周期要求经现有任务草稿发布。 | 六类栏目允许留空，不要求制造空周报。成员报告的 Gate 只能算线索，不能冒充负责人确认。 |
| C 中央工具与模板 | 任务草稿可新建、修改或停用，说明理由并固定原版本、正文哈希和远端记录；发布前先核对飞书。确定未写入的版本冲突可保留输入、重新审阅；结果不确定则沿原操作标识回查。管理员新增只读运行诊断，标明观察时间及 Local Agent 证据限制。六种可变指令文案集中到本地受控模板，复用既有提案编辑／确认界面；确认后版本生效，旧限时链接失效。身份、范围、反馈位置、去重与权限仍由程序生成。 | 当前模板唯一生效来源是本地 SQLite，没有正式飞书模板来源。模板发布与说明读取分别留存；实际执行和效果须待后续反馈核对，本轮未验证。 |

主要接口变化：`POST /api/central/onboarding/check` 可带期望轮次和步骤；`GET /api/central/diagnostics` 只供管理员读取；任务草稿增加操作、理由及原任务版本／哈希，继续使用原编辑、退回、发布接口。Agent 的 `prepare_task` 可起草新建、修改、停用以及周期反馈要求；`diagnose_runtime` 是管理员只读工具。限时说明按当前模板版本校验，旧版返回失效而非继续执行。

## 验证结果与证据边界

| 层次 | 已运行结果 | 不能据此推断 |
| --- | --- | --- |
| Python 隔离回归 | 项目 `.venv` 执行 `python -m unittest discover -s tests -p 'test_*.py' -q`：353 项，6 项跳过，无失败。A 接入定向 90 项、关联任务与运行定向 64 项也分别通过（与全套有重叠）。 | 这些测试使用隔离 SQLite 与飞书／模型桩，不是正式服务联调。 |
| A → B → C 隔离贯通 | `tests/test_workbench_journey.py` 用替身模拟阶段判断，检查 0→1→3、每步新指令可读和旧链接失效；模型替身调用中央工具读取项目反馈、起草模板，管理员经 API 确认后读取新版指令。 | Local Agent、模型与飞书均为显式测试桩；只能证明应用内部衔接。 |
| 新旧前端隔离检查 | 项目绑定、乱序响应、表单草稿、任务冲突、接入步骤、反馈时间和模板状态等新版 39 项；仍可访问的旧版 10 项，通过。Python 编译检查与 `git diff --check` 通过。 | DOM 隔离断言不等于浏览器视觉和真实接口验收。 |
| 浏览器体验 | 用隔离 fixture 在 1280×800 和 390×844，分别以管理员、研究员、双角色走查新版五个入口、错误／空／成功状态、关键弹窗及 `/legacy`。实际点击了下一步、项目指令过期恢复、草稿版本冲突后的重审、结果不确定时沿原操作继续、模板来源变化拒绝发布。截图在下文。 | 账号和数据是角色测试；fixture 不联网写正式飞书，也不运行真实模型。 |
| 本轮真实外部链路 | 未运行。 | 正式飞书读写、独立 Local Agent 读取与执行、真实模型输出、中央及本地定时触发都保持“未验证”。历史真实结果仍以各自原记录为准，不能移作本次新版验收。 |

收尾只读代码审查额外发现并修复假阳性／恢复风险：第 0 步不再把“写后读回”当作“已读取接入材料”，旧轮复用共享一次检查时间预算；不确定任务写入重试固定首次远端基线，关联的原任务变化也会停止补充写入，后续新版本不会被旧操作覆盖。这些反例有独立回归，不以此前全套测试通过代替修复。

## 两轮前端体验审核

审核依照 [frontend-experience-review](</Users/alexliu/.codex/skills/frontend-experience-review/SKILL.md>)：先确认任务路径与动作，再核对信息层级、文案、视觉和失败恢复；源码判断、浏览器观察、实际点击分别记录。两轮共用冻结的隔离数据；特殊失败状态由 fixture 切换，截图不冒充同一次连续操作。

### 第一轮：完整流程和问题定位

在管理员、双角色与研究员身份下，桌面和手机分别查看其有权进入的页面与弹窗。浏览器观察到：过期项目指令和接入指令隐藏了可恢复动作；换账号加载期间可能短暂留有前一账号的指令状态；对话输入区在桌面和手机首屏偏下；组织级模板在建议列表中看似属于当前项目；任务发布冲突同时出现重复提示。检查失败时，接入卡的说明还会错误指向“获取给 Agent 的指令”。这些问题会让用户误以为不能继续、误读影响范围或按错下一步，已在本轮修复。

第一轮截图例：[管理员工作](review_assets/workbench-20261013/r1-admin-work-desktop.jpg)、[管理员诊断](review_assets/workbench-20261013/r1-admin-diagnostics-desktop.jpg)、[双角色接入](review_assets/workbench-20261013/r1-dual-onboard-desktop.jpg)、[双角色反馈](review_assets/workbench-20261013/r1-dual-feedback-mobile.jpg)、[研究员接入](review_assets/workbench-20261013/r1-researcher-onboard-mobile.jpg)、[研究员项目](review_assets/workbench-20261013/r1-researcher-project-mobile.jpg)、[未保存任务弹窗](review_assets/workbench-20261013/r1-dual-task-unsaved.jpg)。[全部第一轮截图](review_assets/workbench-20261013/)按 `r1-` 前缀查找。

第一轮实际操作记录：在隔离页面依次切换管理员、双角色和研究员，打开各自可见入口；双角色点击接入“继续”并查看下一步指令，打开任务未保存弹窗；桌面和手机分别查看工作反馈、项目与对话。页面检查只证明 fixture 所呈现的账号权限和数据状态。

可直接对照相同冻结数据的三组主页面：双角色[接入第一轮](review_assets/workbench-20261013/r1-dual-onboard-desktop.jpg)／[第二轮](review_assets/workbench-20261013/r2-dual-onboard-desktop.jpg)，双角色[项目第一轮](review_assets/workbench-20261013/r1-dual-project-desktop.jpg)／[第二轮](review_assets/workbench-20261013/r2-dual-project-desktop.jpg)，研究员[项目手机第一轮](review_assets/workbench-20261013/r1-researcher-project-mobile.jpg)／[第二轮](review_assets/workbench-20261013/r2-researcher-project-mobile.jpg)。异常截图用另列场景，不与这三组混称同状态。

### 第二轮：修正后复核

- 接入失败页实际只出现“重试检查”，保留旧进度，不产生无依据的补充指令；[桌面错误状态](review_assets/workbench-20261013/r2-onboard-check-error.jpg)、[手机错误状态](review_assets/workbench-20261013/r2-onboard-check-error-mobile.jpg)。指令过期后主操作变为重新生成，旧链接和正文隐藏；浏览器重新生成后得到当前指令，[过期恢复](review_assets/workbench-20261013/r2-onboard-expiry-recovery.jpg)。
- 项目指令过期后显示重新准备，后返回当前项目指令；[项目恢复](review_assets/workbench-20261013/r2-project-instruction-recovery.jpg)。项目卡和当前项目范围的工作入口不再相互覆盖；[项目桌面](review_assets/workbench-20261013/r2-dual-project-desktop.jpg)、[研究员手机](review_assets/workbench-20261013/r2-researcher-project-mobile.jpg)。
- 对话输入和发送按钮在桌面、手机视口内可见；[桌面](review_assets/workbench-20261013/r2-chat-desktop.jpg)、[手机](review_assets/workbench-20261013/r2-chat-mobile.jpg)。任务版本冲突保留所填内容，重新保存／审阅后才能发布；[冲突](review_assets/workbench-20261013/r2-task-conflict-current.jpg)、[恢复](review_assets/workbench-20261013/r2-task-conflict-recovered.jpg)。结果不确定时使用原操作继续，[待核对发布](review_assets/workbench-20261013/r2-publish-uncertain.jpg)。
- 模板建议明确影响全部适用成员；发布后仅称模板已生效，不称采用有效；[模板弹窗](review_assets/workbench-20261013/r2-template-review.jpg)。来源变化保留草稿并阻止重复发布；[来源冲突](review_assets/workbench-20261013/r2-method-source-changed.jpg)。研究员手机可见身份和主动作；[接入](review_assets/workbench-20261013/r2-researcher-onboard-mobile.jpg)。旧版入口仍有明确标识与返回新版的链接；[旧版](review_assets/workbench-20261013/r2-legacy-entry.jpg)。

第二轮实际操作记录：重新以三类角色打开对应入口，点击接入和项目过期指令的恢复动作；在任务弹窗重审版本冲突并保留原输入，沿原操作核对结果不确定的发布；查看模板和方法来源冲突状态，再打开 `/legacy`。浏览器还查看了连接失败和账号切换；[连接失败](review_assets/workbench-20261013/r2-first-read-failure.jpg)。权限、等待和空内容、未保存关闭、重复点击、部分刷新失败及迟到响应由隔离交互断言复核；没有为这些状态伪造浏览器截图。第二轮修复后，没有再观察到阻断操作、丢输入、错误归属或虚报成功的问题；这不是三位真实成员对体验的认可。

本次全量审核的覆盖边界如下。源码列包括页面实际调用的后端错误和指令文案；“浏览器”仅指隔离 fixture 中实际打开或操作过的状态。

| 入口 | 角色与关键状态 | 动态文案来源 | 证据 | 未覆盖 |
| --- | --- | --- | --- | --- |
| Agent 接入 | 三角色；待执行、通过、失败、过期、下一步 | `central_onboarding.py`、`central_templates.py`、`central.js` | 源码、两轮截图、检查与恢复点击 | 真实 Local Agent 按新版指令执行 |
| 项目 | 三角色；有项目、项目指令过期 | `central_projects.py`、`project_feedback_guidance.py`、`central.js` | 源码、两轮截图、重新准备点击 | 正式项目资料创建或写回 |
| 中央 Agent | 管理员、双角色；空对话、历史、等待 | `central_service.py`、中央 Agent 回答（本轮仅 fixture 输出和代码检查）、`central.js` | 源码、桌面及手机截图、隔离交互 | 本轮未运行真实模型回答 |
| 任务与反馈 | 管理员、双角色；六类反馈、任务冲突、结果不确定、方法来源变化 | `central_service.py`、`central_api.py`、`central.js` | 源码、截图、发布恢复点击 | 正式飞书发布和成员采用 |
| 全局设置及诊断 | 管理员、双角色；诊断结果与失败 | `central_api.py`、`central_service.py`、`central.js` | 源码、第一轮截图、隔离断言 | 线上权限与实际中央周期 |
| `/legacy` | 管理员；旧版入口及返回 | 旧版三份前端文件 | 源码、第二轮截图、10 项隔离检查 | 本轮未重做旧版所有历史业务流程 |

## 本地试用与待发布事项

在项目目录沿用已有 `.env.local` 和虚拟环境运行 `.venv/bin/python start_local.py`，打开 `http://127.0.0.1:8000`，使用已有本机账号。此命令连接当前配置的真实后端，可能触发中央周期检查；本轮仅启动了隔离 fixture 进行浏览器审核，没有启动该真实服务或写正式资料。不要把 fixture 的成功状态当成正式结果。

发布前仍需：在本地真实服务下核对既有账号和这版页面；经用户确认后再用正式飞书与真实模型验证任务／反馈／模板链路；由独立 Local Agent 验证新版说明的读取与实际写回；分别观察真正的定时触发和成员采用反馈。若将来配置飞书正式模板来源，须另行确定资料位置和授权；当前本地模板已足够进行本机功能试用。本轮不推送 `main`，不触发线上自动部署。
