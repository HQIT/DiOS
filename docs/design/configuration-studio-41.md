# 配置工作台

Console → Studio 提供运行配置的副本编辑、JSON 校验、字段差异审阅、自然语言建议和 JSON 草案导出。配置覆盖通用执行 Profile；岗位、客户流程和业务模板由独立上层应用管理。

## 编辑与审阅

选择现有配置并载入副本，或将 JSON 文件导入当前草案。左侧保留基准，右侧编辑草案。校验先检查 JSON 语法和字段类型、范围，再调用 Runtime 已有的 Profile、Secret 引用和执行边界校验。语法错误提供行列，Schema 错误提供 JSON Pointer、校验规则与预期值。校验通过后展示每项修改前后的值及标准 JSON Patch。

草案保存在当前页面，导出后可重新导入。导出内容为实际 Profile JSON。编辑、校验、建议和导出不会修改激活文件、Revision、Deployment 或已有 Run。S03-1.02 接续草案的受控验证与发布，S03-1.03 承接完整发布链的联合资格。

差异使用 [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902) 与 BSD 许可的 [python-json-patch](https://pypi.org/project/jsonpatch/1.33/)。错误定位复用 [jsonschema 的实例路径与校验规则](https://python-jsonschema.readthedocs.io/en/stable/errors/)。配置限制为 64 KiB、32 层、4096 个节点及单数组 512 项；重复键、非有限数、无效 UTF-8 与内联凭据拒绝进入建议和 diff。Secret 使用既有引用契约。

## 自然语言建议

配置建议智能体通过 `dios-config-proposal-v1` 输入契约注册，启用动态输入与 E2AG。首条路径使用 Cursor ACP 和现有 SDK，任务进入原持久队列，由独立 Worker 执行。任务、Attempt、Session、结果产物与授权证据沿用 Runtime。

建议以输入配置摘要为基准。结果先校验，再显示差异；用户点击“应用到草案”后才修改编辑器。任务运行期间草案发生变化时，旧建议返回 `STUDIO_PROPOSAL_STALE`。重复请求复用幂等键，浏览器观测失败保留原任务。执行或结果接收授权被撤销时沿用 E2AG 拒绝路径。

[配置建议工作负载](../../capabilities/configuration-assistant/README.md) 和 [输入/结果契约](../../examples/runtime/studio-inputs.json) 可供其他 Harness 适配器复用。当前首批真实验证使用 Cursor，后续实现按原 Harness 资格任务推进。

## API 与 CLI

API 使用既有可信操作员认证，独立上层应用的普通身份不能访问管理配置正文。

| 接口 | 内容 |
| --- | --- |
| `GET /api/os/runtime/studio/profiles/{id}` | 现有配置副本、来源 Revision 与摘要 |
| `POST /api/os/runtime/studio/compare` | `{base, draft}` 两段 JSON 文本；返回校验问题、摘要、Patch 与逐项差异 |
| `POST /api/os/runtime/studio/suggestions` | `{profile_id, text, instruction, idempotency_key}`；返回原 Runtime 任务 |
| `POST /api/os/runtime/studio/suggestions/{run_id}/review` | `{text}` 当前草案；校验基准摘要，返回候选 JSON 与差异 |

本地 CLI 复用同一校验与 diff：

```bash
backend/.venv/bin/python scripts/config_studio.py \
  --base profile.json --draft profile-draft.json
```

有效配置返回退出码 0，错误返回 1；正文返回结构化校验和差异。CLI 无发布动作。

## 验证

[工程证据](evidence/configuration-studio-20261126.json) 关联 API/CLI 测试、真实 Cursor 任务、E2AG 决策、浏览器编辑修错、人工应用、文件导出导入，以及中英文、明暗主题和移动视口。历史任务身份与既有配置保留检查单独记录。S03-1.01 的完成依据限于编辑与可审阅建议；正式发布链按后续两项交付。
