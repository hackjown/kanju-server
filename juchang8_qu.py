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
VIDEO_EXTS = (".mp4", ".mkv", ".ts", ".flv", ".mov", ".avi", ".m3u8", ".wmv", ".webm", ".rmvb", ".m2ts", ".iso")
JUNK_WORDS = ("彩蛋", "预告", "花絮", "片花", "广告", "宣传", "sample", "trailer", "说明", "公告")
MIN_EP_BYTES = 80 * 1024 * 1024
DEAD_CODES = (41004, 41006, 41008, 41011, 41012)
LOGIN_CODES = (31001, 31002, 41010, 10005)
_ALIVE = {}


def _pan():
    import juchang8_pan as P
    return P


def ua_for_host(netloc):
    """给 /proxy 用：根据 CDN 域名返回该网盘要求的 UA 和 Referer"""
    n = (netloc or "").lower()
    if "uc.cn" in n:
        return DRIVES["uc"]["ua"], DRIVES["uc"]["ref"], "uc"
    if "quark" in n:
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


def _merge_cookie(cookie, resp):
    """把响应里的新 Cookie（__puus 续期等）合并回去"""
    try:
        new = resp.cookies.get_dict()
    except Exception:
        new = {}
    if not new:
        return cookie
    jar = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k] = v
    changed = False
    for k, v in new.items():
        if jar.get(k) != v:
            jar[k] = v
            changed = True
    return "; ".join(f"{k}={v}" for k, v in jar.items()) if changed else cookie


class _Sess:
    """带 Cookie 自动续期的请求封装；结束时把新 Cookie 写回账号"""

    def __init__(self, pan, cookie):
        self.pan, self.cookie, self.orig = pan, cookie or "", cookie or ""

    def req(self, method, path, params=None, body=None, timeout=10):
        r = requests.request(method, _url(self.pan, path, **(params or {})),
                             headers=_headers(self.pan, self.cookie),
                             json=body if method == "POST" else None, timeout=timeout)
        self.cookie = _merge_cookie(self.cookie, r)
        try:
            return r.json()
        except Exception:
            return {"code": -1, "message": f"HTTP {r.status_code}"}

    def save_cookie(self):
        if self.cookie and self.cookie != self.orig:
            try:
                P = _pan()
                with P._db() as conn:
                    conn.execute("UPDATE pan_accounts SET cookie=?, updated_at=? WHERE pan_type=? AND cookie=?",
                                 (self.cookie, int(time.time()), self.pan, self.orig))
                    conn.commit()
                P._persist_accounts()
            except Exception as e:
                print(f"[KanjuPan] 保存续期 Cookie 失败: {e}")


# ---------------- 文件判断 ----------------

def is_video(f):
    return (f.get("format_type") or "").lower().startswith("video") or (f.get("obj_category") == "video") or \
        any((f.get("file_name") or "").lower().endswith(e) for e in VIDEO_EXTS)


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
    queue, seen = [("0", 0)], 0
    while queue and seen < max_dirs:
        fid, depth = queue.pop(0)
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
                    eps.append(f)
                elif is_dir(f) and depth < max_depth:
                    queue.append((f.get("fid"), depth + 1))
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


def _wait_task(s, task_id, tries=15):
    for i in range(tries):
        try:
            d = s.req("GET", "/task", params={"task_id": task_id, "retry_index": i}, timeout=8)
            data = d.get("data") or {}
            if data.get("status") == 2:
                return (data.get("save_as") or {}).get("save_as_top_fids") or [], None
            if d.get("code") not in (0, None):
                return [], d.get("message") or "转存任务失败"
        except Exception as e:
            print(f"[KanjuPan] 查询转存任务异常: {e!r}")
        time.sleep(0.6)
    return [], "转存任务超时"


def _play_stream(s, fid):
    """官方转码流：返回 (url, 清晰度说明)"""
    try:
        p = s.req("POST", "/file/v2/play", body={"fid": fid, "resolutions": "normal,low,high,super,2k,4k",
                                                  "supports": "fmp4,m3u8"}, timeout=10)
    except Exception:
        return None, None
    vl = (p.get("data") or {}).get("video_list") or []
    order = {"4k": 6, "2k": 5, "super": 4, "high": 3, "normal": 2, "low": 1}
    best = None
    for v in vl:
        url = (v.get("video_info") or {}).get("url")
        if not url or v.get("accessable") is False:
            continue
        rk = order.get(v.get("resolution") or "", 0)
        if not best or rk > best[0]:
            best = (rk, url, v.get("resolution"), (v.get("video_info") or {}).get("height"))
    if not best:
        return None, None
    label = {"4k": "4K", "2k": "2K", "super": "超清", "high": "1080P", "normal": "720P", "low": "480P"}.get(best[2], best[2])
    if best[3]:
        label = f"{label}({best[3]}p)"
    return best[1], f"官方转码 {label}"


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
        st, msg, dead = share_token(pan, pwd_id, passcode, cookie)
        if not st:
            if dead:
                with P._db() as conn:
                    conn.execute("DELETE FROM pan_shares_index WHERE pwd_id=?", (pwd_id,))
                    conn.commit()
            return {"ok": False, "error": f"{name}分享不可用: {msg}"}

        top, eps, err = list_episodes(pan, pwd_id, st, cookie)
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

        # 24 小时内转存过同一个文件就直接复用
        saved_fid = None
        now = int(time.time())
        with P._db() as conn:
            row = conn.execute("SELECT file_fid FROM pan_transfers WHERE pan_type=? AND share_key=? AND status='saved' "
                               "AND expires_at>? ORDER BY id DESC LIMIT 1", (pan, f"{pwd_id}:{share_fid}", now + 600)).fetchone()
            if row:
                saved_fid = row["file_fid"]

        if saved_fid:
            url, label = _play_stream(s, saved_fid)
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
            with P._db() as conn:
                conn.execute("INSERT INTO pan_transfers (pan_type, file_fid, file_name, pdir_fid, status, created_at, expires_at, share_key) "
                             "VALUES (?, ?, ?, ?, 'saved', ?, ?, ?)",
                             (pan, saved_fid, fname, to_dir or "0", now, now + 86400, f"{pwd_id}:{share_fid}"))
                conn.commit()
            url, label = _play_stream(s, saved_fid)

        kind = "m3u8"
        if not url:
            url = _download_url(s, saved_fid)
            label, kind = "原画直链", "mp4"
        if not url:
            return {"ok": False, "error": f"{name}网盘没有返回播放地址（普通账号可能被限制）"}
        print(f"[KanjuPan] {name}网盘取流成功: {fname} · {label}")
        return {"ok": True, "pan_type": pan, "stream_url": url, "url_kind": kind, "title": fname,
                "quality": label, "temp_fid": saved_fid,
                "files": [{"name": fname, "stream_url": url, "quality": label}]}
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
