# 腾讯云 GitHub Actions 一次性接入

只增加运维入口，不修改业务代码、账号角色或现有部署。工作流仅手动触发，默认 check。
先将本运维 PR 合并到默认分支 main；不用因此合并业务 PR #3。合并不会部署。

## 一次性配置

1. Windows PowerShell 创建专用密钥（两个口令提示直接回车，不覆盖已有密钥）：

```powershell
# 创建存放专用密钥的目录。
New-Item -ItemType Directory -Force D:\cht\ssh
# 私钥保留在本机，只把公钥上传服务器。
ssh-keygen -t ed25519 -C stock-actions -f D:\cht\ssh\stock_actions
```

2. 上传本目录的 server_gateway.py、install_gateway.py 和公钥 stock_actions.pub 到 ubuntu 的 home。
   通过你原来的 SSH 连接登录，不要上传私钥。服务器执行：

```bash
# 安装受限入口并追加公钥，不替换原 authorized_keys。
python3 ~/install_gateway.py ~/stock_actions.pub
```

安装器检查现有 ~/stock-agent-platform/deploy/server.env 与 Docker 权限。
入口安装在 ~/.local/lib/stock-actions/server_gateway.py，业务部署不会覆盖它。
公钥使用 restrict + forced command，只允许 check、diagnose、deploy，不接受 shell/scp。
安装器输出一行服务器公钥 ssh-ed25519 AAAA...，用于下面的 SSH_HOST_KEY。

3. 使用仓库所有者账号 lxh-boop，在 Settings → Environments 新建 production。
   Required reviewers 选本人；只有本人审批且本人触发时，不启用 Prevent self-review。
   Deployment branches/tags 选 Selected branches and tags，只允许 main。
   在该环境 Environment secrets 新建：

| 名称 | 内容 |
| --- | --- |
| SSH_HOST | 服务器实际公网 IP，例如 119.29.182.63 |
| SSH_USER | ubuntu |
| SSH_PRIVATE_KEY | 本机 stock_actions 私钥完整内容，包括 BEGIN/END |
| SSH_HOST_KEY | 安装器输出的服务器公钥，仅 ssh-ed25519 和 Base64 两段 |

SSH 端口非 22 时，环境 Variables 添加 SSH_PORT；默认是 22。
不上传 server.env、数据库密码、Token 或 STOCK_USER_CREDENTIAL_KEY。
本机复制私钥到剪贴板（不在终端打印）：

```powershell
# 粘贴到 GitHub SSH_PRIVATE_KEY Secret，不发送到聊天。
Get-Content D:\cht\ssh\stock_actions -Raw | Set-Clipboard
# 填写完成后清空剪贴板；也清除 Windows 剪贴板历史或同步记录。
Set-Clipboard -Value ''
```

## 首次检查

Actions → Tencent Server Operations → Run workflow：Use workflow from=main，operation=check，其余空。
需要审批时 Review deployments → production → Approve and deploy。
connection=ok 表示连接成功；各容器的 state/health 单独报告，不代表应用已全健康。
check 不部署、不迁移数据库、不重启容器，只保存私有检查记录。

如防火墙仅允许本人电脑 IP，GitHub 托管 runner 无法访问。不要删除原登录规则或开放数据库端口；
先安排固定出口/私网连接或经确认的 SSH 防火墙策略。脚本不修改防火墙。
Host key verification failed 时核对服务器公钥，不要关闭主机密钥校验。

## 部署与诊断

确认没有日更/Agent 任务，并避开计划执行时间。在 main 的工作流中选择：
- operation=deploy
- revision=已审查并测试的完整 40 位提交 SHA（可以来自 chatgpt，但不能填分支名）
- confirmation=DEPLOY

GitHub 拉取该提交并上传 → 校验摘要/源码路径 → 构建 API 与前端 → 再检查活动任务 →
备份并验证 PostgreSQL → 保存旧源码/配置/镜像 → 覆盖源码但保护数据目录 →
执行该版本的幂等账户表初始化 → 更新 API 和前端 → 检查健康。

仅处理当前账户表的增量初始化，不自动运行所有 SQL/baseline 或破坏性迁移。
未来其他迁移需单独审查。不会执行 down -v，不恢复或清空数据库，不重新生成加密主密钥。
旧镜像保留 stock_backup_* 标签，备份和完整日志保存于 ~/.local/state/stock-actions/run-*/。
失败不自动恢复数据库，以免丢失部署后新写入的数据；不要盲目重复部署或删除卷。
部署会重启应用，检查任务不是全站写入锁，仍应选择维护窗口。

公共仓库不能公开原始日志：diagnose 只输出预定义故障类型及容器状态；原文保留在服务器 private.log。
目前诊断不是完整根因分析，也不提供任意远程命令入口。需要追加诊断时审查并更新入口脚本。
备份不会自动删除；验收和异机安全备份之后再清理具体目录。

## 安全与撤销

拥有 Docker 权限的部署依然是高权限操作，forced command 不是恶意部署代码的沙箱。
保留环境审批，只部署可信代码。不在生产机安装公开 PR 可调用的 self-hosted runner。
撤销时，从 ~/.ssh/authorized_keys 删除结尾 stock-actions 的整行，并删除 GitHub SSH_PRIVATE_KEY；
不要清空 authorized_keys，也不要删除原 STOCK_USER_CREDENTIAL_KEY。

## 测试边界

运行：python3 -m unittest discover -s deploy/actions -p 'test_*.py'
本次完成 18 项离线测试、Python 编译与 workflow YAML/Bash 语法检查。
未使用真实 SSH 密钥、未连接生产服务器、未执行生产迁移或线上端到端验收。

## 官方参考

https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow
https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments
https://docs.github.com/en/actions/reference/security/secure-use
https://man.openbsd.org/sshd.8
https://docs.docker.com/engine/install/linux-postinstall/
