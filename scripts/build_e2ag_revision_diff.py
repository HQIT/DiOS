"""Build a cumulative LaTeX redline against the submitted rr022 baseline.

Uses Git's Perl and MiKTeX's standalone latexdiff, without global PATH changes.
No manuscript files are edited. Each invocation creates a fresh build directory.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "2bfc5ce06ee9fa00743a2c34ba7b693e2319539c"
BASE_ENTRY = "docs/paper/latex/e2ag-paper-v1.1.39-authors.tex"
BASE_PDF = "output/pdf/E2AG-paper-authors-v1.1.39-20260817.pdf"
BASE_PDF_SHA = "1A9228C691BF4E89B4B2E2D61EC47C9EFBAD822C113154BB17ACBDE7476B82BE"


def run(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=180)
    if result.returncode:
        details = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        raise RuntimeError(f"Command failed: {args[0]}\n{details[-7000:]}")
    return result.stdout


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", type=Path, help="New manuscript entry; omit for identical-baseline test")
    parser.add_argument("--smoke", action="store_true", help="Use the small Chinese redline fixtures")
    parser.add_argument("--perl", type=Path, help="Override Perl executable")
    parser.add_argument("--math-markup", choices=("off", "whole", "coarse", "fine"),
                        default="whole", help="Mark changed equations as a whole by default")
    args = parser.parse_args()
    if args.smoke and args.new:
        parser.error("--smoke and --new cannot be combined")

    perl = args.perl or Path(shutil.which("perl") or "C:/Program Files/Git/usr/bin/perl.exe")
    latex = shutil.which("xelatex")
    diff_exe = shutil.which("latexdiff")
    if not perl.is_file() or not latex or not diff_exe:
        raise RuntimeError("Require Perl, XeLaTeX and MiKTeX latexdiff; use --perl if necessary")
    miktex_root = Path(diff_exe).resolve().parents[3]
    diff_script = miktex_root / "scripts/latexdiff/latexdiff-so"
    expand_script = miktex_root / "scripts/latexpand/latexpand"
    if not diff_script.is_file() or not expand_script.is_file():
        raise RuntimeError(f"Missing MiKTeX script under {miktex_root}")
    version_process = subprocess.run([str(perl), str(diff_script), "--version"],
                                     cwd=ROOT, capture_output=True, timeout=30, check=True)
    version = (version_process.stdout + version_process.stderr).decode().strip()

    base_pdf = run(["git", "show", f"{BASE_COMMIT}:{BASE_PDF}"], ROOT)
    if sha(base_pdf) != BASE_PDF_SHA:
        raise RuntimeError("Submitted PDF does not match pinned SHA-256")
    build_parent = ROOT / "tmp/e2ag-revision"
    build_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rr022-", dir=build_parent))
    snapshot = work / "submitted"
    snapshot.mkdir()
    archive = run(["git", "archive", "--format=zip", BASE_COMMIT, "docs/paper/latex"], ROOT)
    manifest = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            target = (snapshot / entry.filename).resolve()
            if not target.is_relative_to(snapshot.resolve()):
                raise RuntimeError("Unsafe archive path")
            data = bundle.read(entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest[entry.filename] = sha(data)
    (snapshot / "submitted.pdf").write_bytes(base_pdf)

    old = snapshot / BASE_ENTRY
    new = args.new.resolve() if args.new else old
    if args.smoke:
        fixtures = ROOT / "docs/paper/revision-rr022/diff-fixtures"
        old, new = fixtures / "old.tex", fixtures / "new.tex"
    if not new.is_file():
        raise FileNotFoundError(new)
    for name, source in (("old-flat.tex", old), ("new-flat.tex", new)):
        expanded = run([str(perl), str(expand_script), str(source)], source.parent)
        (work / name).write_bytes(expanded)
    diff = run([
        str(perl), str(diff_script), "--encoding=utf8", "--type=UNDERLINE",
        f"--math-markup={args.math_markup}", "--graphics-markup=none",
        str(work / "old-flat.tex"), str(work / "new-flat.tex"),
    ], work).decode("utf-8")
    # CJK-aware marks permit line breaks in Chinese. Monochrome keeps semantic
    # distinctions visible in print. Mathematical/graphic changes need manual QA.
    diff = diff.replace(r"\RequirePackage[normalem]{ulem}",
                        "\\RequirePackage[normalem]{ulem}\n\\RequirePackage{xeCJKfntef}")
    diff = diff.replace(r"\protect\color{blue}\uwave{#1}", r"\protect\color{black}\CJKunderline{#1}")
    diff = diff.replace(r"\protect\color{red}\sout{#1}", r"\protect\color{black}\CJKsout{#1}")
    # A replaced manual algorithm rule is decoration, not deleted prose. Sending
    # a full-width rule through CJK strikeout adds glyph spacing and overflows.
    # Keep all algorithm captions/statements; omit only struck-out old rules.
    diff, omitted_rules = re.subn(r"\\DIFdel\{\\rule\{\\linewidth\}\{[0-9.]+pt\}\s*\}", "", diff)
    # Revision marks lengthen mixed CJK/math paragraphs. Extra stretch changes
    # only line breaking, not the submitted/new source or the displayed content.
    diff = diff.replace(r"\begin{document}",
                        "\\begin{document}\n\\setlength{\\emergencystretch}{6em}", 1)
    (work / "redline.tex").write_text(diff, encoding="utf-8")
    for _ in range(3):
        log = run([latex, "-interaction=nonstopmode", "-halt-on-error",
                   f"-output-directory={work}", str(work / "redline.tex")], new.parent)
        (work / "compile-console.txt").write_bytes(log)
    tex_log = (work / "redline.log").read_text(encoding="utf-8", errors="replace")
    warnings = [line for line in tex_log.splitlines()
                if "Overfull" in line or "undefined" in line or "Missing character:" in line]
    report = {
        "purpose": "tool smoke test" if args.smoke else "baseline comparison" if not args.new else "cumulative revision",
        "baseline_commit": BASE_COMMIT, "baseline_pdf_sha256": BASE_PDF_SHA,
        "baseline_entry": BASE_ENTRY, "baseline_source_sha256": manifest,
        "old_flat_sha256": sha((work / "old-flat.tex").read_bytes()),
        "new_entry": str(new), "new_flat_sha256": sha((work / "new-flat.tex").read_bytes()),
        "perl": str(perl), "latexdiff": version, "warnings": warnings,
        "math_markup": args.math_markup,
        "omitted_deleted_decorative_rules": omitted_rules,
        "output_pdf": str(work / "redline.pdf"),
        "output_pdf_sha256": sha((work / "redline.pdf").read_bytes()),
        "limits": "Not Word tracked changes. Formula, figure and table changes require manual review; editorial acceptance pending.",
    }
    (work / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "baseline_source_sha256"}, ensure_ascii=True, indent=2))
    if warnings:
        raise RuntimeError("PDF built with QA findings; inspect manifest and log before delivery")


if __name__ == "__main__":
    main()
