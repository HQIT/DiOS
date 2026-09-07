# 工作日报 2026-06-04

## 今日目标

为 DiOS 产品介绍制作可审阅的视频素材与自动化流水线，支持逐段反馈后再拼接成片。

## 今日完成

### 1. 视频脚本与素材清单

- 新增 [`docs/video-intro-dios.md`](video-intro-dios.md)：完整分镜口播、素材 ID 对照、审片检查项
- 明确产品全称：**Deep Intelligent Operation System**（D = Deep，非 DiFlow）
- 口播读音约定：**Di-OS**（Di 与 OS 两组）；TTS 语速 **+22%**

### 2. 静态与示意图素材（`docs/video-assets/`）

- SVG 字卡/架构图：开场、NANA 类比、分层架构、OS 类比表、双模式对比、结尾卡等
- Mermaid → PNG：Git 协作序列、Event Gateway、邮件协作流程
- UI 静帧截图：Console 各 Tab + Chat（Playwright 抓取脚本）
- 修复 SVG 中文编码与 XML 转义问题（避免预览/渲染报错）

### 3. 旁白 TTS 与逐段成片

- `scripts/generate-video-segment-tts.py`：19 段 edge-tts，合并为 `audio/AU-01-narration.mp3`
- `scripts/assemble-video-segments.sh`：每段独立 `segments/NN-xxx.mp4` + `manifest.json`，**默认不拼接总片**
- `scripts/concat-video-from-segments.sh`：段审通过后一键拼接
- 录屏素材：S-07/08/10/11、CLI/Docker 终端页（S-12/13）

### 4. Segment 01（开场）迭代

- 背景由椭圆改为**正圆**光晕（径向渐变 + 呼吸动画）
- 标题页增加渐显动效：`scripts/record-title-animation.mjs` 录制 SVG 动画
- 全称文案改为 **Deep Intelligent Operation System**
- 单段重建：`ONLY_SEGMENT=01-title ./scripts/assemble-video-segments.sh`

### 5. 其它脚本与预览片（可选）

- `scripts/assemble-video-draft.sh`：早期一键成片预览（已被逐段流程替代为主流程）
- 初版合成片：`DiOS-intro-draft.mp4`（可按需重新生成）

## 目录速查

| 路径 | 用途 |
|------|------|
| `docs/video-assets/segments/*.mp4` | 逐段审片 |
| `docs/video-assets/segments/narration/` | 旁白文本 / 分段 mp3 |
| `docs/video-assets/svg/title-dios.svg` | 开场动效源文件 |
| `scripts/rebuild-segment.sh` | 只重建某一节 |

## 下一步

1. 按段收集修改意见（画面时长、录屏、口播文案）
2. 段审完成后执行 `concat-video-from-segments.sh` 出送审总片
3. 视需要调整 TTS 音色/语速或补充 BGM

## 备注

- 不提交本地缓存：`scripts/.venv-video/`、`.assemble-*`、`segments/.cache/` 等（见 `.gitignore`）
- 完整旁白约 3m28s（+22% 语速后）
