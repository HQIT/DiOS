# 版本发布与回滚资格

DiOS 以不可变 Revision 绑定任务，以 Deployment 指向当前可接单版本。候选版本先执行受 E2AG 管理的健康任务，随后等待旧版本任务自然结束；失败候选保留原可用版本。回滚重新申请工作授权并完成健康检查，历史任务、尝试、会话和产物继续使用原身份。

## 验收映射

| 原要求 | 验证及结果 |
| --- | --- |
| K02-1-A01 版本可追溯 | 真实 Cursor ACP 的独立 Worker 记录进程、会话、Revision 和固定镜像；API、CLI、Console 返回相同版本。模型实现标为不可见。Adapter 摘要包含在不可变 manifest 中。 |
| K02-1-A02 配置编辑不改写运行版本 | 发布后修改源配置并删除本次复制的受控配置文件，已发布镜像仍成功执行。升级期间旧任务保留原 Attempt，完成后才激活新版本。 |
| K02-1-A03 失败部署与回滚审计 | 候选进程退出 7 后保留正常版本；回滚通过新的 E2AG Work 和工具审批，Deployment 记录各次发布及回滚。原任务五份产物经 HTTP 下载逐一校验摘要。 |
| K02-1-A04 外部兼容与回退范围 | 复用[外部兼容门禁证据](evidence/endpoint-compatibility-20261125.json)：声明变化要求复核，版本化派发绑定声明摘要。任务页分开展示配置回退、受控镜像回退和模型实现可见性。 |

详细记录见[发布资格证据](evidence/release-qualification-20261125.json)。真实 Harness 使用合成只读源码，不涉及客户仓库。四次成功模型任务均经过工具审批；故障候选不调用模型。审批使用专用资格验证身份，产品接受与高影响独立评审仍按工程手册执行。

## 复现

在已安装依赖的开发环境中，以受控 Cursor ACP profile、既有凭据引用和新的输出目录运行：

```sh
backend/.venv/bin/python scripts/qualify_revision_release.py \
  --profile /path/to/cursor-profile.json \
  --secrets /path/to/secret-registry.json \
  --output /path/to/new-qualification-directory
```

脚本创建独立 SQLite scope，保留 Worker 日志、尝试及产物；发布后仅修改和删除输出目录内复制的配置。每次运行需要真实模型调用，默认不会启动。凭据 registry 和日志置于私有目录，公开证据只保留标识、摘要及结果。

受控镜像可以固定本地 Harness 与配置。远端模型的内部版本不可见；A2A Agent Card 属于端点声明，无法证明远端实现未变，也不提供远端实现回滚。
