# Windows 11 x64 + WorkBuddy 实机验收

状态：**全部待验收**。Windows 10 或 GitHub Windows Server 的结果不能代替本机验收；ARM64 不在范围内。

记录：测试日期 / Windows 版本及 build / Python 3.13.x x64 / WorkBuddy 版本 / 候选完整 SHA / 测试服务 URL / 节点 ID。凭证不进入记录。由管理员提供隔离测试服务和节点，不修改正式云端或飞书正文。

- [ ] 普通用户，无提权、开发者模式或符号链接授权；确认不是 ARM64/模拟架构。
- [ ] 初始无 Runner；Panel 新节点或已有节点提供“下载个人配置 + Windows 接入指令”，旧 Token 无法重读。
- [ ] WorkBuddy 读技术说明并核验版本；飞书不可访问时报告限制；不执行无关 Skill 安装或研发任务。
- [ ] 缺少 Python 时引导官方 Python 3.13 x64 用户级安装，不绕过系统保护。
- [ ] 默认安装目录及默认 Workspace 正确；中文、空格、本地 NTFS 自定义目录成功，OneDrive/网络盘/移动盘不作为工作目录。
- [ ] Windows ACL 私有，Token 不在 Task、命令参数、日志或公共指令中。
- [ ] heartbeat 回报本节点 OS、版本、实际 Workspace，其他节点和建议路径不被修改。
- [ ] 读取 global/project runtime.json；同一批资料保持同一快照；非法文件名、大小写冲突、文件占用失败保持旧入口。
- [ ] Progress 唯一事件收到 ACK；Rules、Skill、RAG、项目和日报正确同步；工作文件正文不自动上传。
- [ ] 重复安装不覆盖工作/重复运行；后台和手动启动互斥；status/stop/start 行为清楚。
- [ ] 注销再登录自动启动；普通权限、InteractiveToken、不保存用户密码；WorkBuddy 退出不影响 Runner。
- [ ] 电池/休眠/恢复/断网测试；不主动唤醒机器，恢复自动重连和补传，未 ACK 事件仍保留。
- [ ] Defender、Smart App Control、Controlled Folder Access 或组织策略提示有记录，必要授权由用户处理，不关闭防护。
- [ ] 连续运行至少 24 小时；上海时区工作日日报无错日，无任务运行时限，无多份 Runner。
- [ ] 测试节点 Token 轮换后旧 Token 失效；新配置恢复且保留 Workspace。
- [ ] 卸载仅删除已核实的本节点任务/程序/安装内凭证，保留 Workspace；云端 Token 未自动撤销。

结论：通过 / 未通过 / 未完成。附脱敏证据及阻塞项。两份客户端清单和 WorkBuddy 接入完成并获得用户确认后，才讨论合并与正式部署。
