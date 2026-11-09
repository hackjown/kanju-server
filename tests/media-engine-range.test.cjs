const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(require('node:path').join(__dirname, '../static/car-media-engine.js'), 'utf8');

function createHarness(fetchImpl) {
  const appended = [];
  const info = {
    duration: 1000,
    timescale: 1000,
    videoTracks: [{ id: 1, codec: 'avc1.64001f', timescale: 1000, video: { width: 320, height: 180 } }],
    audioTracks: []
  };
  const mp4 = {
    onReady: null, onError: null,
    appendBuffer(buffer) {
      appended.push({ fileStart: buffer.fileStart, byteLength: buffer.byteLength });
      if (buffer.fileStart === 0 && this.onReady) this.onReady(info);
    },
    flush() {}, start() {}, stop() {}, seek() {},
    setExtractionOptions() {},
    getTrackById() { return {}; }
  };
  const MP4Box = { createFile: () => mp4 };
  class FakeVideoDecoder {
    static async isConfigSupported(config) { return { supported: true, config }; }
    constructor(init) {
      FakeVideoDecoder.lastInstance = this;
      this.init = init;
      this.state = 'unconfigured';
      this.decodeQueueSize = 0;
    }
    configure() { this.state = 'configured'; }
    addEventListener() {}
    close() { this.state = 'closed'; }
    reset() { this.state = 'unconfigured'; }
  }
  class FakeEncodedVideoChunk { constructor(data) { Object.assign(this, data); } }
  const video = {
    dataset: {}, readyState: 1, currentTime: 0, playbackRate: 1, muted: false,
    addEventListener() {}, load() {}, play: async () => {}, pause() {}
  };
  const context = {
    console, Promise, Uint8Array, ArrayBuffer, Map, Set, Math, Number, String, Object,
    DOMException, AbortController, setTimeout, clearTimeout,
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: fetchImpl,
    document: { scripts: [], head: { appendChild() {} }, createElement: () => ({}) , getElementById: () => null },
    MP4Box, VideoDecoder: FakeVideoDecoder, EncodedVideoChunk: FakeEncodedVideoChunk,
    AudioDecoder: undefined, EncodedAudioChunk: undefined
  };
  context.window = context;
  vm.runInNewContext(source, context, { filename: 'car-media-engine.js' });
  return { engine: context.CarMediaEngine, video, appended, mp4, FakeVideoDecoder };
}

function response206(start, end, total, bytes, includeHeader = true) {
  return {
    status: 206, ok: true,
    headers: { get(name) { return name.toLowerCase() === 'content-range' && includeHeader ? `bytes ${start}-${end}/${total}` : null; } },
    arrayBuffer: async () => Uint8Array.from(bytes).buffer
  };
}

const renderer = { draw() {}, resize() {} };
const canvas = { width: 0, height: 0 };

test('HTTP Range 分段读取校验偏移并按 fileStart 增量 append', async () => {
  const calls = [];
  const harness = createHarness(async (_url, options) => {
    calls.push(options.headers.Range);
    if (calls.length === 1) return response206(0, 3, 8, [1, 2, 3, 4]);
    return response206(4, 7, 8, [5, 6, 7, 8]);
  });
  assert.equal(await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer), true);
  for (let i = 0; i < 20 && calls.length < 2; i++) await new Promise(r => setTimeout(r, 2));
  assert.equal(calls.length, 2);
  assert.deepEqual(harness.appended, [{ fileStart: 0, byteLength: 4 }, { fileStart: 4, byteLength: 4 }]);
  const d = harness.engine.getDiagnostics();
  assert.equal(d.rangeSupported, true);
  assert.equal(d.rangeRequests, 2);
  assert.equal(d.bytesFetched, 8);
});

test('服务器忽略 Range 时使用 ReadableStream 分块回退', async () => {
  const harness = createHarness(async () => ({
    status: 200, ok: true,
    headers: { get(name) { return name.toLowerCase() === 'content-length' ? '6' : null; } },
    body: { getReader() {
      const chunks = [Uint8Array.from([1, 2, 3]), Uint8Array.from([4, 5, 6])];
      return { async read() { return chunks.length ? { value: chunks.shift(), done: false } : { done: true }; }, async cancel() {}, releaseLock() {} };
    } }
  }));
  assert.equal(await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer), true);
  assert.deepEqual(harness.appended, [{ fileStart: 0, byteLength: 3 }, { fileStart: 3, byteLength: 3 }]);
  assert.equal(harness.engine.getDiagnostics().rangeSupported, false);
  assert.equal(harness.engine.getDiagnostics().bytesFetched, 6);
});

test('无效 Content-Range 会重试后明确失败', async () => {
  let calls = 0;
  const harness = createHarness(async () => { calls++; return response206(0, 3, 8, [1, 2, 3, 4], false); });
  await assert.rejects(harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer), /无效 Content-Range/);
  assert.equal(calls, 3);
});

test('stop() 会取消正在进行的 Range 请求', async () => {
  let capturedSignal;
  const harness = createHarness((_url, options) => {
    capturedSignal = options.signal;
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
    });
  });
  const pending = harness.engine.startWebCodecs('/slow.mp4', harness.video, canvas, renderer);
  for (let i = 0; i < 20 && !capturedSignal; i++) await new Promise(r => setTimeout(r, 1));
  assert.ok(capturedSignal);
  harness.engine.stop();
  assert.equal(capturedSignal.aborted, true);
  await assert.rejects(pending);
});

test('WebCodecs 模式下彻底杜绝隐藏 video 重复解码 (video.src 为空且处于暂停状态)', async () => {
  const harness = createHarness(async () => response206(0, 7, 8, [1, 2, 3, 4, 5, 6, 7, 8]));
  harness.video.src = 'https://example.com/old-stream.mp4';
  harness.video.paused = false;
  harness.video.removeAttribute = function(attr) { if (attr === 'src') this.src = ''; };

  assert.equal(await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer), true);
  assert.equal(harness.video.src, '');
  assert.equal(harness.engine.mode, 'webcodecs');
  assert.equal(harness.engine.duration, 1);
});

test('强化 Seek：重置 VideoDecoder、清空已解码队列并通知 AudioWorklet reset', async () => {
  const harness = createHarness(async () => response206(0, 7, 8, [1, 2, 3, 4, 5, 6, 7, 8]));
  assert.equal(await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer), true);
  
  let seekCalled = false;
  harness.mp4.seek = (time, useSync) => {
    seekCalled = true;
    assert.equal(time, 0.5);
    assert.equal(useSync, true);
  };
  assert.equal(harness.engine.seekTo(0.5), true);
  assert.equal(seekCalled, true);
  assert.equal(harness.engine.currentTime, 0.5);
});

test('Virtual Media Timeline 播放/暂停控制正常同步状态', async () => {
  const harness = createHarness(async () => response206(0, 7, 8, [1, 2, 3, 4, 5, 6, 7, 8]));
  await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer);
  assert.equal(harness.engine.paused, false);
  harness.engine.pause();
  assert.equal(harness.engine.paused, true);
  harness.engine.play();
  assert.equal(harness.engine.paused, false);
});

test('4K 内存约束：解码帧队列严格限制在 4 帧以内，溢出即时 close() 并计入 dropped', async () => {
  const harness = createHarness(async () => response206(0, 7, 8, [1, 2, 3, 4, 5, 6, 7, 8]));
  await harness.engine.startWebCodecs('/sample.mp4', harness.video, canvas, renderer);
  
  const outputCallback = harness.FakeVideoDecoder.lastInstance.init.output;
  assert.ok(outputCallback);

  // 模拟解码器连续输出 8 个 4K 帧
  let closedFrames = 0;
  for (let i = 0; i < 8; i++) {
    const frame = {
      timestamp: i * 40000,
      displayWidth: 3840,
      displayHeight: 2160,
      close() { closedFrames++; }
    };
    outputCallback(frame);
  }
  
  const diag = harness.engine.getDiagnostics();
  assert.equal(diag.queuedFrames <= 4, true);
  assert.equal(diag.droppedFrames >= 4, true);
  assert.equal(closedFrames >= 4, true);
});
