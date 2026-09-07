#!/usr/bin/env python3
"""用 edge-tts 生成视频旁白 AU-01（从分段旁白合并，请先维护 segments/narration/*.txt）。"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# re-exec 进 venv 后才会执行到 main
TEXT = ROOT / "docs/video-assets/audio/narration-full.txt"
OUT_MP3 = ROOT / "docs/video-assets/audio/AU-01-narration.mp3"
# 男声偏沉稳；可改为 zh-CN-XiaoxiaoNeural
VOICE = "zh-CN-YunxiNeural"
RATE = "+22%"


def venv_python() -> Path:
    venv = ROOT / "scripts/.venv-video"
    py = venv / "bin" / "python"
    if not py.exists():
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    try:
        subprocess.check_call(
            [str(py), "-c", "import edge_tts"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        subprocess.check_call(
            [str(py), "-m", "pip", "install", "edge-tts", "-q",
             "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        )
    return py


def ensure_edge_tts():
    global sys
    py = venv_python()
    if Path(sys.executable).resolve() != py.resolve():
        os = __import__("os")
        os.execv(str(py), [str(py), *sys.argv])
    import edge_tts  # noqa: F401


async def synth_part(idx: int, text: str, dest: Path) -> None:
    import edge_tts

    # 避免部分标点导致 Edge 拒收
    safe = (
        text.replace("「", "").replace("」", "")
        .replace(""", '"').replace(""", '"')
        .replace("'", "'")
    )
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            communicate = edge_tts.Communicate(safe, VOICE, rate=RATE)
            await communicate.save(str(dest))
            return
        except edge_tts.exceptions.NoAudioReceived as e:
            last_err = e
            await asyncio.sleep(2 * (attempt + 1))
    raise last_err  # type: ignore[misc]


async def main() -> None:
    nar_dir = ROOT / "docs/video-assets/segments/narration"
    order = sorted(nar_dir.glob("[0-9][0-9]-*.txt"))
    if order:
        parts = [p.read_text(encoding="utf-8").strip() for p in order]
        TEXT.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        print(f"已从 {len(order)} 个分段旁白同步 {TEXT}")
    elif not TEXT.exists():
        raise SystemExit(f"缺少旁白文本: {TEXT}")
    raw = TEXT.read_text(encoding="utf-8").strip()
    parts = [p.strip() for p in raw.split("\n\n") if p.strip()]
    OUT_MP3.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = OUT_MP3.parent / ".tts-parts"
    tmp_dir.mkdir(exist_ok=True)

    part_files: list[Path] = []
    for i, part in enumerate(parts):
        part_path = tmp_dir / f"part-{i:02d}.mp3"
        print(f"合成段落 {i + 1}/{len(parts)} ({len(part)} 字)...")
        await synth_part(i, part, part_path)
        part_files.append(part_path)

    list_file = tmp_dir / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{f.resolve()}'" for f in part_files),
        encoding="utf-8",
    )
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(OUT_MP3),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"已生成: {OUT_MP3} ({len(parts)} 段合并)")
    print("提示: 可用 AU-03 SRT 或录完后用 Whisper 对齐时间轴")


if __name__ == "__main__":
    ensure_edge_tts()
    asyncio.run(main())
