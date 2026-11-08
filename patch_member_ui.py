#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把会员前端 UI（登录/注册弹窗 + 顶栏用户区 + 收藏/历史接后端 + 18+ 入口）注入 index.html
幂等：已注入则跳过"""
import io, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(HERE, "static", "index.html")
s = open(P, encoding="utf-8").read()

if "KANJU_MEMBER_UI START" in s:
    print("已注入过，跳过")
    sys.exit(0)

BLOCK = r'''
<!-- ===== KANJU_MEMBER_UI START ===== -->
<style>
#kjAuthMask{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:10000;display:none;align-items:center;justify-content:center}
#kjAuthBox{width:360px;max-width:92vw;background:#171a21;border:1px solid #252a34;border-radius:14px;padding:22px}
#kjAuthBox h3{margin:0 0 4px;font-size:17px}
#kjAuthBox .tip{color:#8b8579;font-size:12px;margin-bottom:16px;line-height:1.5}
#kjAuthBox label{display:block;font-size:12px;color:#b9b3a7;margin:10px 0 5px}
#kjAuthBox input{width:100%;box-sizing:border-box;background:#1b1f27;border:1px solid #252a34;color:#e9e6df;padding:9px 11px;border-radius:8px;outline:none;font-size:13px}
#kjAuthBox input:focus{border-color:#e8b44a}
#kjAuthErr{color:#ff7a6b;font-size:12px;min-height:16px;margin-top:8px}
#kjAuthBtn{width:100%;margin-top:12px;background:#e8b44a;color:#1a1200;border:0;padding:10px;border-radius:8px;font-weight:700;cursor:pointer;font-size:14px}
#kjAuthSw{text-align:center;margin-top:12px;font-size:12.5px;color:#8b8579}
#kjAuthSw a{color:#e8b44a;cursor:pointer;text-decoration:none}
#kjAuthClose{float:right;color:#8b8579;cursor:pointer;font-size:18px;line-height:1}
.kjuser{display:flex;align-items:center;gap:8px;font-size:13px}
.kjuser .nm{color:#e8b44a;font-weight:700}
.kjuser button{background:#1b1f27;border:1px solid #252a34;color:#c9c4bb;padding:7px 13px;border-radius:8px;cursor:pointer;font-size:12.5px}
.kjuser button:hover{border-color:#e8b44a}
.kjuser button.pri{background:#e8b44a;border-color:#e8b44a;color:#14161b;font-weight:700}
.kjuser button.adult{background:#3a1f26;border-color:#6b2f3a;color:#ffb3bf}
#kjAdultMask{position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:10001;display:none;align-items:center;justify-content:center}
#kjAdultBox{width:400px;max-width:92vw;background:#171a21;border:1px solid #6b2f3a;border-radius:14px;padding:24px;text-align:center}
#kjAdultBox h3{margin:0 0 10px;font-size:18px;color:#ffb3bf}
#kjAdultBox p{color:#b9b3a7;font-size:13px;line-height:1.7;margin:0 0 16px}
#kjAdultBox button{background:#e8b44a;color:#1a1200;border:0;padding:10px 22px;border-radius:8px;font-weight:700;cursor:pointer;margin:0 6px}
#kjAdultBox button.no{background:#1b1f27;color:#c9c4bb;border:1px solid #252a34}
</style>

<div id="kjAuthMask"><div id="kjAuthBox">
  <span id="kjAuthClose">×</span>
  <h3 id="kjAuthTitle">登录 看剧AI</h3>
  <div class="tip" id="kjAuthTip">登录后收藏、观看历史、进度会跟随账号</div>
  <label>账号</label><input id="kjAuthAcc" placeholder="账号（≥4 位）" autocomplete="username"/>
  <label>密码</label><input id="kjAuthPwd" type="password" placeholder="密码（≥6 位）" autocomplete="current-password"/>
  <div id="kjInviteWrap" style="display:none"><label>邀请码</label><input id="kjAuthInv" placeholder="邀请码（如 KANJU2026）"/></div>
  <div id="kjAuthErr"></div>
  <button id="kjAuthBtn">登录</button>
  <div id="kjAuthSw">还没有账号？<a id="kjAuthToggle">立即注册</a></div>
</div></div>

<div id="kjAdultMask"><div id="kjAdultBox">
  <h3>18+ 午夜剧场</h3>
  <p>本区含成人向内容，仅限已满 18 周岁的登录用户访问。<br/>确认后，该偏好会记入你的账号。</p>
  <button class="no" id="kjAdultNo">未满 18，返回</button>
  <button id="kjAdultYes">我已满 18 岁，进入</button>
</div></div>

<script>
(function(){
  var API='/api', me=null, mode='login';
  function $(id){ return document.getElementById(id); }
  function toast(m){ if(window.KJ3&&KJ3.toast) return KJ3.toast(m);
    var t=document.createElement('div'); t.textContent=m;
    t.style.cssText='position:fixed;left:50%;bottom:40px;transform:translateX(-50%);background:rgba(0,0,0,.85);color:#e9e6df;padding:10px 18px;border-radius:8px;font-size:13px;z-index:10002';
    document.body.appendChild(t); setTimeout(function(){t.remove();},1800); }
  function req(u,o){ o=o||{}; o.headers=o.headers||{}; o.credentials='same-origin';
    if(o.body&&typeof o.body!=='string'){ o.headers['Content-Type']='application/json'; o.body=JSON.stringify(o.body); }
    return fetch(API+u,o).then(function(r){ return r.json().catch(function(){return{};}); }); }

  /* ---------- 认证 ---------- */
  function openAuth(m){
    mode=m||'login';
    $('kjAuthTitle').textContent = mode==='login'?'登录 看剧AI':'注册 看剧AI';
    $('kjAuthTip').textContent = mode==='login'?'登录后收藏、观看历史、进度会跟随账号':'注册需邀请码；注册后自动登录';
    $('kjAuthBtn').textContent = mode==='login'?'登录':'注册并登录';
    $('kjInviteWrap').style.display = mode==='login'?'none':'block';
    $('kjAuthSw').innerHTML = mode==='login'?'还没有账号？<a id="kjAuthToggle">立即注册</a>':'已有账号？<a id="kjAuthToggle">去登录</a>';
    $('kjAuthErr').textContent='';
    $('kjAuthMask').style.display='flex';
    setTimeout(function(){ $('kjAuthAcc').focus(); },50);
  }
  function closeAuth(){ $('kjAuthMask').style.display='none'; }

  function doAuth(){
    var acc=$('kjAuthAcc').value.trim(), pwd=$('kjAuthPwd').value;
    if(!acc||!pwd){ $('kjAuthErr').textContent='请填写账号和密码'; return; }
    var body={account:acc,password:pwd};
    if(mode==='register') body.invite_code=$('kjAuthInv').value.trim();
    $('kjAuthBtn').disabled=true;
    req(mode==='login'?'/auth/login':'/auth/register',{method:'POST',body:body}).then(function(d){
      $('kjAuthBtn').disabled=false;
      if(!d.ok){ $('kjAuthErr').textContent=d.error||'操作失败'; return; }
      me=d.user; closeAuth(); renderUser(); toast(mode==='login'?'已登录':'注册成功，已登录');
      syncLocalToServer();
      if(window.KJ3&&KJ3.refresh) KJ3.refresh();
    }).catch(function(){ $('kjAuthBtn').disabled=false; $('kjAuthErr').textContent='网络错误'; });
  }

  /* ---------- 顶栏用户区 ---------- */
  function renderUser(){
    var nav=$('kjUserNav');
    if(!nav){ nav=document.createElement('div'); nav.id='kjUserNav'; nav.className='kjuser';
      var bar=document.querySelector('header .bar'); if(bar) bar.appendChild(nav); }
    nav.innerHTML='';
    if(!me){
      var b=document.createElement('button'); b.className='pri'; b.textContent='登录 / 注册';
      b.onclick=function(){ openAuth('login'); }; nav.appendChild(b);
      var m=document.createElement('button'); m.textContent='🌙 18+';
      m.onclick=function(){ goMidnight(); }; nav.appendChild(m);
    } else {
      var nm=document.createElement('span'); nm.className='nm'; nm.textContent=(me.nickname||me.account);
      nm.title='等级 Lv.'+me.level+' · 词元 '+me.tokens+' · 成长 '+me.growth; nav.appendChild(nm);
      var a=document.createElement('button'); a.className=me.adult_confirmed?'adult':'';
      a.textContent=me.adult_confirmed?'🌙 午夜剧场':'🌙 18+';
      a.onclick=function(){ goMidnight(); }; nav.appendChild(a);
      var o=document.createElement('button'); o.textContent='退出';
      o.onclick=function(){ req('/auth/logout',{method:'POST'}).then(function(){ me=null; renderUser(); toast('已退出'); if(window.KJ3&&KJ3.refresh) KJ3.refresh(); }); };
      nav.appendChild(o);
    }
  }

  /* ---------- 18+ 入口 ---------- */
  function goMidnight(){
    if(!me){ toast('请先登录后再进入午夜剧场'); openAuth('login'); return; }
    req('/midnight/state').then(function(d){
      if(d.state==='unlocked'){ location.href='/midnight-theater'; return; }
      $('kjAdultMask').style.display='flex';
    });
  }
  function confirmAdult(){
    req('/auth/adult',{method:'POST',body:{adult_confirmed:true}}).then(function(d){
      $('kjAdultMask').style.display='none';
      if(d.ok){ me=d.user; renderUser(); toast('已确认成年，正在进入…'); setTimeout(function(){ location.href='/midnight-theater'; },400); }
      else { toast(d.error||'确认失败'); }
    });
  }

  /* ---------- 收藏 / 历史：本地 → 服务器 ---------- */
  function syncLocalToServer(){
    if(!me) return;
    try{
      var fav=JSON.parse(localStorage.getItem('kjFav')||'[]');
      fav.slice(0,200).forEach(function(x){
        var cid=x.id||x.cid||x.token; if(!cid) return;
        req('/fav',{method:'POST',body:{cid:String(cid),title:x.title||'',poster:x.poster||''}});
      });
      var hist=JSON.parse(localStorage.getItem('kjHist')||'[]');
      (Array.isArray(hist)?hist:[]).slice(0,60).forEach(function(x){
        var cid=x.id||x.cid||x.token; if(!cid) return;
        req('/history',{method:'POST',body:{cid:String(cid),title:x.title||'',episode:x.epTitle||'',
          position:x.time||0,duration:x.duration||0,poster:x.poster||''}});
      });
    }catch(e){}
  }

  /* ---------- 暴露 KJ4 ---------- */
  window.KJ4 = {
    me:function(){ return me; },
    openAuth:openAuth,
    goMidnight:goMidnight,
    refresh:function(){ return req('/auth/me').then(function(d){ me=d.logged_in?d.user:null; renderUser(); return me; }); },
    favList:function(){ return req('/fav').then(function(d){ return d.items||[]; }); },
    favToggle:function(item){
      if(!me){ openAuth('login'); return Promise.resolve(false); }
      var cid=String(item.id||item.cid||item.token);
      return req('/fav',{method:'POST',body:{cid:cid,title:item.title||'',poster:item.poster||''}}).then(function(d){
        return !!(d&&d.has);
      });
    },
    favDel:function(cid){ if(!me) return Promise.resolve(); return req('/fav?cid='+encodeURIComponent(cid),{method:'DELETE'}); },
    histList:function(){ return req('/history').then(function(d){ return d.items||[]; }); },
    histPut:function(o){ if(!me) return Promise.resolve(); return req('/history',{method:'POST',body:o}); },
    histClear:function(){ if(!me) return Promise.resolve(); return req('/history',{method:'DELETE'}); }
  };

  /* 事件绑定 */
  document.addEventListener('click',function(e){
    var t=e.target;
    if(t.id==='kjAuthToggle'){ openAuth(mode==='login'?'register':'login'); return; }
    if(t.id==='kjAuthClose'||t.id==='kjAuthMask'){ closeAuth(); return; }
    if(t.id==='kjAuthBtn'){ doAuth(); return; }
    if(t.id==='kjAdultNo'||t.id==='kjAdultMask'){ $('kjAdultMask').style.display='none'; toast('已返回'); return; }
    if(t.id==='kjAdultYes'){ confirmAdult(); return; }
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Enter'){
      if($('kjAuthMask').style.display==='flex') doAuth();
    }
    if(e.key==='Escape'){ closeAuth(); $('kjAdultMask').style.display='none'; }
  });

  document.addEventListener('DOMContentLoaded',function(){ setTimeout(function(){ KJ4.refresh(); },200); });
  setTimeout(function(){ KJ4.refresh(); },700);
  window.addEventListener('load',function(){ setTimeout(function(){ KJ4.refresh(); },300); });
})();
</script>
<!-- ===== KANJU_MEMBER_UI END ===== -->
'''

# 插到 </body> 之前（没有就追加）
if "</body>" in s:
    s = s.replace("</body>", BLOCK + "\n</body>", 1)
else:
    s += BLOCK

open(P, "w", encoding="utf-8").write(s)
print("OK 注入完成，index.html 现 %d 行 / %d 字节" % (s.count("\n") + 1, len(s.encode("utf-8"))))
for k in ["登录 / 注册", "KJ4", "midnight-theater", "kjAdultYes", "syncLocalToServer"]:
    print("  含 %s: %d" % (k, s.count(k)))
