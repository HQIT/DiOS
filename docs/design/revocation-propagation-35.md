# 来源撤销与委派传播

DiOS 将已验证的 OIDC 注销事件、委派撤销记录和受影响的 Work/Grant 保存在同一 Runtime 事务中。任务授权详情与委派查询提供来源关联、授权代次及事件时间，执行退出确认继续使用原 Worker/远端任务状态。

## 来源协议与事务边界

[OpenID Connect Back-Channel Logout 1.0](https://openid.net/specs/openid-connect-backchannel-1_0.html)定义签名注销令牌、发行者、受众、会话/主体及唯一事件身份。现有原生验证器核验令牌后，接收器按作用域、发行者、客户端和事件 ID 去重；相同 ID 的冲突内容拒绝入账。重复投递返回原处理结果。

[RFC 8935](https://www.rfc-editor.org/rfc/rfc8935.html)定义安全事件的 HTTP 推送与可靠投递规则。DiOS 当前对外接口仍是 OIDC 后台注销接口；持久接收与幂等处理沿用现有事件账本。新增字段属于 DiOS 查询契约。

注销事件与委派签发者匹配后，事务撤销对应委派及其依赖的 Work/Grant。事务已提交的注销阻止新授权与新派发。先提交的派发属于在途动作，原回执可以归档；任务授权撤销后不接受新的成功结果。两个提交顺序的资格见 [E2AG 矩阵](governance-matrix-34.md)。

## 可追踪的观测范围

`authorization.source_event` 及 `authorization.delegation.chain[].source_event` 表示与当前绑定匹配的来源撤销记录：

| 字段 | 含义 |
| --- | --- |
| `id`、`issued_at` | 已收到事件中来源时间最大的事件及其来源时间；并列时选择最早记录 |
| `received_at` | 所选事件在 Runtime 事务中记录的时间 |
| `first_received_at` | 匹配事件首次记录时间 |
| `last_received_at` | 匹配事件最近记录时间 |
| `event_count` | 去重后的匹配事件数 |

迟到旧事件增加接收记录，来源时间上界保持单调。该上界只覆盖已收到的事件；事件流是否完整及传输延迟需要来源侧投递确认。发行者时钟与 Runtime 时钟分别展示，不将两者相减当作可靠的传播时延。

委派链的 `revocation` 提供本地撤销审计 ID、操作、记录时间及来源事件引用。任务详情、委派 API 和 CLI 的原 JSON 输出均保留这些字段。Console 支持中文、英文及窄屏展开查看，继续显示“授权已撤销，等待执行端退出确认”。

## 已验证行为

[工程记录](evidence/revocation-observation-20261126.json)覆盖重复/冲突事件、乱序到达、错误来源隔离、旧主体事件与新会话并存、重启后的持久记录，以及原生 OPA 委派任务的来源传播。原生 OPA 场景同时验证：迟到事件不重复提升撤销代次，委派查询与任务详情引用相同来源事件。

UI 验证使用呈现夹具，覆盖时间、事件数、委派关联、撤销与执行停止的区分、中英文和 390px 窄屏。后端集成验证独立覆盖真实数据库与原生 OPA；开发部署保留全部既有任务、尝试与产物身份。

## 持久撤销与身份管理

签发者停用、身份版本失配、委派权限撤回或有效期限收窄一经控制点确认，立即撤销所检查的委派及依赖的 Work/Grant。对于记录拒绝后正常提交的路径，撤销与拒绝审计一起提交；对于整笔操作回滚的路径，先完整回滚被拒操作并释放写入资源，再用独立事务保存撤销。拒绝响应等待撤销持久化完成。恢复来源身份后，原委派保持撤销，新的授权需要重新签发。

这沿用 [SQLAlchemy 的事务回滚及后续事务](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)，使用原有授权代次与幂等撤销账本。[RFC 7009](https://www.rfc-editor.org/rfc/rfc7009.html)描述撤销授权及其关联令牌的语义；DiOS 在本地委派记录中保留同类终态。[RFC 7662](https://www.rfc-editor.org/rfc/rfc7662.html)提供当前有效状态查询；暂时无法查询来源保持可重试，不触发永久撤销。

[持久撤销证据](evidence/revocation-durable-20261126.json)包括两个原生 OPA 缺陷回归：修复前旧委派仍有效，修复后恢复身份仍无法复用。四组策略/存储矩阵共 52 项通过，另有 17 项租约和写入队列回归；回滚产生的临时业务记录不保留，撤销代次和审计只产生一次。

Linux 文件身份目录通过 `identity-update` 管理。命令接受当前身份版本与私有 JSON 变更，支持启停、期限、令牌、权限及对象访问范围；每次变更递增身份版本，并在 Runtime 作用域事务内撤销旧版本的直接 Work/Grant 和该身份签发的委派。重新启用产生新身份版本，旧授权保持撤销。

```bash
# changes.json 内容例如 {"enabled": false}，文件应仅管理员可读。
python -m app.runtime.cli --database-url "$DIOS_RUNTIME_DATABASE_URL" --scope "$DIOS_RUNTIME_SCOPE" \
  identity-update --catalog /absolute/path/principals.json \
  --principal staff-01 --revision 7 --file /absolute/path/changes.json
```

预期版本检查沿用[条件更新](https://www.rfc-editor.org/rfc/rfc9110.html#name-if-match)的并发控制原则；私有临时文件经校验及 `fsync` 后使用 [POSIX 原子替换](https://docs.python.org/3/library/os.html#os.replace)，再提交数据库。文件与数据库属于两个持久化资源：若文件替换后数据库提交中断，新版本仍使旧绑定失效；重试同一版本和变更可完成数据库撤销对账。重试时不恢复旧文件、不递增第二次版本。配置目录需由运行管理员拥有且禁止组/其他用户写入；文件管理锁限定为本机 Linux，多节点源应使用统一身份服务与验证事件。

动态身份配置须通过该入口或已验证的来源事件更新。绕过入口直接覆盖文件不提供变更历史或撤销顺序保证。配置恢复需遵循安装级恢复的隔离与对账要求。目录应挂载为目录供服务读取，避免单文件 bind mount 在原子替换后继续读取旧 inode。

## 交付与资格

[受控更新与退出证据](evidence/principal-updates-20261126.json)包含 16 项检查：启停与版本递增、重复更新、错误范围/版本、写入中断及重试、并发管理、事务竞争、CLI，以及原生 Docker Worker 的退出确认。原生 Worker 本次测得停止请求 3.241 秒、退出确认 4.623 秒；暂停退出操作时任务保持非终态，确认退出后保留原 Attempt、Session 和日志。这是单次 Linux 合成工作负载观测，部署传播目标需在目标环境测量。

K03-3.01 实现与开发部署完成。来源追溯、四组事务顺序矩阵及持久撤销证据共同覆盖该项；三层委派、替换会话和并发的[组合资格](delegation-qualification-35.md)已由 K03-3.02 完成，独立安全评审由 K03-3.03 承接。
