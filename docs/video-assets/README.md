# DiOS 视频素材包

与 [video-intro-dios.md](../video-intro-dios.md) 配套。本目录为**可直接用于剪辑**的素材（SVG / Mermaid / 字幕 / 录屏截图）。

## 制作状态

| ID | 文件 | 状态 | 说明 |
|----|------|------|------|
| B-01 | `svg/title-dios.svg` | ✅ | 开场主标题 1920×1080 |
| B-02 | `svg/tagline-agent-os.svg` | ✅ | 副标题金句 |
| B-04 | `svg/nana-concept.svg` | ✅ | NANA / NAS 类比 |
| B-05 | `svg/architecture-layers.svg` | ✅ | 三层架构 |
| B-06 | `svg/os-analogy-table.svg` | ✅ | OS 类比表 |
| B-07 | `svg/ending-card.svg` | ✅ | 结尾信息卡（链接可改） |
| B-03 | DiFlow 官方 Logo | ⏳ | 需官方品牌资源，结尾卡用文字占位 |
| A-01 | `mermaid/git-collab-sequence.mmd` + `png/` | ✅ | Git 协作序列图 |
| A-02 | `mermaid/event-gateway-overview.mmd` + `png/` | ✅ | Event Gateway 总览 |
| A-03 | `mermaid/email-collab-flow.mmd` + `png/` | ✅ | 邮件协作流程 |
| A-04 | `svg/service-vs-task.svg` | ✅ | 两种 Agent 模式对比 |
| AU-01 | `audio/AU-01-narration.mp3` | ✅ | edge-tts 合成，见 `audio/README.md` |
| AU-02 | BGM | — | 按你的要求暂不制作 |
| AU-03 | `subtitles/video-intro-zh.srt` | ✅ | 草稿；建议按 AU-01 实长微调 |
| S-01～S-06, S-09 | `screenshots/S-*.png` | ✅ | 静帧，见 `screenshots/README.md` |
| S-07～S-08, S-10～S-13 | `recordings/*.webm` 等 | ✅ | Playwright/终端页录制，见 `recordings/README.md` |
| B-03 | DiFlow Logo | — | 无官方素材，结尾卡用文字 |

## 导出 PNG（可选）

已安装 Node 时，在仓库根目录执行：

```bash
./scripts/export-video-diagrams.sh
```

将 `docs/video-assets/mermaid/*.mmd` 导出为 `docs/video-assets/png/*.png`（1920×1080 透明底）。

未安装时脚本会通过 `npx -p @mermaid-js/mermaid-cli mmdc` 临时拉取 CLI。

## 剪辑建议

- **矢量**：SVG 可直接导入 Premiere / DaVinci / Figma，或浏览器打开后全屏录屏。
- **位图**：`png/` 与 `screenshots/` 为 16:9 素材，按分镜表裁剪即可。
- **字幕**：`subtitles/video-intro-zh.srt` 时间码为口播参考，录完后按实际音频微调。

## 逐段预览（推荐审片方式）

| 路径 | 说明 |
|------|------|
| [`segments/`](segments/README.md) | `01-title.mp4` … `19-ending.mp4`，每段含旁白 |
| `segments/manifest.json` | 顺序与时长清单 |
| 发音 | 口播 **Di-OS**；语速 +22% |

```bash
./scripts/assemble-video-segments.sh      # 生成各段，不拼接
./scripts/concat-video-from-segments.sh # 全部 OK 后再拼总片
```

## 初版合成片（旧流程，一键成片）

| 文件 | 说明 |
|------|------|
| `DiOS-intro-draft.mp4` | 旁白 + 分镜自动剪辑预览 |

```bash
./scripts/assemble-video-draft.sh
```

## 重新生成命令

```bash
./scripts/generate-video-narration-tts.py   # AU-01
node scripts/record-video-screencasts.mjs   # S-07/08/10/11
./scripts/record-video-terminal-clips.sh    # S-12/13
./scripts/capture-video-screenshots.sh      # 静帧
./scripts/export-video-diagrams.sh          # 示意图 PNG
```

## 可选人工润色

| 类别 | 说明 |
|------|------|
| AU-01 | 试听 TTS，不满意可换 `VOICE` 或改 `narration-full.txt` 后重跑 |
| AU-03 | 按 `AU-01-narration.mp3` 实际时长重打轴 |
| 录屏 | 自动化录屏无鼠标轨迹；要更「演示感」可用 OBS 重录同流程 |
| B-03 | 仍无 Logo，可跳过 |
