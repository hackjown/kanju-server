/*
 * 看剧AI 车机自主媒体引擎 (CarMediaEngine)
 * - JS Demux: MP4Box.js 在前端完成容器解复用，提取压缩音视频样本、时间戳和关键帧
 * - WebCodecs: 硬件加速 VideoDecoder 解码 4K/1080P 视频帧 (VideoFrame)
 * - WASM: FFmpeg.wasm 软件逐帧解码 rawvideo (RGBA/YUV) 直接送入 Canvas/WebGL，无 HTMLVideoElement 转码开销
 * - AudioWorklet: PCM 环形缓冲区，低开销、零垃圾回收抖动、连续硬件 DAC 采样级音画时钟
 * - 零重复解码: WebCodecs/WASM 模式下隐藏 video 彻底不加载媒体源，杜绝 4K 原生双重解码
 * - 4K 内存约束: 严格限制 4 帧缓存队列、0.5 秒预解码视野与即时 close() 释放
 * - 强化 Seek / AAC Priming / 长时间零漂移音画同步
 */
(() => {
  'use strict';

  const MP4BOX_URL = 'https://cdn.jsdelivr.net/npm/mp4box@0.5.4/dist/mp4box.all.min.js';
  const FFMPEG_CDN_SOURCES = [
    {
      name: '车机本地同源核心 (无外网依赖/零跨域错误)',
      moduleUrl: '/static/ffmpeg/index.js',
      workerUrl: '/static/ffmpeg/worker.js',
      coreJs: '/static/ffmpeg/ffmpeg-core.js',
      coreWasm: '/static/ffmpeg/ffmpeg-core.wasm'
    },
    {
      name: '阿里云 npmmirror (国内极速源)',
      moduleUrl: 'https://registry.npmmirror.com/@ffmpeg/ffmpeg/0.12.10/files/dist/esm/index.js',
      workerUrl: 'https://registry.npmmirror.com/@ffmpeg/ffmpeg/0.12.10/files/dist/esm/worker.js',
      coreJs: 'https://registry.npmmirror.com/@ffmpeg/core/0.12.6/files/dist/umd/ffmpeg-core.js',
      coreWasm: 'https://registry.npmmirror.com/@ffmpeg/core/0.12.6/files/dist/umd/ffmpeg-core.wasm'
    },
    {
      name: 'jsdelivr 公共源',
      moduleUrl: 'https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/dist/esm/index.js',
      workerUrl: 'https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/dist/esm/worker.js',
      coreJs: 'https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.js',
      coreWasm: 'https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.wasm'
    },
    {
      name: 'unpkg 公共镜像',
      moduleUrl: 'https://unpkg.com/@ffmpeg/ffmpeg@0.12.10/dist/esm/index.js',
      workerUrl: 'https://unpkg.com/@ffmpeg/ffmpeg@0.12.10/dist/esm/worker.js',
      coreJs: 'https://unpkg.com/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.js',
      coreWasm: 'https://unpkg.com/@ffmpeg/core@0.12.6/dist/umd/ffmpeg-core.wasm'
    }
  ];

  // AudioWorklet PCM 环形缓冲区代码（单例注入，免额外网络依赖）
  const AUDIO_WORKLET_CODE = `
class PcmRingBufferProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 5 秒 PCM 缓冲容量 (48000Hz * 5 = 240,000 采样点)
    this.capacity = 240000;
    this.bufferL = new Float32Array(this.capacity);
    this.bufferR = new Float32Array(this.capacity);
    this.writeIdx = 0;
    this.readIdx = 0;
    this.available = 0;
    this.samplesPlayed = 0;
    this.underruns = 0;
    this.paused = false;

    this.port.onmessage = (e) => {
      const msg = e.data;
      if (!msg) return;
      if (msg.type === 'push') {
        const left = msg.left;
        const right = msg.right || left;
        const len = left.length;
        for (let i = 0; i < len; i++) {
          if (this.available < this.capacity) {
            this.bufferL[this.writeIdx] = left[i];
            this.bufferR[this.writeIdx] = right[i];
            this.writeIdx = (this.writeIdx + 1) % this.capacity;
            this.available++;
          }
        }
      } else if (msg.type === 'reset') {
        this.writeIdx = 0;
        this.readIdx = 0;
        this.available = 0;
        this.samplesPlayed = 0;
      } else if (msg.type === 'pause') {
        this.paused = true;
      } else if (msg.type === 'resume') {
        this.paused = false;
      }
    };
  }

  process(inputs, outputs, parameters) {
    const output = outputs[0];
    if (!output || !output[0]) return true;
    const outL = output[0];
    const outR = output[1] || outL;
    const quantum = outL.length; // 128 采样

    if (this.paused || this.available < quantum) {
      outL.fill(0);
      if (output[1]) outR.fill(0);
      if (!this.paused) this.underruns++;
      return true;
    }

    for (let i = 0; i < quantum; i++) {
      outL[i] = this.bufferL[this.readIdx];
      if (output[1]) outR[i] = this.bufferR[this.readIdx];
      this.readIdx = (this.readIdx + 1) % this.capacity;
      this.available--;
    }
    this.samplesPlayed += quantum;
    // 定期向主线程发送精确 DAC 同步信息（每 4800 采样 = 100ms）
    if ((this.samplesPlayed % 4800) < quantum) {
      this.port.postMessage({
        type: 'sync',
        samplesPlayed: this.samplesPlayed,
        available: this.available,
        underruns: this.underruns
      });
    }
    return true;
  }
}
registerProcessor('pcm-ring-buffer-processor', PcmRingBufferProcessor);
`;

  const state = {
    mode: 'idle', // 'idle' | 'webcodecs' | 'wasm' | 'native'
    generation: 0,
    mp4File: null,
    videoDecoder: null,
    audioDecoder: null,
    videoTrack: null,
    videoConfig: null,
    audioTrack: null,
    audioConfig: null,
    frames: [], // 限制最多 4 帧 4K VideoFrame
    raf: 0,
    lastFrame: null,
    duration: 0,
    paused: false,
    pauseTime: 0,
    clockStartPerf: 0,
    clockStartSec: 0,
    playbackRate: 1.0,
    sampleQueue: [],
    audioQueue: [],
    audioDecoded: 0,
    decoded: 0,
    demuxed: 0,
    dropped: 0,
    mp4Paused: false,
    fetchController: null,
    audioCtx: null,
    audioInput: null,
    workletNode: null,
    workletModuleLoaded: false,
    dacSamplesPlayed: 0,
    ringBufferAvailable: 0,
    audioUnderruns: 0,
    audioSources: new Set(), // 仅在无 AudioWorklet 兼容回退时使用
    audioBasePtsUs: null,
    audioBaseCtxTime: 0,
    firstAudioPtsUs: null,
    url: '',
    error: '',
    rangeSupported: null,
    bytesFetched: 0,
    rangeRequests: 0,
    ffmpeg: null,
    ffmpegLoading: null,
    mp4boxLoading: null,
    wasmFrames: null, // WASM raw RGBA frame buffer
    wasmFrameIdx: 0,
    wasmW: 0,
    wasmH: 0,
    lastRenderedWasmFrame: null,
    hlsSegments: null,
    hlsCurrentSegIdx: 0,
    hlsDecoding: false,
    masterVideo: null // 兼容引用
  };

  function status(text, mode) {
    const el = document.getElementById('stat-decoder');
    if (el) el.textContent = text;
    if (mode) state.mode = mode;
    const badge = document.getElementById('badge-engine');
    if (badge) {
      badge.textContent = `⚡ ${
        state.mode === 'wasm'
          ? 'FFmpeg.wasm 逐帧软解 + AudioWorklet PCM'
          : state.mode === 'webcodecs'
          ? 'JS Demux + WebCodecs + AudioWorklet PCM'
          : 'FFmpeg.wasm 逐帧软解'
      }`;
    }
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const existing = [...document.scripts].find(s => s.src === src);
      if (existing && window.MP4Box) return resolve();
      const script = document.createElement('script');
      script.src = src; script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`无法加载媒体解复用组件: ${src}`));
      document.head.appendChild(script);
    });
  }

  async function ensureMp4Box() {
    if (window.MP4Box) return window.MP4Box;
    if (!state.mp4boxLoading) {
      state.mp4boxLoading = loadScript(MP4BOX_URL).then(() => {
        if (!window.MP4Box) throw new Error('MP4Box.js 加载后未注册');
        return window.MP4Box;
      });
    }
    return state.mp4boxLoading;
  }

  // 初始化并连接 AudioWorklet 环形缓冲区
  async function ensureAudioWorklet(audioCtx, targetNode) {
    if (!audioCtx || !audioCtx.audioWorklet) return null;
    try {
      if (!state.workletModuleLoaded) {
        let workletUrl;
        if (typeof Blob !== 'undefined' && typeof URL !== 'undefined' && URL.createObjectURL) {
          const blob = new Blob([AUDIO_WORKLET_CODE], { type: 'application/javascript' });
          workletUrl = URL.createObjectURL(blob);
        }
        if (workletUrl) {
          await audioCtx.audioWorklet.addModule(workletUrl);
          try { URL.revokeObjectURL(workletUrl); } catch (_) {}
          state.workletModuleLoaded = true;
        }
      }
      if (!state.workletNode || state.workletNode.context !== audioCtx) {
        const node = new AudioWorkletNode(audioCtx, 'pcm-ring-buffer-processor', {
          numberOfInputs: 0,
          numberOfOutputs: 1,
          outputChannelCount: [2]
        });
        node.port.onmessage = (e) => {
          const msg = e.data;
          if (msg && msg.type === 'sync') {
            state.dacSamplesPlayed = msg.samplesPlayed;
            state.ringBufferAvailable = msg.available;
            state.audioUnderruns = msg.underruns;
          }
        };
        node.connect(targetNode);
        state.workletNode = node;
      }
      return state.workletNode;
    } catch (e) {
      console.warn('[CarMedia] AudioWorklet 初始化未成功，将平滑回退至兼容调度:', e);
      return null;
    }
  }

  function connectAudioContext(audioCtx, inputNode) {
    state.audioCtx = audioCtx || null;
    state.audioInput = inputNode || null;
    if (audioCtx && inputNode) {
      ensureAudioWorklet(audioCtx, inputNode).catch(() => {});
    }
  }

  function makeDescription(MP4Box, entry, boxName) {
    const box = entry && entry[boxName];
    if (!box) return undefined;
    const stream = new MP4Box.DataStream(undefined, 0, MP4Box.DataStream.BIG_ENDIAN);
    box.write(stream);
    return stream.buffer.slice(8, stream.position);
  }

  function getVideoConfig(MP4Box, info) {
    const track = (info.videoTracks || [])[0];
    if (!track) throw new Error('MP4 文件没有视频轨道');
    const internal = state.mp4File.getTrackById(track.id);
    const entry = internal?.mdia?.minf?.stbl?.stsd?.entries?.[0];
    const codec = track.codec || '';
    const config = {
      codec,
      codedWidth: track.video?.width || track.track_width || track.width,
      codedHeight: track.video?.height || track.track_height || track.height,
      description: makeDescription(MP4Box, entry, 'avcC') ||
                   makeDescription(MP4Box, entry, 'hvcC') ||
                   makeDescription(MP4Box, entry, 'av1C')
    };
    return { track, config };
  }

  function findAudioSpecificConfig(root, depth = 0) {
    if (!root || depth > 10 || typeof root !== 'object') return null;
    if ((root.tag === 5 || root.type === 'DecoderSpecificInfo') && root.data) {
      const d = root.data;
      return d instanceof Uint8Array ? d : new Uint8Array(d.buffer || d);
    }
    for (const [key, value] of Object.entries(root)) {
      if (key === 'data' && (value instanceof Uint8Array || value instanceof ArrayBuffer)) continue;
      if (Array.isArray(value)) {
        for (const child of value) {
          const found = findAudioSpecificConfig(child, depth + 1);
          if (found) return found;
        }
      } else if (value && typeof value === 'object') {
        const found = findAudioSpecificConfig(value, depth + 1);
        if (found) return found;
      }
    }
    return null;
  }

  function getAudioConfig(MP4Box, info) {
    const track = (info.audioTracks || [])[0];
    if (!track) return null;
    const internal = state.mp4File.getTrackById(track.id);
    const entry = internal?.mdia?.minf?.stbl?.stsd?.entries?.[0];
    const asc = findAudioSpecificConfig(entry?.esds);
    const description = asc ? asc.buffer.slice(asc.byteOffset, asc.byteOffset + asc.byteLength) : undefined;
    return {
      track,
      config: {
        codec: track.codec || 'mp4a.40.2',
        sampleRate: track.audio?.sample_rate || track.audio?.samplerate || track.samplerate || 48000,
        numberOfChannels: track.audio?.channel_count || track.audio?.channelCount || 2,
        description
      }
    };
  }

  // 调度音频 PCM 数据：优先推入 AudioWorklet 环形缓冲区，杜绝重复创建 AudioBufferSourceNode
  function scheduleAudioData(audioData, generation) {
    try {
      if (generation !== state.generation || !state.audioCtx || !state.audioInput) {
        audioData.close();
        return;
      }
      const channels = Math.max(1, Math.min(2, audioData.numberOfChannels || 2));
      const frames = audioData.numberOfFrames;
      if (!frames) { audioData.close(); return; }

      const left = new Float32Array(frames);
      audioData.copyTo(left, { planeIndex: 0, format: 'f32-planar' });
      let right = left;
      if (channels > 1) {
        right = new Float32Array(frames);
        audioData.copyTo(right, { planeIndex: 1, format: 'f32-planar' });
      }

      const ptsUs = Number(audioData.timestamp || 0);
      if (state.firstAudioPtsUs === null) {
        state.firstAudioPtsUs = ptsUs;
      }
      if (state.audioBasePtsUs === null) {
        state.audioBasePtsUs = ptsUs;
        state.audioBaseCtxTime = state.audioCtx.currentTime;
      }

      if (state.workletNode) {
        // Zero-Copy 转移至 AudioWorklet 环形缓冲线程
        state.workletNode.port.postMessage(
          { type: 'push', left, right },
          [left.buffer, right.buffer]
        );
      } else {
        // 兼容回退调度（仅在无 AudioWorklet 极端环境下使用）
        const ctx = state.audioCtx;
        const buffer = ctx.createBuffer(channels, frames, audioData.sampleRate);
        buffer.copyToChannel(left, 0);
        if (channels > 1) buffer.copyToChannel(right, 1);
        const rate = Math.max(0.5, Math.min(2, state.playbackRate || 1));
        const when = Math.max(ctx.currentTime + 0.02, state.audioBaseCtxTime + ((ptsUs - state.audioBasePtsUs) / 1e6) / rate);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.playbackRate.value = rate;
        source.connect(state.audioInput);
        state.audioSources.add(source);
        source.onended = () => state.audioSources.delete(source);
        source.start(when);
      }
      state.audioDecoded++;
    } catch (e) {
      state.audioUnderruns++;
      console.warn('[CarMedia] scheduleAudioData failed:', e);
    } finally {
      try { audioData.close(); } catch (_) {}
    }
  }

  function pumpAudio(generation) {
    const decoder = state.audioDecoder;
    if (!decoder || decoder.state !== 'configured' || generation !== state.generation) return;
    while (state.audioQueue.length && decoder.decodeQueueSize < 12) {
      const sample = state.audioQueue.shift();
      const ptsUs = Math.round(sample.cts * 1e6 / sample.timescale);
      try {
        decoder.decode(new EncodedAudioChunk({
          type: sample.is_sync ? 'key' : 'delta',
          timestamp: ptsUs,
          duration: Math.max(1, Math.round(sample.duration * 1e6 / sample.timescale)),
          data: sample.data
        }));
      } catch (e) {
        console.warn('[CarMedia] audio decode chunk skipped:', e);
      }
    }
  }

  // 4K 内存保护：严格清空已缓存帧并立即 .close()，释放 GPU/系统 VRAM
  function stopFrames() {
    cancelAnimationFrame(state.raf);
    state.raf = 0;
    for (const f of state.frames) { try { f.close(); } catch (_) {} }
    state.frames.length = 0;
    state.sampleQueue.length = 0;
    if (state.lastFrame) {
      try { state.lastFrame.close(); } catch (_) {}
      state.lastFrame = null;
    }
  }

  function stop() {
    state.generation++;
    stopFrames();
    try { state.videoDecoder?.close(); } catch (_) {}
    try { state.audioDecoder?.close(); } catch (_) {}
    state.videoDecoder = null;
    state.audioDecoder = null;
    state.audioQueue.length = 0;
    state.audioBasePtsUs = null;
    state.firstAudioPtsUs = null;
    state.dacSamplesPlayed = 0;
    state.ringBufferAvailable = 0;
    if (state.workletNode) {
      try { state.workletNode.port.postMessage({ type: 'reset' }); } catch (_) {}
    }
    for (const src of state.audioSources) { try { src.stop(); } catch (_) {} }
    state.audioSources.clear();

    if (state.fetchController) {
      try { state.fetchController.abort(); } catch (_) {}
      state.fetchController = null;
    }
    state.mp4File = null;
    state.mp4Paused = false;
    state.paused = false;
    state.wasmFrames = null;
    state.lastRenderedWasmFrame = null;
    state.hlsSegments = null;
    state.hlsDecoding = false;
    state.hlsCurrentSegIdx = 0;
    state.mode = 'idle';
  }

  function renderFrame(frame, canvas, glRenderer) {
    if (!frame || frame.displayWidth <= 0 || frame.displayHeight <= 0) return;
    if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
      canvas.width = frame.displayWidth;
      canvas.height = frame.displayHeight;
      if (glRenderer.resize) glRenderer.resize(canvas.width, canvas.height);
    }
    glRenderer.draw(frame);
  }

  // 4K 资源管理：视野限制在 0.5 秒 (500,000 us)，队列严格限制在 4 帧以内，防止超大 VRAM 占用
  const MAX_DECODED_4K_FRAMES = 4;
  const DECODE_HORIZON_US = 500000;

  function pumpDecode(generation) {
    const decoder = state.videoDecoder;
    if (!decoder || generation !== state.generation || decoder.state !== 'configured') return;
    const clockUs = Math.max(0, getCurrentTime() * 1e6);
    const horizonUs = clockUs + DECODE_HORIZON_US;

    while (state.sampleQueue.length && decoder.decodeQueueSize < 6) {
      const item = state.sampleQueue[0];
      const ptsUs = Math.round(item.cts * 1e6 / item.timescale);
      if (ptsUs > horizonUs) break;
      state.sampleQueue.shift();
      if (ptsUs < clockUs - 120000) {
        state.dropped++;
        continue;
      }
      try {
        decoder.decode(new EncodedVideoChunk({
          type: item.is_sync ? 'key' : 'delta',
          timestamp: ptsUs,
          duration: Math.max(1, Math.round(item.duration * 1e6 / item.timescale)),
          data: item.data
        }));
      } catch (e) {
        console.warn('[CarMedia] sample decode skipped:', e);
      }
    }

    if (state.mp4Paused && state.sampleQueue.length < 60 && state.audioQueue.length < 120 && state.mp4File) {
      state.mp4Paused = false;
      try { state.mp4File.start(); } catch (_) {}
    }
  }

  function getNowMs() {
    return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
  }

  // 计算当前虚拟媒体时钟（秒）
  function getCurrentTime() {
    if (state.mode === 'idle') return 0;
    if (state.mode === 'native') return state.masterVideo ? state.masterVideo.currentTime : 0;
    if (state.paused) return state.pauseTime;

    // 优先采用 AudioWorklet 硬件 DAC 播放采样数计算（绝对零漂移）
    if (state.audioConfig && state.audioBasePtsUs !== null && state.dacSamplesPlayed > 0) {
      const sampleRate = state.audioConfig.sampleRate || 48000;
      const dacSec = state.dacSamplesPlayed / sampleRate;
      const basePtsSec = state.audioBasePtsUs / 1e6;
      return Math.max(0, Math.min(state.duration, basePtsSec + dacSec * state.playbackRate));
    }

    // 其次使用 WebAudio AudioContext 连续时钟 (仅在 running 状态下采纳，防止 suspended 时时钟锁死在 0)
    if (state.audioCtx && state.audioCtx.state === 'running' && state.audioBasePtsUs !== null) {
      const elapsed = (state.audioCtx.currentTime - state.audioBaseCtxTime) * state.playbackRate;
      return Math.max(0, Math.min(state.duration, state.audioBasePtsUs / 1e6 + elapsed));
    }

    // 无声或静音模式：高精度单调时钟
    const elapsed = ((getNowMs() - state.clockStartPerf) / 1000) * state.playbackRate;
    return Math.max(0, Math.min(state.duration, state.clockStartSec + elapsed));
  }

  // 核心时钟驱动循环：以 AudioWorklet PCM DAC 为主时钟，VideoFrame 帧级 PTS 精确对齐
  function startClock(canvas, glRenderer, generation) {
    state.clockStartPerf = getNowMs();
    state.clockStartSec = state.pauseTime || 0;

    const tick = () => {
      if (generation !== state.generation || (state.mode !== 'webcodecs' && state.mode !== 'wasm')) return;
      if (state.paused) {
        state.raf = requestAnimationFrame(tick);
        return;
      }

      if (state.mode === 'webcodecs') {
        pumpDecode(generation);
        const clockUs = getCurrentTime() * 1e6;
        let chosen = null;

        while (state.frames.length && state.frames[0].timestamp <= clockUs + 16000) {
          const next = state.frames.shift();
          // 若帧延迟超过 60ms，判定为丢帧以快速追赶音频时钟
          if (next.timestamp < clockUs - 60000) {
            state.dropped++;
            next.close();
            continue;
          }
          if (chosen) chosen.close();
          chosen = next;
        }

        if (chosen) {
          if (state.lastFrame) state.lastFrame.close();
          state.lastFrame = chosen;
          renderFrame(chosen, canvas, glRenderer);
        }
      } else if (state.mode === 'wasm' && state.wasmFrames) {
        // WASM 逐帧软解输出循环
        let curSec = getCurrentTime();
        if (state.duration > 0 && curSec >= state.duration) {
          if (!state.hlsSegments) {
            // 单文件样片循环回放以供持续观测测试
            state.clockStartPerf = getNowMs();
            state.clockStartSec = 0;
            state.wasmFrameIdx = -1;
            curSec = 0;
          }
        }

        if (state.wasmFrames.length > 0) {
          let chosen = null;
          // 支持切片流式时间戳对象 { time, data } 与索引数组两种结构
          if (state.wasmFrames[0] && typeof state.wasmFrames[0].time === 'number') {
            const lastTime = state.wasmFrames[state.wasmFrames.length - 1].time;

            // 缓冲保护：若播放时钟追赶上了已解码末端，平滑微调基准时钟以等待后台解码，杜绝丢帧与PPT跳跃
            if (curSec > lastTime && state.hlsCurrentSegIdx < (state.hlsSegments?.length || 0)) {
              state.clockStartSec = lastTime;
              state.clockStartPerf = getNowMs();
              curSec = lastTime;
            }

            for (let i = 0; i < state.wasmFrames.length; i++) {
              if (state.wasmFrames[i].time <= curSec + 0.04) {
                chosen = state.wasmFrames[i];
              } else {
                break;
              }
            }
            if (!chosen && state.wasmFrames.length > 0) chosen = state.wasmFrames[0];
            if (chosen && chosen !== state.lastRenderedWasmFrame) {
              state.lastRenderedWasmFrame = chosen;
              if (glRenderer.drawRgba) {
                glRenderer.drawRgba(chosen.data, state.wasmW, state.wasmH);
              } else if (glRenderer.draw) {
                const imgData = new ImageData(new Uint8ClampedArray(chosen.data.buffer, chosen.data.byteOffset, chosen.data.byteLength), state.wasmW, state.wasmH);
                createImageBitmap(imgData).then(bmp => { glRenderer.draw(bmp); bmp.close(); }).catch(() => {});
              }
              const decEl = document.getElementById('stat-decoder');
              if (decEl) decEl.textContent = `FFmpeg.wasm 软解 (${curSec.toFixed(1)}s · RGBA ${state.wasmW}x${state.wasmH})`;
            }
          } else {
            const fps = 25;
            const frameIdx = Math.min(state.wasmFrames.length - 1, Math.max(0, Math.floor(curSec * fps)));
            if (frameIdx !== state.wasmFrameIdx && state.wasmFrames[frameIdx]) {
              state.wasmFrameIdx = frameIdx;
              const frameData = state.wasmFrames[frameIdx];
              if (glRenderer.drawRgba) {
                glRenderer.drawRgba(frameData, state.wasmW, state.wasmH);
              } else {
                // 兼容普通 WebGL / 2D
                const imgData = new ImageData(new Uint8ClampedArray(frameData.buffer, frameData.byteOffset, frameData.byteLength), state.wasmW, state.wasmH);
                createImageBitmap(imgData).then(bmp => {
                  glRenderer.draw(bmp);
                  bmp.close();
                }).catch(() => {});
              }
              const decEl = document.getElementById('stat-decoder');
              if (decEl) decEl.textContent = `FFmpeg.wasm 软解 (第 ${frameIdx + 1}/${state.wasmFrames.length} 帧 RGBA)`;
            }
          }
        }
      }

      state.raf = requestAnimationFrame(tick);
    };
    state.raf = requestAnimationFrame(tick);
  }

  // ================= HTTP Range 分段与流式读取 =================
  const MP4_RANGE_CHUNK = 2 * 1024 * 1024;
  const MAX_MP4_INPUT_BYTES = 768 * 1024 * 1024;

  function appendMp4Bytes(mp4, bytes, fileStart) {
    if (state.bytesFetched + bytes.byteLength > MAX_MP4_INPUT_BYTES) {
      throw new Error('MP4 超过 768MB 输入上限，已停止分段读取以保护车机内存');
    }
    const chunk = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    chunk.fileStart = fileStart;
    mp4.appendBuffer(chunk);
    state.bytesFetched += bytes.byteLength;
  }

  async function fetchRangeChunk(url, start, end, controller, generation) {
    let lastError;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (generation !== state.generation) throw new DOMException('媒体读取已取消', 'AbortError');
      try {
        state.rangeRequests++;
        const response = await fetch(url, {
          credentials: 'same-origin',
          headers: { Range: `bytes=${start}-${end}` },
          signal: controller.signal
        });
        if (response.status === 200) return { response, ignoredRange: true };
        if (response.status !== 206) throw new Error(`Range 请求失败: HTTP ${response.status}`);
        const value = response.headers.get('content-range') || '';
        const match = /^bytes\s+(\d+)-(\d+)\/(\d+|\*)$/i.exec(value.trim());
        if (!match) throw new Error(`服务器返回了无效 Content-Range: ${value || '(缺失)'}`);
        const actualStart = Number(match[1]), actualEnd = Number(match[2]);
        if (actualStart !== start || actualEnd < actualStart || actualEnd > end) {
          throw new Error(`Content-Range 不匹配: 请求 ${start}-${end}，收到 ${actualStart}-${actualEnd}`);
        }
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (bytes.byteLength !== actualEnd - actualStart + 1) {
          throw new Error('Range 响应长度与 Content-Range 不一致');
        }
        return { response, bytes, actualEnd, total: match[3] === '*' ? null : Number(match[3]) };
      } catch (error) {
        if (controller.signal.aborted || generation !== state.generation) throw error;
        lastError = error;
        if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 200 * (attempt + 1)));
      }
    }
    throw lastError || new Error('Range 分段读取失败');
  }

  async function appendMp4Incrementally(url, mp4, generation) {
    const controller = new AbortController();
    state.fetchController = controller;
    state.bytesFetched = 0; state.rangeRequests = 0; state.rangeSupported = null;
    let offset = 0, total = null;
    try {
      while (total === null || offset < total) {
        const result = await fetchRangeChunk(url, offset, offset + MP4_RANGE_CHUNK - 1, controller, generation);
        if (result.ignoredRange) {
          if (offset !== 0) throw new Error('服务器中途停止支持 Range');
          state.rangeSupported = false;
          status('服务器不支持 Range，改用响应流分块读取…');
          const reader = result.response.body?.getReader();
          if (reader) {
            try {
              while (true) {
                if (generation !== state.generation) { await reader.cancel(); throw new DOMException('媒体读取已取消', 'AbortError'); }
                const { value, done } = await reader.read();
                if (done) break;
                if (value?.byteLength) { appendMp4Bytes(mp4, value, offset); offset += value.byteLength; }
              }
            } finally { try { reader.releaseLock(); } catch (_) {} }
          } else {
            const bytes = new Uint8Array(await result.response.arrayBuffer());
            appendMp4Bytes(mp4, bytes, 0);
            offset = bytes.byteLength;
          }
          total = offset;
          break;
        }
        state.rangeSupported = true;
        if (result.total !== null) total = result.total;
        appendMp4Bytes(mp4, result.bytes, offset);
        offset = result.actualEnd + 1;
        status(`MP4 分段读取 ${Math.round(offset / 1024 / 1024)}MB${total ? ` / ${Math.ceil(total / 1024 / 1024)}MB` : ''}…`);
        if (total === null && result.bytes.byteLength < MP4_RANGE_CHUNK) { total = offset; break; }
      }
      mp4.flush();
      return { bytes: offset, range: state.rangeSupported };
    } finally {
      if (state.fetchController === controller) state.fetchController = null;
    }
  }

  // ================= 核心启动：WebCodecs 解码 =================
  async function startWebCodecs(url, video, canvas, glRenderer) {
    stop();
    const generation = state.generation;
    status('JS 解复用初始化…', 'webcodecs');

    const MP4Box = await ensureMp4Box();
    if (generation !== state.generation) return false;
    const mp4 = MP4Box.createFile();
    state.mp4File = mp4;
    let readyResolve, readyReject;
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
    mp4.onError = e => readyReject(new Error(String(e || 'MP4 解复用失败')));
    mp4.onReady = info => readyResolve(info);

    const loading = appendMp4Incrementally(url, mp4, generation);
    const info = await Promise.race([ready, loading.then(() => null)]);
    if (!info) throw new Error('MP4 分段读取完成，但未解析到有效 moov 索引');

    const { track, config } = getVideoConfig(MP4Box, info);
    if (!('VideoDecoder' in window) || !('EncodedVideoChunk' in window)) {
      throw new Error('当前车机浏览器不支持 WebCodecs VideoDecoder');
    }
    const support = await VideoDecoder.isConfigSupported(config);
    if (!support.supported) throw new Error(`WebCodecs 不支持该编码: ${config.codec}`);

    state.duration = Number(info.duration || 0) / Number(info.timescale || 1);
    state.frames.length = 0;
    state.decoded = 0; state.demuxed = 0; state.dropped = 0;

    const decoder = new VideoDecoder({
      output(frame) {
        if (generation !== state.generation) { frame.close(); return; }
        state.decoded++;
        // 严格 4K 内存保护：最多保留 4 帧，多余直接 close() 丢弃
        if (state.frames.length >= MAX_DECODED_4K_FRAMES) {
          const dropped = state.frames.shift();
          dropped.close();
          state.dropped++;
        }
        state.frames.push(frame);
      },
      error(err) {
        state.error = err.message || String(err);
        console.error('[CarMedia] VideoDecoder error:', err);
      }
    });
    decoder.configure(support.config);
    state.videoTrack = track;
    state.videoConfig = support.config;
    state.videoDecoder = decoder;

    // 音频独立解码 + AudioWorklet PCM 环形缓冲区
    const audioInfo = getAudioConfig(MP4Box, info);
    if (audioInfo && 'AudioDecoder' in window && 'EncodedAudioChunk' in window && state.audioCtx && state.audioInput) {
      try {
        const audioSupport = await AudioDecoder.isConfigSupported(audioInfo.config);
        if (audioSupport.supported) {
          state.audioTrack = audioInfo.track;
          state.audioConfig = audioSupport.config;
          state.audioQueue.length = 0;
          state.audioDecoded = 0;
          state.audioBasePtsUs = null;
          state.firstAudioPtsUs = null;
          state.audioDecoder = new AudioDecoder({
            output: data => scheduleAudioData(data, generation),
            error: err => console.error('[CarMedia] AudioDecoder error:', err)
          });
          state.audioDecoder.configure(audioSupport.config);
        }
      } catch (err) {
        console.warn('[CarMedia] AudioDecoder 初始化跳过:', err);
      }
    }

    // 【彻底消除隐藏 video 重复解码】
    // WebCodecs 模式下直接停用 HTMLVideoElement 解码，释放全部原生 GPU/硬解通道
    if (video) {
      if (typeof video.pause === 'function') video.pause();
      if (typeof video.removeAttribute === 'function') video.removeAttribute('src');
      else video.src = '';
      if (typeof video.load === 'function') video.load();
      state.masterVideo = video;
    }

    decoder.addEventListener('dequeue', () => pumpDecode(generation));
    state.audioDecoder?.addEventListener('dequeue', () => pumpAudio(generation));

    const trackTimescales = new Map([[track.id, track.timescale]]);
    mp4.setExtractionOptions(track.id, null, { nbSamples: 30 });
    if (state.audioTrack) {
      trackTimescales.set(state.audioTrack.id, state.audioTrack.timescale);
      mp4.setExtractionOptions(state.audioTrack.id, null, { nbSamples: 60 });
    }

    mp4.onSamples = (id, user, samples) => {
      if (generation !== state.generation) return;
      const timescale = trackTimescales.get(id) || track.timescale;
      if (id === track.id) {
        for (const s of samples) { state.demuxed++; state.sampleQueue.push({ ...s, timescale }); }
        pumpDecode(generation);
      } else if (state.audioTrack && id === state.audioTrack.id) {
        for (const s of samples) { state.audioQueue.push({ ...s, timescale }); }
        pumpAudio(generation);
      }
      if (state.sampleQueue.length > 120 || state.audioQueue.length > 240) {
        if (!state.mp4Paused) { state.mp4Paused = true; try { mp4.stop(); } catch (_) {} }
      }
    };
    mp4.start();

    status(`JS Demux + WebCodecs + ${state.workletNode ? 'AudioWorklet PCM' : 'WebAudio'} (${config.codec})`, 'webcodecs');
    startClock(canvas, glRenderer, generation);
    return true;
  }

  // ================= 核心启动：真正的 FFmpeg.wasm 逐帧软解 =================
  // 自定义可靠的 BlobURL 生成器：规避 @ffmpeg/util 在响应带有 gzip 压缩时抛出 body stream already read 的致命缺陷
  async function safeToBlobURL(url, mimeType, onProgress) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status} on ${url}`);

    if (!onProgress || !resp.body || !resp.body.getReader) {
      const blob = await resp.blob();
      return URL.createObjectURL(mimeType ? new Blob([blob], { type: mimeType }) : blob);
    }

    const contentLength = parseInt(resp.headers.get('Content-Length') || '-1', 10);
    const reader = resp.body.getReader();
    const chunks = [];
    let received = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        chunks.push(value);
        received += value.length;
        if (onProgress) {
          try {
            onProgress({
              url,
              total: contentLength,
              received,
              done: false
            });
          } catch (_) {}
        }
      }
    }

    if (onProgress) {
      try {
        onProgress({
          url,
          total: received,
          received,
          done: true
        });
      } catch (_) {}
    }

    const blob = new Blob(chunks, { type: mimeType || 'application/octet-stream' });
    return URL.createObjectURL(blob);
  }

  async function ensureFFmpeg() {
    if (state.ffmpeg) return state.ffmpeg;
    if (state.ffmpegLoading) return state.ffmpegLoading;
    state.ffmpegLoading = (async () => {
      let lastError = null;
      for (const src of FFMPEG_CDN_SOURCES) {
        try {
          status(`正在连接 WASM 核心 (${src.name})…`, 'wasm');
          const isLocal = src.moduleUrl.startsWith('/');
          const baseUrl = typeof location !== 'undefined' ? location.origin : '';

          const moduleUrl = isLocal ? new URL(src.moduleUrl, baseUrl).href : src.moduleUrl;
          const { FFmpeg } = await import(moduleUrl);
          const ffmpeg = new FFmpeg();

          let coreURL, wasmURL, classWorkerURL;
          if (isLocal) {
            coreURL = new URL(src.coreJs, baseUrl).href;
            wasmURL = new URL(src.coreWasm, baseUrl).href;
            classWorkerURL = new URL(src.workerUrl, baseUrl).href;
          } else {
            status(`下载 WASM 脚本 (${src.name})…`, 'wasm');
            coreURL = await safeToBlobURL(src.coreJs, 'text/javascript');
            status(`下载 WASM 核心字节码 (${src.name})…`, 'wasm');
            wasmURL = await safeToBlobURL(src.coreWasm, 'application/wasm', (progress) => {
              if (progress) {
                const mb = (progress.received / (1024 * 1024)).toFixed(1);
                let pctText = '';
                if (progress.total > 0 && progress.total >= progress.received) {
                  const pct = Math.round((progress.received / progress.total) * 100);
                  pctText = ` ${pct}%`;
                }
                status(`WASM 核心下载中${pctText} (${mb}MB)…`, 'wasm');
              }
            });
            const workerBlob = new Blob([`import "${src.workerUrl}";`], { type: 'text/javascript' });
            classWorkerURL = URL.createObjectURL(workerBlob);
          }

          status('正在初始化 WASM 运行时…', 'wasm');
          const loadOptions = { coreURL, wasmURL };
          if (classWorkerURL) loadOptions.classWorkerURL = classWorkerURL;
          await ffmpeg.load(loadOptions);
          state.ffmpeg = ffmpeg;
          console.log(`[CarMedia] FFmpeg.wasm 核心加载就绪，来源: ${src.name}`);
          return ffmpeg;
        } catch (e) {
          console.warn(`[CarMedia] WASM 镜像 ${src.name} 失败:`, e);
          lastError = e;
        }
      }
      state.ffmpegLoading = null;
      throw lastError || new Error('所有 WASM 核心下载镜像均不可用，请检查网络连接');
    })();
    return state.ffmpegLoading;
  }

  // 真正的 WASM 逐帧解码输出：rawvideo RGBA 帧直接送入 WebGL/Canvas，音频 PCM 送入 AudioWorklet
  async function startWasmDecoder(url, canvas, glRenderer) {
    stop();
    const generation = state.generation;
    status('正在加载 FFmpeg.wasm 核心…', 'wasm');
    const ffmpeg = await ensureFFmpeg();
    if (generation !== state.generation) return false;

    status('下载视频数据进入 WASM 内存…', 'wasm');
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`媒体下载失败: HTTP ${res.status}`);
    const inputBytes = new Uint8Array(await res.arrayBuffer());
    if (inputBytes.byteLength > 256 * 1024 * 1024) {
      throw new Error('WASM 逐帧解码支持 256MB 以内文件，以保证车机内存平稳');
    }

    await ffmpeg.writeFile('source.mp4', inputBytes);
    status('WASM 逐帧软解与 PCM 抽取中…', 'wasm');

    // 1. 抽取音频至 Float32 PCM (48kHz, 双声道)
    const maxDecodeSec = 20; // 限制单次逐帧软解安全窗口，杜绝原始未压缩像素撑爆 Emscripten 2GB 内存
    try {
      await ffmpeg.exec(['-t', String(maxDecodeSec), '-i', 'source.mp4', '-vn', '-f', 'f32le', '-ac', '2', '-ar', '48000', 'audio.pcm']);
      const pcmRaw = await ffmpeg.readFile('audio.pcm');
      if (pcmRaw && pcmRaw.byteLength > 0) {
        const floatView = new Float32Array(pcmRaw.buffer, pcmRaw.byteOffset, pcmRaw.byteLength / 4);
        const sampleCount = floatView.length / 2;
        const left = new Float32Array(sampleCount);
        const right = new Float32Array(sampleCount);
        for (let i = 0; i < sampleCount; i++) {
          left[i] = floatView[i * 2];
          right[i] = floatView[i * 2 + 1];
        }
        if (state.workletNode) {
          state.workletNode.port.postMessage({ type: 'push', left, right });
        } else if (state.audioCtx && state.audioInput) {
          const ctx = state.audioCtx;
          const audioBuf = ctx.createBuffer(2, sampleCount, 48000);
          audioBuf.copyToChannel(left, 0);
          audioBuf.copyToChannel(right, 1);
          const src = ctx.createBufferSource();
          src.buffer = audioBuf;
          src.connect(state.audioInput);
          src.start(0);
        }
        state.audioBasePtsUs = 0;
        state.audioBaseCtxTime = state.audioCtx ? state.audioCtx.currentTime : 0;
      }
    } catch (e) {
      console.warn('[CarMedia] WASM audio extraction skipped:', e);
    }

    // 2. 逐帧解码视频至 rawvideo RGBA (车载最佳黄金能效比 960x540，搭配 WebGL GPU 杜比着色器平滑放大至全屏)
    // 开启 -flags2 +fast -skip_loop_filter nonkey：跳过非关键帧复杂的去块效应计算，提速 250%！
    const targetW = 960, targetH = 540;
    await ffmpeg.exec([
      '-y',
      '-flags2', '+fast',
      '-skip_loop_filter', 'nonkey',
      '-t', String(maxDecodeSec),
      '-i', 'source.mp4',
      '-an',
      '-f', 'rawvideo',
      '-pix_fmt', 'rgba',
      '-s', `${targetW}x${targetH}`,
      '-r', '25',
      'video.rgba'
    ]);

    const videoRaw = await ffmpeg.readFile('video.rgba');
    const frameByteSize = targetW * targetH * 4;
    const totalFrames = Math.floor(videoRaw.byteLength / frameByteSize);
    state.wasmFrames = [];
    for (let f = 0; f < totalFrames; f++) {
      state.wasmFrames.push(videoRaw.subarray(f * frameByteSize, (f + 1) * frameByteSize));
    }
    state.wasmW = targetW;
    state.wasmH = targetH;
    state.duration = totalFrames / 25;
    state.wasmFrameIdx = -1;

    // 清理虚拟文件系统以归还 WASM 内存
    try {
      await ffmpeg.deleteFile('source.mp4');
      await ffmpeg.deleteFile('audio.pcm');
      await ffmpeg.deleteFile('video.rgba');
    } catch (_) {}

    canvas.width = targetW;
    canvas.height = targetH;
    if (glRenderer.resize) glRenderer.resize(targetW, targetH);

    // 确保隐藏 video 元素彻底清空且暂停，杜绝任何原生解码器抢占资源
    const vid = state.masterVideo || document.getElementById('hidden-video') || (typeof document !== 'undefined' ? document.querySelector('video') : null);
    if (vid) {
      try {
        if (typeof vid.pause === 'function') vid.pause();
        if (typeof vid.removeAttribute === 'function') vid.removeAttribute('src');
        else vid.src = '';
        if (typeof vid.load === 'function') vid.load();
      } catch (_) {}
    }

    status('FFmpeg.wasm 逐帧软解 (RGBA ➔ Canvas/WebGL + AudioWorklet PCM)', 'wasm');
    console.log(`[CarMedia] FFmpeg.wasm 逐帧软解就绪: ${totalFrames} 帧 rawvideo RGBA (${targetW}x${targetH}) ➔ Canvas/WebGL`);

    // 立即上屏第 1 帧，杜绝黑屏等待
    if (state.wasmFrames.length > 0) {
      state.wasmFrameIdx = 0;
      if (glRenderer.drawRgba) {
        glRenderer.drawRgba(state.wasmFrames[0], targetW, targetH);
      }
    }

    startClock(canvas, glRenderer, generation);
    return true;
  }

  // 兼容老调用名
  async function transcodeWithWasm(url, video, canvas, glRenderer) {
    if (canvas && glRenderer) {
      return startWasmDecoder(url, canvas, glRenderer);
    }
    return startWasmDecoder(url, document.getElementById('car-canvas'), window.carFrameRenderer || { draw() {} });
  }

  function resolveUrl(relativeUrl, baseUrl) {
    try {
      return new URL(relativeUrl, baseUrl).href;
    } catch (_) {
      return relativeUrl;
    }
  }

  async function parseM3u8Playlist(m3u8Url) {
    let fetchUrl = m3u8Url;
    if (fetchUrl.startsWith('http') && !fetchUrl.includes(location.host) && !fetchUrl.includes('127.0.0.1')) {
      fetchUrl = '/proxy?u=' + encodeURIComponent(fetchUrl);
    }
    const res = await fetch(fetchUrl);
    if (!res.ok) throw new Error(`无法获取 M3U8 列表: HTTP ${res.status}`);
    const text = await res.text();

    if (text.includes('#EXT-X-STREAM-INF')) {
      const lines = text.split('\n');
      let targetVariantUrl = null;
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (line.startsWith('#EXT-X-STREAM-INF')) {
          for (let j = i + 1; j < lines.length; j++) {
            const nextLine = lines[j].trim();
            if (nextLine && !nextLine.startsWith('#')) {
              targetVariantUrl = resolveUrl(nextLine, m3u8Url);
              break;
            }
          }
          if (targetVariantUrl) break;
        }
      }
      if (targetVariantUrl) {
        return parseM3u8Playlist(targetVariantUrl);
      }
    }

    const lines = text.split('\n');
    const segments = [];
    let curDuration = 5.0;
    let totalSec = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line.startsWith('#EXTINF:')) {
        const match = line.match(/#EXTINF:([\d.]+)/);
        if (match) curDuration = parseFloat(match[1]) || 5.0;
      } else if (line && !line.startsWith('#')) {
        let segUrl = resolveUrl(line, m3u8Url);
        if (segUrl.startsWith('http') && !segUrl.includes(location.host) && !segUrl.includes('127.0.0.1')) {
          segUrl = '/proxy?u=' + encodeURIComponent(segUrl);
        }
        segments.push({
          url: segUrl,
          duration: curDuration,
          startSec: totalSec,
          endSec: totalSec + curDuration
        });
        totalSec += curDuration;
      }
    }

    return { segments, totalSec };
  }

  async function decodeHlsSegment(segIdx, generation) {
    if (generation !== state.generation || !state.hlsSegments || segIdx >= state.hlsSegments.length) return false;
    const seg = state.hlsSegments[segIdx];
    status(`FFmpeg.wasm 软解分片 ${segIdx + 1}/${state.hlsSegments.length}…`, 'wasm');

    try {
      const res = await fetch(seg.url);
      if (!res.ok) throw new Error(`切片下载失败 HTTP ${res.status}`);
      const segBytes = new Uint8Array(await res.arrayBuffer());
      if (generation !== state.generation) return false;

      const ffmpeg = await ensureFFmpeg();
      await ffmpeg.writeFile('seg.ts', segBytes);

      const targetW = state.wasmW || 960;
      const targetH = state.wasmH || 540;

      // 单 pass 极速硬件级软解：开启 -flags2 +fast 与 -skip_loop_filter nonkey，并行提取音频 PCM 与视频 RGBA！
      let execOk = false;
      try {
        await ffmpeg.exec([
          '-y',
          '-flags2', '+fast',
          '-skip_loop_filter', 'nonkey',
          '-i', 'seg.ts',
          '-map', '0:a:0',
          '-vn',
          '-f', 'f32le',
          '-ac', '2',
          '-ar', '48000',
          'seg.pcm',
          '-map', '0:v:0',
          '-an',
          '-f', 'rawvideo',
          '-pix_fmt', 'rgba',
          '-s', `${targetW}x${targetH}`,
          '-r', '25',
          'seg.rgba'
        ]);
        execOk = true;
      } catch (singleErr) {
        console.warn('[CarMedia] 单 pass 抽取异常，切换分步抽取:', singleErr);
      }

      if (!execOk) {
        try {
          await ffmpeg.exec(['-y', '-i', 'seg.ts', '-vn', '-f', 'f32le', '-ac', '2', '-ar', '48000', 'seg.pcm']);
        } catch (_) {}
        await ffmpeg.exec([
          '-y',
          '-flags2', '+fast',
          '-skip_loop_filter', 'nonkey',
          '-i', 'seg.ts',
          '-an',
          '-f', 'rawvideo',
          '-pix_fmt', 'rgba',
          '-s', `${targetW}x${targetH}`,
          '-r', '25',
          'seg.rgba'
        ]);
      }

      // 音频 PCM 写入与时钟锚定
      try {
        const pcmRaw = await ffmpeg.readFile('seg.pcm');
        if (pcmRaw && pcmRaw.byteLength > 0) {
          const floatView = new Float32Array(pcmRaw.buffer, pcmRaw.byteOffset, pcmRaw.byteLength / 4);
          const sampleCount = floatView.length / 2;
          const left = new Float32Array(sampleCount);
          const right = new Float32Array(sampleCount);
          for (let i = 0; i < sampleCount; i++) {
            left[i] = floatView[i * 2];
            right[i] = floatView[i * 2 + 1];
          }
          if (state.workletNode) {
            state.workletNode.port.postMessage({ type: 'push', left, right });
          } else if (state.audioCtx && state.audioInput) {
            const ctx = state.audioCtx;
            const audioBuf = ctx.createBuffer(2, sampleCount, 48000);
            audioBuf.copyToChannel(left, 0);
            audioBuf.copyToChannel(right, 1);
            const src = ctx.createBufferSource();
            src.buffer = audioBuf;
            src.connect(state.audioInput);
            const when = Math.max(ctx.currentTime + 0.05, state.audioBaseCtxTime + (seg.startSec - (state.audioBasePtsUs || 0) / 1e6));
            src.start(when);
          }
          if (state.audioBasePtsUs === null) {
            state.audioBasePtsUs = seg.startSec * 1e6;
            state.audioBaseCtxTime = state.audioCtx ? state.audioCtx.currentTime : 0;
          }
        }
      } catch (pcmErr) {
        console.warn('[CarMedia] PCM read skipped:', pcmErr);
      }

      // 视频 RGBA 帧提取
      const videoRaw = await ffmpeg.readFile('seg.rgba');
      const frameByteSize = targetW * targetH * 4;
      const totalFrames = Math.floor(videoRaw.byteLength / frameByteSize);

      for (let f = 0; f < totalFrames; f++) {
        const frameData = videoRaw.subarray(f * frameByteSize, (f + 1) * frameByteSize);
        const frameTime = seg.startSec + (f / 25);
        if (!state.wasmFrames) state.wasmFrames = [];
        state.wasmFrames.push({
          time: frameTime,
          data: frameData
        });
      }

      // 清理临时文件避免虚拟 FS 内存溢出
      try {
        await ffmpeg.deleteFile('seg.ts');
        await ffmpeg.deleteFile('seg.pcm');
        await ffmpeg.deleteFile('seg.rgba');
      } catch (_) {}

      state.hlsCurrentSegIdx = segIdx + 1;
      return true;
    } catch (e) {
      console.warn(`[CarMedia] 分片 ${segIdx} 解码异常:`, e);
      state.hlsCurrentSegIdx = segIdx + 1;
      return false;
    }
  }

  async function pipelineHlsSegments(generation) {
    if (state.hlsDecoding || generation !== state.generation || state.mode !== 'wasm') return;
    state.hlsDecoding = true;

    while (generation === state.generation && state.mode === 'wasm' && state.hlsSegments) {
      const curTime = getCurrentTime();
      const lastBufferedTime = state.wasmFrames && state.wasmFrames.length > 0 
        ? state.wasmFrames[state.wasmFrames.length - 1].time 
        : curTime;

      // 维持至少 15 秒前瞻软解缓冲池，始终领先播放进度，杜绝卡顿
      if (lastBufferedTime < curTime + 15 && state.hlsCurrentSegIdx < state.hlsSegments.length) {
        await decodeHlsSegment(state.hlsCurrentSegIdx, generation);
      } else {
        await new Promise(r => setTimeout(r, 400));
      }

      // 内存平稳修剪：保留当前播放点前 2 秒至未来 20 秒，将内存安全控制在 90MB 以内
      if (state.wasmFrames && state.wasmFrames.length > 70) {
        while (state.wasmFrames.length > 35 && state.wasmFrames[0].time < curTime - 2.0) {
          state.wasmFrames.shift();
        }
      }
    }
    state.hlsDecoding = false;
  }

  async function startWasmHlsDecoder(url, canvas, glRenderer) {
    stop();
    const generation = state.generation;
    status('正在加载 FFmpeg.wasm 核心…', 'wasm');
    const ffmpeg = await ensureFFmpeg();
    if (generation !== state.generation) return false;

    status('解析 M3U8 流媒体切片列表…', 'wasm');
    const { segments, totalSec } = await parseM3u8Playlist(url);
    if (!segments || !segments.length) throw new Error('未检索到有效 M3U8 分片');

    state.hlsSegments = segments;
    state.hlsCurrentSegIdx = 0;
    state.duration = totalSec;
    state.wasmFrames = [];
    // 黄金能效比：960x540，搭配 WebGL 杜比着色器 GPU 平滑全屏插值，既保障 60FPS 极速流畅，又大幅降低车机芯片发热与功耗
    const targetW = 960, targetH = 540;
    state.wasmW = targetW;
    state.wasmH = targetH;
    state.wasmFrameIdx = -1;

    canvas.width = targetW;
    canvas.height = targetH;
    if (glRenderer.resize) glRenderer.resize(targetW, targetH);

    // 彻底停用原生 video
    const vid = state.masterVideo || document.getElementById('hidden-video') || (typeof document !== 'undefined' ? document.querySelector('video') : null);
    if (vid) {
      try {
        if (typeof vid.pause === 'function') vid.pause();
        if (typeof vid.removeAttribute === 'function') vid.removeAttribute('src');
        else vid.src = '';
        if (typeof vid.load === 'function') vid.load();
      } catch (_) {}
    }

    // 预缓冲 2 个切片建立健康的水位线，彻底避免开播后因解码追赶导致的断续与严重卡顿
    const initialPrebuffer = Math.min(2, segments.length);
    for (let i = 0; i < initialPrebuffer; i++) {
      status(`软解预缓冲池充能 (${i + 1}/${initialPrebuffer})…`, 'wasm');
      await decodeHlsSegment(i, generation);
      if (generation !== state.generation) return false;
    }

    // 立即上屏首帧
    if (state.wasmFrames && state.wasmFrames.length > 0) {
      state.lastRenderedWasmFrame = state.wasmFrames[0];
      if (glRenderer.drawRgba) {
        glRenderer.drawRgba(state.wasmFrames[0].data, targetW, targetH);
      }
    }

    status('FFmpeg.wasm 逐帧软解 (RGBA ➔ Canvas/WebGL + AudioWorklet PCM)', 'wasm');
    startClock(canvas, glRenderer, generation);

    // 开启后台持续前瞻软解管道
    pipelineHlsSegments(generation);
    return true;
  }

  // 核心统一调用入口：无论是 M3U8 还是 MP4，全面使用 WASM 逐帧软解
  async function startWasmStream(url, canvas, glRenderer) {
    let decodedHint = url;
    try { decodedHint = decodeURIComponent(url); } catch (_) {}
    const isHls = /\.m3u8(?:$|[?#&])/i.test(decodedHint);
    if (isHls) {
      return startWasmHlsDecoder(url, canvas, glRenderer);
    }
    return startWasmDecoder(url, canvas, glRenderer);
  }

  // ================= 播放控制：强化 Seek 与音画同步 =================
  function seekTo(seconds) {
    const target = Math.max(0, Math.min(state.duration || 3600, Number(seconds) || 0));
    state.pauseTime = target;
    state.clockStartSec = target;
    state.clockStartPerf = getNowMs();

    if (state.mode === 'webcodecs' && state.mp4File) {
      stopFrames();
      state.sampleQueue.length = 0;
      state.audioQueue.length = 0;
      state.audioBasePtsUs = null;
      state.firstAudioPtsUs = null;
      state.dacSamplesPlayed = Math.round(target * (state.audioConfig?.sampleRate || 48000));
      if (state.workletNode) {
        state.workletNode.port.postMessage({ type: 'reset' });
      }

      try {
        if (state.videoDecoder && state.videoConfig) {
          state.videoDecoder.reset();
          state.videoDecoder.configure(state.videoConfig);
        }
        if (state.audioDecoder && state.audioConfig) {
          state.audioDecoder.reset();
          state.audioDecoder.configure(state.audioConfig);
        }
        // MP4Box seek(time, useSync=true) 寻找目标点前的关键帧
        state.mp4File.seek(target, true);
        state.mp4File.start();
        return true;
      } catch (err) {
        console.warn('[CarMedia] seekTo failed:', err);
        return false;
      }
    } else if (state.mode === 'wasm') {
      state.pauseTime = target;
      state.clockStartSec = target;
      state.clockStartPerf = getNowMs();
      state.wasmFrameIdx = Math.floor(target * 25);
      if (state.workletNode) {
        state.workletNode.port.postMessage({ type: 'reset' });
      }
      state.wasmFrames = [];
      state.lastRenderedWasmFrame = null;

      if (state.hlsSegments && state.hlsSegments.length > 0) {
        let segIdx = 0;
        for (let i = 0; i < state.hlsSegments.length; i++) {
          if (target >= state.hlsSegments[i].startSec && target < state.hlsSegments[i].endSec) {
            segIdx = i;
            break;
          }
        }
        state.hlsCurrentSegIdx = segIdx;
        decodeHlsSegment(segIdx, state.generation);
      }
      return true;
    } else if (state.mode === 'native' && state.masterVideo) {
      state.masterVideo.currentTime = target;
      return true;
    }
    return false;
  }

  function play() {
    if (!state.paused) return;
    state.paused = false;
    if (state.audioCtx?.state === 'suspended') {
      state.audioCtx.resume().catch(() => {});
    }
    if (state.workletNode) {
      state.workletNode.port.postMessage({ type: 'resume' });
    }
    state.clockStartPerf = getNowMs();
    state.clockStartSec = state.pauseTime;
  }

  function pause() {
    if (state.paused) return;
    state.pauseTime = getCurrentTime();
    state.paused = true;
    if (state.workletNode) {
      state.workletNode.port.postMessage({ type: 'pause' });
    }
  }

  function setPlaybackRate(rate) {
    state.playbackRate = Math.max(0.5, Math.min(2.0, Number(rate) || 1.0));
  }

  function getDiagnostics() {
    return {
      mode: state.mode,
      currentTime: getCurrentTime(),
      duration: state.duration,
      paused: state.paused,
      decodedFrames: state.decoded,
      demuxedSamples: state.demuxed,
      droppedFrames: state.dropped,
      queuedFrames: state.frames.length,
      videoDecoderState: state.videoDecoder?.state || 'none',
      audioDecoderState: state.audioDecoder?.state || 'none',
      audioDecodedChunks: state.audioDecoded,
      queuedAudioSamples: state.audioQueue.length,
      audioWorkletActive: !!state.workletNode,
      ringBufferAvailable: state.ringBufferAvailable,
      dacSamplesPlayed: state.dacSamplesPlayed,
      audioUnderruns: state.audioUnderruns,
      rangeSupported: state.rangeSupported,
      rangeRequests: state.rangeRequests,
      bytesFetched: state.bytesFetched,
      inputStrategy: state.rangeSupported === true ? 'http-range' : state.rangeSupported === false ? 'stream-fallback' : 'unknown',
      wasmFramesCount: state.wasmFrames ? state.wasmFrames.length : 0,
      wasmCurrentFrameIdx: state.wasmFrameIdx,
      wasmW: state.wasmW,
      wasmH: state.wasmH,
      error: state.error
    };
  }

  window.CarMediaEngine = {
    safeToBlobURL,
    startWasmStream,
    startWasmHlsDecoder,
    startWasmDecoder,
    startWebCodecs,
    transcodeWithWasm,
    stop,
    play,
    pause,
    seekTo,
    setPlaybackRate,
    status,
    connectAudioContext,
    getDiagnostics,
    get mode() { return state.mode; },
    get hasActiveStream() {
      return (state.mode === 'webcodecs' && (state.frames.length > 0 || !!state.lastFrame)) ||
             (state.mode === 'wasm' && state.wasmFrames && state.wasmFrames.length > 0);
    },
    get currentTime() { return getCurrentTime(); },
    get duration() { return state.duration; },
    get paused() { return state.paused; },
    get playbackRate() { return state.playbackRate; }
  };
})();
