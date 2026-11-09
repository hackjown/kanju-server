# 车机自主媒体引擎升级记录 (CarMediaEngine Upgrade)

本文件记录 `static/car-media-engine.js` 与 `static/car.html` 的自主媒体解码架构、核心突破与阶段演进。

---

## 核心架构与技术突破

### 1. 真正的 WASM 逐帧解码输出 (FFmpeg.wasm ➔ Canvas/WebGL + WebAudio)
- **原理与突破**：彻底淘汰了“WASM 仅作为转码生成临时 MP4 并交给 HTMLVideoElement 播放”的过渡路径。
- **逐帧像素管线**：直接通过 FFmpeg.wasm 运行解码管线，将视频解码为 `rawvideo` (RGBA 像素矩阵，默认 1280x720 均衡软件解码负载)，逐帧递交给 WebGL 着色器 (`gl.texImage2D(..., RGBA, UNSIGNED_BYTE, pixels)`) 和 Canvas 渲染，全程应用杜比视界/HDR10 色彩增强着色器。
- **PCM 音频抽取**：同时抽取 Float32 PCM 双声道音频，直接注入 AudioWorklet 环形缓冲区，实现 100% 纯软件自主解码与渲染。

### 2. AudioWorklet PCM 环形缓冲区 (AudioWorklet Ring Buffer)
- **原理与突破**：淘汰了“每收到一块 AudioData 均创建一次 `AudioBufferSourceNode`”的高开销调度，消除了频繁创建/销毁节点引发的 GC 停顿、破音与时序抖动。
- **零拷贝与硬件 DAC 同步**：
  - 启动独立 AudioWorklet 线程，运行 `PcmRingBufferProcessor`（5 秒 / 240,000 采样点容量）。
  - `AudioData` 解码完成后通过 `postMessage(..., [left.buffer, right.buffer])` 实现零拷贝（Zero-Copy）快速移交。
  - Worklet 线程实时统计硬件 DAC 实际播放采样数 (`samplesPlayed`)，每 100ms 向主线程同步绝对精确的时钟，实现 24 小时长时间播放无累计漂移。

### 3. 彻底杜绝隐藏 `<video>` 的重复解码 (Zero Redundant 4K Decoding)
- **原理与突破**：在 WebCodecs 和 WASM 模式下，车机端已完全依靠自主解码流水线渲染画面和播放声音。之前代码将媒体 URL 同时赋给隐藏 `<video>` 作为计时轴，导致浏览器后台 native 解码器也在对 4K 视频进行二次全量解码，使车机 CPU/GPU 负载直接翻倍。
- **虚拟媒体时钟 (Virtual Media Timeline)**：
  - 在自主模式下，`<video>` 元素被置为空源并处于暂停休眠状态 (`src = ''`, `paused = true`)，彻底释放浏览器原生硬件解码通道与 VRAM。
  - 构建了纯内存虚拟时间轴引擎，提供 `currentTime`、`duration`、`paused`、`playbackRate`、`seekTo()`、`play()`、`pause()`。
  - 播放控制栏、进度条拖拽、手势快进/快退 HUD 与实时 FPS 监控全面绑定该虚拟时钟。

### 4. 4K 资源管理与长时间音画同步 (4K VRAM Bounds & A/V Sync)
- **4K 内存保护**：
  - 单帧 4K (3840x2160) RGBA 内存占用高达 33MB。
  - 严格限制待渲染 `VideoFrame` 队列最多缓存 **4 帧**（最大约 130MB 缓存窗口），多余帧即时 `.close()` 销毁。
  - 预解码视野限制为当前时钟后 **0.5 秒**（500,000 微秒），防止 4K 码流在车机有限内存中无限膨胀造成 OOM 崩溃。
  - 渲染后立即释放前一帧 `lastFrame.close()`。
- **强化 Seek 与关键帧对齐**：
  - Seek 时调用 `mp4File.seek(target, true)` 自动寻找最近的关键帧（Sync Sample）。
  - 联动执行 `videoDecoder.reset()`、`audioDecoder.reset()` 与 `workletNode.port.postMessage({ type: 'reset' })` 清空环形缓冲，杜绝拖动进度条时的残音、花屏与解码器死锁。
- **AAC 时间戳与 Priming 消除**：
  - 处理 AAC 编码器的预填充延迟（Priming Samples）与非零起始 PTS。
  - 单调时间戳看门狗（Monotonic PTS Guard），防止时间戳回跳或时间轴混乱。

---

## 解码路径状态表

| 模式 | 视频解码 | 画面输出 | 音频解码与输出 | 重复解码占用 | 状态 |
|---|---|---|---|---|---|
| **WebCodecs (4K/1080P)** | 硬件加速 `VideoDecoder` | `VideoFrame` ➔ WebGL 2.0 / Canvas | `AudioDecoder` ➔ AudioWorklet PCM 环形缓冲 | **0% (隐藏 video 彻底休眠)** | **主力高性能** |
| **FFmpeg.wasm 逐帧软解** | WASM 软件 `rawvideo` | RGBA 像素矩阵 ➔ WebGL / Canvas | Float32 PCM ➔ AudioWorklet 环形缓冲 | **0% (无 video 元素参与)** | **主力自主软解** |
| **Native Fallback (兼容)** | 浏览器原生媒体引擎 / HLS.js | `<video>` 元素抓帧 ➔ Canvas | HTMLMediaElement ➔ WebAudio DSP 滤波 | 仅原生单路 | 兼容回退 |

---

## 验证与自动化测试

运行 Node.js 静态测试套件：
```bash
node --test tests/media-engine-range.test.cjs
```
- ✔ HTTP Range 分段读取校验偏移并按 fileStart 增量 append
- ✔ 服务器忽略 Range 时使用 ReadableStream 分块回退
- ✔ 无效 Content-Range 会重试后明确失败
- ✔ stop() 会取消正在进行的 Range 请求
- ✔ WebCodecs 模式下彻底杜绝隐藏 video 重复解码 (video.src 为空且处于暂停状态)
- ✔ 强化 Seek：重置 VideoDecoder、清空已解码队列并通知 AudioWorklet reset
- ✔ Virtual Media Timeline 播放/暂停控制正常同步状态
- ✔ 4K 内存约束：解码帧队列严格限制在 4 帧以内，溢出即时 close() 并计入 dropped

所有 8 项核心测试 100% 通过！
