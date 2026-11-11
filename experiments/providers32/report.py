from pathlib import Path
import json,statistics,hashlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
R=Path(__file__).resolve().parents[2]
E=R/'docs/design/evidence'
d=json.loads((E/'provider-summary-20261111.json').read_text());groups=d['groups']
names={'pg-workspace':'PG / workspace','pg-row':'PG / run','mysql-row':'MySQL','etcd-cas':'etcd CAS','sqlite-wal':'SQLite WAL','lmdb-txn':'LMDB'}
cn={'pg-workspace':'PG Workspace 串行','pg-row':'PG 按 Run 加锁','mysql-row':'MySQL 行级事务','etcd-cas':'etcd CAS','sqlite-wal':'SQLite WAL 服务','lmdb-txn':'LMDB 事务服务'}
colors=['#94a3b8','#2563eb','#0891b2','#7c3aed','#ea580c','#059669']
def get(kind,w='uniform',confirm=False,arrival='closed',rate=None,c=32):
    return next(x for x in groups if x['kind']==kind and x['workload']==w and x['confirmation']==confirm and x['concurrency']==c and x['arrival']==arrival and x['offered_rate']==rate)
def num(x):return f'{x:,.1f}'
def span(g,key):
    x=g[key];return f"{num(x['median'])}（{num(x['min'])}–{num(x['max'])}）"
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
fig,axes=plt.subplots(2,2,figsize=(13,8),layout='constrained')
for ax,w,metric,title in [(axes[0,0],'uniform','throughput','Independent runs · committed intents/s'),(axes[0,1],'hotspot','throughput','One hot run · committed intents/s'),(axes[1,0],'uniform','p99_ms','Independent runs · P99 latency (ms)'),(axes[1,1],'hotspot','p99_ms','One hot run · P99 latency (ms)')]:
    rows=[get(k,w) for k in names];med=[v[metric]['median'] for v in rows]
    errors=[[v[metric]['median']-v[metric]['min'] for v in rows],[v[metric]['max']-v[metric]['median'] for v in rows]]
    ax.bar(range(6),med,color=colors,width=.64,yerr=errors,capsize=4)
    ax.set_xticks(range(6),list(names.values()),rotation=20,ha='right');ax.set_title(title,loc='left',fontweight='bold');ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    if metric=='p99_ms':ax.set_yscale('log')
    else:ax.set_ylim(bottom=0)
fig.suptitle('DiOS #32 · Same intent-commit contract, six implementations\n32 clients · 2 CPU / 1 GiB per service · synchronous local persistence',fontsize=15,fontweight='bold')
fig.supxlabel('Three 5-second rounds: median and min/max (not confidence intervals). Gateway/driver costs are included.',fontsize=10)
F=R/'docs/design/diagrams';F.mkdir(exist_ok=True)
fig.savefig(F/'provider-comparison-20261111.svg');p=F/'provider-comparison-20261111.svg';p.write_text('\n'.join(x.rstrip() for x in p.read_text(encoding='utf-8').splitlines())+'\n',encoding='utf-8',newline='\n');fig.savefig(F/'provider-comparison-20261111.png',dpi=150);plt.close(fig)
table='| 实现 | 独立任务提交/秒 | 同 Run 提交/秒 | 独立任务 P99 ms | 测量结束内存 MiB |\n| --- | ---: | ---: | ---: | ---: |\n'
for k in names:
    g=get(k);table+=f"| {cn[k]} | {span(g,'throughput')} | {span(get(k,'hotspot'),'throughput')} | {span(g,'p99_ms')} | {span(g,'memory_end_mib')} |\n"
confirm='| 实现 | 负载 | 提交/秒 | P99 ms |\n| --- | --- | ---: | ---: |\n'
for k in ['pg-row','sqlite-wal','lmdb-txn']:
    for w in ['uniform','hotspot']:
        g=get(k,w,True);confirm+=f"| {cn[k]} | {'64 个独立 Run' if w=='uniform' else '同一个 Run'} | {span(g,'throughput')} | {span(g,'p99_ms')} |\n"
arrival='| 实现 | 到达/秒 | 窗口内提交/秒 | P99 ms | 丢弃/总到达（三轮） |\n| --- | ---: | ---: | ---: | ---: |\n'
for rate in [25,100,500]:
    for k in (['sqlite-wal','lmdb-txn'] if rate==25 else list(names)):
        g=get(k,arrival='open',rate=rate)
        arrival+=f"| {cn[k]} | {rate} | {num(g['throughput']['median'])} | {span(g,'p99_ms')} | {g['dropped']}/{g['arrivals']} |\n"
checks=json.loads((E/'provider-correctness-20261111.json').read_text());reg=json.loads((E/'provider-runtime-regression-20261111.json').read_text())
correct='| 实现 | 通过检查 | 杀进程至重新可读（秒） |\n| --- | ---: | ---: |\n'
for x in checks:correct+=f"| {cn[x['kind']]} | {x['passed']}/12 | {x['recovery_seconds']} |\n"
doc=f'''# #32：运行时存储与实现比较

日期：2026-11-11 · 范围：单节点意图提交与恢复 · #32 保持进行中。

## 决策

DiOS 的内核契约不要求关系型数据库。下一步产品化采用两条实现路线：**轻量控制服务采用嵌入式事务存储，SQLite WAL 与 LMDB 进入写入队列及混合查询复测；共享事务服务以 PostgreSQL 按对象并发为首选验证实现**。LMDB 已证明非 SQL 事务 KV 能实现本轮相同的原子状态/证据契约。轻量默认后端保留待定。生产接管仍需完整授权、业务接入、长期容量、备份恢复及部署档位验收。

SQLite 的轻载性能、内置查询与历史工具复用有利于快速产品化；LMDB 在本轮 100/s 到达负载中的尾延迟更稳定。SQLite 相同负载的一轮 P99 达到约 1.94 秒，默认实现冻结前需要解决排队问题并加入查询对照。PG 共享事务路线依据独立 Run 的提交吞吐与现有事务生态进入正式实现。上述取舍限于本轮实现与工作负载。MySQL/etcd 保留对照证据，FoundationDB/TiDB 的分布式能力待多节点阶段评估。

轻量部署可以由一个控制服务管理跨主机 Worker。Worker 经 API 接入，数据库文件仅由控制服务所在主机访问；控制服务的多副本写入、仲裁和自动故障切换属于另一组验收。数据库类型不决定整个 Agent OS 的部署上限。

## 统一条件与结果

所有候选使用同一状态机、身份和请求内容：校验 Run epoch、授权 epoch、撤销、动作/资源和审批单次消费，将新状态与不可变意图记录原子持久化后确认。重放返回原身份，变更参数冲突，工具结果保持 unknown。每个服务限额 2 CPU/1 GiB，使用同一 Docker 存储上的独立卷、单节点同步持久提交；客户端固定两个 WSL 逻辑 CPU、32 个连接/在途槽。环境版本及镜像摘要见 [环境记录](evidence/provider-environment-20261111.json)。

共 **{d['cases']} 个性能用例，{d['errors']} 个提交错误**；每个用例均逐条验证证据链、请求摘要、作用域、epoch、审批与撤销顺序。132 组服务式筛选、36 组嵌入式核心筛选、12 组轻载到达补充、18 组较长窗口复核。下表为 32 并发闭环、每轮 5 秒、3 轮的中位数及最小–最大值；区间表示轮间波动。

{table}

![六种实现的吞吐和尾延迟](diagrams/provider-comparison-20261111.svg)

内存是用例结束时容器计数，包含缓存和此前实验数据，不能作为最低内存配置。筛选每次创建新命名空间，底层数据逐渐累积；不同候选的索引、日志和缓存机制是实现成本的一部分。CPU 计量区分客户端与服务端。Docker 磁盘计数不支持完整的持久写放大分析，本轮不据此给出磁盘或货币成本排名。

SQLite/LMDB 使用相同的轻量 TCP/JSON 服务封装，事务逻辑在服务内执行；PG/MySQL/etcd 由客户端完成读取、计算和条件提交。结果比较的是完整实现路径，包含协议、驱动、进程位置与往返次数。SQLite/LMDB 两者的封装一致，PG 两者仅改变 Workspace/Run 冲突范围。

### 15 秒窗口复核

每个用例之前清空隔离实验库中的逻辑数据，再进行同样的 2 秒并发预热；引擎物理文件和缓存保留。三轮随机顺序，仍为 32 并发。

{confirm}

SQLite 100/s 到达负载的异常轮中，服务锁等待 P99 为 1933.41 ms，提交落盘 P99 为 26.28 ms，连接池等待 P99 为 0.0068 ms；这些独立分位数不相加。下一步优先验证公平有界写队列。

同 Run 热点暴露出锁竞争、CAS 重试及排队长尾。无关 Run 应按对象并发；同一冲突域应使用有界、公平队列与背压。增加连接数不会解除单个对象或单写事务的顺序约束。批提交可作为后续实现实验，每个动作必须等该批同步落盘后才能转发。

### 开放到达与轻量运行区间

开放负载从计划到达时刻计时，包含队列等待；队列上限 256，饱和丢弃单列。吞吐只计测量窗口内完成，延迟包含窗口结束后排空的请求。以下同样为三轮统计。

{arrival}

已有 [两档容量假设](capacity-slo-v1.md) 给出峰值事件、审计和续期合计约 1.34/14.53 次/秒；这些为设计估算。本实验的意图事务是持久路径探针，尚未覆盖管理查询、完整授权与所有事件类型，因此按实际混合业务复测后才能验收这两档容量。大日志和产物按独立文件/对象存储归档，查询投影可重建，避免扩大权威事务库。

## 一致性与故障证据

{correct}

六种实现合计 {sum(x['passed'] for x in checks)} 项通过：32 路重复提交与参数冲突、审批争抢、撤销后拒绝、旧/当前 epoch、越界资源、提交故障无半状态、响应丢失重放、独立证据验证、实际服务 SIGKILL 后完整已确认集合恢复、服务暂停与查询超时后按身份对账。恢复计时包含连接和应用初始化，仅为单次进程故障样本。

SQL/嵌入式故障注入在写入后、事务提交前触发回滚；etcd 注入在原子请求前。暂停服务模拟不可达，不等同于真实跨节点网络分区。客户端就绪以真实请求成功为准；TCP 连接或客户端对象创建不能单独作为可用性证据。

现有后端回归 **{reg['tests_run']} 项通过、{reg['skipped']} 项跳过**，见 [回归记录](evidence/provider-runtime-regression-20261111.json)。独立证据验证器另有 7 个测试，覆盖重新计算哈希后的越权、旧 epoch、重复审批、伪造成功和截断链。当前用例不执行模型或业务工具，完整 PEP、跨 Run 授权根与真实 Harness 留给 #30/#34 联合验收。

## 分层与交付边界

| 职责 | 轻量控制服务 | 多节点治理服务 |
| --- | --- | --- |
| 任务、租约、幂等、审批与意图 | 同一嵌入式事务提交点；SQLite WAL/LMDB 按相同接口复测 | 共享事务 Provider；先产品化 PG 对象粒度实现 |
| 事件与证据 | 追加记录、小链头、事务 outbox | 分区追加、复制及保留策略；保持唯一权威提交点 |
| 日志、报告及大产物 | 文件/对象 Provider，按字节校验 | 对象服务、生命周期、权限与可靠复制 |
| 管理查询 | SQLite 查询或可重建投影 | 独立读模型/分析服务，明确水位和新鲜度 |
| Worker | 跨主机 API 接入，进程与 Session 独立观测 | 同一协议及身份契约，按资源和授权调度 |
| 恢复 | 本机事务恢复、备份恢复、明确单控制服务故障域 | 仲裁、旧主隔离、同步复制、故障切换及灾备验收 |

etcd 的事务/CAS 能表达本轮治理状态操作，但作为独立协调服务仍需部署维护；大证据及复杂管理查询需要其他数据路径。组合后端只有在实际需求与收益成立时引入。自建追加日志引擎还需自行承担索引、检查点、损坏检测、恢复和复制，当前优先复用成熟事务存储。

## 版本、许可与运维

SQLite 采用 3.51.3，包含官方列出的 WAL-reset 修复；原有参考实现所链接的 SQLite 版本须在产品化时做同样预检。[SQLite WAL](https://www.sqlite.org/wal.html)。SQLite 无独立数据库服务，WAL 文件限同主机访问；跨主机访问通过控制服务 API。[适用场景](https://www.sqlite.org/whentouse.html)。

LMDB 0.9.33 / py-lmdb 1.7.5 使用同步事务，单写者、并发读者；2 GiB 是虚拟映射上限，容器居民内存仍受限于 1 GiB。[LMDB 文档](https://lmdb.readthedocs.io/en/release/)、[锁定版本及 OLDAP-2.8 许可](https://pypi.org/project/lmdb/1.7.5/)。SQLite 公共领域声明与 APSW 封装许可分别纳入分发清单。[SQLite](https://www.sqlite.org/copyright.html)、[APSW](https://pypi.org/project/apsw/3.51.3.0/)。

PG 16.13 使用 fsync/synchronous_commit；MySQL 8.0.46 使用 InnoDB、flush_log_at_trx_commit=1、sync_binlog=1，并在会话设置 READ COMMITTED；etcd 3.6.5 使用原生 gRPC、同步本地持久化与条件事务。[PG 持久配置](https://www.postgresql.org/docs/16/runtime-config-wal.html)、[MySQL 持久配置](https://dev.mysql.com/doc/refman/8.0/en/innodb-parameters.html)、[etcd 保证](https://etcd.io/docs/v3.6/learning/api_guarantees/)。PG、MySQL 与 etcd 的许可及商业分发方式按各自依赖清单评审。[PG](https://www.postgresql.org/about/licence/)、[MySQL](https://www.mysql.com/about/legal/)、[etcd](https://github.com/etcd-io/etcd/blob/v3.6.5/LICENSE)。

## 下一步与剩余门槛

**#30/#32 下一步先把实验中的原子操作接口接入正式 Runtime：轻量 SQLite 有界写入队列，对象粒度 PG 并发，保留同一任务/尝试/会话身份及恢复契约。** 补齐满队列背压、授权根跨对象排序、升级排空、备份/恢复、历史关联与 API 投影；高频日志和大产物保持独立路径。SQLite 与 LMDB 的轻量默认选择由公平写入、查询索引成本和恢复验证共同决定。

#34 集成完整 E2AG/PEP、原生工具限制及真实 Agent；#54 承接多节点、长时间混合负载、磁盘满/写失败、主机掉电与分区、备份恢复和单位成本。单节点同步提交与进程崩溃测试尚不能验收复制 RPO、HA 或企业 SLO。G2 独立评审仍待安排，#32 的历史迁移、断网 spool 与业务切换工作继续进行。

完整数据见 [汇总及 SHA-256 输入清单](evidence/provider-summary-20261111.json)、[一致性记录](evidence/provider-correctness-20261111.json)。[实验代码与复现说明](../../experiments/providers32/README.md) 保留原始逐轮数据；冷连接试跑单独保存为 pilot 并排除统计。现有评审业务、活跃任务与历史库保持原有运行状态。
'''
(R/'docs/design/provider-selection-32.md').write_text(doc,encoding='utf-8',newline='\n')
print(json.dumps({'cases':d['cases'],'errors':d['errors'],'correctness':sum(x['passed'] for x in checks),'regression':reg['tests_run']}))
