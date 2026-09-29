# Windows 10 22H2 x64 + WorkBuddy 实机验收

状态：**全部待验收**。CI Windows Server 结果不能勾选本清单。

记录：测试日期 / OS build / Python 3.13.x x64 / WorkBuddy 版本 / 候选完整 SHA / 测试服务 URL / 节点 ID。不要记录 Token。只用管理员明确指定的隔离测试服务和测试节点，不使用正式项目数据。

- [ ] 普通本地用户，没有管理员或开发者模式；记录 OS 补丁/ESU 状态和组织是否允许继续使用该设备（本轮不处理 OS 升级）。
- [ ] 完全没有 Runner 安装、配置、后台任务或 Workspace 时，从 Panel 下载个人配置、复制 Windows 指令。
- [ ] WorkBuddy 可访问技术说明；飞书无法访问时如实报告，不冒充已读，不执行无关 Skill/任务。
- [ ] 检查并在必要时由用户安装官方 Python 3.13 x64；无提权、无 ExecutionPolicy 修改。
- [ ] 固定 SHA 安装器校验；空 Workspace 自动确定；中文、空格本地 NTFS 路径成功；拒绝网络盘/移动盘/重解析点。
- [ ] 安装目录配置和日志 ACL 不向其他普通用户开放；Task XML/命令/日志没有 Token。
- [ ] Panel 看到本节点 Online、实际 Windows/版本/Workspace，管理员建议路径不被覆盖。
- [ ] 全局及项目 runtime.json 完整；规则、Skill、RAG、项目同步正常；不依赖符号链接。
- [ ] 唯一 Progress 测试事件被 ACK 并进入 sent；跨节点配置不能冒充其他节点。
- [ ] 相同配置重复安装不重复运行，工作文件不变；手动启动和后台共用 Workspace 锁。
- [ ] stop 后不登录自启动；start 恢复；注销再登录自动运行，关闭 WorkBuddy 后 Runner 仍可运行。
- [ ] 电池供电持续运行；休眠期间 Offline，不强行唤醒；恢复后自动重连和补传。
- [ ] 断网重连、文件占用、内容损坏与快照切换失败保留 last-known-good；未 ACK 不归档。
- [ ] Defender / SmartScreen / 组织防护软件是否提示；仅由用户处理必要授权，不关防护。记录未解决项。
- [ ] 连续运行至少 24 小时，检查日志、上海日期日报、内存与重复进程。Task 无默认 72 小时截止设置。
- [ ] 管理员对测试节点轮换 Token；旧配置失效，新配置重新安装恢复，同一 Workspace 保留。
- [ ] uninstall 移除 Task、程序和安装内凭证；Workspace/工作文件留存；提示云端 Token 未撤销，原始下载配置仍需保管。

结论只选：通过 / 未通过 / 未完成。每个失败记录复现步骤、脱敏日志、是否阻断试用。用户确认前不合并、不发布云端。
