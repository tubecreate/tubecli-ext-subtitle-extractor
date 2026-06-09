---
name: Subtitle Extractor
description: Tách phụ đề từ video/audio bằng Whisper AI, Gemini Cloud, hoặc YouTube CC
---

# Subtitle Extractor Skill

## Khả năng
- **Whisper** (Local AI): Tách phụ đề offline bằng mô hình Whisper `small`, hỗ trợ 98+ ngôn ngữ
- **Gemini** (Cloud AI): Tách phụ đề nhanh qua Gemini, hỗ trợ dịch thuật, xử lý song song chunks
- **YouTube CC**: Tải phụ đề có sẵn trên YouTube (manual hoặc auto-generated)
- **Export**: SRT, JSON, VTT, ASS
- **Burn**: Ghi phụ đề vào video bằng FFmpeg

## Trigger Keywords
- "tách sub", "tách phụ đề", "extract subtitle", "lấy sub"
- "phụ đề", "subtitle", "caption", "字幕"
- "transcribe", "speech to text"

## Cách sử dụng

### Tách sub từ file local
```
Tách phụ đề file /path/to/video.mp4
```

### Tách sub từ YouTube URL
```
Lấy sub video https://youtube.com/watch?v=xxx
```

### Tách sub + dịch
```
Tách sub video.mp4 dịch sang tiếng Anh
```

### Node Pipeline
```
Input File → [Subtitle Extract] → [Export SRT] → Output
```

## API Endpoints
- `POST /api/v1/subtitle/extract` — Tách sub (file_path, engine, language)
- `POST /api/v1/subtitle/extract/youtube` — Tách sub YouTube
- `GET /api/v1/subtitle/status/{task_id}` — Polling tiến trình
- `POST /api/v1/subtitle/export` — Export SRT/JSON/VTT/ASS
- `POST /api/v1/subtitle/burn` — Ghi sub vào video (FFmpeg)
- `POST /api/v1/subtitle/translate` — Dịch sub sang ngôn ngữ khác

## Yêu cầu
- `pip install openai-whisper` (cho engine Whisper)
- `pip install yt-dlp` (cho engine YouTube)
- Gemini API key trong Cloud API extension (cho engine Gemini)
- FFmpeg (cho burn subtitle)
