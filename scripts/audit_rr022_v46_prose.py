"""Verify the prose-only revision and render contact sheets for manual review."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
from PIL import Image, ImageDraw
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
TEX = ROOT / 'docs/paper/latex'
OUT = ROOT / 'output/pdf'
NAME = 'E2AG-paper-authors-v1.1.46-working-20261008'
QA = ROOT / 'outputs/qa-v1.1.46-prose'
QA.mkdir(parents=True, exist_ok=True)
old = (TEX / 'e2ag-body-v1.1.45.tex').read_text(encoding='utf-8')
new = (TEX / 'e2ag-body-v1.1.46.tex').read_text(encoding='utf-8')
checks = {}
checks['numeric_tokens_unchanged'] = re.findall(r'\d+(?:\.\d+)*', old) == re.findall(r'\d+(?:\.\d+)*', new)
checks['citations_unchanged'] = re.findall(r'\\cite\{[^}]+\}', old) == re.findall(r'\\cite\{[^}]+\}', new)
checks['references_unchanged'] = old.split('\\begin{thebibliography}', 1)[-1] == new.split('\\begin{thebibliography}', 1)[-1]
for env in ['equation', 'align', 'algorithm', 'table']:
    pattern = r'\\begin\{' + env + r'\}.*?\\end\{' + env + r'\}'
    a, b = re.findall(pattern, old, re.S), re.findall(pattern, new, re.S)
    if env == 'table':
        a = [v.replace('路径 & 当前状态', '路径 & 治理方式').replace('使用原有运行时配置', '使用服务或聊天运行时配置') for v in a]
    checks[env + '_content_preserved'] = a == b
checks['figure_inputs_unchanged'] = re.findall(r'\\input\{[^}]+\}', old) == re.findall(r'\\input\{[^}]+\}', new)
checks['no_revision_narrative_patterns'] = not re.search(r'本版|本轮|扩展前后|扩展验证另设|最初采用|修复后|早期消融|加入参数约束|增加 \$K\$ 后', new)
log = (OUT / (NAME + '.log')).read_text(encoding='utf-8', errors='replace')
checks['no_unresolved_or_overfull'] = not re.search(r'undefined references|Reference .* undefined|Citation .* undefined|Overfull', log)
pdf = OUT / (NAME + '.pdf')
reader = PdfReader(pdf)
checks['page_count_21'] = len(reader.pages) == 21
checks['no_blank_pages'] = all(len(p.extract_text().strip()) > 30 for p in reader.pages)
subprocess.run(['pdftoppm', '-r', '65', '-png', str(pdf), str(QA / 'page')], check=True, capture_output=True)
pages = sorted(QA.glob('page-*.png'))
for offset in range(0, len(pages), 6):
    sheet = Image.new('RGB', (1200, 3 * 865), 'white')
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(pages[offset:offset + 6]):
        im = Image.open(path).convert('RGB')
        im.thumbnail((580, 825))
        x, y = (i % 2) * 600, (i // 2) * 865
        sheet.paste(im, (x + 10, y + 30))
        draw.text((x + 10, y + 10), f'Page {offset + i + 1}', fill='black')
    sheet.save(QA / f'contact-{offset // 6 + 1}.png')
report = {'checks': checks, 'pages': len(reader.pages), 'sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(), 'visual_review': 'pending'}
(QA / 'automated-checks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
assert all(checks.values()), checks
