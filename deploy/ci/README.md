# Linux 交付 CI

`Linux delivery qualification` 在干净的 Ubuntu 24.04 runner 上构建固定基线、候选后端和前端，再运行 E2AG 受控的三路 ACP 任务、自然排空、安装备份校验、失败候选回退与实际版本切换。排队任务恢复后保持原身份，任务、尝试、会话和产物连续。另以真实 SIGKILL 中断完成输出后的 Worker，再经 E2AG 复核恢复原结果，确认没有重复 prompt。

当前开发分支的相关代码推送自动触发；主分支与面向主分支的 PR 通过 `docker-build.yml` 调用。两个镜像发布 job 均依赖资格 job 成功。主分支及版本标签发布使用门禁导出的同一份镜像，加载后核验 image ID，再打发布标签；PR 只验证。实现采用 [GitHub 可复用工作流](https://docs.github.com/en/actions/sharing-automations/reusing-workflows)和 [Docker 先测试后发布流程](https://docs.docker.com/build/ci/github-actions/test-before-push/)。

本地复现使用 Linux、Python 3.12、Docker Engine 与 Compose v2，先安装 `backend/requirements.txt`，提交被测源码，再运行：

```sh
python scripts/qualify_delivery.py --output workspace/delivery-run
```

输出目录必须不存在。构建上下文仅包含 Git 提交中的文件，固定升级基线为 `33e898ba9bfe761cbe8fad29a45b3f61f1cc6c66`；基线升级通过独立提交明确变更。报告包含基线/候选提交、实际镜像摘要、依赖版本、测试结果和执行身份，GitHub 保留 14 天。

门禁只接受 R07/R13 的全部断言通过且来源未变化；跳过、失败、缺失原生证据均失败。参考故障总套件仍有未完成场景，因此其退出码 2 只有在这组明确断言均通过时才被本门禁接受。报告保留 `reference_suite_complete=false`。

ACP bridge 为生产实现，协议端点提供合成输出，无模型凭据或付费调用。真实模型资格、其他故障场景、schema 变化和物理跨主机资格继续使用各自证据。CI 不访问现有开发或生产部署。
