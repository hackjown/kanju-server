#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 index.html 里 KJ3 的收藏/历史/续看接到会员接口：
   - 收藏：登录后 KJ4.favToggle / favDel / favList；未登录仍用 localStorage
   - 历史：登录后 KJ4.histPut / histList / histClear；未登录仍用 localStorage
   做法：在 KANJU_UI_V3 段末尾（window.KJ3 = KJ3; 前）注入一层 KJ3 包装。
"""
import io, os, re, sys

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
s = io.open(P, encoding="utf-8").read()

MARK = "/* ===== KJ3_SERVER_SYNC START ===== */"
if MARK in s:
    print("已注入过，先剥离旧段")
    a = s.index(MARK)
    b = s.index("/* ===== KJ3_SERVER_SYNC END ===== */") + len("/* ===== KJ3_SERVER_SYNC END ===== */")
    s = s[:a] + s[b:]

BLOCK = r"""
/* ===== KJ3_SERVER_SYNC START ===== */
/* 收藏 / 历史 / 续看：登录后走会员接口（跟账号走，换机也在）；游客仍用 localStorage */
(function(){
  if(!window.KJ3) return;
  var K = window.KJ3;
  var _isFav = K.isFav, _toggleFav = K.toggleFav, _history = K.history;

  function logged(){ return !!(window.KJ4 && window.KJ4.me && window.KJ4.me()); }
  function cidOf(x){ return String(x && (x.id || x.cid || x.token) || ""); }

  /* 收藏：本地写一份做即时反馈，登录时同步写服务器 */
  K.toggleFav = function(item){
    var on = _toggleFav(item);
    if(logged()){
      var cid = cidOf(item);
      if(cid && window.KJ4.favToggle){
        window.KJ4.favToggle(item).then(function(){ /* 服务器已写 */ });
      }
    }
    return on;
  };

  /* 续看/历史：KJ3.saveResume 已经写了 localStorage，这里再补写服务器 */
  var _saveResume = K.saveResume;
  K.saveResume = function(token, epTitle, tid, curTitle, time){
    _saveResume(token, epTitle, tid, curTitle, time);
    if(logged() && window.KJ4.histPut){
      window.KJ4.histPut({
        cid: String(tid || token || ""),
        title: curTitle || "",
        episode: epTitle || "",
        position: Number(time) || 0,
        duration: 0,
        poster: ""
      });
    }
  };

  /* 历史页：登录态下用服务器数据渲染 */
  var _openHistory = K.openHistory;
  K.openHistory = function(){
    if(!logged()) return _openHistory();
    var home = document.getElementById('home'), det = document.getElementById('detail');
    if(det) det.style.display = 'none';
    if(!home) return;
    home.style.display = 'block';
    home.innerHTML = '';
    var sec = document.createElement('div'); sec.className = 'sec';
    var h2 = document.createElement('h2'); h2.textContent = '观看历史（会员账号 · 跟账号走）';
    sec.appendChild(h2);
    home.appendChild(sec);
    window.KJ4.histList().then(function(items){
      if(!items.length){ sec.appendChild(document.createTextNode('还没有观看记录')); return; }
      var row = document.createElement('div'); row.className = 'row';
      items.forEach(function(x){
        var d = document.createElement('div'); d.className = 'card';
        d.innerHTML = '<div class="t">' + String(x.title || x.cid || '').replace(/[<>&]/g,'') + '</div>' +
                      '<div class="s">' + String(x.episode || '').replace(/[<>&]/g,'') + ' · 看至 ' +
                      fmtSec(x.position) + '</div>';
        d.onclick = function(){ if(window.openDetail) openDetail(x.cid); };
        row.appendChild(d);
      });
      sec.appendChild(row);
    }).catch(function(){ sec.appendChild(document.createTextNode('加载失败，请重试')); });
  };

  /* 收藏页：登录态下用服务器数据渲染 */
  var _openFav = K.openFav;
  K.openFav = function(){
    if(!logged()) return _openFav();
    var home = document.getElementById('home'), det = document.getElementById('detail');
    if(det) det.style.display = 'none';
    if(!home) return;
    home.style.display = 'block';
    home.innerHTML = '';
    var sec = document.createElement('div'); sec.className = 'sec';
    var h2 = document.createElement('h2'); h2.textContent = '我的收藏（会员账号 · 跟账号走）';
    sec.appendChild(h2);
    home.appendChild(sec);
    window.KJ4.favList().then(function(items){
      if(!items.length){ sec.appendChild(document.createTextNode('还没有收藏')); return; }
      var row = document.createElement('div'); row.className = 'row';
      items.forEach(function(x){
        var d = document.createElement('div'); d.className = 'card';
        d.innerHTML = '<div class="t">' + String(x.title || x.cid || '').replace(/[<>&]/g,'') + '</div>' +
                      '<div class="s">点击播放 · 取消收藏点 ×</div>';
        var xb = document.createElement('span'); xb.textContent = '×';
        xb.style.cssText = 'position:absolute;right:6px;top:6px;background:rgba(0,0,0,.6);color:#fff;' +
          'width:20px;height:20px;border-radius:50%;text-align:center;line-height:20px;cursor:pointer;z-index:2';
        d.style.position = 'relative';
        xb.onclick = function(e){
          e.stopPropagation();
          window.KJ4.favDel(x.cid).then(function(){ d.remove(); if(window.KJ3.toast) KJ3.toast('已取消收藏'); });
        };
        d.appendChild(xb);
        d.onclick = function(){ if(window.openDetail) openDetail(x.cid); };
        row.appendChild(d);
      });
      sec.appendChild(row);
    }).catch(function(){ sec.appendChild(document.createTextNode('加载失败，请重试')); });
  };

  function fmtSec(t){
    t = Math.max(0, Math.floor(Number(t)||0));
    var m = Math.floor(t/60), ss = t%60;
    return m + ':' + (ss<10?'0':'') + ss;
  }

  /* 登录状态变化后刷新按钮/页面 */
  K.refresh = function(){
    if(window.KJ4 && window.KJ4.refresh) return window.KJ4.refresh();
    return Promise.resolve(null);
  };
})();
/* ===== KJ3_SERVER_SYNC END ===== */
"""

# 注入到 KANJU_UI_V3 END 之前（该段里 window.KJ3 = KJ3; 之后）
anchor = "window.KJ3 = KJ3;"
i = s.find(anchor)
if i < 0:
    print("ERROR: 找不到 window.KJ3 = KJ3;"); sys.exit(1)
i = s.find("})();", i)  # 该 IIFE 的结尾
if i < 0:
    print("ERROR: 找不到 IIFE 结尾"); sys.exit(1)
i += len("})();")
s = s[:i] + "\n" + BLOCK + s[i:]

io.open(P, "w", encoding="utf-8").write(s)
print("OK 注入完成，index.html 现 %d 行 / %d 字节" % (s.count("\n")+1, len(s.encode("utf-8"))))
for k in ["KJ3_SERVER_SYNC", "KJ4.histList", "KJ4.favList", "KJ4.favDel", "fmtSec"]:
    print("  %s = %d" % (k, s.count(k)))
