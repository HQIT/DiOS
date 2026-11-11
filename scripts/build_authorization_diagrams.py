"""Build editable vector authorization and revocation lifecycle figure."""
from pathlib import Path
from html import escape
R=Path(__file__).resolve().parents[1]
svg=['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1320 680" role="img" aria-labelledby="title desc"><title id="title">授权与撤销的生命周期</title><desc id="desc">授权路径从准入到工作授权、执行 Grant 和动作决策；撤销路径从代次提交到缓存失效、执行阻断和确认。</desc><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10Z" fill="#587282"/></marker></defs><rect width="1320" height="680" fill="white"/>']
def text(x,y,s,size=20,color='#173348',weight=400):
    svg.append(f'<text x="{x}" y="{y}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(s)}</text>')
text(40,55,'授权持续收窄，动作始终绑定当前所有者',29,weight=600)
for y,label,fill,items in [(145,'授权路径','#edf7f6',[('一次准入','900 秒内兑换','同事务创建 Run'),('工作授权','默认截止 24 小时','保留委托与预算'),('执行 Grant','900 秒 / 300 秒续期','绑定会话和租约'),('动作决策','参数、范围与审批','持久意图后派发')]),(415,'撤销路径','#f5f1ec',[('提交撤销','递增授权代次','记录生效截止时间'),('失效传播','缓存最长 2 秒','派发核验当前代次'),('执行阻断','停止新动作','取消与对账在途动作'),('控制点确认','生效目标 P99 ≤ 5 秒','远端确认独立记录')])]:
    text(40,y-25,label,22,weight=600)
    for i,(title,a,b) in enumerate(items):
        x=40+i*320
        svg.append(f'<rect x="{x}" y="{y}" width="280" height="150" rx="10" fill="{fill}" stroke="#b7cbd2"/>')
        text(x+18,y+40,title,24,weight=600);text(x+18,y+80,a);text(x+18,y+116,b)
        if i<3:svg.append(f'<path d="M{x+280} {y+75} H{x+312}" stroke="#587282" stroke-width="2" marker-end="url(#arrow)"/>')
text(40,345,'权限变化重新校验；新 Attempt、Session 或 epoch 使用新 Grant。',21)
text(40,630,'已派发动作保留现场和结果身份；未确认控制点阻断新动作。',21)
svg.append('</svg>')
p=R/'docs/design/diagrams/authorization-lifecycle.svg';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(''.join(svg)+'\n',encoding='utf-8')
