"""Build editable SVG architecture figures (stdlib only)."""
from pathlib import Path
from html import escape

OUT = Path(__file__).resolve().parents[1] / "docs/architecture/diagrams"
OUT.mkdir(parents=True, exist_ok=True)
INK="#172d43"; MUTED="#476078"; LINE="#56748f"
def text(x,y,s,size=21,weight=400,color=INK):
    return f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(s)}</text>'
def box(x,y,w,h,title,lines=(),fill="#f1f6fa",size=22):
    s=f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{LINE}" stroke-width="1.5"/>'
    s+=text(x+22,y+36,title,size,650)
    for n,line in enumerate(lines):s+=text(x+22,y+69+n*29,line,19,400,MUTED)
    return s
def edge(d,label=None,x=0,y=0):
    s=f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="2.2" stroke-linejoin="round" marker-end="url(#arrow)"/>'
    if label:s+=text(x,y,label,18,500,MUTED)
    return s
def save(name,title,desc,h,body):
    svg=f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 {h}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>
<defs><marker id="arrow" viewBox="0 0 10 8" refX="10" refY="4" markerWidth="9" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 4 L0 8 Z" fill="{LINE}"/></marker></defs>
<rect width="1280" height="{h}" fill="white"/>
<g font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif">
{text(40,46,title,28,700)}{text(40,79,desc,18,400,MUTED)}{body}</g></svg>'''
    (OUT/name).write_text(svg+"\n",encoding="utf-8")

b=""
b+=box(40,120,850,160,"L3 · 开放业务应用",["业务工作台 / 行业流程 / 模板 / 业务指标","拥有：场景配置与业务对象映射"],"#f8fafc",25)
b+=box(40,355,850,170,"L2 · 数字员工管理与服务",["组织与身份 / 数字员工 / Workspace / WorkItem / 知识 / Studio","拥有：长期身份、业务协作、对象 ACL、发布与运营记录"],"#edf5f4",25)
b+=box(40,605,850,170,"L1 · DiOS 内核",["资源 / 调度 / Runtime / 执行授权 / 审批门禁 / 审计与恢复","拥有：Revision、Deployment、Run、资源策略与执行证据"],"#edf3fa",25)
b+=edge("M300 280 V355","服务契约",320,325)
b+=edge("M780 355 V280","工作状态",670,325)
b+=edge("M300 525 V605","受控调用",320,574)
b+=edge("M780 605 V525","运行与证据事件",630,574)
b+=box(1010,145,230,120,"业务系统",["CRM / ERP / 教学","主数据与事务"],size=22)
b+=box(1010,355,230,90,"企业身份源",["SSO / 目录"],size=22)
b+=box(1010,470,230,90,"知识 Provider",["源数据 ACL"],size=22)
b+=box(1010,635,230,120,"模型与工具",["模型 / MCP 等","外部动作与结果"],size=22)
b+=edge("M890 200 H1010","适配",920,187)
b+=edge("M890 400 H1010","身份",920,387)
b+=edge("M890 495 H1010","检索",920,482)
b+=edge("M890 690 H1010","调用",920,677)
b+=edge("M300 775 V865","任务 / 会话",320,830)
b+=edge("M780 865 V775","状态 / 产物 / 证据",590,830)
b+=box(40,865,1200,150,"开放执行生态 · 通过 L1 协议 Adapter 接入",["A2A · 任务委派     ACP（Agent Client Protocol）· 交互会话     厂商 SDK / API / CLI 适配","DiAgent / DeepSeek Harness（dsh）/ Cursor / Codex / Claude Code（CC）/ 其他 Agent"],"#f8fafc",24)
b+=text(40,1060,"Agent 自身提供 loop / harness；接入能力与治理范围逐项验证。Console、CLI 为 L1 运维入口。",20)
save("01-layers.svg","三层结构与开放智能体生态","目标架构 · 框表示责任域；箭头表示契约调用或标注的状态反馈。",1100,b)

b=""
b+=text(62,130,"L2 · 长期身份与工作",25,650)
b+=text(722,130,"L1 · 技术运行与证据",25,650)
b+=box(40,160,500,120,"DigitalEmployee",["长期身份、岗位与职责"],"#edf5f4")
b+=box(700,160,540,120,"AgentDefinition / Deployment",["部署引用固定 Revision"],"#edf3fa")
b+=box(40,380,500,120,"WorkItem",["分派、协作、复核与业务关闭"],"#edf5f4")
b+=box(700,380,540,120,"Run · 逻辑运行",["分工与业务修订可创建不同 Run"],"#edf3fa")
b+=box(700,600,540,145,"Attempt · 执行尝试",["重试保留 Run，创建新 Attempt","关联 Worker、Process、Session 与租约"],"#edf3fa")
b+=box(40,840,500,120,"Workspace",["共享文件、会话、成果可见性与保留"],"#edf5f4")
b+=box(700,840,540,120,"Artifact",["内容摘要、校验结果与执行关联"],"#edf3fa")
b+=edge("M540 218 H700","绑定 1 : N",563,200)
b+=edge("M540 438 H700","关联 1 : N",563,420)
b+=edge("M700 898 H540","发布引用",572,880)
b+=edge("M970 280 V380","派生 1 : N",990,338)
b+=edge("M970 500 V600","尝试 1 : N",990,558)
b+=edge("M970 745 V840","形成 1 : N",990,800)
b+=edge("M290 280 V380","参与工作",310,338)
b+=edge("M290 500 V840","工作上下文",310,690)
b+=text(40,1015,"Run 可独立于 WorkItem 存在；L1 校验运行结果，L2/L3 组织业务验收。",20)
save("02-ownership.svg","对象归属：身份、逻辑运行与执行尝试分别记录","目标概念模型 · 连线表示引用或派生；最终 Schema 与状态机由 #25 固定。",1050,b)

b=""
b+=box(40,125,330,135,"① L2 创建工作",["验证身份与对象 ACL","形成受限代理请求"],"#edf5f4")
b+=box(475,125,330,135,"② L1 创建并调度 Run",["验证代理链与资源范围","Attempt、Worker 租约"],"#edf3fa")
b+=box(910,125,330,135,"③ E2AG 决策与控制",["权限 / 策略 / 预算 / 审批","绑定动作、参数与有效期"],"#edf3fa")
b+=edge("M370 190 H475")
b+=edge("M805 190 H910")
b+=box(40,430,330,145,"拒绝",["返回明确原因与证据","L2 处理失败或人工接管"],"#fbf5f4")
b+=box(475,430,330,145,"等待审批",["L2 选择审批人并形成决议","批准后返回③，重新校验"],"#faf7ef")
b+=box(910,430,330,145,"④ 执行工具并留证",["使用受限凭据与资源","保存结果、产物与审计"],"#edf3fa")
b+=edge("M955 260 V310 Q955 320 945 320 H215 Q205 320 205 330 V430","拒绝",215,366)
b+=edge("M1015 260 V365 Q1015 375 1005 375 H650 Q640 375 640 385 V430","需审批",660,402)
b+=edge("M1140 260 V430","允许",1157,350)
b+=box(910,670,330,130,"⑤ L2 业务复核",["接受、修订或人工接管","按业务规则关闭 WorkItem"],"#edf5f4")
b+=edge("M1075 575 V670","运行结果",1090,631)
b+=text(40,650,"权限撤销或参数变化：后续动作重新评估。",22,650)
b+=text(40,696,"知识读取：Provider / 源系统执行数据 ACL。",20)
b+=text(40,737,"结果未知：进入对账；重试遵循动作幂等语义。",20)
b+=text(40,778,"运行成功与业务验收分别记录。",20)
save("03-execution.svg","受管执行：在实际控制点实施授权","目标流程 · 适用于已接入工具网关、沙箱或执行钩子的 Agent；远端委派按能力治理。",845,b)

b=""
b+=box(40,125,470,630,"初期 · 模块化开发验证",[],"#fafcfe",25)
b+=box(65,205,420,105,"应用与数字员工服务",["同仓库 / 可同进程"])
b+=box(65,365,420,105,"DiOS 内核与 Worker",["持久任务、授权、隔离"])
b+=box(65,530,420,135,"持久数据与备份",["数据库 / 任务状态 / 产物","明确路径与恢复边界"])
b+=edge("M275 310 V365")
b+=edge("M275 470 V530")
b+=box(560,125,680,630,"企业 · 多节点与高可用",[],"#fafcfe",25)
b+=box(590,205,620,105,"应用 / 服务 / 内核 API 多副本",["共享授权状态；负载分配与独立故障恢复"])
b+=box(590,385,250,105,"持久队列与租约",["调度与状态协调"])
b+=box(920,385,290,105,"Worker 池",["按负载与隔离扩展"])
b+=edge("M715 310 V385","调度",730,356)
b+=edge("M840 437 H920")
b+=box(590,570,620,135,"共享持久服务与依赖",["数据库 / 对象存储 / Secret / 身份源","各自的复制、备份、故障切换与恢复目标"])
b+=edge("M715 490 V570")
b+=edge("M1065 490 V570")
b+=text(40,807,"验收：容量、排队时延、恢复时间、数据恢复点、滚动升级与单位工作成本。",21)
b+=text(40,844,"图示仅保留部署主关系；API 也访问状态服务。具体拓扑与选型由容量和故障模型确定。",18,400,MUTED)
save("04-deployment.svg","部署演进：逻辑边界稳定，运行规模可扩展","目标部署 · 单机用于起步验证；企业形态同时覆盖计算节点和共享依赖的故障域。",880,b)
b=""
b+=box(40,120,1200,400,"L1 · DiOS 运行治理与智能体接入",[],"#edf3fa",26)
b+=box(70,195,1140,95,"统一接入契约",["身份与 E2AG 策略 / 能力声明 / Run、Attempt ↔ Task、Session / 状态、产物与证据"],"#ffffff",23)
b+=box(70,365,320,110,"A2A Adapter",["发现与任务委派"],"#ffffff",23)
b+=box(480,365,320,110,"ACP Client / Adapter",["交互会话与权限请求"],"#ffffff",23)
b+=box(890,365,320,110,"厂商 Adapter",["SDK / API / CLI 映射"],"#ffffff",23)
for x in (230,640,1050): b+=edge(f"M{x} 290 V365")
b+=box(40,655,1200,180,"Agent 自身的 loop / harness",["DiAgent / DeepSeek Harness（dsh）/ Cursor / Codex / Claude Code（CC）/ 其他 Agent","推理与规划 · 上下文与会话 · 工具调用循环 · Agent 内部扩展","候选目标按实际接口选择接入路径；每个实现独立声明协议、版本与治理能力。"],"#edf5f4",25)
for x in (230,640,1050): b+=edge(f"M{x} 475 V655")
b+=text(70,573,"Agent2Agent",19,500)
b+=text(660,573,"Agent Client Protocol",19,500)
b+=text(910,573,"实现专属接口",19,500)
b+=text(40,895,"托管执行：Runtime Adapter 管生命周期，网关 / 沙箱 / 执行钩子落实动作约束。",21,600)
b+=text(40,940,"远端委派：DiOS 管入口授权、数据外发与结果；内部动作控制按端点能力验证。",21,600)
b+=text(40,985,"接入协议与可控制范围分别登记。MCP 提供工具与数据，A2A / ACP 连接智能体。",20)
save("05-agent-interop.svg","智能体接入：治理与 loop / harness 分离","目标架构 · 上方为 DiOS 责任域，下方为智能体执行域；箭头表示适配调用。",1030,b)
b=""
b+=box(40,125,1200,125,"Agent / Harness 与接入 Adapter",["携带主体、Run / Attempt / Session、资源与动作参数；原生工具及 MCP 按控制点接入"],"#edf5f4",25)
b+=box(40,335,700,180,"PEP · E2AG 执行控制点",["MCP 网关 / 原生工具沙箱 / 可信执行钩子","允许、拒绝或阻止等待审批","动作前保存意图；限制凭据、挂载与网络"],"#edf3fa",25)
b+=box(835,335,405,180,"PDP · E2AG 策略决策",["主体与委派权限交集","策略版本、epoch、参数与有效期","审批、续期与撤销"],"#faf7ef",24)
b+=edge("M390 250 V335","动作请求",410,300)
b+=edge("M740 400 H835","请求",763,385)
b+=edge("M835 475 H740","决议",763,460)
b+=box(40,680,700,125,"工具与资源",["文件 / Shell / 网络 / MCP 工具 / 业务系统"],"#f8fafc",25)
b+=box(835,680,405,125,"运行证据",["决议、意图、结果与校验","关联运行、主体、版本与产物"],"#f8fafc",24)
b+=edge("M390 515 V680","允许后执行",410,610)
b+=edge("M690 515 V595 H1035 V680","控制证据",765,579)
b+=edge("M740 745 H835","结果",763,729)
b+=text(40,870,"扩展方向：原生工具控制、长任务续期、委派约束、远端治理与性能提升。",22,600)
b+=text(40,915,"准入依据：实际拒绝 / 审批 / 撤销 / 恢复证据；能力缺口进入对应实现与联合验收。",20)
save("07-e2ag-control.svg","E2AG：策略决策、实际控制点与运行证据","目标控制结构 · L2 提供组织授权与审批规则；L1 执行约束，Agent 提供 loop / harness。",960,b)
print("Generated 6 SVG figures; figure 06 is maintained separately")
