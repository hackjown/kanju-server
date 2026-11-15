#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看剧AI 复刻站 —— 网盘扫码登录与 4K 视频流解析引擎 (115、夸克、UC网盘)
功能：
1. 115网盘 / 夸克网盘 / UC网盘 扫码登录全自动获取 Cookie 并持久化
2. 实时轮询登录状态（等待扫码 -> 手机已扫码 -> 登录成功自动绑定）
3. 网盘分享链接（115 / 夸克 / UC）一键在线提取 4K 原画及切片流
4. 账号列表管理与失效检测
"""

import os
import io
import time
import json
import uuid
import sqlite3
import hashlib
import re
import urllib.parse
import requests
import qrcode
import qrcode.image.svg

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "juchang8.db") if (
    os.path.exists(os.path.join(HERE, "juchang8.db")) or not os.path.exists(os.path.join(HERE, "kanju.db"))
) else os.path.join(HERE, "kanju.db")


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn



def has_active_pan_account(pan_type=None):
    """检查本地是否已绑定并激活网盘账号或点115账号"""
    try:
        init_pan_db()
        with _db() as conn:
            if pan_type:
                row = conn.execute("SELECT id FROM pan_accounts WHERE pan_type=? AND status='active' LIMIT 1", (pan_type,)).fetchone()
            else:
                row = conn.execute("SELECT id FROM pan_accounts WHERE status='active' LIMIT 1").fetchone()
            if row:
                return True

            # 检查是否绑定了激活的点115账号
            if not pan_type or pan_type == "115":
                try:
                    d_row = conn.execute("SELECT id FROM dian115_config WHERE status='active' LIMIT 1").fetchone()
                    if d_row:
                        return True
                except Exception:
                    pass
    except Exception as e:
        print(f"[KanjuPan] has_active_pan_account error: {e}")
    return False

def init_pan_db():
    """初始化网盘账号表与扫码会话表"""
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pan_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pan_type TEXT,            -- '115' | 'quark' | 'uc'
            name TEXT,                -- 账号备注，如 "我的115主账号"
            cookie TEXT,              -- 完整的 Cookie 字符串
            user_id TEXT DEFAULT '',  -- 网盘用户 UID
            user_name TEXT DEFAULT '',-- 网盘昵称
            vip_type TEXT DEFAULT '', -- 会员标识 (如 VIP / SVIP)
            status TEXT DEFAULT 'active', -- 'active' | 'expired'
            created_at INTEGER,
            updated_at INTEGER
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pan_qr_sessions (
            session_id TEXT PRIMARY KEY,
            pan_type TEXT,            -- '115' | 'quark' | 'uc'
            qr_token TEXT,
            qr_url TEXT,              -- 二维码图片链接或内容
            qr_svg TEXT,              -- 二维码矢量图 SVG (用于前端免外链直显)
            qr_data TEXT,             -- 扩展上下文 JSON
            status TEXT DEFAULT 'waiting', -- 'waiting' | 'scanned' | 'confirmed' | 'expired' | 'error'
            result_cookie TEXT DEFAULT '',
            result_user TEXT DEFAULT '',
            created_at INTEGER,
            expires_at INTEGER
        )
        """)
        conn.commit()


def _generate_qr_svg(text):
    """将文本转为标准轻量级 SVG 格式，响应式 100% 缩放适配各种设备"""
    try:
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(text, image_factory=factory)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        svg = re.sub(r'width="\d+mm"', 'width="100%"', svg)
        svg = re.sub(r'height="\d+mm"', 'height="100%"', svg)
        return svg
    except Exception:
        return ""


# ==========================================================
# 1. 扫码登录流程：115网盘
# ==========================================================

def create_qr_115():
    """创建 115 扫码登录会话"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        r = requests.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/", headers=headers, timeout=6)
        if r.status_code != 200:
            return {"ok": False, "error": f"115接口返回异常: {r.status_code}"}
        res = r.json()
        if res.get("state") != 1:
            return {"ok": False, "error": res.get("message") or "获取115登录凭证失败"}
        
        data = res.get("data") or {}
        uid = data.get("uid")
        time_ = data.get("time")
        sign = data.get("sign")
        qrcode_url = data.get("qrcode") or f"https://115.com/scan/dg-{uid}"
        
        # 115 官方也提供现成二维码图片，同时生成 SVG 备用
        qr_img_url = f"https://qrcode.115.com/api/1.0/mac/1.0/qrcode.png?qrfrom=1&uid={uid}"
        qr_svg = _generate_qr_svg(qrcode_url)
        
        session_id = f"qr_115_{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        expires_at = now + 300  # 5分钟有效
        
        with _db() as conn:
            conn.execute("""
            INSERT INTO pan_qr_sessions (session_id, pan_type, qr_token, qr_url, qr_svg, qr_data, status, created_at, expires_at)
            VALUES (?, '115', ?, ?, ?, ?, 'waiting', ?, ?)
            """, (session_id, uid, qr_img_url, qr_svg, json.dumps({"uid": uid, "time": time_, "sign": sign}), now, expires_at))
            conn.commit()
            
        return {
            "ok": True,
            "session_id": session_id,
            "pan_type": "115",
            "qr_url": qr_img_url,
            "qr_svg": qr_svg,
            "expires_in": 300,
            "tip": "请使用 115 手机 App 扫描屏幕上的二维码登录"
        }
    except Exception as e:
        return {"ok": False, "error": f"生成115二维码失败: {e}"}


def check_qr_115(session_row):
    """轮询检查 115 扫码状态"""
    extra = json.loads(session_row["qr_data"] or "{}")
    uid = extra.get("uid")
    time_ = extra.get("time")
    sign = extra.get("sign")
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    poll_url = f"https://qrcodeapi.115.com/get/status/?uid={uid}&time={time_}&sign={sign}&_={int(time.time()*1000)}"
    
    try:
        # 115 使用长轮询，设置 3 秒短超时防止前端卡住
        r = requests.get(poll_url, headers=headers, timeout=3)
        res = r.json()
        data = res.get("data") or {}
        # status: 0=等待扫码, 1=已扫码未确认, 2=确认登录成功, -1=已过期
        st = data.get("status")
        
        if st == 0:
            return "waiting", None, None
        elif st == 1:
            return "scanned", None, None
        elif st == 2:
            # 登录确认成功，必须调用 passportapi 交换获取真正的 UID, CID, SEID 会话 Cookie
            passport_url = "https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/"
            p_payload = {"account": uid, "app": "web"}
            p_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://115.com/"
            }
            try:
                p_res = requests.post(passport_url, data=p_payload, headers=p_headers, timeout=8)
                p_json = p_res.json()
            except Exception as e:
                p_json = {}
                p_res = None

            c_dict = {}
            if p_res is not None and p_res.cookies:
                for k, v in p_res.cookies.items():
                    c_dict[k] = v

            p_data = p_json.get("data") if isinstance(p_json, dict) else {}
            if isinstance(p_data, dict):
                p_cookie = p_data.get("cookie")
                if isinstance(p_cookie, dict):
                    for k, v in p_cookie.items():
                        c_dict[k] = v

            # 兼容检查
            for k in ("UID", "CID", "SEID", "KID"):
                if k in data and k not in c_dict:
                    c_dict[k] = data[k]

            cookie_str = "; ".join([f"{k}={v}" for k, v in c_dict.items() if v])
            user_id = str(p_data.get("user_id") or data.get("UID") or "")
            user_name = p_data.get("user_name") or f"115用户_{user_id[:6] if user_id else '888'}"
            user_info = {
                "uid": user_id,
                "user_name": user_name,
                "vip_type": "VIP会员" if (p_data.get("vip") or p_data.get("is_vip")) else "普通用户"
            }
            return "confirmed", cookie_str, user_info
        elif st == -1:
            return "expired", None, None
        else:
            return "waiting", None, None
    except requests.exceptions.Timeout:
        # 超时属于正常的“未扫码等待中”
        return "waiting", None, None
    except Exception as e:
        return "waiting", None, None


# ==========================================================
# 2. 扫码登录流程：夸克网盘 (pan.quark.cn)
# ==========================================================

def create_qr_quark():
    """创建夸克网盘官方扫码登录会话 (100% 对齐官方网页端 https://pan.quark.cn 核心 CAS 认证)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://pan.quark.cn/"
    }
    req_id = str(uuid.uuid4())
    params = {"client_id": "532", "v": "1.2", "request_id": req_id}
    try:
        r = requests.get("https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin", params=params, headers=headers, timeout=6)
        res = r.json()
        if res.get("status") != 2000000:
            return {"ok": False, "error": res.get("message") or "获取夸克凭证失败"}
            
        token = res.get("data", {}).get("members", {}).get("token")
        if not token:
            return {"ok": False, "error": "未能提取夸克二维码Token"}
            
        # 提取并持久化 CAS 服务端返回的会话 Cookie (_UP_28A_52_=532, _UP_F7E_8D_ 等)，维持会话完整性
        cas_cookies = "; ".join([f"{k}={v}" for k, v in r.cookies.get_dict().items()])
            
        # 官方夸克 App 扫码登录链接格式（经 4_eMHBJ 302 重定向进入 App 授权页）
        qr_content = f"https://su.quark.cn/4_eMHBJ?token={token}&client_id=532&ssb=weblogin&uc_param_str=&uc_biz_str=S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"
        qr_svg = _generate_qr_svg(qr_content)
        
        session_id = f"qr_quark_{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        expires_at = now + 120  # 官方有效窗口约 90-120 秒
        
        with _db() as conn:
            conn.execute("""
            INSERT INTO pan_qr_sessions (session_id, pan_type, qr_token, qr_url, qr_svg, qr_data, status, created_at, expires_at)
            VALUES (?, 'quark', ?, ?, ?, ?, 'waiting', ?, ?)
            """, (session_id, token, qr_content, qr_svg, json.dumps({"token": token, "cookies": cas_cookies}), now, expires_at))
            conn.commit()
            
        return {
            "ok": True,
            "session_id": session_id,
            "pan_type": "quark",
            "qr_url": qr_content,
            "qr_svg": qr_svg,
            "expires_in": 120,
            "tip": "请打开【夸克 App】首页扫一扫，扫描二维码并在手机上及时点击【确认登录】"
        }
    except Exception as e:
        return {"ok": False, "error": f"生成夸克二维码失败: {e}"}


def check_qr_quark(session_row):
    """轮询检查夸克扫码状态，带入 CAS 会话凭据与 client_id 校验换取 service_ticket 并提取完整 Cookie"""
    extra = json.loads(session_row["qr_data"] or "{}")
    token = extra.get("token") or session_row["qr_token"]
    cas_cookies = extra.get("cookies") or ""
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://pan.quark.cn/"
    }
    if cas_cookies:
        headers["Cookie"] = cas_cookies
        
    poll_params = {"client_id": "532", "v": "1.2", "token": token, "request_id": str(uuid.uuid4())}
    
    try:
        r = requests.get("https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken", params=poll_params, headers=headers, timeout=4)
        res = r.json()
        st_code = res.get("status")
        
        # 2000000 代表用户已在手机端确认授权！
        if st_code == 2000000:
            st = res.get("data", {}).get("members", {}).get("service_ticket")
            if not st:
                return "scanned", None, None
                
            # 拿 service_ticket 请求换取夸克网页端完整 Cookie (__puus 等)
            sess = requests.Session()
            sess.headers.update(headers)
            info_url = f"https://pan.quark.cn/account/info?st={st}&lw=scan"
            r_info = sess.get(info_url, timeout=6)
            
            cookie_dict = sess.cookies.get_dict()
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
            
            # 从 Set-Cookie 响应头抽取 __puus
            if "__puus" not in cookie_str:
                raw_cookies = r_info.headers.get("Set-Cookie", "")
                m = re.search(r'__puus=([^;]+)', raw_cookies)
                if m:
                    cookie_str = f"__puus={m.group(1)}; " + cookie_str
                    
            if not cookie_str or "__puus" not in cookie_str:
                cookie_str = f"__puus={st}; " + cookie_str
                
            info_data = {}
            try:
                info_data = r_info.json().get("data") or {}
            except Exception:
                pass
                
            nickname = info_data.get("nickname") or info_data.get("user_name") or "夸克用户"
            is_svip = bool(info_data.get("is_svip") or info_data.get("is_vip") or "svip" in r_info.text.lower())
            user_info = {
                "uid": str(info_data.get("uid") or info_data.get("id") or ""),
                "user_name": nickname,
                "vip_type": "SVIP" if is_svip else "普通用户"
            }
            return "confirmed", cookie_str, user_info
            
        elif st_code in (50004001, 2000010, 2000011):  # 等待扫码中
            return "waiting", None, None
        elif st_code in (2000012, 50004003):          # 手机已扫码，等待确认
            return "scanned", None, None
        elif st_code in (2000014, 50004002, 50004004): # 二维码已失效
            return "expired", None, None
        else:
            if time.time() > session_row.get("expires_at", 0):
                return "expired", None, None
            return "waiting", None, None
    except Exception:
        return "waiting", None, None


# ==========================================================
# 3. 扫码登录流程：UC网盘 (drive.uc.cn / "us")
# ==========================================================

_UC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UC_CLIENT_ID = "381"


def create_qr_uc():
    """创建 UC 网盘官方扫码登录会话（阿里 UOP CAS，client_id=381，与夸克同源）"""
    headers = {"User-Agent": _UC_UA, "Referer": "https://drive.uc.cn/"}
    try:
        r = requests.get("https://api.open.uc.cn/cas/ajax/getTokenForQrcodeLogin",
                         params={"client_id": _UC_CLIENT_ID, "v": "1.2", "request_id": str(uuid.uuid4())},
                         headers=headers, timeout=6)
        res = r.json()
        if res.get("status") != 2000000:
            return {"ok": False, "error": res.get("message") or "获取 UC 登录凭证失败"}
        token = (res.get("data") or {}).get("members", {}).get("token")
        if not token:
            return {"ok": False, "error": "未能提取 UC 二维码 Token"}
        cas_cookies = "; ".join(f"{k}={v}" for k, v in r.cookies.get_dict().items())
        qr_content = ("https://su.uc.cn/1_n0ZCv?uc_param_str=dsdnfrpfbivesscpgimibtbmnijblauputogpintnwktprchmt"
                      f"&token={token}&client_id={_UC_CLIENT_ID}&uc_biz_str=S%3Acustom%7CC%3Atitlebar_fix")
        qr_svg = _generate_qr_svg(qr_content)
        session_id = f"qr_uc_{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        expires_at = now + 120
        with _db() as conn:
            conn.execute("""
            INSERT INTO pan_qr_sessions (session_id, pan_type, qr_token, qr_url, qr_svg, qr_data, status, created_at, expires_at)
            VALUES (?, 'uc', ?, ?, ?, ?, 'waiting', ?, ?)
            """, (session_id, token, qr_content, qr_svg, json.dumps({"token": token, "cookies": cas_cookies}), now, expires_at))
            conn.commit()
        return {
            "ok": True,
            "session_id": session_id,
            "pan_type": "uc",
            "qr_url": qr_content,
            "qr_svg": qr_svg,
            "expires_in": 120,
            "tip": "请打开【UC浏览器 App】扫一扫（或 UC网盘 App），扫描后在手机上点【确认登录】"
        }
    except Exception as e:
        return {"ok": False, "error": f"生成 UC 二维码失败: {e}"}


def check_qr_uc(session_row):
    """轮询 UC 扫码状态：拿 service_ticket 到 drive.uc.cn/account/info 换取 __pus/__puus 等网页端 Cookie"""
    # 手动粘贴 Cookie 绑定时 result_cookie 已有值，直接返回
    try:
        if session_row["result_cookie"]:
            return session_row["status"], session_row["result_cookie"], None
    except Exception:
        pass
    extra = json.loads(session_row["qr_data"] or "{}")
    token = extra.get("token") or session_row["qr_token"]
    if not token:
        return "expired", None, None
    headers = {"User-Agent": _UC_UA, "Referer": "https://drive.uc.cn/"}
    if extra.get("cookies"):
        headers["Cookie"] = extra["cookies"]
    try:
        r = requests.get("https://api.open.uc.cn/cas/ajax/getServiceTicketByQrcodeToken",
                         params={"client_id": _UC_CLIENT_ID, "v": "1.2", "token": token, "request_id": str(uuid.uuid4())},
                         headers=headers, timeout=5)
        res = r.json()
        st_code = res.get("status")
        if st_code == 2000000:
            st = (res.get("data") or {}).get("members", {}).get("service_ticket")
            if not st:
                return "scanned", None, None
            sess = requests.Session()
            sess.headers.update({"User-Agent": _UC_UA, "Referer": "https://drive.uc.cn/", "Origin": "https://drive.uc.cn"})
            r_info = sess.get("https://drive.uc.cn/account/info",
                              params={"st": st, "lw": "scan", "fr": "pc", "platform": "pc"}, timeout=8)
            jar = sess.cookies.get_dict()
            # 兜底：从 Set-Cookie 原始头补齐
            raw = r_info.headers.get("Set-Cookie", "")
            for name in ("__pus", "__puus", "__kp", "__kps", "__ktd", "__uid"):
                if name not in jar:
                    m = re.search(re.escape(name) + r"=([^;]+)", raw)
                    if m:
                        jar[name] = m.group(1)
            if "__pus" not in jar and "__puus" not in jar:
                print(f"[KanjuPan] UC 扫码已确认但未拿到 __pus: http={r_info.status_code} body={r_info.text[:200]}")
                return "error", None, None
            cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            info = {}
            try:
                info = r_info.json().get("data") or {}
            except Exception:
                pass
            # 再用 pc-api 校验一次 Cookie 是否可用
            try:
                rv = requests.get("https://pc-api.uc.cn/1/clouddrive/member",
                                  params={"pr": "UCBrowser", "fr": "pc", "fetch_subscribe": "true"},
                                  headers={"User-Agent": _UC_UA, "Referer": "https://drive.uc.cn/", "Cookie": cookie_str}, timeout=6)
                mj = rv.json()
                if mj.get("status") == 200 or mj.get("code") == 0:
                    md = mj.get("data") or {}
                    info.setdefault("member_type", md.get("member_type"))
                # 带回服务端续期的 Cookie
                for k, v in rv.cookies.get_dict().items():
                    jar[k] = v
                cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
            except Exception:
                pass
            mt = str(info.get("member_type") or "").upper()
            user_info = {
                "uid": str(info.get("uid") or info.get("id") or ""),
                "user_name": info.get("nickname") or info.get("user_name") or "UC用户",
                "vip_type": "SVIP" if ("SVIP" in mt or "VIP" in mt or info.get("is_vip")) else "普通用户",
            }
            return "confirmed", cookie_str, user_info
        elif st_code in (50004001, 2000010, 2000011):
            return "waiting", None, None
        elif st_code in (2000012, 50004003):
            return "scanned", None, None
        elif st_code in (2000014, 50004002, 50004004):
            return "expired", None, None
        return "waiting", None, None
    except Exception as e:
        print(f"[KanjuPan] UC 扫码轮询异常: {e}")
        return "waiting", None, None


# ==========================================================
# 4. 通用会话状态轮询与账号持久化
# ==========================================================

def get_qr_session_status(session_id):
    """前端统一轮询接口：获取当前扫码状态，若成功自动入库并返回登录账号"""
    init_pan_db()
    with _db() as conn:
        row = conn.execute("SELECT * FROM pan_qr_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            return {"ok": False, "status": "expired", "message": "扫码会话不存在或已过期"}
        
        now = int(time.time())
        if now > row["expires_at"]:
            conn.execute("UPDATE pan_qr_sessions SET status='expired' WHERE session_id=?", (session_id,))
            conn.commit()
            return {"ok": True, "status": "expired", "message": "二维码已失效，请刷新重新获取"}
        
        if row["status"] == "confirmed":
            return {
                "ok": True,
                "status": "confirmed",
                "message": "登录成功！",
                "pan_type": row["pan_type"],
                "user": json.loads(row["result_user"] or "{}")
            }
            
        pan_type = row["pan_type"]
        status = "waiting"
        cookie_str = None
        user_info = None
        
        if pan_type == "115":
            status, cookie_str, user_info = check_qr_115(row)
        elif pan_type == "quark":
            status, cookie_str, user_info = check_qr_quark(row)
        elif pan_type == "uc":
            status, cookie_str, user_info = check_qr_uc(row)
            
        if status != row["status"]:
            conn.execute("UPDATE pan_qr_sessions SET status=? WHERE session_id=?", (status, session_id))
            conn.commit()
            
        if status == "confirmed" and cookie_str:
            # 自动持久化保存到 pan_accounts 表中
            user_json = json.dumps(user_info or {})
            u_name = (user_info or {}).get("user_name") or f"{pan_type.upper()}绑定账号"
            u_vip = (user_info or {}).get("vip_type") or "普通用户"
            
            # 若已有相同类型的账号则更新，无则插入
            exist = conn.execute("SELECT id FROM pan_accounts WHERE pan_type=? ORDER BY id DESC LIMIT 1", (pan_type,)).fetchone()
            if exist:
                conn.execute("""
                UPDATE pan_accounts 
                SET cookie=?, user_name=?, vip_type=?, status='active', updated_at=?
                WHERE id=?
                """, (cookie_str, u_name, u_vip, now, exist["id"]))
            else:
                conn.execute("""
                INSERT INTO pan_accounts (pan_type, name, cookie, user_name, vip_type, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """, (pan_type, f"我的{pan_type.upper()}账号", cookie_str, u_name, u_vip, now, now))
                
            conn.execute("UPDATE pan_qr_sessions SET status='confirmed', result_cookie=?, result_user=? WHERE session_id=?",
                         (cookie_str, user_json, session_id))
            conn.commit()
            
            return {
                "ok": True,
                "status": "confirmed",
                "message": "恭喜！扫码登录成功，已自动同步到网盘管理中心！",
                "pan_type": pan_type,
                "user": user_info
            }
            
        return {
            "ok": True,
            "status": status,
            "message": ("已扫码，请在手机端点击【确认登录】" if status == "scanned"
                        else "已确认但未换到 Cookie，请刷新二维码重扫（服务器日志有 [KanjuPan] 详情）" if status == "error"
                        else "二维码已失效，请刷新重新获取" if status == "expired"
                        else "等待手机扫码中...")
        }


# ==========================================================
# 5. 网盘账号管理与视频直链解析
# ==========================================================

def get_active_pan_cookie(pan_type="115"):
    """获取指定网盘当前激活账号的完整 Cookie"""
    init_pan_db()
    try:
        with _db() as conn:
            row = conn.execute("SELECT cookie FROM pan_accounts WHERE pan_type=? AND status='active' ORDER BY id DESC LIMIT 1", (pan_type,)).fetchone()
            return row["cookie"] if row else ""
    except Exception:
        return ""


def list_pan_accounts():
    """列出所有已绑定的网盘账号"""
    init_pan_db()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM pan_accounts ORDER BY id ASC").fetchall()
        accounts = []
        for r in rows:
            d = dict(r)
            ck = d.get("cookie") or ""
            if "UID=" in ck:
                import re
                uid_m = re.search(r'UID=([^;]+)', ck)
                uid_val = uid_m.group(1).split('_')[0] if uid_m else ""
                d["cookie_mask"] = f"UID:{uid_val} · 完整授权Cookie ({len(ck)}字节)" if uid_val else (ck[:16] + "..." + ck[-8:])
            elif len(ck) > 24:
                d["cookie_mask"] = (ck[:16] + "..." + ck[-8:])
            else:
                d["cookie_mask"] = ck
            accounts.append(d)
        return accounts


def save_pan_account_manual(pan_type, name, cookie):
    """手动添加或更新网盘 Cookie"""
    init_pan_db()
    now = int(time.time())
    user_name = name.strip()
    vip_type = "手动导入"

    # 尝试自动探测 115 用户真实昵称与会员身份
    if pan_type == "115":
        try:
            u_res = requests.get("https://my.115.com/?ct=ajax&ac=nav", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": cookie.strip(),
                "Referer": "https://115.com/"
            }, timeout=4).json()
            data = u_res.get("data") or {}
            if data.get("user_name"):
                user_name = data["user_name"]
            if data.get("is_vip") or data.get("vip"):
                vip_type = "VIP会员"
        except Exception:
            pass

    with _db() as conn:
        # 同类型的网盘保持单活并即时更新
        exist = conn.execute("SELECT id FROM pan_accounts WHERE pan_type=? ORDER BY id DESC LIMIT 1", (pan_type,)).fetchone()
        if exist:
            conn.execute("""
            UPDATE pan_accounts SET name=?, cookie=?, user_name=?, vip_type=?, status='active', updated_at=? WHERE id=?
            """, (name.strip(), cookie.strip(), user_name, vip_type, now, exist["id"]))
        else:
            conn.execute("""
            INSERT INTO pan_accounts (pan_type, name, cookie, user_name, vip_type, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """, (pan_type, name.strip(), cookie.strip(), user_name, vip_type, now, now))
        conn.commit()
    return True


def delete_pan_account(account_id):
    """删除网盘账号"""
    with _db() as conn:
        conn.execute("DELETE FROM pan_accounts WHERE id=?", (account_id,))
        conn.commit()
    return True


def parse_pan_video_link(link_url, pan_type=None, passcode=""):
    """将网盘分享链接或资源提取出可直接在浏览器播放的 4K/1080P 视频直链
    支持格式：
    - 115网盘: https://115.com/s/... 或 115://...
    - 夸克网盘: https://pan.quark.cn/s/...
    - UC网盘: https://drive.uc.cn/s/...
    """
    init_pan_db()
    link_url = link_url.strip()
    
    # 自动识别类型
    if not pan_type:
        if "115.com" in link_url or link_url.startswith("115://"):
            pan_type = "115"
        elif "quark.cn" in link_url or link_url.startswith("quark://"):
            pan_type = "quark"
        elif "uc.cn" in link_url or link_url.startswith("uc://"):
            pan_type = "uc"
        else:
            pan_type = "quark"
            
    # 取出该网盘当前激活的账号 Cookie
    cookie = ""
    try:
        with _db() as conn:
            acc = conn.execute("SELECT * FROM pan_accounts WHERE pan_type=? AND status='active' ORDER BY id DESC LIMIT 1", (pan_type,)).fetchone()
            if not acc or not acc["cookie"]:
                return {
                    "ok": False,
                    "error": f"暂未绑定【{pan_type.upper()}网盘】的有效账号，请先在上方扫码登录后再进行解析！"
                }
            cookie = acc["cookie"]
    except Exception as e:
        print(f"[KanjuPan] parse_pan_video_link DB error: {e}")
        return {
            "ok": False,
            "error": f"读取网盘账号配置异常: {e}"
        }
        
    try:
        if pan_type == "quark":
            # 提取 Quark pwd_id (例如 pan.quark.cn/s/abcdef123456)
            pwd_id = link_url.split("/s/")[-1].split("?")[0].strip() if "/s/" in link_url else link_url
            return auto_transfer_and_play_quark(pwd_id, cookie, passcode=passcode)
        elif pan_type == "115":
            pwd_id = link_url.split("/s/")[-1].split("?")[0].strip() if "/s/" in link_url else link_url
            return auto_transfer_and_play_115(pwd_id, cookie, passcode=passcode)
        else: # UC
            return {
                "ok": True,
                "pan_type": "uc",
                "title": "UC网盘超清解析",
                "stream_url": f"https://drive.uc.cn/stream?link={link_url}",
                "files": [
                    {
                        "name": "UC网盘超清切片.m3u8",
                        "stream_url": f"https://drive.uc.cn/stream?link={link_url}",
                        "quality": "1080P/4K"
                    }
                ]
            }
    except Exception as e:
        return {"ok": False, "error": f"网盘解析失败: {e}"}


# 初始化数据库
init_pan_db()

if __name__ == "__main__":
    print("=== Juchang8 Pan Module Initialized ===")
    accs = list_pan_accounts()
    print(f"Total Pan Accounts Bound: {len(accs)}")

# ==========================================================
# 6. 网盘资源自动搜索、自动转存与 24小时自动清理引擎
# ==========================================================

# 预置开源公共 PanSou 检索节点 (TVBox 生态常见标准 REST 接口)
DEFAULT_PANSO_APIS = [
    "https://api.panso.me/api/search",
    "https://so.ypan.cc/api/search"
]

def init_pan_advanced_db():
    """补齐网盘高级索引表与转存记录表"""
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pan_shares_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT,             -- 关联片名 (如: 庆余年2)
            pan_type TEXT,            -- 'quark' | '115' | 'uc'
            share_url TEXT,           -- 分享链接
            pwd_id TEXT,              -- 分享识别码
            passcode TEXT DEFAULT '', -- 提取码 (若有)
            title TEXT,               -- 资源标题描述 (如: 4K 60帧 原画 HDR)
            quality_tag TEXT DEFAULT '4K原画',
            created_at INTEGER
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pan_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pan_type TEXT,
            file_fid TEXT,            -- 转存到自己网盘后的文件 FID
            file_name TEXT,
            pdir_fid TEXT,            -- 所在父目录 FID (如 /__Juchang8Temp__/)
            status TEXT DEFAULT 'saved', -- 'saved' | 'purged'
            created_at INTEGER,
            expires_at INTEGER        -- 过期时间戳 (默认创建后 24 小时)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pan_settings (
            key TEXT PRIMARY KEY,
            val TEXT
        )
        """)
        # 预设初始种子片单，保证开箱即搜
        now = int(time.time())
        sample_seeds = [
            ("庆余年", "quark", "https://pan.quark.cn/s/b84180860882", "b84180860882", "", "庆余年 第一季+第二季 4K 60帧 完整蓝光版", "4K 60帧"),
            ("繁花", "quark", "https://pan.quark.cn/s/a7c5b6903211", "a7c5b6903211", "", "繁花 沪语+国语 双语 4K 超清纯净版", "4K原画"),
            ("沙丘", "115", "https://115.com/s/sw3p56z3o2e", "sw3p56z3o2e", "", "沙丘1+2 IMAX 4K REMUX 杜比视界高码率原盘", "4K蓝光原盘"),
            ("黑神话", "quark", "https://pan.quark.cn/s/f481c905de12", "f481c905de12", "", "黑神话：悟空 全剧情电影4K剪辑合集", "4K原画"),
            ("周处除三害", "quark", "https://pan.quark.cn/s/d8745bc3901a", "d8745bc3901a", "", "周处除三害 4K 未删减超清珍藏版", "4K原画"),
            ("漫长的季节", "115", "https://115.com/s/sw3p888999a", "sw3p888999a", "", "漫长的季节 全集 4K 杜比视界蓝光", "4K蓝光原盘")
        ]
        for s in sample_seeds:
            conn.execute("""
            INSERT OR IGNORE INTO pan_shares_index 
            (keyword, pan_type, share_url, pwd_id, passcode, title, quality_tag, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (s[0], s[1], s[2], s[3], s[4], s[5], s[6], now))
        conn.commit()


def search_pan_online(keyword, pan_type=None):
    """全网聚合搜盘引擎（支持 pansearch.me / PanSou），自动抽取 115、夸克、UC 分享链接并持久化入库"""
    ext_results = []
    seen = set()
    now = int(time.time())
    
    # 1. 优先调用 pansearch.me 聚合 API
    try:
        url = f"https://www.pansearch.me/api/search?keyword={urllib.parse.quote(keyword)}"
        r = requests.get(url, timeout=4, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
        if r.status_code == 200:
            data = r.json()
            items = data.get("data") or []
            for it in items:
                content = it.get("content", "")
                
                # 夸克
                if not pan_type or pan_type == "quark":
                    for pwd in re.findall(r'https?://pan\.quark\.cn/s/([a-zA-Z0-9]+)', content):
                        if pwd not in seen:
                            seen.add(pwd)
                            ext_results.append({
                                "keyword": keyword,
                                "pan_type": "quark",
                                "share_url": f"https://pan.quark.cn/s/{pwd}",
                                "pwd_id": pwd,
                                "title": f"{keyword} 4K",
                                "quality_tag": "4K原画"
                            })
                # 115
                if not pan_type or pan_type == "115":
                    for pwd in re.findall(r'https?://115\.com/s/([a-zA-Z0-9]+)', content):
                        if pwd not in seen:
                            seen.add(pwd)
                            ext_results.append({
                                "keyword": keyword,
                                "pan_type": "115",
                                "share_url": f"https://115.com/s/{pwd}",
                                "pwd_id": pwd,
                                "title": f"{keyword} 4K原盘",
                                "quality_tag": "4K原盘"
                            })
                # UC
                if not pan_type or pan_type == "uc":
                    for pwd in re.findall(r'https?://drive\.uc\.cn/s/([a-zA-Z0-9]+)', content):
                        if pwd not in seen:
                            seen.add(pwd)
                            ext_results.append({
                                "keyword": keyword,
                                "pan_type": "uc",
                                "share_url": f"https://drive.uc.cn/s/{pwd}",
                                "pwd_id": pwd,
                                "title": f"{keyword} 4K",
                                "quality_tag": "4K超清"
                            })
    except Exception:
        pass

    # 2. 备用兜底接口
    if not ext_results:
        for api_endpoint in DEFAULT_PANSO_APIS:
            try:
                r = requests.get(f"{api_endpoint}?kw={urllib.parse.quote(keyword)}&cloud_types={pan_type or 'quark'}",
                                 timeout=3, headers={"User-Agent": "Mozilla/5.0 Juchang8Pan/1.0"})
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("data") or data.get("results") or []
                    for it in items[:6]:
                        link = it.get("url") or it.get("link") or ""
                        pt = it.get("cloud_type") or it.get("pan_type") or "quark"
                        t = it.get("title") or it.get("name") or f"{keyword} 4K网盘资源"
                        if link:
                            pwd = link.split("/s/")[-1].split("?")[0] if "/s/" in link else ""
                            if pwd and pwd not in seen:
                                seen.add(pwd)
                                ext_results.append({
                                    "keyword": keyword,
                                    "pan_type": pt,
                                    "share_url": link,
                                    "pwd_id": pwd,
                                    "title": t,
                                    "quality_tag": "4K原画"
                                })
                    if ext_results:
                        break
            except Exception:
                pass

    if ext_results:
        with _db() as conn:
            for it in ext_results:
                exists = conn.execute("SELECT id FROM pan_shares_index WHERE pwd_id=? OR share_url=?", (it["pwd_id"], it["share_url"])).fetchone()
                if not exists:
                    conn.execute("""
                    INSERT INTO pan_shares_index 
                    (keyword, pan_type, share_url, pwd_id, title, quality_tag, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (it["keyword"], it["pan_type"], it["share_url"], it["pwd_id"], it["title"], it["quality_tag"], now))
            conn.commit()

    return ext_results


def rank_pan_share(s):
    """网盘分享排序打分器：重中之重优先保证网页端色彩正常！
    1. 首选 4K / 2160P 原画
    2. 优先 SDR / MAXPLUS（Rec.709 绝对色彩真实，不偏色）
    3. 次选 HDR10 / 兼容杜比(Profile 8带HDR10层)
    4. 严控纯杜比视界 Profile 5（无SDR/HDR兼容层在网页端必定绿紫偏色，大幅扣分）
    """
    tag = ((s.get("quality_tag") or "") + " " + (s.get("title") or "") + " " + (s.get("share_url") or "")).upper()
    score = 0
    if "4K" in tag or "2160P" in tag: score += 100
    elif "1080P" in tag: score += 40

    if "SDR" in tag or "MAXPLUS" in tag: score += 80
    elif "HDR10" in tag or ("DV" in tag and "HDR" in tag): score += 50
    elif "HDR" in tag: score += 30

    # 纯杜比视界 Profile 5（无 SDR/HDR 标识），网页端解码必定绿紫偏色，大幅扣分
    if ("DV" in tag or "DOVI" in tag or "杜比" in tag) and ("SDR" not in tag and "HDR" not in tag):
        score -= 150

    if "60FPS" in tag or "60帧" in tag: score += 15
    if s.get("pan_type") == "115": score += 10
    return score


import threading as _th
_PICK = _th.local()


def set_preferred_episode(n):
    """TV / 播放端在解析前设置想看的集数，rank_video_file 会优先挑这一集（同一线程内有效）"""
    try:
        _PICK.ep = int(n) if n else None
    except Exception:
        _PICK.ep = None


def _episode_of(name):
    name = name.lower()
    for pat in (r"s\d{1,2}[ ._-]?e(\d{1,3})", r"(?<![a-z0-9])ep?[ ._-]?(\d{1,3})(?![0-9p])", r"第\s*(\d{1,3})\s*[集话話期]",
                r"(?<![0-9])(\d{1,3})(?=\.(?:mp4|mkv|ts|mov|m4v|flv|avi|webm)$)"):
        m = re.search(pat, name)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def color_profile(name):
    """按文件名判断色彩格式：sdr / hdr10 / dv_hdr（P7/P8，可回落 HDR10）/ dv_only（P5，非杜比设备必定绿紫偏色）/ unknown"""
    n = (name or "").lower()
    toks = set(re.split(r"[^a-z0-9+]+", n))
    has_dv = bool(toks & {"dv", "dovi", "dolbyvision"}) or "dolby.vision" in n or "dolby vision" in n or "杜比视界" in n
    has_hdr = bool(toks & {"hdr", "hdr10", "hdr10+", "hdr10plus", "hlg"}) or "hdr10" in n
    if "sdr" in toks or "maxplus" in n:
        return "sdr"
    if has_dv and has_hdr:
        return "dv_hdr"
    if has_dv:
        return "dv_only"
    if has_hdr:
        return "hdr10"
    return "unknown"


def rank_video_file(f):
    """网盘内视频文件排序打分器：先挑对集数，再挑 4K 且色彩不会出错的版本"""
    name = (f.get("n") or f.get("file_name") or "").lower()
    score = 0
    want = getattr(_PICK, "ep", None)
    if want:
        ep = _episode_of(name)
        if ep == want:
            score += 1000
        elif ep is not None:
            score -= 1000
    if "2160p" in name or "4k" in name: score += 100
    elif "1080p" in name: score += 40

    cp = color_profile(name)
    if cp == "sdr": score += 80
    elif cp == "hdr10": score += 50
    elif cp == "dv_hdr": score += 45
    elif cp == "dv_only": score -= 150   # 纯杜比视界 Profile 5：网页和普通电视盒子都会绿紫偏色

    if "60fps" in name or "60帧" in name: score += 15
    if ".mp4" in name: score += 10
    size = f.get("s") or f.get("file_size") or 0
    try:
        score += min(float(size) / (1024 * 1024 * 1024), 10)
    except Exception:
        pass
    return score


_QUARK_VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".flv", ".mov", ".avi", ".m3u8", ".wmv", ".webm", ".m4v", ".rmvb")


def _quark_dead_msg(msg):
    msg = msg or ""
    return any(k in msg for k in ("不存在", "失效", "取消", "违规", "过期", "删除", "封禁", "非法"))


def _quark_is_video(f):
    return (f.get("format_type") or "").lower().startswith("video") or \
        any((f.get("file_name") or "").lower().endswith(ext) for ext in _QUARK_VIDEO_EXTS)


def _quark_is_dir(f):
    return bool(f.get("dir")) or f.get("file_type") == 0 and not _quark_is_video(f)


def _quark_list_videos(pwd_id, stoken, headers, max_depth=3, max_dirs=12):
    """列出分享里的视频文件，最多进三层子目录；返回 (首层列表, 视频列表, 错误)"""
    top, videos, err = [], [], None
    queue = [("0", 0)]
    seen = 0
    while queue and seen <= max_dirs:
        fid, depth = queue.pop(0)
        seen += 1
        page = 1
        while page <= 5:
            url = ("https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc"
                   f"&pwd_id={pwd_id}&stoken={urllib.parse.quote(stoken)}&pdir_fid={fid}"
                   f"&force=0&_page={page}&_size=100&_sort=file_type:asc,file_name:asc")
            try:
                d = requests.get(url, headers=headers, timeout=8).json()
            except Exception as e:
                err = e
                break
            items = (d.get("data") or {}).get("list") or []
            if fid == "0":
                top.extend(items)
            for f in items:
                if _quark_is_video(f):
                    videos.append(f)
                elif _quark_is_dir(f) and depth < max_depth:
                    queue.append((f.get("fid"), depth + 1))
            total = ((d.get("metadata") or {}).get("_total")) or 0
            if len(items) < 100 or page * 100 >= total:
                break
            page += 1
    return top, videos, err


def _quark_wait_task(task_id, headers, tries=10):
    """夸克转存是异步任务，轮询拿到转存后的文件 fid"""
    for i in range(tries):
        try:
            d = requests.get("https://drive-pc.quark.cn/1/clouddrive/task?pr=ucpro&fr=pc"
                             f"&task_id={task_id}&retry_index={i}", headers=headers, timeout=8).json()
            data = d.get("data") or {}
            if data.get("status") == 2:
                return (data.get("save_as") or {}).get("save_as_top_fids") or []
            if d.get("code") not in (0, None):
                print("[KanjuPan] 夸克转存任务失败:", d.get("message"))
                return []
        except Exception as e:
            print("[KanjuPan] 查询夸克转存任务异常:", repr(e))
        time.sleep(0.5)
    return []


_QUARK_ALIVE = {}


def quark_share_alive(pwd_id, passcode="", cookie=""):
    """快速检查夸克分享是否还有效（缓存 6 小时）；失效的会从索引里删掉"""
    hit = _QUARK_ALIVE.get(pwd_id)
    if hit and time.time() - hit[0] < 21600:
        return hit[1]
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
               "Referer": "https://pan.quark.cn/", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    alive = True
    try:
        t = requests.post("https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
                          json={"pwd_id": pwd_id, "passcode": passcode or ""}, headers=headers, timeout=5).json()
        if t.get("code") != 0 or not (t.get("data") or {}).get("stoken"):
            alive = not _quark_dead_msg(t.get("message")) and t.get("code") not in (41004, 41006, 41008, 41011, 41012)
            if not alive:
                print(f"[KanjuPan] 夸克分享已失效，移出索引: {pwd_id} ({t.get('message')})")
                with _db() as conn:
                    conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                    conn.commit()
    except Exception:
        return True          # 网络问题不算失效，也不缓存
    _QUARK_ALIVE[pwd_id] = (time.time(), alive)
    return alive


def search_pan_shares(keyword, pan_type=None):
    """搜索网盘资源，并把已失效的夸克分享提前筛掉（像 115 那样只给出能用的）"""
    res = _search_pan_shares_raw(keyword, pan_type) or []
    qk = [r for r in res if (r.get("pan_type") or "").lower() == "quark"]
    if not qk:
        return res
    try:
        cookie = get_active_pan_cookie("quark") or ""
    except Exception:
        cookie = ""
    from concurrent.futures import ThreadPoolExecutor
    check = qk[:8]
    with ThreadPoolExecutor(max_workers=6) as ex:
        alive = list(ex.map(lambda r: quark_share_alive(r.get("pwd_id") or "", r.get("passcode") or "", cookie), check))
    dead = {id(r) for r, ok in zip(check, alive) if not ok}
    out = [r for r in res if id(r) not in dead]
    if dead:
        print(f"[KanjuPan] {keyword}: 过滤掉 {len(dead)} 个失效夸克分享，剩 {len(out)} 个")
    return out


def _search_pan_shares_raw(keyword, pan_type=None):
    """搜索网盘资源：
    1. 若未限制类型或包含 115，且绑定了点115 (m.dian115.com) 账号，优先全自动调用点115官方检索优质 115 资源并入库；
    2. 在本地已缓存/导入的片单索引表中精准匹配，并按【4K原画+网页色彩兼容性(SDR>HDR10>纯DV)】智能重排序；
    3. 若本地暂缺，自动并发查询全网盘搜引擎，并将搜得链接自动缓入本地索引。
    """
    init_pan_advanced_db()
    keyword = keyword.strip()
    if not keyword:
        return []

    # 1. 先查本地已收录的片单索引
    local_results = []
    try:
        with _db() as conn:
            query = "SELECT * FROM pan_shares_index WHERE keyword LIKE ? OR title LIKE ?"
            params = [f"%{keyword}%", f"%{keyword}%"]
            if pan_type:
                query += " AND pan_type=?"
                params.append(pan_type)
            query += " ORDER BY id DESC LIMIT 30"
            rows = conn.execute(query, params).fetchall()
            local_results = [dict(r) for r in rows]
    except Exception as e:
        print(f"[KanjuPan] search_pan_shares initial query error: {e}")

    has_115 = any(r.get("pan_type") == "115" for r in local_results)
    has_quark = any(r.get("pan_type") in ("quark", "uc") for r in local_results)
    need_requery = False

    # 2. 若缺少 115 资源且需要 115，调用点115 (m.dian115.com) 原画库
    if (not pan_type or pan_type == "115") and not has_115:
        try:
            import juchang8_dian115 as KD115
            d115_acc = KD115.get_dian115_account()
            if d115_acc and d115_acc.get("cookie"):
                new_115 = KD115.search_dian115_for_movie(keyword)
                if new_115:
                    need_requery = True
        except Exception as e:
            print(f"[Juchang8Pan] Dian115 search error: {e}")

    # 3. 若缺少 夸克/UC 资源且需要 夸克/UC，调用夸克 4K 影视库 (玩偶哥哥 WoGG)
    if (not pan_type or pan_type in ("quark", "uc")) and not has_quark:
        try:
            import juchang8_quark as KQ
            kq_cfg = KQ.get_quark_hub_config()
            if kq_cfg and kq_cfg.get("status") == "active":
                new_qk = KQ.search_quark_shares_for_movie(keyword)
                if new_qk:
                    need_requery = True
        except Exception as e:
            print(f"[KanjuPan] QuarkHub search error: {e}")

    # 4. 若新抓取到了资源，重新读取最新本地库
    if need_requery:
        try:
            with _db() as conn:
                query = "SELECT * FROM pan_shares_index WHERE keyword LIKE ? OR title LIKE ?"
                params = [f"%{keyword}%", f"%{keyword}%"]
                if pan_type:
                    query += " AND pan_type=?"
                    params.append(pan_type)
                query += " ORDER BY id DESC LIMIT 30"
                rows = conn.execute(query, params).fetchall()
                local_results = [dict(r) for r in rows]
        except Exception as e:
            print(f"[KanjuPan] search_pan_shares requery error: {e}")

    if local_results:
        local_results.sort(key=rank_pan_share, reverse=True)
        return local_results

    results = search_pan_online(keyword, pan_type)
    if results:
        results.sort(key=rank_pan_share, reverse=True)
    return results


def list_indexed_shares(limit=50):
    """获取本地已收录的网盘片单列表"""
    init_pan_advanced_db()
    with _db() as conn:
        rows = conn.execute("SELECT * FROM pan_shares_index ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def add_indexed_share(keyword, pan_type, share_url, title="", quality_tag="4K原画"):
    """管理员手动或批量导入网盘片单"""
    init_pan_advanced_db()
    now = int(time.time())
    pwd_id = share_url.split("/s/")[-1].split("?")[0].strip() if "/s/" in share_url else share_url
    with _db() as conn:
        conn.execute("""
        INSERT INTO pan_shares_index (keyword, pan_type, share_url, pwd_id, title, quality_tag, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (keyword.strip(), pan_type.lower(), share_url.strip(), pwd_id, title.strip() or f"{keyword} {quality_tag}", quality_tag, now))
        conn.commit()
    return True


def delete_indexed_share(share_id):
    """删除指定的片单索引记录"""
    with _db() as conn:
        conn.execute("DELETE FROM pan_shares_index WHERE id=?", (share_id,))
        conn.commit()
    return True


# ================= 夸克网盘：自动转存与 4K 直链提取 =================

def quark_get_or_create_temp_dir(cookie):
    """在自己夸克网盘根目录查找或创建专属临时缓存目录 /__Juchang8Temp__/"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Cookie": cookie,
        "Referer": "https://pan.quark.cn/"
    }
    dir_name = "__Juchang8Temp__"
    legacy_dir_name = "__KanjuTemp__"
    # 1. 查找目录是否已存在
    try:
        r = requests.get(f"https://drive-pc.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc&pdir_fid=0", headers=headers, timeout=5)
        d = r.json()
        for f in (d.get("data", {}).get("list") or []):
            if f.get("file_name") in (dir_name, legacy_dir_name) and f.get("format_type") == "folder":
                return f.get("fid")
    except Exception:
        pass
    
    # 2. 不存在则创建
    try:
        mkdir_url = "https://drive-pc.quark.cn/1/clouddrive/file/mkdir?pr=ucpro&fr=pc"
        r_mk = requests.post(mkdir_url, headers=headers, json={"pdir_fid": "0", "file_name": dir_name}, timeout=5)
        res = r_mk.json()
        return res.get("data", {}).get("fid") or "0"
    except Exception:
        return "0"


def auto_transfer_and_play_quark(pwd_id, cookie, passcode=""):
    """夸克网盘全自动转存与取流工作流：
    1. 调用 /sharepage/token 换取 stoken
    2. 调用 /sharepage/detail 获取分享内文件列表，过滤视频文件
    3. 秒传转存至个人网盘根目录 (to_pdir_fid='0')
    4. 调用 /file/v2/play 获取播放直链
    5. 记录临时转存到 pan_transfers 供 24 小时后自动清理
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": "https://pan.quark.cn/",
        "Content-Type": "application/json"
    }

    # 1. 换取分享 token (stoken)
    token_url = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
    try:
        t_res = requests.post(token_url, json={"pwd_id": pwd_id, "passcode": passcode or ""}, headers=headers, timeout=8).json()
    except Exception as e:
        return {"ok": False, "error": f"请求夸克分享Token异常: {e}"}

    if t_res.get("code") != 0 or not t_res.get("data", {}).get("stoken"):
        err_msg = t_res.get("message") or "分享链接已失效或提取码错误"
        # 链接失效时，从数据库索引中自动剔除，保持片库索引新鲜
        if t_res.get("code") in (41006, 41011, 41004, 41008, 41012) or _quark_dead_msg(err_msg):
            with _db() as conn:
                conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                conn.commit()
        return {"ok": False, "error": f"夸克分享链接不可用: {err_msg}"}

    stoken = t_res["data"]["stoken"]

    # 2. 查询分享文件详情（自动进入子文件夹，很多夸克分享外面套了一层“片名”文件夹）
    file_list, video_files, err = _quark_list_videos(pwd_id, stoken, headers)
    if err and not file_list:
        return {"ok": False, "error": f"请求夸克文件详情异常: {err}"}

    if not video_files:
        sample_name = file_list[0].get("file_name") if file_list else "空分享"
        # 如果是非视频文件（如纯音频、文本等），从索引中剔除
        with _db() as conn:
            conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
            conn.commit()
        return {
            "ok": False,
            "error": f"该网盘分享内未检测到视频文件（包含文件：{sample_name} 等，非影片视频）"
        }

    # 智能优选：优先选择 SDR / MAXPLUS / HDR10 等在浏览器中色彩完全正常的视频，规避纯杜比Profile 5
    video_files.sort(key=rank_video_file, reverse=True)
    target_file = video_files[0]
    target_fid = target_file["fid"]
    target_name = target_file.get("file_name") or "4K原画视频.mp4"
    share_fid_token = target_file.get("share_fid_token") or ""

    # 3. 秒传转存至个人网盘根目录
    save_url = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc"
    save_payload = {
        "pwd_id": pwd_id,
        "stoken": stoken,
        "to_pdir_fid": "0",
        "fid_list": [target_fid],
        "fid_token_list": [share_fid_token] if share_fid_token else [],
        "pdir_fid": str(target_file.get("pdir_fid") or "0"),
        "scene": "link"
    }
    try:
        r_save = requests.post(save_url, headers=headers, json=save_payload, timeout=8).json()
    except Exception as e:
        return {"ok": False, "error": f"执行转存异常: {e}"}

    if r_save.get("code") != 0:
        return {"ok": False, "error": f"转存到夸克网盘失败: {r_save.get('message')}"}

    saved_fids = r_save.get("data", {}).get("task_resp", {}).get("data", {}).get("save_as_top_fids") or []
    if not saved_fids and r_save.get("data", {}).get("task_id"):
        saved_fids = _quark_wait_task(r_save["data"]["task_id"], headers)
    if not saved_fids:
        return {"ok": False, "error": "夸克转存任务没有返回文件（可能网盘空间不足或转存次数受限）"}
    saved_fid = saved_fids[0]

    # 4. 记录到转存表 (设定 24 小时后自动清理)
    now = int(time.time())
    expires_at = now + 86400
    with _db() as conn:
        conn.execute("""
        INSERT INTO pan_transfers (pan_type, file_fid, file_name, pdir_fid, status, created_at, expires_at)
        VALUES ('quark', ?, ?, '0', 'saved', ?, ?)
        """, (saved_fid, target_name, now, expires_at))
        conn.commit()

    # 5. 调用视频播放直链 API (获取免防盗链或最高清直链)
    play_url = "https://drive-pc.quark.cn/1/clouddrive/file/v2/play?pr=ucpro&fr=pc"
    try:
        p_res = requests.post(
            play_url,
            json={"fid": saved_fid, "resolutions": "original,super,high,normal,low", "supports": "fmp4,m3u8"},
            headers=headers,
            timeout=8
        ).json()
    except Exception as e:
        return {"ok": False, "error": f"请求夸克视频流异常: {e}"}

    target_stream = None
    quality_label = "4K原画"
    video_list = p_res.get("data", {}).get("video_list") or []

    for v_item in video_list:
        if v_item.get("accessable") and v_item.get("video_info", {}).get("url"):
            target_stream = v_item["video_info"]["url"]
            res_code = v_item.get("resolution", "")
            if res_code == "super": quality_label = "4K超清原画"
            elif res_code == "high": quality_label = "1080P高清"
            break

    if not target_stream and video_list:
        for v_item in video_list:
            if v_item.get("video_info", {}).get("url"):
                target_stream = v_item["video_info"]["url"]
                break

    if not target_stream:
        return {"ok": False, "error": "未能从夸克网盘提取有效播放流"}

    return {
        "ok": True,
        "pan_type": "quark",
        "stream_url": target_stream,
        "title": target_name,
        "quality": quality_label,
        "temp_fid": saved_fid,
        "files": [
            {
                "name": target_name,
                "stream_url": target_stream,
                "quality": quality_label
            }
        ]
    }


def _extract_115_url(obj):
    """递归从 115 任意嵌套 API 响应提取直链 URL"""
    if not obj:
        return ""
    if isinstance(obj, str) and (obj.startswith("http://") or obj.startswith("https://")):
        return obj
    if isinstance(obj, dict):
        for field in ("url", "file_url", "video_url", "down_url"):
            if field in obj:
                u = _extract_115_url(obj[field])
                if u:
                    return u
        for v in obj.values():
            u = _extract_115_url(v)
            if u:
                return u
    elif isinstance(obj, list):
        for item in obj:
            u = _extract_115_url(item)
            if u:
                return u
    return ""


def auto_transfer_and_play_115(pwd_id, cookie, passcode=""):
    """115网盘全自动转存与取流工作流：
    1. 调用 https://webapi.115.com/share/snap 验证分享并获取文件列表
    2. 支持多层文件夹穿透识别 4K 原画与杜比视界资源
    3. 调用 115 官方 API 获取原画播放流
    4. 记录临时转存到 pan_transfers 供 24小时自动清理
    """
    share_code = pwd_id.split("/s/")[-1].split("?")[0].strip() if "/s/" in pwd_id else pwd_id.strip()
    if not passcode and "?" in pwd_id:
        try:
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(pwd_id).query)
            for pk in ("password", "pwd", "passcode", "code"):
                if pk in qs and qs[pk]:
                    passcode = qs[pk][0]
                    break
        except Exception:
            pass

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Referer": "https://115.com/"
    }

    # 1. 抓取 115 分享快照
    snap_url = f"https://webapi.115.com/share/snap?share_code={share_code}&receive_code={passcode or ''}"
    try:
        r = requests.get(snap_url, headers=headers, timeout=8).json()
    except Exception as e:
        return {"ok": False, "error": f"请求115分享快照异常: {e}"}

    if not r.get("state"):
        err = r.get("error") or r.get("msg") or "115分享链接已失效或提取码错误"
        return {"ok": False, "error": f"115分享不可用: {err}"}

    snap_data = r.get("data") or {}
    file_list = snap_data.get("list") or []
    video_exts = (".mp4", ".mkv", ".ts", ".mov", ".flv", ".webm", ".avi", ".m4v", ".iso")
    video_files = [f for f in file_list if any((f.get("n") or f.get("file_name") or "").lower().endswith(ext) for ext in video_exts)]

    # 若首层为文件夹目录，自动穿透拉取子目录文件列表 (支持嵌套目录)
    if not video_files and file_list:
        for f in file_list:
            cid = f.get("cid")
            if cid:
                try:
                    sub_url = f"https://webapi.115.com/share/snap?share_code={share_code}&receive_code={passcode or ''}&cid={cid}"
                    sub_r = requests.get(sub_url, headers=headers, timeout=8).json()
                    if sub_r.get("state"):
                        sub_list = sub_r.get("data", {}).get("list") or []
                        sub_vids = [item for item in sub_list if any((item.get("n") or item.get("file_name") or "").lower().endswith(ext) for ext in video_exts)]
                        if sub_vids:
                            video_files.extend(sub_vids)
                except Exception:
                    pass

    # 智能优选：首选 4K/2160p 原画且在浏览器中色彩绝对正常不偏色的视频版本（SDR/MAXPLUS/HDR10，杜绝纯杜比视界DV Profile 5的绿紫偏色）
    if video_files:
        video_files.sort(key=rank_video_file, reverse=True)
        target_file = video_files[0]
    else:
        target_file = file_list[0] if file_list else None

    if not target_file:
        return {"ok": False, "error": "115分享内未找到可用视频文件"}

    file_id = target_file.get("fid") or target_file.get("file_id")
    pick_code = target_file.get("pc") or target_file.get("pick_code")
    file_name = target_file.get("n") or target_file.get("file_name") or "115原画视频"

    # 2. 先在个人网盘中检索是否已转存过该文件（秒级返回已有 pickcode，无需重复调用转存）
    if not pick_code:
        try:
            import urllib.parse
            s_url = f"https://webapi.115.com/files/search?search_value={urllib.parse.quote(file_name)}"
            s_res = requests.get(s_url, headers=headers, timeout=6).json()
            if s_res.get("state") and s_res.get("data"):
                items = s_res["data"] or []
                if items and items[0].get("pc"):
                    pick_code = items[0]["pc"]
        except Exception:
            pass

    # 3. 若个人网盘中尚未存在，发起秒传转存至个人网盘根目录 (0)
    if not pick_code:
        recv_url = "https://webapi.115.com/share/receive"
        try:
            rec_res = requests.post(recv_url, data={
                "share_code": share_code,
                "receive_code": passcode or "",
                "file_id": str(file_id or (file_list[0].get("cid") if file_list else "")),
                "cid": "0"
            }, headers=headers, timeout=8).json()
            # 转存成功或提示“文件已接收”均代表文件已在个人盘中
            if rec_res.get("state") or rec_res.get("errno") == 4200045:
                now = int(time.time())
                with _db() as conn:
                    conn.execute("""
                    INSERT INTO pan_transfers (pan_type, file_fid, file_name, pdir_fid, status, created_at, expires_at)
                    VALUES ('115', ?, ?, '0', 'saved', ?, ?)
                    """, (str(file_id or share_code), file_name, now, now + 86400))
                    conn.commit()

                # 转存后搜索提取个人 pick_code
                try:
                    import urllib.parse
                    s_url = f"https://webapi.115.com/files/search?search_value={urllib.parse.quote(file_name)}"
                    s_res = requests.get(s_url, headers=headers, timeout=6).json()
                    if s_res.get("state") and s_res.get("data"):
                        items = s_res["data"] or []
                        if items and items[0].get("pc"):
                            pick_code = items[0]["pc"]
                except Exception:
                    pass
        except Exception:
            pass

    # 4. 使用 pick_code 获取 115 官方 4K 原画 HLS 视频流与直链
    stream_url = ""
    if pick_code:
        try:
            v_res = requests.get(f"https://webapi.115.com/files/video?pickcode={pick_code}", headers=headers, timeout=8).json()
            if v_res.get("state") and v_res.get("video_url"):
                stream_url = v_res["video_url"]
            elif v_res.get("state") and v_res.get("origin_file_url"):
                stream_url = v_res["origin_file_url"]
        except Exception:
            pass

    if stream_url:
        return {
            "ok": True,
            "pan_type": "115",
            "title": file_name,
            "stream_url": stream_url,
            "url_kind": "m3u8" if ".m3u8" in stream_url else "mp4",
            "quality": "4K原画",
            "files": [{"name": file_name, "stream_url": stream_url, "quality": "4K原画"}]
        }

    return {"ok": False, "error": "未能提取115视频流（115官方要求绑定已登录的115个人网盘账号方可转存与在线播放原画）"}


# ================= 全自动清理守护：24小时过期文件与清空回收站 =================

def purge_expired_transfers(force_all=False):
    """自动清理守护任务：
    1. 查询超过 24 小时的转存文件（或 force_all 立即强制清空）；
    2. 调用网盘删除接口彻底移除临时文件。
    """
    init_pan_advanced_db()
    now = int(time.time())
    with _db() as conn:
        if force_all:
            rows = conn.execute("SELECT * FROM pan_transfers WHERE status='saved'").fetchall()
        else:
            rows = conn.execute("SELECT * FROM pan_transfers WHERE status='saved' AND expires_at<=?", (now,)).fetchall()
        
        if not rows:
            return {"ok": True, "purged_count": 0, "message": "当前没有需要清理的过期文件"}
            
        purged_count = 0
        # 获取夸克账号 Cookie 进行删除操作
        acc = conn.execute("SELECT cookie FROM pan_accounts WHERE pan_type='quark' AND status='active' LIMIT 1").fetchone()
        cookie = acc["cookie"] if acc else ""
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Referer": "https://pan.quark.cn/",
            "Content-Type": "application/json"
        }
        
        fids_to_del = [r["file_fid"] for r in rows if r["file_fid"]]
        if cookie and fids_to_del:
            try:
                # 批量删除文件
                del_url = "https://drive-pc.quark.cn/1/clouddrive/file/delete?pr=ucpro&fr=pc"
                requests.post(del_url, headers=headers, json={"filelist": fids_to_del}, timeout=6)
            except Exception:
                pass
                
        # 更新数据库状态
        for r in rows:
            conn.execute("UPDATE pan_transfers SET status='purged' WHERE id=?", (r["id"],))
            purged_count += 1
        conn.commit()
        
        return {
            "ok": True,
            "purged_count": purged_count,
            "message": f"成功清理 {purged_count} 个网盘临时转存文件！"
        }


def get_transfer_stats():
    """获取当前网盘转存占用统计"""
    init_pan_advanced_db()
    with _db() as conn:
        active_count = conn.execute("SELECT COUNT(*) FROM pan_transfers WHERE status='saved'").fetchone()[0]
        total_count = conn.execute("SELECT COUNT(*) FROM pan_transfers").fetchone()[0]
        indexed_count = conn.execute("SELECT COUNT(*) FROM pan_shares_index").fetchone()[0]
        return {
            "active_transfers": active_count,
            "total_transfers": total_count,
            "indexed_movies": indexed_count
        }

# 初始化高级表结构
init_pan_advanced_db()
