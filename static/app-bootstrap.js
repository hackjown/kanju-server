(function bootstrapKanjuPage() {
  document.documentElement.dataset.theme = "movie-dark";
  document.documentElement.classList.add("dark");

  // ---------------- 1. Web Crypto / 安全上下文兼容垫片（专为局域网 HTTP / Docker 部署设计） ----------------
  // 在非 HTTPS (如 http://192.168.x.x:8077) 环境下，Safari/Chrome 默认禁用 crypto.subtle 与 crypto.randomUUID
  // 若无 polyfill，前端 HMAC 签名与会话初始化将抛出 TypeError，阻断 /v1/* 接口与验证码请求
  try {
    if (typeof window.crypto === "undefined") {
      window.crypto = {};
    }

    // 1.1 crypto.randomUUID 垫片
    if (typeof window.crypto.randomUUID !== "function") {
      var genRandomUUID = function() {
        if (typeof window.crypto.getRandomValues === "function") {
          var b = new Uint8Array(16);
          window.crypto.getRandomValues(b);
          b[6] = (b[6] & 0x0f) | 0x40;
          b[8] = (b[8] & 0x3f) | 0x80;
          var hex = Array.from(b, function(x) { return x.toString(16).padStart(2, "0"); }).join("");
          return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20, 32);
        }
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
          var r = (Math.random() * 16) | 0, v = c === "x" ? r : ((r & 0x3) | 0x8);
          return v.toString(16);
        });
      };
      try {
        Object.defineProperty(window.crypto, "randomUUID", {
          value: genRandomUUID,
          configurable: true,
          writable: true
        });
      } catch (_) {
        window.crypto.randomUUID = genRandomUUID;
      }
    }

    // 1.2 纯 JS SHA-256 与 HMAC-SHA-256 算法实现（100% 严格对齐 RFC 6234 与 Python hashlib）
    var K_SHA256 = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ];

    function rawSha256(data) {
      var bytes = typeof data === "string" ? new TextEncoder().encode(data) : new Uint8Array(data);
      var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
      var bitLen = bytes.length * 8;
      var newLen = ((bytes.length + 8) >> 6 << 6) + 64;
      var padded = new Uint8Array(newLen);
      padded.set(bytes);
      padded[bytes.length] = 0x80;
      var view = new DataView(padded.buffer);
      view.setUint32(newLen - 4, bitLen, false);

      var W = new Int32Array(64);
      for (var i = 0; i < newLen; i += 64) {
        for (var j = 0; j < 16; j++) {
          W[j] = view.getInt32(i + (j << 2), false);
        }
        for (var j = 16; j < 64; j++) {
          var s0 = ((W[j - 15] >>> 7) | (W[j - 15] << 25)) ^ ((W[j - 15] >>> 18) | (W[j - 15] << 14)) ^ (W[j - 15] >>> 3);
          var s1 = ((W[j - 2] >>> 17) | (W[j - 2] << 15)) ^ ((W[j - 2] >>> 19) | (W[j - 2] << 13)) ^ (W[j - 2] >>> 10);
          W[j] = (W[j - 16] + s0 + W[j - 7] + s1) | 0;
        }
        var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
        for (var j = 0; j < 64; j++) {
          var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
          var ch = (e & f) ^ ((~e) & g);
          var temp1 = (h + S1 + ch + K_SHA256[j] + W[j]) | 0;
          var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
          var maj = (a & b) ^ (a & c) ^ (b & c);
          var temp2 = (S0 + maj) | 0;
          h = g; g = f; f = e; e = (d + temp1) | 0;
          d = c; c = b; b = a; a = (temp1 + temp2) | 0;
        }
        H[0] = (H[0] + a) | 0;
        H[1] = (H[1] + b) | 0;
        H[2] = (H[2] + c) | 0;
        H[3] = (H[3] + d) | 0;
        H[4] = (H[4] + e) | 0;
        H[5] = (H[5] + f) | 0;
        H[6] = (H[6] + g) | 0;
        H[7] = (H[7] + h) | 0;
      }
      var res = new Uint8Array(32);
      var outView = new DataView(res.buffer);
      for (var i = 0; i < 8; i++) {
        outView.setInt32(i << 2, H[i], false);
      }
      return res;
    }

    function rawHmacSha256(keyData, msgData) {
      var te = new TextEncoder();
      var key = typeof keyData === "string" ? te.encode(keyData) : new Uint8Array(keyData);
      var msg = typeof msgData === "string" ? te.encode(msgData) : new Uint8Array(msgData);
      var block = 64;
      if (key.length > block) {
        key = rawSha256(key);
      }
      var oPad = new Uint8Array(block);
      var iPad = new Uint8Array(block);
      for (var i = 0; i < block; i++) {
        var k = i < key.length ? key[i] : 0;
        oPad[i] = k ^ 0x5c;
        iPad[i] = k ^ 0x36;
      }
      var inner = new Uint8Array(block + msg.length);
      inner.set(iPad, 0);
      inner.set(msg, block);
      var innerHash = rawSha256(inner);
      var outer = new Uint8Array(block + 32);
      outer.set(oPad, 0);
      outer.set(innerHash, block);
      return rawSha256(outer);
    }

    // 暴露全局快速签名 helper
    window.__kanjuHmac = function(k, m) {
      var u8 = rawHmacSha256(k, m);
      return Array.from(u8, function(x) { return x.toString(16).padStart(2, "0"); }).join("");
    };

    // 1.3 crypto.subtle 垫片
    if (!window.crypto.subtle) {
      var subtlePolyfill = {
        importKey: async function(format, keyData, algorithm, extractable, keyUsages) {
          return { _rawKey: keyData, algorithm: algorithm };
        },
        sign: async function(algorithm, key, data) {
          var rawKey = (key && key._rawKey) ? key._rawKey : key;
          var sig = rawHmacSha256(rawKey, data);
          return sig.buffer;
        },
        digest: async function(algorithm, data) {
          var hash = rawSha256(data);
          return hash.buffer;
        }
      };
      try {
        Object.defineProperty(window.crypto, "subtle", {
          value: subtlePolyfill,
          configurable: true,
          writable: true
        });
      } catch (_) {
        window.crypto.subtle = subtlePolyfill;
      }
    }
  } catch (err) {
    console.warn("Crypto polyfill initialized with notice:", err);
  }

  // ---------------- 2. 成年人确认标记、会话同步与清理广告缓存 ----------------
  try {
    if (!localStorage.getItem("movie-search:adult-catalog-confirmed")) {
      localStorage.setItem("movie-search:adult-catalog-confirmed", "true");
    }
    if (!localStorage.getItem("movie-search:midnight-player-tutorial-seen")) {
      localStorage.setItem("movie-search:midnight-player-tutorial-seen", "true");
    }

    // 检查是否有登录凭证，确保前端 localStorage 状态即时同步
    if (document.cookie.includes("ai_movie_session") || document.cookie.includes("kj_sid")) {
      localStorage.setItem("movie-search:account-signed-in", "1");
      if (!localStorage.getItem("movie-search:account-scope")) {
        localStorage.setItem("movie-search:account-scope", "usr_1");
      }
    }

    // 清除历史缓存中的广告与翻转推荐配置
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && (k.includes("app-experience") || k.includes("player-info-ad") || k.includes("startup-ad"))) {
        localStorage.removeItem(k);
      }
    }
  } catch (e) {
    console.warn("Bootstrap storage initialization failed:", e);
  }
})();
