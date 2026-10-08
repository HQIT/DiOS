"""Render publication tables directly from the verified measurement summary."""
import argparse
import json
from pathlib import Path


def render(data, output):
    if not data['valid'] or data['cells'] != 40:
        raise ValueError('incomplete measurement')
    business, stages = [], []
    for g in data['groups']:
        name = 'issue' if g['kind']=='issue' else 'PR'
        prefix = f"{name} & {g['concurrency']} & {g['mode']}"
        business.append(prefix + f" & {g['p50_ms']['median']:.2f} & {g['p95_ms']['median']:.2f} & {g['completion_per_second']['median']:.2f} & {g['attempts']} " + r'\\')
        values = [g['stage_p50_ms'][key]['median'] for key in ['admission_route','pre_upstream_ms','upstream_ms','post_upstream_ms']]
        stages.append(prefix + ' & ' + ' & '.join(f'{v:.2f}' for v in values) + r' \\')
    for name, caption, label, header, rows in [
        ('business','同路径对照的端到端延迟与业务完成情况','tab:overhead-business-rr022',
         r'业务 & 并发数 & 配置 & p50/ms & p95/ms & 完成速率/(次/s) & 请求数 \\',business),
        ('stages','准入及工具调用的分阶段墙钟时间（ms）','tab:overhead-stages-rr022',
         r'业务 & 并发数 & 配置 & 准入路径 & 调用前处理 & 工具往返 & 调用后处理 \\',stages)]:
        text = '\n'.join([r'\begin{table}[!htb]',r'\centering',r'\caption{'+caption+'}',r'\label{'+label+'}',
                          r'\small',r'\begin{tabular}{llrrrrr}',r'\toprule',header,r'\midrule',*rows,
                          r'\bottomrule',r'\end{tabular}',r'\end{table}',''])
        with (output/f'tab-overhead-{name}-v1.1.47.tex').open('x',encoding='utf-8') as f:
            f.write(text)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('summary',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    render(json.loads(args.summary.read_text(encoding='utf-8')),args.output)
