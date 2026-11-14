# -*- coding: utf-8 -*-
"""
剧场8 iOS App（H5Shell 壳）配套接口

改过的 IPA 启动时不再去 kanju 的配置地址，而是请求本服务器：
  GET /cfg/<任意>.json 或 /app-config.json  → 明文 JSON 启动配置（apiUrl 指向选服务器页）
  GET /app-select                           → 选择服务器页面（本服务器 / kanju.ai 官方）

选择会记在 App 的本地存储里，下次启动会倒计时 3 秒自动进入上次的选择，倒计时期间可以改选。
"""
import json
import os
import time

from flask import Response, jsonify, request

# 改这里可以增减选项；"self" 表示当前这台服务器
SERVERS = [
    {"key": "self", "name": "我的服务器", "desc": "剧场8 · 8077 服务器（网盘源 / 4K 源）", "url": "self"},
    {"key": "kanju", "name": "看剧AI 官方", "desc": "kanju.ai 官方服务器", "url": "https://kanju.ai/"},
]

CONFIG_VERSION = os.environ.get("APPSHELL_CONFIG_VERSION", "2026111501")


def _base():
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "http").split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}"


def _config():
    base = _base()
    return {
        "appName": "剧场吧AI",
        "type": "web",
        "apiUrl": base + "/app-select",
        "enable": True,
        "config": {"region": "CN"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fallbackPage": {
            "title": "暂时无法连接服务器",
            "message": "请检查网络，或稍后重新打开 App。",
            "links": [{"title": "我的服务器", "url": base + "/"},
                      {"title": "看剧AI 官方", "url": "https://kanju.ai/"}],
        },
        "version": CONFIG_VERSION,
    }


PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#080706">
<title>剧场吧AI · 选择服务器</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#080706;color:#f2f2f2;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{min-height:100%;display:flex;flex-direction:column;justify-content:center;padding:calc(env(safe-area-inset-top) + 32px) 22px calc(env(safe-area-inset-bottom) + 32px);max-width:520px;margin:0 auto}
h1{font-size:28px;margin:0 0 6px;letter-spacing:1px}
.sub{color:#9a9a9a;font-size:14px;margin:0 0 28px}
.opt{display:block;width:100%;text-align:left;border:1px solid #2a2a2a;background:#141312;color:inherit;border-radius:16px;padding:18px 18px;margin:0 0 14px;font-size:17px;cursor:pointer;transition:border-color .15s,background .15s}
.opt b{display:block;font-size:18px;margin-bottom:4px}
.opt span{color:#9a9a9a;font-size:13px}
.opt.on{border-color:#16E05A;background:#0f1d14}
.opt.on b:after{content:"  上次选择";font-size:12px;color:#16E05A;font-weight:500}
.go{width:100%;border:0;border-radius:14px;background:#16E05A;color:#04120a;font-size:17px;font-weight:700;padding:15px;margin-top:8px}
.tip{color:#7d7d7d;font-size:13px;text-align:center;margin-top:16px;min-height:18px}
.custom{display:flex;gap:8px;margin:4px 0 14px}
.custom input{flex:1;min-width:0;background:#141312;border:1px solid #2a2a2a;border-radius:12px;color:#fff;padding:12px;font-size:15px}
.custom button{border:1px solid #2a2a2a;background:#1d1c1b;color:#ddd;border-radius:12px;padding:0 14px;font-size:15px}
</style></head>
<body><div class="wrap">
<h1>剧场吧AI</h1>
<p class="sub">选择要连接的服务器，之后登录、播放都走这台服务器</p>
<div id="list"></div>
<div class="custom"><input id="cu" placeholder="其他地址，例如 192.168.1.10:8077" autocapitalize="off" autocorrect="off"><button id="cub">进入</button></div>
<button class="go" id="go">进入</button>
<div class="tip" id="tip"></div>
</div>
<script>
var SERVERS = __SERVERS__;
var SELF = location.protocol + "//" + location.host + "/";
var KEY = "jc8_app_server";
var list = document.getElementById("list"), tip = document.getElementById("tip");
var saved = null; try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
var picked = saved && saved.key ? saved.key : SERVERS[0].key;
var timer = null, left = 3;
function urlOf(s) { return s.url === "self" ? SELF : s.url; }
function render() {
  list.innerHTML = "";
  SERVERS.forEach(function (s) {
    var b = document.createElement("button");
    b.className = "opt" + (saved && saved.key === s.key ? " on" : "");
    b.innerHTML = "<b>" + s.name + "</b><span>" + s.desc + "</span>";
    b.onclick = function () { go({key: s.key, url: urlOf(s)}); };
    list.appendChild(b);
  });
}
function norm(v) {
  v = (v || "").trim(); if (!v) return "";
  if (!/^https?:\/\//i.test(v)) v = (/:\d+$/.test(v.split("/")[0]) && !/:443$/.test(v.split("/")[0]) ? "http://" : "https://") + v;
  return v.replace(/\/+$/, "") + "/";
}
function go(c) {
  if (timer) { clearInterval(timer); timer = null; }
  try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {}
  tip.textContent = "正在进入…";
  location.replace(c.url);
}
document.getElementById("cub").onclick = function () {
  var u = norm(document.getElementById("cu").value);
  if (!u) { tip.textContent = "请填写服务器地址"; return; }
  go({key: "custom", url: u});
};
document.getElementById("go").onclick = function () {
  if (saved && saved.url) return go(saved);
  var s = SERVERS.filter(function (x) { return x.key === picked; })[0] || SERVERS[0];
  go({key: s.key, url: urlOf(s)});
};
render();
if (saved && saved.url) {
  var name = (SERVERS.filter(function (x) { return x.key === saved.key; })[0] || {name: saved.url}).name;
  document.getElementById("go").textContent = "进入 " + name;
  var stop = function () { if (timer) { clearInterval(timer); timer = null; tip.textContent = "已取消自动进入，请选择服务器"; } };
  document.addEventListener("touchstart", stop, {once: true});
  document.addEventListener("mousedown", stop, {once: true});
  tip.textContent = left + " 秒后自动进入 " + name + "，点一下屏幕可以改选";
  timer = setInterval(function () {
    left -= 1;
    if (left <= 0) { clearInterval(timer); timer = null; go(saved); return; }
    tip.textContent = left + " 秒后自动进入 " + name + "，点一下屏幕可以改选";
  }, 1000);
}
</script></body></html>"""


def register(app):
    def cfg_resp():
        resp = jsonify(_config())
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @app.route("/app-config.json")
    def appshell_config():
        return cfg_resp()

    @app.route("/cfg/<path:name>")
    def appshell_config_any(name):
        return cfg_resp()

    @app.route("/app-select")
    def appshell_select():
        html = PAGE.replace("__SERVERS__", json.dumps(SERVERS, ensure_ascii=False))
        resp = Response(html, content_type="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    print("[appshell] iOS App 选服务器页已启用：/app-select，启动配置：/app-config.json")
