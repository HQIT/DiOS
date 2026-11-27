# 跨主机身份与对象权限验证

同一份内容可以被多个对象版本复用，每个对象仍按自身 ACL 授权。读取与工具执行分别在实际边界重检身份、版本和撤销状态。

## 验证路径

控制端运行真实 HTTPS API、mTLS 工具网关及合成 MCP 文件读取服务。客户端是独立 Linux 进程，可在另一台主机经既有 SSH 转发访问；SSH 只承载测试连接，API 身份、TLS 证书和 E2AG 仍由 DiOS 校验。

| 场景 | 实际断言 |
| --- | --- |
| 同摘要不同 ACL | 两个对象共用内容摘要；主体各自成功下载自己的对象，读取另一对象返回 404 |
| ACL 与令牌撤销 | 撤销对象授权后下载拒绝；关闭或过期令牌后实际 HTTP 请求返回 401 |
| 证书与接收方 | 错误服务名的 TLS 握手失败；工具通道拒绝不匹配证书、错误令牌、过期凭据与错误 audience |
| 工具最终执行点 | 已批准调用成功读取一次；调用前或 MCP 初始化期间撤销机器身份后，工具端调用数保持一次 |
| 工作撤销 | E2AG Work 撤销后，原远程工具通道拒绝访问 |
| 凭据保护 | 记录 TLS 协议与加密套件；原始加密流量、客户端输出及工具产物均检查凭据未泄漏 |

测试以合成活动 Attempt 建立工具调用上下文，不启动客户任务或调用模型。客户端与控制端 Linux boot 摘要必须不同，双机模式才能通过；`--local` 只确认独立进程测试入口可用。

## 请求取消与连接回收

真实 HTTPS 下载在请求结束时暴露 AnyIO 重复取消对数据库清理的影响。旧实现仅使用 `asyncio.shield`，后续回滚或关闭仍可能再次被取消。按照 [AnyIO 资源清理要求](https://anyio.readthedocs.io/en/stable/cancellation.html#finalization)，驱动操作收尾、回滚和连接关闭置于取消屏蔽作用域，随后继续传播原取消。

回归断言包括取消读取后释放锁、取消写入后不提交、取消正在执行的 SQLite 驱动操作后正确回收，以及连接池无异常日志、后续写入可用。PostgreSQL 同时覆盖查询执行期间取消。SQLAlchemy 2.0.55 已修复重复取消后连接池复用已关闭连接的问题，2.0.58 修正成功关闭时的异常日志；依赖固定为 2.0.58，沿用[上游修复](https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html)。原授权检查、事务准入及取消结果保持生效。

## 执行

```bash
backend/.venv/bin/python scripts/qualify_identity_access.py \
  --local --output workspace/identity-access-local

backend/.venv/bin/python scripts/qualify_identity_access.py \
  --ssh-config /path/to/private/ssh_config --host physical-worker \
  --output workspace/identity-access-two-hosts
```

输出目录必须是新的仓库 workspace 子目录。远端只需 Python 3 和已核验主机密钥的 SSH；测试凭据通过标准输入投递，临时证书文件权限为 0600，结束后删除。控制端测试材料保存在权限为 0700 的独立目录，原服务与历史不变。

## 当前结果

本地独立进程的 HTTPS、mTLS、实际 MCP 读取与撤销验证通过。第二台物理主机当前无法经 Tailscale/SSH 连接，K03-1.03 保持待验证；整体 WBS 完成数保持 60/79。对应[工程证据](evidence/identity-access-20261127.json)分别记录已通过断言和双机缺口。

证书绑定依据 [RFC 8705](https://www.rfc-editor.org/rfc/rfc8705.html)，逐请求授权与拒绝规则参考 [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)。
