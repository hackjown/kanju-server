# -*- coding: utf-8 -*-
"""
夸克网盘 / UC 网盘 统一客户端（两家是同一套阿里 clouddrive 接口，只是域名和 pr 参数不同）

功能：
- 分享校验：token + 递归列目录，必须找到真正的正片（排除彩蛋/预告/小文件）才算有效
- 转存：存到自己网盘的 /__Juchang8Temp__/，异步任务轮询；同一个分享文件 24 小时内复用，不重复转存
- 取流：优先官方转码 m3u8（/file/v2/play），拿不到再用 /file/download 原画直链
- Cookie 自动续期：接口返回的新 __puus/__pus 写回账号，避免几小时后掉登录
- 清理：按网盘类型删除过期临时文件
"""
import json
import re
import threading
import time
import urllib.parse

import requests

DRIVES = {
    "quark": {
        "api": "https://drive-pc.quark.cn/1/clouddrive",
        "pr": "ucpro",
        "ref": "https://pan.quark.cn/",
        "origin": "https://pan.quark.cn",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch",
        "name": "夸克",
    },
    "uc": {
        "api": "https://pc-api.uc.cn/1/clouddrive",
        "pr": "UCBrowser",
        "ref": "https://drive.uc.cn/",
        "origin": "https://drive.uc.cn",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/100.0.4896.160 Safari/537.36 UCBrowser/17.0.0",
        "name": "UC",
    },
}

TEMP_DIR = "__Juchang8Temp__"
VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".flv", ".mov", ".avi", ".m3u8", ".wmv", ".webm", ".m4v", ".rmvb", ".m2ts")
DISGUISED_EXTS = (".iso", ".img", ".bdmv", ".mds", ".mdf", ".mp3", ".aac", ".flac", ".wav", ".zip", ".rar", ".7z", ".txt", ".pdf", ".jpg", ".png", ".exe", ".apk")
JUNK_WORDS = ("彩蛋", "预告", "花絮", "片花", "广告", "宣传", "sample", "trailer", "说明", "公告")
MIN_EP_BYTES = 80 * 1024 * 1024
DEAD_CODES = (41004, 41006, 41008, 41011, 41012)
LOGIN_CODES = (31001, 31002, 41010, 10005)
_ALIVE = {}
_CK_LOCK = threading.RLock()
# pan_type -> {"cookie": str, "db_updated_at": int, "persisted_at": float}
_LIVE_COOKIES = {}


def _pan():
    import juchang8_pan as P
    return P


def ua_for_host(netloc, ref=""):
    """给 /proxy 用：根据 CDN 域名或 Referer 返回该网盘要求的 UA、Referer 和 pan_type"""
    n = (netloc or "").lower()
    r = (ref or "").lower()
    if "uc.cn" in n or "ucweb.com" in n or "drive.uc.cn" in r:
        return DRIVES["uc"]["ua"], DRIVES["uc"]["ref"], "uc"
    if "quark" in n or "pan.quark.cn" in r:
        return DRIVES["quark"]["ua"], DRIVES["quark"]["ref"], "quark"
    return None, None, None


def _dead_msg(msg):
    return bool(re.search(r"不存在|失效|取消|违规|过期|删除|封禁|非法", msg or ""))


def _url(pan, path, **params):
    d = DRIVES[pan]
    q = {"pr": d["pr"], "fr": "pc", "uc_param_str": ""}
    q.update(params)
    return f"{d['api']}{path}?{urllib.parse.urlencode(q)}"


def _headers(pan, cookie=""):
    d = DRIVES[pan]
    h = {"User-Agent": d["ua"], "Referer": d["ref"], "Origin": d["origin"],
         "Content-Type": "application/json", "Accept": "application/json, text/plain, */*"}
    if cookie:
        h["Cookie"] = cookie
    return h


def _extract_resp_cookies(resp):
    """提取响应里下发的所有 Cookie（包括 __puus、Video-Auth 等 CDN 鉴权 Cookie）"""
    found = {}
    try:
        for k, v in (resp.cookies.get_dict() or {}).items():
            if k and v:
                found[k] = v
    except Exception:
        pass
    # 兜底：部分 Set-Cookie 带特定 Domain/Path 属性，requests 可能未写入 resp.cookies
    try:
        raw_list = []
        raw_hdrs = getattr(resp, "raw", None)
        if raw_hdrs is not None and getattr(raw_hdrs, "headers", None) is not None and hasattr(raw_hdrs.headers, "getlist"):
            raw_list = raw_hdrs.headers.getlist("Set-Cookie") or []
        if not raw_list:
            sc = resp.headers.get("Set-Cookie") or ""
            if sc:
                raw_list = [sc]
        for sc in raw_list:
            for key in ("__puus", "__pus", "Video-Auth", "video-auth", "__kp", "__kps", "__ktd", "__uid", "ctoken"):
                m = re.search(r"(?:^|[,;\s])" + re.escape(key) + r"=([^;,\s]+)", sc)
                if m and m.group(1) and m.group(1).lower() not in ("deleted", "expired", '""'):
                    found[key] = m.group(1)
    except Exception:
        pass
    return found


def _merge_cookie_dict(cookie, updates):
    """把 updates 合并进 cookie 字符串，返回 (new_cookie, changed)"""
    if not updates:
        return cookie or "", False
    jar = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k:
                jar[k] = v
    changed = False
    for k, v in updates.items():
        if k and v and jar.get(k) != v:
            jar[k] = v
            changed = True
    return ("; ".join(f"{k}={v}" for k, v in jar.items()) if changed else (cookie or "")), changed


def _merge_cookie(cookie, resp):
    """把响应里的新 Cookie（__puus / Video-Auth 续期等）合并回去"""
    merged, _ = _merge_cookie_dict(cookie, _extract_resp_cookies(resp))
    return merged


def get_live_cookie(pan, fallback=""):
    """线程安全地获取最新可用 Cookie（含内存中最新的 Video-Auth 与 __puus；若后台重新扫码则自动刷新）"""
    if pan not in DRIVES:
        return fallback or ""
    with _CK_LOCK:
        db_ck, db_ts = "", 0
        try:
            P = _pan()
            with P._db() as conn:
                row = conn.execute(
                    "SELECT cookie, updated_at FROM pan_accounts WHERE pan_type=? AND status='active' ORDER BY id DESC LIMIT 1",
                    (pan,),
                ).fetchone()
                if row:
                    db_ck = row["cookie"] or ""
                    db_ts = int(row["updated_at"] or 0)
        except Exception:
            pass
        entry = _LIVE_COOKIES.get(pan)
        if db_ck and (not entry or db_ts > entry.get("db_updated_at", 0)):
            # 若数据库里有更新的账号（例如用户重新扫码或手动保存），以数据库为基准但保留可能的新 Video-Auth
            merged = db_ck
            if entry and entry.get("cookie") and db_ts == entry.get("db_updated_at", 0):
                merged = entry["cookie"]
            _LIVE_COOKIES[pan] = {"cookie": merged, "db_updated_at": db_ts, "persisted_at": time.time()}
            return merged
        if entry and entry.get("cookie"):
            return entry["cookie"]
        return db_ck or fallback or ""


def update_live_cookie(pan, resp_or_dict, base_cookie=""):
    """原子合并新下发的 Cookie（__puus / Video-Auth 等）到内存与数据库，杜绝并发覆盖丢失"""
    if pan not in DRIVES:
        return base_cookie or ""
    updates = resp_or_dict if isinstance(resp_or_dict, dict) else _extract_resp_cookies(resp_or_dict)
    if not updates:
        return get_live_cookie(pan, base_cookie)
    with _CK_LOCK:
        current = get_live_cookie(pan, base_cookie) or base_cookie
        merged, changed = _merge_cookie_dict(current, updates)
        if not changed:
            return current
        now = int(time.time())
        prev_entry = _LIVE_COOKIES.get(pan) or {}
        last_persist = prev_entry.get("persisted_at", 0.0)
        _LIVE_COOKIES[pan] = {"cookie": merged, "db_updated_at": now, "persisted_at": last_persist}
        try:
            P = _pan()
            with P._db() as conn:
                row = conn.execute(
                    "SELECT id FROM pan_accounts WHERE pan_type=? AND status='active' ORDER BY id DESC LIMIT 1",
                    (pan,),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE pan_accounts SET cookie=?, updated_at=? WHERE id=?",
                        (merged, now, row["id"]),
                    )
                    conn.commit()
            # 备份 JSON 节流（至少间隔 15 秒，避免切片播放期间频繁写盘）
            if time.time() - last_persist >= 15.0:
                P._persist_accounts()
                _LIVE_COOKIES[pan]["persisted_at"] = time.time()
        except Exception as e:
            print(f"[KanjuPan] 实时同步 {pan} Cookie 失败: {e}")
        return merged


class _Sess:
    """带 Cookie 实时自动续期（含 Video-Auth / __puus）的请求封装"""

    def __init__(self, pan, cookie=""):
        self.pan = pan
        self.cookie = get_live_cookie(pan, cookie) if cookie else ""
        self.orig = self.cookie

    def req(self, method, path, params=None, body=None, timeout=10):
        ck = get_live_cookie(self.pan, self.cookie) if self.cookie else ""
        r = requests.request(method, _url(self.pan, path, **(params or {})),
                             headers=_headers(self.pan, ck),
                             json=body if method == "POST" else None, timeout=timeout)
        if self.cookie:
            self.cookie = update_live_cookie(self.pan, r, base_cookie=ck or self.cookie)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "message": f"HTTP {r.status_code}"}

    def save_cookie(self):
        if self.cookie and self.cookie != self.orig:
            try:
                _pan()._persist_accounts()
            except Exception as e:
                print(f"[KanjuPan] 持久化 {self.pan} Cookie 失败: {e}")


# ---------------- 文件判断 ----------------

def is_video(f):
    name = (f.get("file_name") or "").lower()
    if any(name.endswith(e) for e in DISGUISED_EXTS):
        return False
    return (f.get("format_type") or "").lower().startswith("video") or (f.get("obj_category") == "video") or \
        any(name.endswith(e) for e in VIDEO_EXTS)


def is_episode(f):
    if not is_video(f):
        return False
    size = f.get("size") or 0
    if size and size < MIN_EP_BYTES:
        return False
    name = (f.get("file_name") or "").lower()
    return not any(w in name for w in JUNK_WORDS)


def is_dir(f):
    return bool(f.get("dir")) or (f.get("file_type") == 0 and not is_video(f))


# ---------------- 分享 ----------------

def share_token(pan, pwd_id, passcode="", cookie=""):
    """返回 (stoken, 错误信息, 是否确定失效)"""
    s = _Sess(pan, cookie)
    try:
        t = s.req("POST", "/share/sharepage/token", body={"pwd_id": pwd_id, "passcode": passcode or "",
                                                          "support_visit_limit_private_share": True}, timeout=8)
    except Exception as e:
        return None, f"网络异常: {e}", False
    st = (t.get("data") or {}).get("stoken")
    if t.get("code") == 0 and st:
        return st, None, False
    msg = t.get("message") or "分享不可用"
    return None, msg, (t.get("code") in DEAD_CODES or _dead_msg(msg))


def list_episodes(pan, pwd_id, stoken, cookie="", max_depth=3, max_dirs=12):
    """递归列出分享内正片；返回 (首层列表, 正片列表, 错误)"""
    s = _Sess(pan, cookie)
    top, eps, err = [], [], None
    queue, seen = [("0", 0, "")], 0
    while queue and seen < max_dirs:
        fid, depth, dir_path = queue.pop(0)
        seen += 1
        page = 1
        while page <= 5:
            try:
                d = s.req("GET", "/share/sharepage/detail", params={
                    "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": fid, "force": "0",
                    "_page": page, "_size": 100, "_fetch_banner": 0, "_fetch_share": 0, "_fetch_total": 1,
                    "_sort": "file_type:asc,file_name:asc"}, timeout=8)
            except Exception as e:
                err = str(e)
                break
            if d.get("code") not in (0, None):
                err = f"{d.get('code')} {d.get('message')}"
                break
            items = (d.get("data") or {}).get("list") or []
            if fid == "0":
                top.extend(items)
            for f in items:
                if is_episode(f):
                    if dir_path:
                        f["_path"] = dir_path
                    eps.append(f)
                elif is_dir(f) and depth < max_depth:
                    sub_name = (f.get("file_name") or "").strip()
                    next_path = f"{dir_path}/{sub_name}".strip("/") if sub_name else dir_path
                    queue.append((f.get("fid"), depth + 1, next_path))
            total = (d.get("metadata") or {}).get("_total") or 0
            if len(items) < 100 or page * 100 >= total:
                break
            page += 1
    return top, eps, err


def share_alive(pan, pwd_id, passcode="", cookie=""):
    """分享是否真能用（缓存 6 小时）；确定不能用的从索引删除。网络/接口抽风返回 True 且不缓存。"""
    key = f"{pan}:{pwd_id}"
    hit = _ALIVE.get(key)
    if hit and time.time() - hit[0] < 21600:
        return hit[1]
    name = DRIVES[pan]["name"]
    st, msg, dead = share_token(pan, pwd_id, passcode)
    alive, why = True, ""
    if not st:
        if not dead:
            return True
        alive, why = False, msg
    else:
        top, eps, err = list_episodes(pan, pwd_id, st, max_dirs=6)
        if err:
            return True
        if not eps:
            alive = False
            why = "内容已被清空" if not top else f"没有正片（只有：{(top[0].get('file_name') or '')[:20]} 等）"
    if not alive:
        print(f"[KanjuPan] {name}分享不可用，移出索引: {pwd_id} ({why})")
        try:
            P = _pan()
            with P._db() as conn:
                conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                conn.commit()
        except Exception:
            pass
    _ALIVE[key] = (time.time(), alive)
    return alive


# ---------------- 自己网盘 ----------------

def _temp_dir(s):
    try:
        d = s.req("GET", "/file/sort", params={"pdir_fid": "0", "_page": 1, "_size": 200,
                                                 "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc"})
        if d.get("code") in LOGIN_CODES:
            return None, "login"
        for f in (d.get("data") or {}).get("list") or []:
            if f.get("file_name") in (TEMP_DIR, "__KanjuTemp__") and is_dir(f):
                return f.get("fid"), None
        m = s.req("POST", "/file", body={"pdir_fid": "0", "file_name": TEMP_DIR, "dir_path": "", "dir_init_lock": False})
        fid = (m.get("data") or {}).get("fid")
        if fid:
            return fid, None
        if m.get("code") in LOGIN_CODES:
            return None, "login"
    except Exception:
        pass
    return "0", None


def _wait_task(s, task_id, tries=20):
    for i in range(tries):
        try:
            d = s.req("GET", "/task", params={"task_id": task_id, "retry_index": i}, timeout=8)
            data = d.get("data") or {}
            st = data.get("status")
            if st == 2:
                return (data.get("save_as") or {}).get("save_as_top_fids") or [], None
            if st == 3:
                return [], data.get("message") or d.get("message") or "转存任务失败（可能空间不足或文件受限）"
            if d.get("code") not in (0, None):
                return [], d.get("message") or "转存任务失败"
        except Exception as e:
            print(f"[KanjuPan] 查询转存任务异常: {e!r}")
        time.sleep(0.6)
    return [], "转存任务超时"


def _detect_stream_kind(url, fmt=""):
    u = (url or "").lower()
    f = (fmt or "").lower()
    if ".m3u8" in u or "m3u8" in f or "hls" in f:
        return "m3u8"
    return "mp4"


def _play_stream(s, fid):
    """官方转码流：返回 (url, 清晰度说明或状态标记, url_kind['m3u8'|'mp4'])
    若文件被网盘判定为非视频（如 code=21005 not video，典型防和谐假PNG头伪装），返回 (None, 'not_video', None)。
    """
    try:
        p = s.req("POST", "/file/v2/play", body={"fid": fid, "resolutions": "normal,low,high,super,2k,4k",
                                                  "supports": "fmp4,m3u8"}, timeout=10)
    except Exception:
        return None, None, None
    p_code = p.get("code")
    p_msg = (p.get("message") or "").lower()
    if p_code == 21005 or "not video" in p_msg:
        return None, "not_video", None
    vl = (p.get("data") or {}).get("video_list") or []
    order = {"4k": 6, "2k": 5, "super": 4, "high": 3, "normal": 2, "low": 1}
    best = None
    for v in vl:
        vi = v.get("video_info") or {}
        url = vi.get("url")
        if not url or v.get("accessable") is False:
            continue
        rk = order.get(v.get("resolution") or "", 0)
        kind = _detect_stream_kind(url, vi.get("format"))
        # 同清晰度优先 HLS m3u8，否则按清晰度从高到低选
        score = (rk, 1 if kind == "m3u8" else 0)
        if not best or score > best[0]:
            best = (score, url, v.get("resolution"), vi.get("height"), kind)
    if not best:
        return None, None, None
    label = {"4k": "4K", "2k": "2K", "super": "超清", "high": "1080P", "normal": "720P", "low": "480P"}.get(best[2], best[2])
    if best[3]:
        label = f"{label}({best[3]}p)"
    return best[1], f"官方转码 {label}", best[4]


def _is_fake_header_stream(pan, url, cookie):
    """抽检原画直链前 16 字节，识别假 PNG/JPG/GIF/ZIP/RAR 文件头伪装的防和谐文件"""
    try:
        d = DRIVES.get(pan) or {}
        h = {
            "User-Agent": d.get("ua") or "Mozilla/5.0",
            "Referer": d.get("ref") or "",
            "Cookie": get_live_cookie(pan, cookie),
            "Range": "bytes=0-15",
        }
        r = requests.get(url, headers=h, timeout=6, stream=True)
        head = next(r.iter_content(16), b"")
        r.close()
        if any(head.startswith(sig) for sig in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04", b"Rar!")):
            return True
    except Exception:
        pass
    return False


def _download_url(s, fid):
    try:
        d = s.req("POST", "/file/download", body={"fids": [fid]}, timeout=10)
        lst = d.get("data") or []
        if lst and lst[0].get("download_url"):
            return lst[0]["download_url"]
    except Exception:
        pass
    return None


def transfer_and_play(pan, pwd_id, cookie, passcode=""):
    """转存 + 取流，返回 parse_pan_video_link 同样格式"""
    P = _pan()
    name = DRIVES[pan]["name"]
    s = _Sess(pan, cookie)
    try:
        st, msg, dead = share_token(pan, pwd_id, passcode, s.cookie)
        if not st:
            if dead:
                with P._db() as conn:
                    conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                    conn.commit()
            return {"ok": False, "error": f"{name}分享不可用: {msg}"}

        top, eps, err = list_episodes(pan, pwd_id, st, s.cookie)
        if not eps:
            if err:
                return {"ok": False, "error": f"读取{name}分享目录失败: {err}"}
            with P._db() as conn:
                conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                conn.commit()
            sample = top[0].get("file_name") if top else "分享内容已被清空"
            return {"ok": False, "error": f"{name}分享里没有正片（{sample}；彩蛋/预告/小于80MB 的不算）"}

        eps.sort(key=P.rank_video_file, reverse=True)
        target = eps[0]
        share_fid = target["fid"]
        fname = target.get("file_name") or "视频.mp4"

        want_ep = getattr(P._PICK, "ep", None)
        if want_ep and getattr(P._PICK, "strict", False):
            got_ep = P._episode_of(fname)
            if got_ep is not None and got_ep != want_ep:
                return {"ok": False, "error": f"{name}分享里没有第 {want_ep} 集（只找到 {fname}）"}

        # 24 小时内转存过同一个文件就直接复用
        saved_fid = None
        newly_saved = False
        now = int(time.time())
        with P._db() as conn:
            row = conn.execute("SELECT file_fid FROM pan_transfers WHERE pan_type=? AND share_key=? AND status='saved' "
                               "AND expires_at>? ORDER BY id DESC LIMIT 1", (pan, f"{pwd_id}:{share_fid}", now + 600)).fetchone()
            if row:
                saved_fid = row["file_fid"]

        url, label, kind = None, None, None
        if saved_fid:
            url, label, kind = _play_stream(s, saved_fid)
            if label == "not_video":
                print(f"[KanjuPan] {name}文件为防和谐伪装非视频(21005)，跳过以便自动切源: {fname}")
                return {"ok": False, "error": f"{name}文件为防和谐伪装文件（带假文件头，无法转码播放）"}
            if not url:
                saved_fid = None          # 可能被手动删了，重新转存

        if not saved_fid:
            to_dir, lerr = _temp_dir(s)
            if lerr == "login":
                return {"ok": False, "error": f"{name}网盘登录已失效，请到后台重新扫码绑定"}
            r = s.req("POST", "/share/sharepage/save", body={
                "fid_list": [share_fid], "fid_token_list": [target.get("share_fid_token") or ""],
                "to_pdir_fid": to_dir or "0", "pwd_id": pwd_id, "stoken": st,
                "pdir_fid": str(target.get("pdir_fid") or "0"), "scene": "link"}, timeout=10)
            if r.get("code") != 0:
                m = r.get("message") or ""
                if r.get("code") in LOGIN_CODES or "登录" in m or "login" in m.lower():
                    return {"ok": False, "error": f"{name}网盘登录已失效，请到后台重新扫码绑定（{m}）"}
                return {"ok": False, "error": f"转存到{name}网盘失败: {m or r.get('code')}"}
            data = r.get("data") or {}
            fids = ((data.get("task_resp") or {}).get("data") or {}).get("save_as_top_fids") or []
            if not fids and data.get("task_id"):
                fids, terr = _wait_task(s, data["task_id"])
                if not fids:
                    return {"ok": False, "error": f"{name}转存失败: {terr}（可能网盘空间不足或转存次数受限）"}
            if not fids:
                return {"ok": False, "error": f"{name}转存没有返回文件"}
            saved_fid = fids[0]
            newly_saved = True
            with P._db() as conn:
                conn.execute("INSERT INTO pan_transfers (pan_type, file_fid, file_name, pdir_fid, status, created_at, expires_at, share_key) "
                             "VALUES (?, ?, ?, ?, 'saved', ?, ?, ?)",
                             (pan, saved_fid, fname, to_dir or "0", now, now + 86400, f"{pwd_id}:{share_fid}"))
                conn.commit()
            url, label, kind = _play_stream(s, saved_fid)
            if label == "not_video":
                # 刚转存即确认是假文件头伪装文件：立刻删掉临时文件省空间，直接失败以便快速切下一条源
                delete_files(pan, s.cookie, [saved_fid])
                with P._db() as conn:
                    conn.execute("UPDATE pan_transfers SET status='purged' WHERE file_fid=?", (saved_fid,))
                    conn.commit()
                print(f"[KanjuPan] {name}文件为防和谐伪装非视频(21005)，已清理并跳过: {fname}")
                return {"ok": False, "error": f"{name}文件为防和谐伪装文件（带假文件头，无法转码播放）"}

        if not url:
            url = _download_url(s, saved_fid)
            if url and _is_fake_header_stream(pan, url, s.cookie):
                if newly_saved:
                    delete_files(pan, s.cookie, [saved_fid])
                print(f"[KanjuPan] {name}原画直链检测到假图片/压缩包文件头，跳过: {fname}")
                return {"ok": False, "error": f"{name}文件为防和谐伪装文件（带假文件头，浏览器无法解码）"}
            label, kind = "原画直链", "mp4"
        if not url:
            return {"ok": False, "error": f"{name}网盘没有返回播放地址（普通账号可能被限制）"}
        kind = kind or _detect_stream_kind(url)
        print(f"[KanjuPan] {name}网盘取流成功: {fname} · {label} ({kind})")
        return {"ok": True, "pan_type": pan, "stream_url": url, "url_kind": kind, "title": fname,
                "quality": label, "temp_fid": saved_fid,
                "files": [{"name": fname, "stream_url": url, "quality": label, "url_kind": kind}]}
    except Exception as e:
        return {"ok": False, "error": f"{name}网盘解析异常: {e}"}
    finally:
        s.save_cookie()


def delete_files(pan, cookie, fids):
    if not cookie or not fids:
        return
    s = _Sess(pan, cookie)
    try:
        s.req("POST", "/file/delete", body={"action_type": 2, "filelist": fids, "exclude_fids": []})
    except Exception:
        pass
    s.save_cookie()


def purge_temp_dir(pan, cookie):
    """清空 /__Juchang8Temp__/ 目录下的残留临时文件（绝不触碰根目录个人文件），返回清理数量"""
    if not cookie or pan not in DRIVES:
        return 0
    s = _Sess(pan, cookie)
    deleted = 0
    try:
        d = s.req("GET", "/file/sort", params={"pdir_fid": "0", "_page": 1, "_size": 200, "_fetch_total": 1})
        temp_fid = None
        for f in (d.get("data") or {}).get("list") or []:
            if f.get("file_name") in (TEMP_DIR, "__KanjuTemp__") and is_dir(f):
                temp_fid = f.get("fid")
                break
        if not temp_fid:
            return 0
        sub = s.req("GET", "/file/sort", params={"pdir_fid": temp_fid, "_page": 1, "_size": 200, "_fetch_total": 1})
        fids = [f.get("fid") for f in ((sub.get("data") or {}).get("list") or []) if f.get("fid")]
        if fids:
            s.req("POST", "/file/delete", body={"action_type": 2, "filelist": fids, "exclude_fids": []})
            deleted = len(fids)
    except Exception as e:
        print(f"[KanjuPan] 清理 {pan} 临时目录异常: {e}")
    finally:
        s.save_cookie()
    return deleted


def check_account(pan, cookie):
    """校验 Cookie 是否还能用：返回 (ok, 昵称/错误)"""
    s = _Sess(pan, cookie)
    try:
        d = s.req("GET", "/member", params={"fetch_subscribe": "true", "_ch": "home", "fetch_identity": "true"})
        ok = d.get("code") == 0
        s.save_cookie()
        return ok, (d.get("data") or {}).get("member_type") if ok else (d.get("message") or str(d.get("code")))
    except Exception as e:
        return False, str(e)
