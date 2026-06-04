# 逐段预览素材

每段 **独立 mp4**（画面 + 该段 TTS 旁白），便于逐条审阅修改意见，**默认不拼接总片**。

## 发音

口播稿中 **DiOS** 写作 **`Di-OS`**（Di 与 OS 两组），TTS 语速约 **+22%**。

## 文件

| 路径 | 说明 |
|------|------|
| `01-title.mp4` … `19-ending.mp4` | 各分镜成片 |
| `manifest.json` | 顺序、时长、旁白路径 |
| `narration/*.txt` | 各段旁白原文（可改后重跑 TTS） |
| `narration/audio/*.mp3` | 各段旁白音频 |

## 命令

```bash
# 1. 各段 TTS（含合并 AU-01-narration.mp3）
./scripts/generate-video-segment-tts.py

# 2. 各段画面 + 旁白 → segments/*.mp4
./scripts/assemble-video-segments.sh

# 3. 全部 OK 后再拼接总片（可选）
./scripts/concat-video-from-segments.sh
```

## 修改某一节

1. 编辑 `narration/NN-xxx.txt`（开场画面改 `svg/title-dios.svg`）
2. `./scripts/generate-video-segment-tts.py`（若改了旁白）
3. 只重建一节：`ONLY_SEGMENT=01-title ./scripts/assemble-video-segments.sh`  
   或：`./scripts/rebuild-segment.sh 01-title`

**01-title 动效**：正圆光晕呼吸 + 文字渐显，见 `svg/title-dios.svg`、`scripts/record-title-animation.mjs`。
