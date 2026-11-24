# Runtime 独立主体与对象访问

2026-11-24 · #33 / #35 / #38 · AUTHZ-1.0.0 的首个多主体实现

DiOS 使用可信认证上下文确定 subject、actor、issuer、audience、scope 与主体修订。企业、部门、项目和岗位由上层管理；提交载荷中的同名字段不产生权限。

## 接入与数据边界

FilePrincipals 实现 PrincipalProvider 的 authenticate/current 契约。管理员在工作策略中登记 identity_catalog 的绝对路径；目录含可信 issuer、固定 audience=dios-runtime 和独立主体。令牌采用 256 位随机值，目录只存 SHA256、启停、not_before 与 expires_at。每次请求重新校验，轮换令牌保持主体及任务身份。OIDC/企业 SSO Provider、自动签发与密钥托管仍由 #33 接续。

现有 DIOS_ACCESS_TOKEN 用于旧管理接口；独立应用使用自己的令牌。生产部署仍须配置管理令牌以保护旧控制面。普通操作员在任务 API 中只看历史操作员任务，新主体任务无默认管理员内容读取权。Worker 继续使用既有执行/机器身份，应用令牌不会送入 Worker 或模型。

主体目录中的 profiles 声明可提交的配置、允许的 API 动作和调用者权限完整元组。提交同时要求 run.submit 和 run.read，以便调用方观察已接收任务。E2AG 将调用者权限与配置中的 caller 层取交集，再执行既有 service/workload/resource 交集。工作主体、期限与权限在准入时冻结；续期、工具预检、原生工具通道、远程凭据和结果接收重新校验当前主体。主体失效一经工作验证器观察，原工作与 Grant 持久撤销，重新启用主体不能复活旧授权。

逻辑任务幂等键按 issuer、主体 ID 和原始键分区。不同主体使用同一键得到不同任务；重试和令牌轮换保留原任务。目录修订及授权变化继续遵循原有内容冲突与重新准入规则。

## API、CLI、SDK 和 Console

GET /api/os/runtime/identity 返回安全的主体引用与 API 动作，不返回令牌摘要、凭据、命令或权限文件。API 使用 Bearer 或 X-DiOS-Access-Token；CLI 使用既有凭据配置，dios task identity 查询当前身份；SDK 0.1.1 提供 TaskClient.identity()。

任务列表在数据库查询中执行归属和显式分享过滤，再分页；详情、会话投影、取消、撤销、工具记录、预览与实际下载执行相同对象关联检查。未授权对象与未知对象均返回 404。相同内容摘要的产物仍按 Run/Artifact 关联授权，存储去重不合并访问权限。大文件每个输出块重新验证当前读取权限。

Console 通过主体身份登录任务页，按返回的 access 动作显示取消、撤销、工具审批和下载操作；主体入口不请求全局 Worker 状态。自动工具派发由受控 Worker 完成。独立审批须同时有对象审批权限及匹配已认证 subject 的独立审批凭据，并继续执行 E2AG 自审批拒绝。

## 目录格式

主体：id 为 principals 的键；记录 subject、actor、scope、enabled、revision、expires_at、tokens、profiles、objects。profiles 的每项包含 actions 与 permissions；permissions 使用 AUTHZ 完整元组。

```json
{
  "version": 1,
  "issuer": "trusted:development",
  "audience": "dios-runtime",
  "principals": {
    "application-a": {
      "subject": "principal:a", "actor": "service:a", "scope": "deployment-scope",
      "enabled": true, "revision": 1, "expires_at": 1900000000,
      "tokens": [{"sha256": "<SHA256 of a generated token>", "enabled": true,
                  "not_before": 1800000000, "expires_at": 1800003600}],
      "profiles": {"registered-profile": {"actions": ["run.submit", "run.read", "artifact.read"],
                                          "permissions": []}},
      "objects": {}
    }
  }
}
```

示例占位摘要和空权限须替换为实际配置。目录最多 256 KiB、256 个主体、每主体 8 个轮换令牌。管理员以原子文件替换更新，文件限制为管理进程可读写。启停和作用域变化立即作用于后续 API/执行预检；revision 增长使旧执行绑定失效。

objects 当前按 Run ID 显式配置 run.read、artifact.read、tool.read、tool.approve；撤回后后续请求与下载块被拒绝。单产物独立分享、持久 ACL 管理 API、三层受控委派及上游撤销事件传播属于后续 #35/#38，当前不宣称其完整验收。源系统的资料授权仍由 Provider 约束；应用获得某配置的使用权不等于已完成企业源 ACL 映射。

## 授权一致性补强状态

事务化授权原型已移出运行源码，保存在本地 workspace/authority-prototype-20261124，待按 [成熟方案优先评审](e2ag-mature-solutions-20261124.md) 确定实现。当前开发环境仍使用文件 Provider；文件重检不提供检查与签发之间的原子性保证。

## 验证

[工程证据](evidence/runtime-principals-20261124.json) 包括中间件下的跨主体任务隔离、同摘要跨关联读取拒绝、只读分享无法取消、轮换、期限/受众/作用域拒绝、在途撤销及结果拒收、下载中撤销、真实独立 Worker/E2AG 与浏览器入口。真实企业 SSO 和物理双主机资格继续按 #33/#58 验收。
