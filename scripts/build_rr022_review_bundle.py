"""Render all eight response drafts and join immutable v1.1.43 PDFs.

Internal review only. No submission, Word-tracking claim, or experiment.
Requires bundled Python with pypdf. Existing outputs are refused.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf"
SOURCE = ROOT / "docs/paper/revision-rr022/response-to-reviewers.md"
TEXDIR = ROOT / "docs/paper/latex"
PARTS = [
    ("累计修改稿：相对已提交v1.1.39", OUT / "E2AG-paper-authors-v1.1.43-marked-from-v1.1.39-20261007.pdf",
     "f8be3289717312e74cfac6ffc09f8decfabe3359b723f18e7f2355e5cdf1a9b1"),
    ("清洁工作稿：v1.1.43", OUT / "E2AG-paper-authors-v1.1.43-working-20261007.pdf",
     "128d696ffe42966123a8aa1c3ebc69de3615faed33d804677f75b905026eae02"),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tex(text: str) -> str:
    mapping = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%",
               "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
               "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(mapping.get(ch, ch) for ch in text.replace(chr(96), ""))


def main() -> None:
    response_output = OUT / "E2AG-rr022-responses-v1.1.43-draft-20261007.pdf"
    bundle_output = OUT / "E2AG-rr022-review-bundle-v1.1.43-draft-20261007.pdf"
    manifest_output = ROOT / "docs/paper/revision-rr022/bundle-v1.1.43-manifest-20261007.json"
    if any(p.exists() for p in (response_output, bundle_output, manifest_output)):
        raise FileExistsError("Versioned outputs already exist; do not overwrite")
    for _, path, expected in PARTS:
        if digest(path) != expected:
            raise ValueError(f"Unexpected input PDF hash: {path.name}")
    baseline = OUT / "E2AG-paper-authors-v1.1.39-20260817.pdf"
    baseline_hash = digest(baseline)
    if baseline_hash != "1a9228c691bf4e89b4b2e2d61ec47c9efbad822c113154bb17acbde7476b82be":
        raise ValueError("Submitted baseline changed")

    raw = SOURCE.read_text(encoding="utf-8")
    sections = re.findall(r"^### (R[12]\.[1-4]) ([^\n]+)\n(.*?)(?=^### |^## |\Z)",
                          raw, re.M | re.S)
    expected_ids = [f"R{r}.{i}" for r in (1, 2) for i in range(1, 5)]
    if [s[0] for s in sections] != expected_ids:
        raise ValueError("Must contain exactly eight ordered reviewer responses")
    body, expected_paragraphs, paragraph_counts = [], [], {}
    for key, title, content in sections:
        quote = re.search(r"^> (.+)$", content, re.M)
        answer = re.search(r"作者答复（供作者确认，未提交）：(.*?)\n\n修改位置与证据：", content, re.S)
        location = re.search(r"修改位置与证据：(.+)", content)
        if not quote or not answer or not location:
            raise ValueError(f"Incomplete response fields: {key}")
        # Internal filenames stay in the Markdown evidence ledger.
        page_location = location[1].split("。证据：", 1)[0]
        paragraphs = [p.strip() for p in answer[1].split("\n\n") if p.strip()]
        paragraph_counts[key] = len(paragraphs)
        expected_paragraphs.extend([quote[1], *paragraphs, page_location])
        body += [
            rf"\section*{{{tex(key)}\quad {tex(title)}}}",
            rf"\noindent\textbf{{审稿意见：}}{tex(quote[1])}\par",
            rf"\medskip\noindent\textbf{{作者答复：}}{tex(paragraphs[0])}\par",
            *[rf"\smallskip {tex(p)}\par" for p in paragraphs[1:]],
            rf"\medskip\noindent\textbf{{修改位置：}}{tex(page_location)}。\par",
        ]
    preamble = r"""\documentclass[UTF8,zihao=5,fontset=none]{ctexart}
\usepackage{jos2025-submission}
\renewcommand{\JOSShortAuthor}{rr022}
\renewcommand{\JOSShortTitle}{逐条修改说明（审阅草案）}
\hypersetup{pdftitle={E2AG rr022 逐条修改说明（审阅草案）},pdfauthor={E2AG作者组}}
\begin{document}
\noindent{\jostitlefont\fontsize{14pt}{18pt}\selectfont rr022 逐条修改说明\par}
\medskip
\noindent 论文：事件驱动智能体操作系统跨层能力治理方法\par
\noindent 对应稿件：v1.1.43；整理日期：2026年10月7日\par
\medskip
\noindent\textbf{审阅草案，非最终上传材料。}
感谢编委和审稿人的建议。修订补充了外部方法对照、参数约束、授权证据核查，以及11个跨项目历史对象的正式比较。以下逐项说明修改内容和结果，部分落实的建议列明尚未验证的范围。页码均指附后的19页清洁工作稿，而非合并文件或累计修改稿的物理页码。\par
\medskip
\noindent 材料依次为本说明、22页累计修改稿、19页清洁工作稿。累计修改对比已于2026年8月17日提交的v1.1.39：新增为下划线、删除为删除线，变更公式按整式标记，图形内容未改。
该标记由LaTeX版本比较生成，不是Word原生修订记录；其能否满足来信所称修订状态尚待确认。\par
"""
    build_parent = ROOT / "tmp/e2ag-revision"
    build_parent.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix="response-", dir=build_parent))
    source = build / "response.tex"
    source.write_text(preamble + "\n\n".join(body) + "\n\\end{document}\n", encoding="utf-8")
    latex = shutil.which("xelatex")
    if not latex:
        raise FileNotFoundError("XeLaTeX is unavailable")
    for _ in range(2):
        run = subprocess.run([latex, "-interaction=nonstopmode", "-halt-on-error",
                              f"-output-directory={build}", str(source)],
                             cwd=TEXDIR, capture_output=True, timeout=120)
        (build / "console.txt").write_bytes(run.stdout + run.stderr)
        if run.returncode:
            raise RuntimeError(f"Compile failed; inspect {build}")
    log = (build / "response.log").read_text(encoding="utf-8", errors="replace")
    bad = [line for line in log.splitlines()
           if "Overfull" in line or "undefined" in line or "Missing character:" in line]
    if bad:
        raise RuntimeError("Layout errors: " + repr(bad))
    rendered = build / "response.pdf"
    response = PdfReader(rendered)
    response_text = "\n".join(p.extract_text() for p in response.pages)
    if any(key not in response_text for key in expected_ids):
        raise ValueError("A response ID is missing from rendered output")
    if response_text.count("作者答复：") != 8 or "\ufffd" in response_text:
        raise ValueError("Rendered response structure/text verification failed")
    # Strip only the verified running header before matching paragraphs that
    # continue onto the next page; do not ignore arbitrary missing content.
    content_pages = []
    for page_number, page in enumerate(response.pages, 1):
        lines = page.extract_text().splitlines()
        expected_header = f"rr022：逐条修改说明（审阅草案）{page_number}"
        if re.sub(r"\s+", "", lines[0]) != expected_header:
            raise ValueError("Unexpected running header; inspect paragraph verification")
        content_pages.append("\n".join(lines[1:]))
    normalized = re.sub(r"\s+", "", "\n".join(content_pages))
    for paragraph in expected_paragraphs:
        if re.sub(r"\s+", "", paragraph.replace(chr(96), "")) not in normalized:
            raise ValueError(f"Response paragraph missing from PDF: {paragraph[:80]}")
    shutil.copyfile(rendered, response_output)
    # Preserve all component PDFs without rasterizing, resizing, or overlays.
    writer = PdfWriter()
    assembled = [("逐条修改说明（审阅草案）", response_output, digest(response_output)), *PARTS]
    offsets, source_texts, link_checks = [], [], []
    for title, path, sha in assembled:
        reader = PdfReader(path)
        start = len(writer.pages)
        # Each component has its own identically named LaTeX destinations.
        # Resolve them to explicit merged-page references, preventing cross-part
        # collisions and pypdf's loss of named links when outlines are omitted.
        for page in reader.pages:
            writer.add_page(page, excluded_keys=["/Annots"])
        for local_no, page in enumerate(reader.pages):
            for ref in page.get("/Annots", []):
                source_annotation = ref.get_object()
                if source_annotation.get("/Subtype") != "/Link":
                    raise ValueError("Unexpected non-link annotation; preserve explicitly")
                annotation = source_annotation.clone(writer)
                annotation.pop("/P", None)
                action = source_annotation.get("/A")
                if action is not None and action.get("/S") == "/GoTo":
                    target = action.get("/D")
                    if not isinstance(target, str) or target not in reader.named_destinations:
                        raise ValueError(f"Unknown link target: {target!r}")
                    destination = reader.named_destinations[target]
                    local_target = reader.get_destination_page_number(destination)
                    if local_target is None or local_target < 0:
                        raise ValueError("Unresolved source destination")
                    dest_array = destination.dest_array
                    resolved = ArrayObject([writer.pages[start + local_target].indirect_reference,
                                            *dest_array[1:]])
                    annotation[NameObject("/A")] = DictionaryObject({
                        NameObject("/S"): NameObject("/GoTo"),
                        NameObject("/D"): resolved,
                    })
                    link_checks.append((start + local_no, start + local_target))
                elif action is None or action.get("/S") != "/URI":
                    raise ValueError("Unexpected link action")
                writer.add_annotation(start + local_no, annotation)
        writer.add_outline_item(title, start)
        offsets.append({"title": title, "file": str(path.relative_to(ROOT)), "sha256": sha,
                        "start_page": start + 1, "end_page": len(writer.pages)})
        source_texts.extend(page.extract_text() for page in reader.pages)
    writer.add_metadata({"/Title": "E2AG rr022 再审材料审阅稿 v1.1.43",
                         "/Subject": "Draft only; evidence and editorial-format gates remain open"})
    with bundle_output.open("wb") as stream:
        writer.write(stream)
    merged = PdfReader(bundle_output)
    if len(merged.pages) != len(source_texts):
        raise ValueError("Merged page count mismatch")
    for i, (page, expected) in enumerate(zip(merged.pages, source_texts), 1):
        if page.extract_text() != expected:
            raise ValueError(f"Page {i} text changed during merge")
    actual_links = []
    for page_no, page in enumerate(merged.pages):
        for ref in page.get("/Annots", []):
            action = ref.get_object().get("/A", {})
            if action.get("/S") == "/GoTo":
                target_page = merged.get_page_number(action["/D"][0].get_object())
                actual_links.append((page_no, target_page))
    if actual_links != link_checks:
        raise ValueError("Merged link destinations changed")
    for _, path, expected in PARTS:
        if digest(path) != expected:
            raise ValueError("A component PDF changed")
    report = {
        "status": "internal_review_draft_not_submission_ready",
        "response_source_sha256": digest(SOURCE),
        "submitted_pdf_sha256": baseline_hash,
        "response_tex": str(source.relative_to(ROOT)),
        "reviewer_ids": expected_ids, "components": offsets,
        "answer_paragraph_counts": paragraph_counts,
        "all_response_paragraphs_verified": True,
        "output": str(bundle_output.relative_to(ROOT)),
        "output_sha256": digest(bundle_output), "pages": len(merged.pages),
        "text_preserved_page_by_page": True, "components_unchanged": True,
        "internal_links_verified": len(link_checks),
        "warnings": bad, "visual_review": "pending",
        "open_gates": ["author_decision_on_partial_reviewer_responses",
                       "editorial_acceptance_of_non_Word_revision_marks"],
    }
    manifest_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
