"""Build the detailed Run lifecycle view as editable SVG."""
from pathlib import Path
from html import escape
ROOT=Path(__file__).resolve().parents[1]
def label(x,y,s,size=20,bold=False):return f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{650 if bold else 400}" fill="#172d43">{escape(s)}</text>'
def box(x,y,name,meaning):return f'<rect x="{x}" y="{y}" width="250" height="120" rx="10" fill="#edf3fa" stroke="#56748f"/>'+label(x+18,y+38,name,16 if len(name)>20 else 23,True)+label(x+18,y+78,meaning,18)
def edge(d):return f'<path d="{d}" fill="none" stroke="#56748f" stroke-width="2.2" marker-end="url(#arrow)"/>'
parts=[label(40,48,'Run：运行终态由结果校验决定',28,True),label(40,85,'主路径与恢复关系 · 完整状态及门槛由 states.json 定义',18)]
for x,n,m in [(40,'queued','等待授权与资源'),(355,'executing','Attempt 持有租约'),(670,'validating','完整性与必要证据'),(985,'succeeded','技术验收通过')]:parts.append(box(x,160,n,m))
for x in [290,605,920]:parts.append(edge(f'M{x} 220 H{x+65}'))
parts += [box(40,450,'retry_wait','旧尝试关闭、退避到期'),box(355,450,'reconciliation_required','外部结果待核对'),box(670,450,'needs_verification','等待补充证据')]
parts += [edge('M480 280 V450'),label(495,370,'结果未知',18),edge('M355 510 H290'),label(294,494,'已核对',17),edge('M165 450 V280'),label(185,370,'允许重试',18),edge('M730 280 V450'),label(742,410,'证据不足',18),edge('M880 450 V280'),label(895,335,'补充校验',18),label(40,635,'查询超时仅改变观测新鲜度；取消确认、失败与业务验收独立记录。',21)]
svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 680" role="img" aria-labelledby="title desc"><title id="title">Run 状态与恢复</title><desc id="desc">排队、执行、校验、技术成功；结果未知先对账，证据不足等待验证。</desc><defs><marker id="arrow" viewBox="0 0 10 8" refX="10" refY="4" markerWidth="9" markerHeight="7" orient="auto"><path d="M0 0 L10 4 L0 8Z" fill="#56748f"/></marker></defs><rect width="1280" height="680" fill="white"/><g font-family="Microsoft YaHei, sans-serif">'+''.join(parts)+'</g></svg>'
out=ROOT/'docs/design/diagrams';out.mkdir(exist_ok=True);(out/'run-lifecycle.svg').write_text(svg+'\n',encoding='utf-8')
