# 本地合成媒体

`local-video.mp4` 为 FFmpeg 生成的 2 秒彩色测试图，无客户数据或音频，用于真实浏览器播放/拖动进度验证。生成命令（应用运行无需 FFmpeg）：

```bash
ffmpeg -f lavfi -i 'testsrc2=size=320x180:rate=12:duration=2' -an -c:v libx264 -pix_fmt yuv420p -movflags +faststart tests/fixtures/local-video.mp4
```

图片由 `tests/browser_app.py` 使用 Python 标准库生成；附件内容也是固定合成数据。
