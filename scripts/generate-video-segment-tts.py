#!/usr/bin/env python3
"""为各分镜段落生成 TTS，并合并为完整旁白 AU-01。"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAR_DIR = ROOT / "docs/video-assets/segments/narration"
AUDIO_DIR = NAR_DIR / "audio"
FULL_MP3 = ROOT / "docs/video-assets/audio/AU-01-narration.mp3"
VOICE = "zh-CN-YunxiNeural"
RATE = "+22%"
ORDER = [f"{i:02d}-{name}" for i, name in enumerate(
    [
        "title", "architecture", "nana", "agents", "models", "mcp", "skills",
        "connectors", "events", "event-logs", "modes", "chat", "git-flow",
        "tagline", "email-flow", "shell-switch", "cli", "os-analogy", "ending",
    ],
    start=1,
)]


def venv_python() -> Path:
    venv = ROOT / "scripts/.venv-video"
    py = venv / "bin/python"
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


def ensure_venv():
    py = venv_python()
    if Path(sys.executable).resolve() != py.resolve():
        import os
        os.execv(str(py), [str(py), *sys.argv])


async def synth(text: str, dest: Path) -> float:
    import edge_tts

    safe = (
        text.replace("「", "").replace("」", "")
        .replace(""", '"').replace(""", '"')
    )
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            await edge_tts.Communicate(safe, VOICE, rate=RATE).save(str(dest))
            break
        except edge_tts.exceptions.NoAudioReceived as e:
            last_err = e
            await asyncio.sleep(2 * (attempt + 1))
    else:
        raise last_err  # type: ignore[misc]

    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(dest)],
        text=True,
    )
    return float(out.strip())


async def main() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    FULL_MP3.parent.mkdir(parents=True, exist_ok=True)
    manifest_parts: list[dict] = []

    for seg_id in ORDER:
        txt_path = NAR_DIR / f"{seg_id}.txt"
        if not txt_path.exists():
            raise SystemExit(f"缺少旁白: {txt_path}")
        mp3_path = AUDIO_DIR / f"{seg_id}.mp3"
        text = txt_path.read_text(encoding="utf-8").strip()
        print(f"TTS {seg_id}...")
        dur = await synth(text, mp3_path)
        manifest_parts.append({
            "id": seg_id,
            "narration_txt": str(txt_path.relative_to(ROOT)),
            "narration_mp3": str(mp3_path.relative_to(ROOT)),
            "audio_duration_sec": round(dur, 3),
        })
        print(f"  → {dur:.1f}s")

    list_file = AUDIO_DIR / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{(AUDIO_DIR / f'{s}.mp3').resolve()}'" for s in ORDER),
        encoding="utf-8",
    )
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(FULL_MP3)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    meta = {
        "voice": VOICE,
        "pronunciation_note": "DiOS 口播写作 Di-OS（Di 与 OS 两组）；语速 +22%",
        "parts": manifest_parts,
    }
    (AUDIO_DIR / "tts-manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"完整旁白: {FULL_MP3}")


if __name__ == "__main__":
    ensure_venv()
    asyncio.run(main())
