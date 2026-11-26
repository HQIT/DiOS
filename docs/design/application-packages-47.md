# 独立应用包与公开客户端

2026-11-24 · #47 · [能力边界](../adr/0011-capability-only-platform-boundary.md)

DiOS 管理执行和证据；应用拥有业务输入、规则、数据与业务验收。参考实现位于 applications/，通过无服务端依赖的 dios-sdk 接入。生产后端不导入应用模块。

| 构建物 | 职责 | 运行入口 |
| --- | --- | --- |
| dios-sdk | 任务 HTTP 客户端、JSONL、受控工具通道、ACP/MCP 客户端 | Python 导入 |
| knowledge-evidence | 输入校验、检索依据、引用与待复核成果 | dios-evidence |
| knowledge-source | 参考资料授权供给与版本 | dios-knowledge-source |
| static-review | 静态评审规则与 ACP/Cursor 报告 | dios-review / dios-cursor-review |
| reference-workflows | 合成选型、岗位汇总和学生导读规则 | dios-reference-workflow |

知识应用依赖轻量 dios-sdk；static-review 0.1.1 依赖 dios-sdk[acp]>=0.1.2,<0.2，ACP 扩展固定官方 agent-client-protocol 1.0.0。Cursor 可执行程序、模型凭据和版本配置由部署者单独提供。真实供应商版本认证按 #56/#58 执行，见 [取消与授权验证](acp-cancellation-56-58.md)。

## 构建与安装

从仓库根目录，在安装 build、setuptools、wheel 的 Python 环境执行：

```sh
python scripts/build_application_packages.py --output /absolute/releases
pip install --no-index --find-links=/absolute/releases dios-sdk knowledge-evidence
```

构建器从 applications/ 的 application.json 与 pyproject.toml 自动发现独立应用；当前输出五组 wheel/sdist、四个 bundle.zip 和 checksums.json。应用间依赖优先由同批构建的本地 wheel 解析。每个 bundle 包含应用清单、Dockerfile、适用的输入目录、SDK/应用及完整依赖 wheel；解压后可用 docker build 离线安装依赖。依赖解析需要可访问配置的包索引，默认目标为 Linux x86_64 / Python 3.12；ARM64 构建传入 `--platform manylinux2014_aarch64`，在对应架构执行镜像验证。checksums.json 保存目标平台及构建物摘要。记录实际镜像 digest，再登记到执行 profile。application.json 描述版本和协议；Registry 固定包发布见[发布与回滚](package-registry-release-44.md)；应用接口要求与兼容门禁见[应用契约](application-contract-47.md)。

## 输入、任务与成果

管理员登记本地输入目录、JSON Schema、摘要规则和 result_schema。dynamic_inputs=true 开启动态提交，并要求输入 Schema。API 的 sample_id 与 input_payload 恰选其一。Console 提供 JSON 输入及 Schema 预览；CLI 支持 --input-file；SDK 提供 TaskClient.submit_input。所有入口使用相同准入和幂等规则。

```python
from dios_sdk import TaskClient
client = TaskClient("https://dios.example", operator_token)
run = client.submit_input(profile_id, input_payload, goal,
                          idempotency_key=logical_task_key)
observed = client.wait(run["id"], timeout=60)
```

逻辑任务键由应用持久化。网络提交失败时保留同一键；观察超时保留 Run 身份，继续查询。HTTP 客户端拒绝重定向和非本地明文连接，产物下载校验大小与 SHA256。客户端可使用 Runtime 认证主体或受信操作员凭据；服务端执行对应的 profile 与逐对象权限检查。[认证委派接口](delegation-identity-35.md)通过 `TaskClient.issue_delegation`、`delegation`、`revoke_delegation` 使用，签发身份由服务端认证确定。

每个 Run 冻结输入、目录摘要和结果 Schema；应用不提供可执行校验器。JSON Schema 仅允许本地引用。输入最大 12 KiB；单条 JSONL 和声明 Schema 的结果最大 64 KiB。Worker、本地/远程完成接口、恢复对账均校验结果；不完整成果进入 EVIDENCE_INSUFFICIENT，保留每次尝试及原始输出。技术成功和应用 review_status 分别记录。

工具客户端只能通过 Worker 提供的受控 socket 调用已登记工具；授权主体、绑定、审批与派发由 DiOS/E2AG 决定。应用不能自行批准。重试沿用 invocation_key，避免重复工具效果。

## 停用、升级和回滚

1. 禁用目标 profile 的新准入，停止目标 Worker 领取新任务。
2. 等待其在途任务自然结束；保留 Run、Attempt、Session、产物和应用数据。
3. 固定新镜像 digest，更新目标执行 profile 和对应 E2AG 资源版本，再启动 Worker、恢复准入。
4. 验证新版本成果、权限和恢复行为。失败时同样排空，再恢复旧镜像与对应策略版本。

工作策略摘要覆盖全局 policy_id/subject/actor 与所选 profile，其他 profile 更新不影响已授权任务。全局策略变更仍撤销所有受影响授权，目标 profile 的权限、版本、凭据引用与期限变更仍使旧授权失效。旧版全文件摘要仅在原策略文件完全一致时兼容；首次切换此摘要规则前排空旧授权任务。应用业务表不由 DiOS 迁移。

## 工程证据

见 [2026-11-24 验证记录](evidence/application-packages-20261124.json)。包含已安装 wheel 的隔离测试、真实 Worker/E2AG、CLI/SDK 同一任务重放、动态 Console、应用排空升级/回滚与另一应用在途任务。客户资料及生产评审没有参与此验证。
