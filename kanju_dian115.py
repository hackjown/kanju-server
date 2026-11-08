# -*- coding: utf-8 -*-
"""
kanju_dian115.py - 看剧AI 点115 (m.dian115.com / MADREEL) 影视资源与115网盘解析集成引擎
功能特性：
1. 完整模拟官方浏览器安全握手：ECDSA P-256 会话签名 (X-Portal-Browser-Sig / X-Portal-Browser-Proof)
2. 自动协商并绑定独立浏览器会话凭证 (__Host-portal_browser) 与用户 portal_token
3. 全自动 TMDB 影视检索与站内海量资源搜索 (/api/portal/resources 与 /api/portal/resource-detail)
4. 资源Key专有 XOR 加解密编码协议 (Kt: Zr=1, en=[55, 161, 92, 233])
5. 4K / REMUX / HDR / 杜比视界 原画级 115 分享链接提取与自动免费/零积分极速解锁 (/api/portal/unlock)
6. 自动同步沉淀到看剧AI本地片库索引表 (pan_shares_index)，无缝对接播放器换源与直链播放
"""

import os
import time
import json
import base64
import secrets
import sqlite3
import urllib.parse
import requests

from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "kanju.db")


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_dian115_db():
    """初始化点115配置表"""
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS dian115_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cookie TEXT NOT NULL,
            portal_token TEXT NOT NULL DEFAULT '',
            account_name TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            points INTEGER DEFAULT 0,
            vip_status TEXT DEFAULT '普通用户',
            status TEXT DEFAULT 'active',
            updated_at INTEGER NOT NULL
        )
        """)
        try:
            conn.execute("ALTER TABLE dian115_config ADD COLUMN portal_token TEXT DEFAULT ''")
        except Exception:
            pass
        conn.commit()


def encode_resource_key(source, media_type, id_val, season=0):
    """
    点115 官方资源Key专有对称编码算法 (Kt):
    格式: 1|source|mediaType|id|season
    按字节循环异或 [55, 161, 92, 233] 后 base64url 编码
    """
    try:
        t = int(id_val)
        n = int(season or 0)
        raw_str = f"1|{source}|{media_type}|{t}|{n}".encode("utf-8")
        xor_key = [55, 161, 92, 233]
        out_bytes = bytearray()
        for idx, b in enumerate(raw_str):
            out_bytes.append(b ^ xor_key[idx % len(xor_key)])
        return base64.urlsafe_b64encode(out_bytes).decode().rstrip("=")
    except Exception as e:
        print(f"[Dian115] encode_resource_key error: {e}")
        return ""


class Dian115Client:
    """点115 (MADREEL) 官方 API 客户端，封装完整的 ECDSA 签名安全握手机制"""

    BASE_URL = "https://m.dian115.com"
    API_PREFIX = "/api/portal"

    def __init__(self, raw_cookie=""):
        self.raw_cookie = (raw_cookie or "").strip()
        self.portal_token = self._extract_portal_token(self.raw_cookie)
        self.session = requests.Session()
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://m.dian115.com/",
            "Origin": "https://m.dian115.com",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest"
        }

        # ECDSA 安全会话与协商凭据缓存
        self._priv_key = None
        self._proof = None
        self._host_browser_cookie = ""
        self._server_time_diff = 0
        self._session_expires_at = 0

    def _extract_portal_token(self, cookie_str):
        """从用户粘贴的 Cookie 字符串中精准提取 portal_token JWT"""
        if not cookie_str:
            return ""
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == "portal_token":
                    return v.strip()
        # 若用户直接粘贴的是 JWT 纯文本
        if cookie_str.startswith("eyJ") and "." in cookie_str:
            return cookie_str
        return cookie_str

    def _int_to_b64u(self, val, length=32):
        return base64.urlsafe_b64encode(val.to_bytes(length, "big")).decode().rstrip("=")

    def _ensure_browser_session(self, force=False):
        """执行 ECDSA P-256 会话注册与挑战换证，并获取专属 __Host-portal_browser"""
        now = int(time.time())
        if not force and self._priv_key and self._proof and self._host_browser_cookie and self._session_expires_at > now + 30:
            return True

        try:
            # 1. 获取浏览器挑战 proof (不带旧会话 Cookie，保证会话独立有效)
            chal_url = f"{self.BASE_URL}{self.API_PREFIX}/auth/browser-challenge"
            r_chal = self.session.get(chal_url, headers=self.base_headers, timeout=10)
            if r_chal.status_code != 200:
                return False
            d_chal = r_chal.json()
            if d_chal.get("code") != "ok" or not d_chal.get("proof"):
                return False

            self._proof = d_chal["proof"]
            ttl = int(d_chal.get("ttl") or 600)

            # 2. 生成本地专属 ECDSA SECP256R1 私钥与 JWK
            self._priv_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
            pub_numbers = self._priv_key.public_key().public_numbers()
            jwk = {
                "kty": "EC",
                "crv": "P-256",
                "x": self._int_to_b64u(pub_numbers.x),
                "y": self._int_to_b64u(pub_numbers.y)
            }

            # 3. 注册公钥建立 browser-session 并捕获服务器签发的专属 __Host-portal_browser
            sess_url = f"{self.BASE_URL}{self.API_PREFIX}/auth/browser-session"
            sess_headers = dict(self.base_headers)
            sess_headers["X-Portal-Browser-Proof"] = self._proof
            r_sess = self.session.post(sess_url, json={"public_jwk": jwk}, headers=sess_headers, timeout=10)
            if r_sess.status_code != 200:
                return False
            d_sess = r_sess.json()
            if d_sess.get("code") != "ok":
                return False

            host_browser = ""
            for c in self.session.cookies:
                if c.name == "__Host-portal_browser":
                    host_browser = f"__Host-portal_browser={c.value}"
                    break
            if not host_browser and "set-cookie" in r_sess.headers:
                sc = r_sess.headers["set-cookie"]
                if "__Host-portal_browser=" in sc:
                    h_val = sc.split("__Host-portal_browser=")[1].split(";")[0].strip()
                    host_browser = f"__Host-portal_browser={h_val}"

            self._host_browser_cookie = host_browser
            server_time_ms = int(d_sess.get("server_time_ms") or (time.time() * 1000))
            self._server_time_diff = server_time_ms - int(time.time() * 1000)
            self._session_expires_at = now + ttl
            return True
        except Exception as e:
            print(f"[Dian115] _ensure_browser_session error: {e}")
            return False

    def request(self, method, subpath, params=None, json_data=None):
        """发送经过 ECDSA P-256 签名的规范安全请求"""
        if not self._ensure_browser_session():
            return {"ok": False, "error": "未能建立点115安全浏览器会话 (Browser Session Handshake Failed)"}

        subpath = subpath if subpath.startswith("/") else f"/{subpath}"
        pathname = f"{self.API_PREFIX}{subpath}"
        method = method.strip().upper()

        now_ms = int(time.time() * 1000)
        ts_str = str(int(round(now_ms + self._server_time_diff)))
        nonce_str = base64.urlsafe_b64encode(secrets.token_bytes(24)).decode().rstrip("=")

        # 规范签名消息体：portal-browser-request/v1\nMETHOD\nPATHNAME\nTS\nNONCE
        sign_content = f"portal-browser-request/v1\n{method}\n{pathname}\n{ts_str}\n{nonce_str}".encode("utf-8")
        der_sig = self._priv_key.sign(sign_content, ec.ECDSA(hashes.SHA256()))
        r_val, s_val = utils.decode_dss_signature(der_sig)
        p1363_sig = r_val.to_bytes(32, "big") + s_val.to_bytes(32, "big")
        sig_b64u = base64.urlsafe_b64encode(p1363_sig).decode().rstrip("=")

        # 组装 Cookie: 协商的 __Host-portal_browser + 用户的 portal_token
        cookies_list = []
        if self._host_browser_cookie:
            cookies_list.append(self._host_browser_cookie)
        if self.portal_token:
            cookies_list.append(f"portal_token={self.portal_token}")
        final_cookie = "; ".join(cookies_list)

        req_headers = dict(self.base_headers)
        req_headers["Cookie"] = final_cookie
        req_headers["X-Portal-Browser-Proof"] = self._proof
        req_headers["X-Portal-Browser-TS"] = ts_str
        req_headers["X-Portal-Browser-Nonce"] = nonce_str
        req_headers["X-Portal-Browser-Sig"] = sig_b64u

        url = f"{self.BASE_URL}{pathname}"
        try:
            r = self.session.request(method, url, params=params, json=json_data, headers=req_headers, timeout=12)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            if r.status_code != 200:
                err_msg = ""
                if isinstance(data, dict):
                    err_msg = data.get("msg") or data.get("message") or data.get("error") or data.get("code") or ""
                if not err_msg:
                    err_msg = f"HTTP {r.status_code}"
                return {"ok": False, "status": r.status_code, "error": err_msg, "data": data}

            return {"ok": True, "status": r.status_code, "data": data}
        except Exception as e:
            return {"ok": False, "error": f"网络请求失败: {e}"}

    def get_profile(self):
        """测试并获取当前点115账号的个人信息、积分余额与权限"""
        res = self.request("GET", "/me")
        if not res.get("ok"):
            return res
        d = res.get("data") or {}
        user = d.get("user") or d.get("data") or d or {}
        nickname = user.get("nickname") or user.get("username") or user.get("email") or "点115用户"
        points = user.get("points") or user.get("score") or 0
        is_vip = bool(user.get("vip") or user.get("is_vip") or user.get("vip_level") or user.get("vip_until"))
        vip_tag = "VIP会员" if is_vip else "普通用户"
        if user.get("vip_until"):
            vip_tag += f" (至 {str(user.get('vip_until'))[:10]})"
        return {
            "ok": True,
            "nickname": nickname,
            "email": user.get("email") or "",
            "points": points,
            "vip_status": vip_tag,
            "user_id": user.get("id") or user.get("uid"),
            "raw": user
        }

    def search_tmdb(self, query):
        """调用 TMDB 接口查找影视条目"""
        return self.request("GET", "/tmdb/search", params={"q": query, "page": 1})

    def search_resources(self, query):
        """按片名直接搜索点115站内资源"""
        return self.request("GET", "/resources", params={"q": query})

    def get_resource_detail(self, key):
        """使用专属加密 key 获取资源详情与旗下挂载的完整 115 分享条目"""
        return self.request("GET", "/resource-detail", params={"key": key})

    def unlock_share(self, share_id):
        """解锁并提取目标 115 分享链接 (带密码及 115 直出分享)"""
        return self.request("POST", "/unlock", json_data={"share_id": share_id})

    def search_and_extract_115_shares(self, title):
        """一站式业务流程：片名搜索 -> 锁定4K蓝光资源 -> 解锁提取115分享链接并入库"""
        extracted = []
        now = int(time.time())

        # 1. 搜站内资源列表
        res = self.search_resources(title)
        items = []
        if res.get("ok"):
            d = res.get("data") or {}
            items = d.get("items") or d.get("data") or []

        # 2. 若未匹配，尝试 TMDB 搜索后拿 TMDB ID 构造资源
        if not items:
            tmdb_res = self.search_tmdb(title)
            if tmdb_res.get("ok"):
                t_items = tmdb_res.get("data", {}).get("items") or tmdb_res.get("data", {}).get("results") or []
                for t_it in t_items[:2]:
                    tmdb_id = t_it.get("id") or t_it.get("tmdb_id")
                    m_type = t_it.get("media_type") or "movie"
                    if tmdb_id:
                        items.append({
                            "resource": {
                                "id": tmdb_id,
                                "kind": "tmdb",
                                "tmdb_id": tmdb_id,
                                "media_type": m_type,
                                "title": t_it.get("title") or title
                            }
                        })

        if not items:
            return extracted

        # 3. 针对前 2 部高相关条目，解析资源详情中的 115 分享条目
        with _db() as conn:
            for item in items[:2]:
                r_info = item.get("resource") or {}
                tmdb_id = r_info.get("tmdb_id")
                m_type = r_info.get("media_type") or "movie"
                r_id = r_info.get("id")
                season = r_info.get("season") or 0

                # 生成点115专有资源 Key
                res_key = ""
                if tmdb_id:
                    res_key = encode_resource_key("tmdb", m_type, tmdb_id, season)
                elif r_id:
                    res_key = encode_resource_key("resource", m_type, r_id, season)

                if not res_key:
                    continue

                detail_res = self.get_resource_detail(res_key)
                if not detail_res.get("ok"):
                    continue

                shares = detail_res.get("data", {}).get("shares") or []
                if not shares:
                    continue

                # 优先挑选 4K / SDR / HDR10 / 兼容杜比等浏览器色彩正常的优质分享
                def share_priority(s):
                    score = 0
                    tag = ((s.get("tag_raw") or "") + " " + (s.get("file_name") or "") + " " + (s.get("title_override") or "")).upper()
                    if "4K" in tag or "2160P" in tag: score += 100
                    elif "1080P" in tag: score += 40

                    # 网页播放色彩适配核心：SDR / MAXPLUS（Rec.709绝对真实不偏色）优先级最高
                    if "SDR" in tag or "MAXPLUS" in tag: score += 80
                    # HDR10 / 兼容杜比(Profile 8带HDR层) 次之（正常 YCbCr，非绿紫）
                    elif "HDR10" in tag or ("DV" in tag and "HDR" in tag): score += 50
                    elif "HDR" in tag: score += 30

                    # 纯杜比视界 Profile 5（无 SDR/HDR 标识），浏览器解码必定绿紫偏色，大幅降级
                    if ("DV" in tag or "DOVI" in tag or "DOLBY" in tag) and ("SDR" not in tag and "HDR" not in tag):
                        score -= 150

                    if "60FPS" in tag or "60帧" in tag: score += 15
                    if "REMUX" in tag: score += 10
                    if s.get("unlock_cost", 10) == 0: score += 10
                    return score

                shares.sort(key=share_priority, reverse=True)

                # 解锁前 3 条优质分享（点115大多数影视分享为 0 积分免费解锁）
                for s in shares[:3]:
                    s_id = s.get("id")
                    if not s_id:
                        continue

                    share_title = s.get("title_override") or s.get("file_name") or f"{title} 4K (点115资源)"
                    quality = s.get("tag_raw") or "4K蓝光原盘"

                    # 尝试调用 unlock 提取实际 115 分享链接
                    unl_res = self.unlock_share(s_id)
                    share_url = ""
                    pwd_id = ""
                    receive_code = ""

                    if unl_res.get("ok"):
                        p = unl_res.get("data", {}).get("payload") or {}
                        share_url = p.get("url_115") or p.get("share_url") or ""
                        receive_code = p.get("receive_code") or ""
                        share_code = p.get("share_code") or ""
                        if not share_url and share_code:
                            share_url = f"https://115.com/s/{share_code}"
                            if receive_code:
                                share_url += f"?password={receive_code}"
                        pwd_id = share_code or (share_url.split("/s/")[-1].split("?")[0] if "/s/" in share_url else "")

                    if share_url and pwd_id:
                        # 检查本地是否已索引
                        exists = conn.execute("SELECT id FROM pan_shares_index WHERE pwd_id=?", (pwd_id,)).fetchone()
                        if not exists:
                            conn.execute("""
                            INSERT INTO pan_shares_index (keyword, pan_type, share_url, pwd_id, passcode, title, quality_tag, created_at)
                            VALUES (?, '115', ?, ?, ?, ?, ?, ?)
                            """, (title, share_url, pwd_id, receive_code, share_title, quality, now))

                        extracted.append({
                            "keyword": title,
                            "pan_type": "115",
                            "share_url": share_url,
                            "pwd_id": pwd_id,
                            "passcode": receive_code,
                            "title": share_title,
                            "quality_tag": quality
                        })

            conn.commit()

        return extracted


# ================= 全局辅助接口 =================

def get_dian115_account():
    """获取本地配置的点115激活账号"""
    init_dian115_db()
    with _db() as conn:
        row = conn.execute("SELECT * FROM dian115_config WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def delete_dian115_account():
    """断开/解绑点115账号"""
    init_dian115_db()
    with _db() as conn:
        conn.execute("DELETE FROM dian115_config")
        conn.commit()
    return True


def save_dian115_cookie(cookie):
    """保存并验证点115 Cookie 与 Token"""
    init_dian115_db()
    cookie = (cookie or "").strip()
    if not cookie:
        return {"ok": False, "error": "Cookie 不能为空"}

    client = Dian115Client(cookie)
    test_res = client.get_profile()
    if not test_res.get("ok"):
        return {"ok": False, "error": f"点115账号验证失败: {test_res.get('error')}"}

    now = int(time.time())
    with _db() as conn:
        conn.execute("UPDATE dian115_config SET status='disabled'")
        conn.execute("""
        INSERT INTO dian115_config (cookie, portal_token, account_name, nickname, points, vip_status, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """, (cookie, client.portal_token, test_res.get("email") or test_res.get("nickname"), test_res.get("nickname"),
              test_res.get("points"), test_res.get("vip_status"), now))
        conn.commit()

    return {
        "ok": True,
        "message": f"成功连接点115账号：{test_res.get('nickname')} ({test_res.get('vip_status')})，积分余额: {test_res.get('points')}",
        "profile": test_res
    }


def search_dian115_for_movie(title):
    """供上层影视聚合搜索调用的统一入口"""
    acc = get_dian115_account()
    if not acc or not acc.get("cookie"):
        return []

    client = Dian115Client(acc.get("portal_token") or acc["cookie"])
    try:
        return client.search_and_extract_115_shares(title)
    except Exception as e:
        print(f"[Dian115] Search failed for {title}: {e}")
        return []


init_dian115_db()
