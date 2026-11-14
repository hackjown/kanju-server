# -*- coding: utf-8 -*-
"""剧场8 TV 投屏服务（原生实现 tv-cast.v1 协议，不再依赖 kanju.ai）

TV 端（Juchang8TV APK）选择本服务器后连接：  ws(s)://<本服务器>/v1/tv-cast/ws
网页/手机端控制器同样连接：                    ws(s)://<本服务器>/v1/tv-cast/ws

本模块负责：
  * 电视配对：生成 6 位配对码、接收端令牌（receiver_session_token）、设备管理
  * 控制会话：/v1/tv-cast/pairings/confirm、/v1/tv-cast/devices、controller-session
  * 消息路由：controller.cast_intent / controller.command  ->  cast.load / player.command
              player.ack / player.state  ->  receiver.player_ack / receiver.player_state
  * 电视端数据请求：首页、栏目、搜索、详情、收藏/历史、选集、线路、综艺目录、跳过片头、弹幕
    全部由本服务器自己的 /v1 接口（app.py 内部调用）生成，跟网页端用的是同一套数据。

数据保存在 tvcast.db（与 app.py 同目录）。用户身份使用本站账号（juchang8_sid 会话）。
"""
import base64
import json
import os
import random
import secrets
import sqlite3
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TVCAST_DB", os.path.join(HERE, "tvcast.db"))
PROTOCOL = "tv-cast.v1"
HEARTBEAT_MS = 30000
PAIRING_TTL = 600            # 配对码有效期（秒）
CTRL_SESSION_TTL = 30 * 86400
COMMAND_TTL = 120            # 电视离线时指令排队时长（秒）
SITE_NAME = os.environ.get("TVCAST_SITE_NAME", "剧场8")

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("TVCAST_WORKERS", "16")))
_lock = threading.RLock()


# ---------------------------------------------------------------- 工具
def _now():
    return time.time()


def _iso(ts=None):
    ts = _now() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4()}"


def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = _db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS tv_devices (
      id TEXT PRIMARY KEY,
      install_id TEXT,
      name TEXT, model TEXT, app_version TEXT,
      status TEXT DEFAULT 'active',
      receiver_token TEXT UNIQUE,
      paired_user_id TEXT, paired_user_name TEXT,
      paired_at REAL, created_at REAL, updated_at REAL,
      last_receiver_seen_at REAL, last_controller_seen_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_tv_devices_install ON tv_devices(install_id);
    CREATE INDEX IF NOT EXISTS idx_tv_devices_user ON tv_devices(paired_user_id);
    CREATE TABLE IF NOT EXISTS tv_pairings (
      code TEXT PRIMARY KEY, device_id TEXT, expires_at REAL
    );
    CREATE TABLE IF NOT EXISTS tv_ctrl_sessions (
      id TEXT PRIMARY KEY, token TEXT UNIQUE, device_id TEXT, user_id TEXT, user_name TEXT,
      browser_session_id TEXT, origin TEXT, created_at REAL, expires_at REAL
    );
    """)
    c.commit()
    c.close()


def _device_row(device_id=None, token=None, install_id=None):
    c = _db()
    try:
        if device_id:
            r = c.execute("SELECT * FROM tv_devices WHERE id=?", (device_id,)).fetchone()
        elif token:
            r = c.execute("SELECT * FROM tv_devices WHERE receiver_token=?", (token,)).fetchone()
        else:
            r = c.execute("SELECT * FROM tv_devices WHERE install_id=? ORDER BY updated_at DESC", (install_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _device_update(device_id, **kw):
    if not kw:
        return
    kw["updated_at"] = _now()
    keys = ", ".join(f"{k}=?" for k in kw)
    c = _db()
    c.execute(f"UPDATE tv_devices SET {keys} WHERE id=?", (*kw.values(), device_id))
    c.commit()
    c.close()


def device_json(d, with_online=False):
    if not d:
        return None
    out = {
        "id": d["id"], "object": "tv_cast.device",
        "name": d.get("name") or "剧场8 TV", "model": d.get("model") or "",
        "app_version": d.get("app_version") or "",
        "status": d.get("status") or "active",
    }
    if d.get("paired_user_id"):
        out["paired_user_id"] = d["paired_user_id"]
        out["paired_at"] = _iso(d.get("paired_at") or _now())
    for k in ("last_receiver_seen_at", "last_controller_seen_at", "created_at", "updated_at"):
        if d.get(k):
            out[k] = _iso(d[k])
    if with_online:
        on = HUB.receiver_online(d["id"])
        out["online"] = on
        out["connected"] = on
    return out


# ---------------------------------------------------------------- 连接封装
class Conn:
    def __init__(self, ws, base):
        self.ws = ws
        self.base = base
        self.role = None            # receiver / controller
        self.device_id = None
        self.session = None         # 控制会话 dict
        self.user = None            # 接收端绑定用户 dict(id, name)
        self.alive = True
        self._send_lock = threading.Lock()

    def send(self, obj):
        if not self.alive:
            return False
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            with self._send_lock:
                self.ws.send(data)
            return True
        except Exception:
            self.alive = False
            return False


class Hub:
    def __init__(self):
        self.receivers = {}         # device_id -> Conn
        self.controllers = {}       # device_id -> set(Conn)
        self.queue = {}             # device_id -> [(expire, msg)]
        self.seq = {}               # device_id -> int
        self.last_state = {}        # device_id -> payload

    def receiver_online(self, device_id):
        c = self.receivers.get(device_id)
        return bool(c and c.alive)

    def next_seq(self, device_id):
        with _lock:
            self.seq[device_id] = self.seq.get(device_id, 0) + 1
            return self.seq[device_id]

    def to_controllers(self, device_id, msg):
        for c in list(self.controllers.get(device_id, ())):
            if not c.send(msg):
                self.controllers.get(device_id, set()).discard(c)

    def to_receiver(self, device_id, msg, queue=True):
        c = self.receivers.get(device_id)
        if c and c.alive and c.send(msg):
            return True
        if queue:
            with _lock:
                q = [x for x in self.queue.get(device_id, []) if x[0] > _now()]
                q.append((_now() + COMMAND_TTL, msg))
                self.queue[device_id] = q[-20:]
        return False

    def flush_queue(self, device_id):
        with _lock:
            q = [x for x in self.queue.pop(device_id, []) if x[0] > _now()]
        for _, m in q:
            self.to_receiver(device_id, m, queue=False)


HUB = Hub()


# ---------------------------------------------------------------- 配对
def _new_pairing(device_id):
    c = _db()
    c.execute("DELETE FROM tv_pairings WHERE device_id=? OR expires_at<?", (device_id, _now()))
    for _ in range(20):
        code = "%06d" % random.SystemRandom().randrange(1000000)
        if not c.execute("SELECT 1 FROM tv_pairings WHERE code=?", (code,)).fetchone():
            break
    exp = _now() + PAIRING_TTL
    c.execute("INSERT INTO tv_pairings(code,device_id,expires_at) VALUES(?,?,?)", (code, device_id, exp))
    c.commit()
    c.close()
    return code, exp


def _pairing_msg(conn, d):
    code, exp = _new_pairing(d["id"])
    disp = code[:3] + " " + code[3:]
    qr = f"{conn.base}/?tv_pair_code={urllib.parse.quote_plus(disp)}"
    conn.pairing_expires = exp
    return {
        "type": "pairing.created",
        "device": device_json(d),
        "receiver_session_token": d["receiver_token"],
        "pairing": {"code": code, "display_code": disp, "expires_at": _iso(exp), "qr_url": qr},
    }


def _paired_msg(d):
    return {"type": "receiver.paired", "device": device_json(d)}


def confirm_pairing(code, user_id, user_name, browser_session_id, origin):
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    c = _db()
    r = c.execute("SELECT * FROM tv_pairings WHERE code=? AND expires_at>?", (code, _now())).fetchone()
    if not r:
        c.close()
        return None
    c.execute("DELETE FROM tv_pairings WHERE code=?", (code,))
    c.commit()
    c.close()
    did = r["device_id"]
    _device_update(did, paired_user_id=user_id, paired_user_name=user_name, paired_at=_now(),
                   status="active", last_controller_seen_at=_now())
    d = _device_row(device_id=did)
    sess = new_ctrl_session(did, user_id, user_name, browser_session_id, origin)
    rc = HUB.receivers.get(did)
    if rc:
        rc.user = {"id": user_id, "name": user_name}
        rc.send(_paired_msg(d))
    return d, sess


def new_ctrl_session(device_id, user_id, user_name, browser_session_id, origin):
    sid = _uid("tvcs")
    tok = "tvc_" + secrets.token_urlsafe(32)
    exp = _now() + CTRL_SESSION_TTL
    c = _db()
    c.execute("INSERT INTO tv_ctrl_sessions(id,token,device_id,user_id,user_name,browser_session_id,origin,created_at,expires_at)"
              " VALUES(?,?,?,?,?,?,?,?,?)",
              (sid, tok, device_id, user_id, user_name, browser_session_id or "", origin or "", _now(), exp))
    c.commit()
    c.close()
    return {"id": sid, "token": tok, "device_id": device_id, "user_id": user_id, "expires_at": exp, "origin": origin}


def ctrl_session_by_token(tok):
    c = _db()
    r = c.execute("SELECT * FROM tv_ctrl_sessions WHERE token=? AND expires_at>?", (tok or "", _now())).fetchone()
    c.close()
    return dict(r) if r else None


def ctrl_session_json(s):
    return {"id": s["id"], "object": "tv_cast.controller_session", "device_id": s["device_id"],
            "expires_at": _iso(s["expires_at"]), "created_at": _iso(_now())}


# ---------------------------------------------------------------- 内部 API 调用
class Api:
    def __init__(self, app):
        self.app = app
        self._feed = None
        self._feed_at = 0

    def get(self, path, timeout=None):
        with self.app.test_client() as c:
            r = c.get(path, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            return r.status_code, r.get_json(silent=True)

    def post(self, path, body):
        with self.app.test_client() as c:
            r = c.post(path, json=body, environ_base={"REMOTE_ADDR": "127.0.0.1"})
            return r.status_code, r.get_json(silent=True)

    def feed(self):
        if self._feed and _now() - self._feed_at < 300:
            return self._feed
        st, d = self.get("/v1/feed/home")
        if st == 200 and isinstance(d, dict) and d.get("sections"):
            self._feed, self._feed_at = d, _now()
        return self._feed or {"sections": []}


API = None


def _abs(conn, url):
    if isinstance(url, str) and url.startswith("/"):
        return conn.base + url
    return url


def tv_card(c):
    if not isinstance(c, dict):
        return None
    seasons = c.get("seasons") or c.get("variants") or []
    vid = (c.get("variant_id") or c.get("selected_variant_id") or c.get("default_variant_id")
           or (seasons[0].get("variant_id") if seasons and isinstance(seasons[0], dict) else None) or c.get("id"))
    if not vid:
        return None
    out = {
        "variant_id": vid,
        "title": c.get("title") or c.get("normalized_title") or "",
        "content_kind": c.get("content_kind"),
        "year": c.get("year"),
        "area": c.get("area"),
        "genres": c.get("genres") or [],
        "poster_url": c.get("poster_url"),
        "backdrop_url": c.get("backdrop_url") or c.get("carousel_url") or c.get("poster_url"),
        "remarks": c.get("remarks"),
        "heat_value": c.get("heat_value"),
        "has_playback": c.get("has_playback", True),
        "availability": c.get("availability") or {"state": "playable", "label": "可播放", "playable": True,
                                                  "disabled": False, "source": "catalog"},
    }
    return {k: v for k, v in out.items() if v is not None}


def _tv_section(s, limit):
    cards = [x for x in (tv_card(c) for c in (s.get("cards") or [])) if x][:limit]
    return {
        "id": s.get("id"), "type": s.get("type"), "role": s.get("role"),
        "title": s.get("title") or "推荐", "subtitle": s.get("subtitle"), "reason": s.get("reason"),
        "more_available": bool(s.get("browse")) or len(s.get("cards") or []) > limit,
        "more_label": "查看更多", "cards": cards,
    }


def _enc_cursor(prefix, n):
    return base64.urlsafe_b64encode(f"{prefix}:{n}".encode()).decode().rstrip("=")


def _dec_cursor(cur, prefix):
    try:
        s = base64.urlsafe_b64decode((cur or "") + "=" * (-len(cur or "") % 4)).decode()
        p, n = s.rsplit(":", 1)
        return int(n) if p == prefix else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- 选集 / 线路
def _episodes_from_catalog(cat):
    eps = []
    for i, e in enumerate(cat.get("episodes") or []):
        eps.append({
            "id": e.get("id"), "token": e.get("token"),
            "display_name": e.get("title") or e.get("display_name") or f"第{i + 1}集",
            "episode_number": e.get("number") or e.get("episode_number") or i + 1,
            "episode_track": e.get("episode_track") or "main",
            "lines": [],
        })
    return eps


def _catalog(variant_id, offset=0, limit=50):
    q = urllib.parse.urlencode({"episode_offset": offset, "episode_limit": limit})
    st, d = API.get(f"/v1/catalog/{urllib.parse.quote(variant_id, safe='')}?{q}")
    if st != 200 or not isinstance(d, dict) or not (d.get("id") or d.get("variant_id")):
        raise ReqError("http_%s" % st, "影片播放信息不存在或已下线，请选择其他影片。")
    return d


def _page(eps, offset, limit, total=None):
    total = len(eps) if total is None else total
    return {"offset": offset, "limit": limit, "returned_count": len(eps),
            "total_count": total, "has_more": offset + len(eps) < total}


def _tv_line(conn, lo, selected_id):
    url = lo.get("url") or ""
    ticket = url.startswith("resolve://") or lo.get("url_kind") == "resolve_ticket"
    out = {
        "id": lo.get("id"), "playback_source_id": lo.get("playback_source_id") or lo.get("id"),
        "provider_id": lo.get("provider_id"), "provider_name": lo.get("provider_name") or lo.get("label"),
        "play_from": lo.get("play_from"), "source_vod_id": lo.get("source_vod_id"),
        "label": lo.get("label"), "display_label": lo.get("display_label") or lo.get("label"),
        "url": "" if ticket else _abs(conn, url),
        "url_kind": "resolve_ticket" if ticket else _kind(lo.get("url_kind"), url),
        "resolved": (not ticket) and bool(url),
        "resolve_mode": lo.get("resolve_mode") or ("parse" if ticket else "direct"),
        "resolve_required": ticket,
        "default_priority": bool(lo.get("default_priority")),
        "preference_weight": lo.get("preference_weight") or 0,
        "minimum_user_level": lo.get("minimum_user_level") or 0,
        "selected": lo.get("id") == selected_id,
    }
    if lo.get("headers"):
        out["headers"] = lo["headers"]
    return {k: v for k, v in out.items() if v is not None}


def _kind(k, url=""):
    k = (k or "").lower()
    if k in ("hls", "m3u8") or ".m3u8" in (url or ""):
        return "m3u8"
    return k or "m3u8"


def _resolve_line(lo):
    """resolve:// 票据换成真实播放地址"""
    url = lo.get("url") or ""
    if not url.startswith("resolve://") and lo.get("url_kind") != "resolve_ticket":
        return url, _kind(lo.get("url_kind"), url)
    st, d = API.post("/v1/playback/resolve-line?view=compact", {"ticket": url})
    line = (d or {}).get("line") if isinstance(d, dict) else None
    if st in (200, 201) and line and line.get("url") and not str(line["url"]).startswith("resolve://"):
        return line["url"], _kind(line.get("url_kind"), line["url"])
    return None, None


def _pick_lines(lines, want_id=None, pref=None):
    """返回按优先级排好的候选线路"""
    def usable(lo):
        return not str(lo.get("id", "")).startswith("local:pan:")
    if want_id:
        hit = [lo for lo in lines if lo.get("id") == want_id or lo.get("playback_source_id") == want_id]
        if hit:
            return hit + [lo for lo in lines if lo not in hit and usable(lo)]
    if pref:
        hit = [lo for lo in lines if lo.get("provider_id") == pref.get("provider_id")
               and (not pref.get("play_from") or lo.get("play_from") == pref.get("play_from"))]
        if hit:
            return hit + [lo for lo in lines if lo not in hit and usable(lo)]
    cand = [lo for lo in lines if usable(lo)]

    def key(lo):
        direct = bool(lo.get("url")) and not str(lo.get("url")).startswith("resolve://")
        return (0 if lo.get("selected") else 1, 0 if lo.get("default_priority") else 1,
                0 if direct else 1, -(lo.get("preference_weight") or 0))
    return sorted(cand, key=key)


def build_entry(conn, req, with_episodes=True):
    vid = req.get("variant_id")
    if not vid:
        raise ReqError("bad_request", "缺少 variant_id")
    off = max(0, int(req.get("episode_offset") or 0))
    lim = max(1, min(200, int(req.get("episode_limit") or 50)))
    cat = _catalog(vid, off, lim)
    all_eps = _episodes_from_catalog(cat)
    pag = cat.get("episode_pagination") or {}
    total = pag.get("total_count") or cat.get("available_episode_count") or len(all_eps)
    # 选集
    tok = req.get("episode_token")
    want = str(req.get("episode") or "")
    ep = None
    if tok:
        ep = next((e for e in all_eps if e["token"] == tok), None)
    if not ep and want:
        ep = next((e for e in all_eps if e["id"] == want or e["token"] == want or str(e["episode_number"]) == want
                   or e["display_name"] == want), None)
    if not ep and not tok:
        ep = next((e for e in all_eps if e["episode_track"] == "main"), all_eps[0] if all_eps else None)
    ep_token = tok or (ep or {}).get("token")
    if not ep_token:
        raise ReqError("http_404", "影片播放信息不存在或已下线，请选择其他影片。")
    st, res = API.get(f"/v1/playback/resolve/{urllib.parse.quote(ep_token, safe='')}")
    if st != 200 or not isinstance(res, dict):
        raise ReqError("http_%s" % st, "获取播放线路失败，请稍后重试。")
    cur = res.get("current_episode") or {}
    lines = res.get("line_options") or []
    url, kind, chosen = None, None, None
    for lo in _pick_lines(lines, req.get("line"), req.get("line_preference"))[:4]:
        url, kind = _resolve_line(lo)
        if url:
            chosen = lo
            break
    if not chosen:
        raise ReqError("playback_unavailable", "当前影片暂无可用线路，请换一部或稍后再试。")
    sel_id = chosen.get("id")
    tv_lines = []
    for lo in lines:
        t = _tv_line(conn, lo, sel_id)
        if lo is chosen:
            t.update({"url": _abs(conn, url), "url_kind": kind or t["url_kind"], "resolved": True,
                      "resolve_required": False})
        tv_lines.append(t)
    eps = []
    if with_episodes:
        for e in all_eps:
            e = dict(e)
            e["selected"] = e["token"] == ep_token or e["id"] == cur.get("id")
            eps.append(e)
    entry = {
        "previous_episode_token": cur.get("previous_episode_token"),
        "next_episode_token": cur.get("next_episode_token"),
        "content_kind": cat.get("content_kind"),
        "object": "playback.entry",
        "variant_id": vid,
        "is_adult": False,
        "title": cat.get("title") or "",
        "url": _abs(conn, url),
        "url_kind": kind,
        "selected_episode_id": cur.get("id") or (ep or {}).get("id"),
        "selected_episode_token": ep_token,
        "selected_episode_label": cur.get("display_name") or (ep or {}).get("display_name"),
        "selected_line_id": sel_id,
        "episodes": eps,
        "line_options": tv_lines,
    }
    if with_episodes:
        entry["episode_pagination"] = _page(all_eps, off, lim, total)
    return entry


# ---------------------------------------------------------------- 接收端请求处理
class ReqError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def _user_of(conn):
    return conn.user or {}


def _local_uid(conn):
    uid = _user_of(conn).get("id") or ""
    if isinstance(uid, str) and uid.startswith("user_"):
        try:
            return int(uid[5:])
        except ValueError:
            return None
    return None


def h_bootstrap(conn, m):
    u = _user_of(conn)
    KM = _km()
    acct = None
    lid = _local_uid(conn)
    if lid and KM:
        try:
            c = KM._conn()
            r = c.execute("SELECT * FROM users WHERE id=?", (lid,)).fetchone()
            c.close()
            acct = dict(r) if r else None
        except Exception:
            acct = None
    d = _device_row(device_id=conn.device_id) or {}
    level = int((acct or {}).get("level") or 1)
    guest = not lid
    return {
        "type": "receiver.bootstrap_response", "request_id": m.get("request_id"),
        "device": {"id": d.get("id"), "name": d.get("name"), "model": d.get("model")},
        "account": {
            "display_name": (acct or {}).get("nickname") or (acct or {}).get("account") or u.get("name") or "访客",
            "avatar": {"kind": "level", "url": None, "level_icon_key": "lianqi-1"},
            "level": {"value": level, "title": f"Lv.{level}", "icon_key": "lianqi-1",
                      "growth": int((acct or {}).get("growth") or 0), "progress": 0},
            "role": "guest" if guest else "member", "is_guest": guest,
            "multi_device_sync": not guest,
            "sync_notice": "收藏与观看记录会同步到使用同一账号的其他设备。" if not guest else "游客账号无法在其他设备自动同步。",
        },
        "site": {"name": SITE_NAME, "theme": {}},
        "modules": [{"id": x, "enabled": True} for x in ("home", "history", "favorites", "quick_search")],
        "excluded_modules": ["watch_calendar"],
        "generated_at": _iso(),
    }


def h_home(conn, m):
    feed = API.feed()
    secs = feed.get("sections") or []
    lim = max(1, min(8, int(m.get("section_limit") or 3)))
    per = max(1, min(20, int(m.get("cards_per_section") or 8)))
    off = _dec_cursor(m.get("cursor"), "tv-home-v1") if m.get("cursor") else 0
    out = [_tv_section(s, per) for s in secs[off:off + lim]]
    nxt = off + lim
    return {
        "type": "receiver.home_response", "request_id": m.get("request_id"),
        "sections": out,
        "next_cursor": _enc_cursor("tv-home-v1", nxt) if nxt < len(secs) else None,
        "meta": {"section_offset": off, "section_limit": lim, "cards_per_section": per,
                 "returned_sections": len(out), "total_sections": len(secs)},
        "generated_at": _iso(), "expires_at": _iso(_now() + 600),
    }


def h_section_browse(conn, m):
    sid = m.get("section_id")
    feed = API.feed()
    sec = next((s for s in feed.get("sections") or [] if s.get("id") == sid), None)
    if not sec:
        raise ReqError("not_found", "栏目不存在或已下线")
    page = _dec_cursor(m.get("cursor"), "tv-browse-v1") if m.get("cursor") else 1
    page = max(1, page)
    cards, has_more = [], False
    b = sec.get("browse") or {}
    if b.get("path"):
        params = {k: v for k, v in (b.get("params") or {}).items() if v not in (None, "")}
        params.update({"page": page, "limit": 24})
        st, d = API.get(b["path"] + "?" + urllib.parse.urlencode(params, doseq=True))
        if st == 200 and isinstance(d, dict):
            raw = d.get("cards") or d.get("items") or []
            cards = [x for x in (tv_card(c) for c in raw) if x]
            has_more = bool(d.get("has_more")) or len(raw) >= 24
    if not cards and page == 1:
        cards = [x for x in (tv_card(c) for c in sec.get("cards") or []) if x]
    return {
        "type": "receiver.section_browse_response", "request_id": m.get("request_id"),
        "section": {"id": sid, "title": sec.get("title"), "subtitle": sec.get("subtitle")},
        "cards": cards,
        "next_cursor": _enc_cursor("tv-browse-v1", page + 1) if has_more and cards else None,
    }


def h_quick_search(conn, m):
    q = (m.get("query") or "").strip()
    lim = max(1, min(50, int(m.get("limit") or 8)))
    off = max(0, int(m.get("offset") or 0))
    page = off // lim + 1
    items, total = [], 0
    if q:
        st, d = API.get("/v1/browse/catalog?" + urllib.parse.urlencode({"q": q, "page": page, "limit": lim}))
        if st == 200 and isinstance(d, dict):
            items = [x for x in (tv_card(c) for c in d.get("cards") or d.get("items") or []) if x]
            total = int(d.get("total") or (d.get("pagination") or {}).get("total") or (off + len(items) + (lim if len(items) >= lim else 0)))
    has_more = len(items) >= lim and off + len(items) < max(total, off + len(items) + 1)
    return {
        "type": "receiver.quick_search_response", "request_id": m.get("request_id"),
        "query": q, "scenario": m.get("scenario") or "general", "fields": m.get("fields") or ["all"],
        "items": items, "offset": off, "next_offset": off + len(items) if has_more else None,
        "total": total, "has_more": has_more,
    }


def h_detail(conn, m):
    cat = _catalog(m.get("variant_id"), 0, 1)
    card = tv_card(cat) or {}
    card.update({
        "variant_id": m.get("variant_id"),
        "description": cat.get("description"),
        "actors": cat.get("actors") or [], "directors": cat.get("directors") or [],
        "episode_count": cat.get("available_episode_count") or cat.get("episode_count"),
        "selected_variant_id": cat.get("selected_variant_id") or m.get("variant_id"),
        "default_variant_id": cat.get("default_variant_id"),
        "season_count": cat.get("season_count"),
        "seasons": [{"variant_id": s.get("variant_id") or s.get("id"), "title": s.get("title"),
                     "season_label": s.get("season_label"), "season_number": s.get("season_number"),
                     "episode_count": s.get("episode_count"),
                     "available_episode_count": s.get("available_episode_count"),
                     "has_playback": s.get("has_playback", True), "selected": bool(s.get("selected")),
                     "default": bool(s.get("default"))} for s in cat.get("seasons") or cat.get("variants") or []],
    })
    return {"type": "receiver.detail_response", "request_id": m.get("request_id"),
            "variant_id": m.get("variant_id"), "detail": {k: v for k, v in card.items() if v is not None}}


def _km():
    try:
        import juchang8_member as KM
        return KM
    except Exception:
        return None


def h_library(conn, m):
    sec = m.get("section") or "history"
    uid = _local_uid(conn)
    KM = _km()
    rows = []
    if uid and KM:
        rows = KM.fav_list(uid) if sec == "favorites" else KM.hist_list(uid, 200)
    q = (m.get("q") or "").strip()
    if q:
        rows = [r for r in rows if q in (r.get("title") or "")]
    off = _dec_cursor(m.get("cursor"), "tv-lib-v1") if m.get("cursor") else 0
    lim = max(1, min(30, int(m.get("limit") or 24)))
    items = []
    for r in rows[off:off + lim]:
        it = {"id": f"{sec}_{r['cid']}", "variant_id": r["cid"], "title": r.get("title") or "",
              "poster_url": r.get("poster") or None, "has_playback": True}
        if sec == "history":
            it.update({"episode_label": r.get("episode") or None,
                       "position_seconds": int(r.get("position") or 0),
                       "duration_seconds": int(r.get("duration") or 0) or None,
                       "watched_at": _iso(r.get("updated_at") or _now()),
                       "updated_at": _iso(r.get("updated_at") or _now())})
        else:
            it["updated_at"] = _iso(r.get("added_at") or _now())
        items.append({k: v for k, v in it.items() if v is not None})
    nxt = off + lim
    return {"type": "receiver.library_response", "request_id": m.get("request_id"), "section": sec,
            "items": items, "next_cursor": _enc_cursor("tv-lib-v1", nxt) if nxt < len(rows) else None,
            "categories": {"kind_facets": [], "unclassified_count": 0}, "generated_at": _iso()}


def h_library_status(conn, m):
    uid, KM = _local_uid(conn), _km()
    vid = m.get("variant_id")
    fav = bool(uid and KM and vid and KM.fav_has(uid, vid))
    return {"type": "receiver.library_status_response", "request_id": m.get("request_id"),
            "variant_id": vid, "favorite": fav, "updated_at": _iso()}


def h_favorite_set(conn, m):
    uid, KM = _local_uid(conn), _km()
    vid, on = m.get("variant_id"), bool(m.get("favorite"))
    if not uid or not KM:
        raise ReqError("login_required", "请先在网页登录账号后再配对电视，才能同步收藏。")
    if on:
        KM.fav_add(uid, vid, m.get("title") or "", m.get("poster_url") or "")
    else:
        KM.fav_del(uid, vid)
    return {"type": "receiver.favorite_set_response", "request_id": m.get("request_id"),
            "idempotency_key": m.get("idempotency_key"), "variant_id": vid, "favorite": on, "updated_at": _iso()}


def h_history_sync(conn, m):
    uid, KM = _local_uid(conn), _km()
    n = 0
    if uid and KM:
        for it in m.get("items") or []:
            if it.get("variant_id"):
                KM.hist_put(uid, it["variant_id"], it.get("title") or "", it.get("poster_url") or "",
                            it.get("episode_label") or "", it.get("position_seconds") or 0, it.get("duration_seconds") or 0)
                n += 1
    return {"type": "receiver.history_sync_response", "request_id": m.get("request_id"),
            "migration_id": m.get("migration_id"), "applied_count": n}


def h_library_mutation(conn, m):
    uid, KM = _local_uid(conn), _km()
    n = 0
    if uid and KM and m.get("operation") == "remove":
        for it in m.get("items") or []:
            vid = it.get("variant_id")
            if not vid:
                continue
            if m.get("section") == "favorites":
                KM.fav_del(uid, vid)
            else:
                KM.hist_del(uid, vid)
            n += 1
    return {"type": "receiver.library_mutation_batch_response", "request_id": m.get("request_id"),
            "mutation_id": m.get("mutation_id"), "section": m.get("section"), "accepted": True,
            "state": "applied", "applied_count": n}


def h_playback_session(conn, m):
    return {"type": "playback.session", "request_id": m.get("request_id"),
            "device": device_json(_device_row(device_id=conn.device_id)), "entry": build_entry(conn, m, True)}


def h_playback_source(conn, m):
    return {"type": "playback.source", "request_id": m.get("request_id"),
            "device": device_json(_device_row(device_id=conn.device_id)), "entry": build_entry(conn, m, False)}


def h_episode_page(conn, m):
    off = max(0, int(m.get("episode_offset") or 0))
    lim = max(1, min(200, int(m.get("episode_limit") or 50)))
    cat = _catalog(m.get("variant_id"), off, lim)
    eps = _episodes_from_catalog(cat)
    total = (cat.get("episode_pagination") or {}).get("total_count") or len(eps)
    if len(eps) > lim:      # 上游忽略分页参数时本地切片
        total, eps = len(eps), eps[off:off + lim]
    return {"type": "episode.page", "request_id": m.get("request_id"),
            "device": device_json(_device_row(device_id=conn.device_id)),
            "object": "tv_cast.episode_page", "variant_id": m.get("variant_id"),
            "episodes": eps, "episode_pagination": _page(eps, off, lim, total)}


def h_variety_directory(conn, m):
    p = {}
    if m.get("cursor"):
        p["cursor"] = m["cursor"]
    else:
        p.update({"limit": 48, "order": "desc"})
        if m.get("month"):
            p["month"] = m["month"]
        if m.get("anchor_episode_id"):
            p["anchor_episode_id"] = m["anchor_episode_id"]
    st, d = API.get(f"/v1/catalog/{urllib.parse.quote(m.get('variant_id') or '', safe='')}/variety-episodes/v5?"
                    + urllib.parse.urlencode(p))
    if st != 200 or not isinstance(d, dict):
        raise ReqError("http_%s" % st, "综艺目录获取失败")
    return {"type": "variety.directory", "request_id": m.get("request_id"), "directory": d}


def h_skip_settings(conn, m):
    vid = m.get("variant_id")
    if m.get("report"):
        try:
            API.post("/v1/playback/skip-settings/report", dict(m["report"], variant_id=vid))
        except Exception:
            pass
    preset = None
    try:
        preset = _catalog(vid, 0, 1).get("playback_skip_settings_preset")
    except ReqError:
        pass
    return {"type": "receiver.skip_settings_response", "request_id": m.get("request_id"),
            "variant_id": vid, "playback_skip_settings_preset": preset}


def h_danmu(conn, m):
    eid = m.get("episode_id") or ""
    st, d = API.get(f"/v1/danmuku/episodes/{urllib.parse.quote(eid, safe='')}/source")
    d = d if isinstance(d, dict) else {}
    return {"type": "danmaku.source", "request_id": m.get("request_id"),
            "protocol": d.get("protocol") or "aimovie.danmu-source.v1", "episode_id": eid,
            "status": d.get("status") or ("unavailable" if st != 200 else "ready"),
            "manifest_url": d.get("manifestUrl") or d.get("manifest_url"),
            "retry_after_ms": d.get("retryAfterMs") or d.get("retry_after_ms")}


HANDLERS = {
    "receiver.bootstrap_request": h_bootstrap,
    "receiver.home_request": h_home,
    "receiver.section_browse_request": h_section_browse,
    "receiver.quick_search_request": h_quick_search,
    "receiver.detail_request": h_detail,
    "receiver.library_request": h_library,
    "receiver.library_status_request": h_library_status,
    "receiver.favorite_set_request": h_favorite_set,
    "receiver.history_sync_request": h_history_sync,
    "receiver.library_mutation_batch_request": h_library_mutation,
    "receiver.playback_session_request": h_playback_session,
    "receiver.playback_source_request": h_playback_source,
    "receiver.episode_page_request": h_episode_page,
    "receiver.variety_directory_request": h_variety_directory,
    "receiver.skip_settings_request": h_skip_settings,
    "receiver.danmu_source_request": h_danmu,
}


def _run_request(conn, m):
    t = m.get("type")
    try:
        res = HANDLERS[t](conn, m)
    except ReqError as e:
        res = {"type": "receiver.request_error", "request_id": m.get("request_id"), "code": e.code,
               "message": e.message, "retryable": e.retryable}
    except Exception as e:  # noqa
        print("[tvcast] 处理", t, "出错:", repr(e))
        res = {"type": "receiver.request_error", "request_id": m.get("request_id"), "code": "internal_error",
               "message": "TV服务暂时不可用，请稍后再试。", "retryable": True}
    conn.send(res)


# ---------------------------------------------------------------- 接收端（电视）
def on_receiver_hello(conn, m):
    install = (m.get("app_install_id") or "").strip()[:120]
    tok = (m.get("receiver_session_token") or "").strip()
    d = _device_row(token=tok) if tok else None
    if not d and install:
        d = _device_row(install_id=install)
    if d and d.get("status") == "revoked":
        d = None
    if not d:
        did = _uid("tvd")
        c = _db()
        c.execute("INSERT INTO tv_devices(id,install_id,name,model,app_version,status,receiver_token,created_at,updated_at,last_receiver_seen_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (did, install, m.get("device_name") or "剧场8 TV", m.get("device_model") or "", m.get("app_version") or "",
                   "active", "tvr_" + secrets.token_urlsafe(32), _now(), _now(), _now()))
        c.commit()
        c.close()
        d = _device_row(device_id=did)
    if m.get("reset_pairing") and d.get("paired_user_id"):
        _device_update(d["id"], paired_user_id=None, paired_user_name=None, paired_at=None)
    _device_update(d["id"], name=m.get("device_name") or d.get("name"), model=m.get("device_model") or d.get("model"),
                   app_version=m.get("app_version") or d.get("app_version"), last_receiver_seen_at=_now(), install_id=install or d.get("install_id"))
    d = _device_row(device_id=d["id"])
    old = HUB.receivers.get(d["id"])
    if old and old is not conn:
        old.alive = False
        try:
            old.ws.close()
        except Exception:
            pass
    conn.role, conn.device_id = "receiver", d["id"]
    HUB.receivers[d["id"]] = conn
    if d.get("paired_user_id"):
        conn.user = {"id": d["paired_user_id"], "name": d.get("paired_user_name")}
        msg = _paired_msg(d)
        msg["receiver_session_token"] = d["receiver_token"]
        conn.send(msg)
        HUB.to_controllers(d["id"], {"type": "receiver.connection", "device_id": d["id"], "receiver_online": True})
        HUB.flush_queue(d["id"])
    else:
        conn.send(_pairing_msg(conn, d))


def on_receiver_message(conn, m):
    t = m.get("type")
    if t == "receiver.hello":
        return on_receiver_hello(conn, m)
    if t == "ping":
        return conn.send({"type": "pong", "ts": m.get("ts") or int(_now() * 1000)})
    if not conn.device_id:
        return conn.send({"type": "error", "code": "hello_required", "message": "请先发送 receiver.hello"})
    did = conn.device_id
    if t == "player.ack":
        ack = {"type": "receiver.player_ack", "command_id": m.get("command_id"), "ok": bool(m.get("ok"))}
        for k in ("status", "error_code", "message"):
            if m.get(k) is not None:
                ack[k] = m[k]
        with _lock:
            HUB.queue[did] = [x for x in HUB.queue.get(did, []) if x[1].get("command_id") != m.get("command_id")]
        return HUB.to_controllers(did, ack)
    if t == "player.state":
        p = m.get("payload") or {}
        HUB.last_state[did] = p
        HUB.to_controllers(did, {"type": "receiver.player_state", "event_seq": m.get("event_seq"),
                                 "payload": p, "received_at": _iso()})
        _save_history(conn, p)
        return
    if t == "receiver.cancel_request":
        target = m.get("target_request_id") or m.get("cancel_request_id") or m.get("request_id")
        return conn.send({"type": "receiver.cancel_ack", "request_id": m.get("request_id"),
                          "target_request_id": target, "accepted": True})
    if t in HANDLERS:
        _pool.submit(_run_request, conn, m)
        return
    if m.get("request_id"):
        conn.send({"type": "receiver.request_error", "request_id": m.get("request_id"),
                   "code": "not_supported", "message": "该功能暂不支持"})


_hist_last = {}


def _save_history(conn, p):
    uid, KM = _local_uid(conn), _km()
    vid = p.get("variant_id")
    if not (uid and KM and vid and p.get("status") in ("playing", "paused")):
        return
    key = (uid, vid)
    if _now() - _hist_last.get(key, 0) < 15:
        return
    _hist_last[key] = _now()
    try:
        KM.hist_put(uid, vid, p.get("title") or "", "", p.get("episode_label") or "",
                    p.get("position_seconds") or 0, p.get("duration_seconds") or 0)
    except Exception:
        pass


# ---------------------------------------------------------------- 控制端（网页 / 手机）
def on_controller_message(conn, m):
    t = m.get("type")
    if t == "ping":
        return conn.send({"type": "pong", "ts": m.get("ts") or int(_now() * 1000)})
    if t == "controller.hello":
        s = ctrl_session_by_token(m.get("controller_session_token"))
        d = _device_row(device_id=s["device_id"]) if s else None
        if not s or not d or d.get("status") == "revoked" or d.get("paired_user_id") != s["user_id"]:
            conn.send({"type": "error", "code": "controller_session_invalid", "message": "设备会话已失效，请重新添加电视设备。"})
            return "close"
        conn.role, conn.device_id, conn.session = "controller", d["id"], s
        HUB.controllers.setdefault(d["id"], set()).add(conn)
        _device_update(d["id"], last_controller_seen_at=_now())
        conn.send({"type": "controller.ready", "device_id": d["id"], "session_id": s["id"],
                   "receiver_online": HUB.receiver_online(d["id"])})
        HUB.to_receiver(d["id"], {"type": "controller.connected", "session_id": s["id"],
                                  "user_id": s["user_id"], "origin": s.get("origin") or conn.base}, queue=False)
        return
    if conn.role != "controller" or not conn.session:
        conn.send({"type": "error", "code": "controller_hello_required", "message": "请先发送 controller.hello"})
        return
    if m.get("controller_session_token") and m["controller_session_token"] != conn.session["token"]:
        return conn.send({"type": "error", "code": "controller_session_invalid", "message": "设备会话已失效"})
    did = conn.device_id
    if t in ("controller.cast_intent", "controller.command"):
        cid = _uid("tvcmd")
        payload = {k: v for k, v in (m.get("payload") or {}).items() if v is not None and v != ""}
        if t == "controller.cast_intent":
            ctype, mtype = "cast.load", "cast.load"
        else:
            ctype = m.get("command_type") or "player.command"
            mtype = "player.command"
        msg = {"type": mtype, "request_id": cid, "command_id": cid, "sequence": HUB.next_seq(did),
               "payload": payload, "created_at": _iso()}
        if mtype == "player.command" and payload.get("command") in ("request_snapshot", "get_status") \
                and not HUB.receiver_online(did) and HUB.last_state.get(did):
            pass
        sent = HUB.to_receiver(did, msg, queue=True)
        conn.send({"type": "controller.command_accepted", "request_id": m.get("request_id"), "command_id": cid,
                   "command_type": ctype, "delivery": "sent" if sent else "queued", "receiver_online": sent})
        return
    conn.send({"type": "error", "code": "unsupported", "message": f"不支持的消息类型 {t}"})


# ---------------------------------------------------------------- WebSocket 入口
def handle_ws(ws, base):
    conn = Conn(ws, base)
    conn.send({"type": "server.hello", "protocol": PROTOCOL, "heartbeat_ms": HEARTBEAT_MS,
               "capabilities": {"playback_skip_settings_v1": True, "library_sync_v1": True,
                                "variety_directory_v1": True, "line_selection_mode_v1": True,
                                "voice_transcription_v1": False}})
    last_rx = _now()
    try:
        while conn.alive:
            raw = ws.receive(timeout=15)
            if raw is None:
                if _now() - last_rx > HEARTBEAT_MS / 1000 * 4:
                    break
                # 未配对电视：配对码过期后自动换新
                if conn.role == "receiver" and getattr(conn, "pairing_expires", 0) and _now() > conn.pairing_expires - 5:
                    d = _device_row(device_id=conn.device_id)
                    if d and not d.get("paired_user_id"):
                        conn.send(_pairing_msg(conn, d))
                    else:
                        conn.pairing_expires = 0
                continue
            last_rx = _now()
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if not isinstance(m, dict):
                continue
            t = str(m.get("type") or "")
            if conn.role is None:
                if t == "receiver.hello":
                    on_receiver_message(conn, m)
                elif t.startswith("controller.") or t == "ping":
                    if on_controller_message(conn, m) == "close":
                        break
                continue
            if conn.role == "receiver":
                on_receiver_message(conn, m)
            elif on_controller_message(conn, m) == "close":
                break
    except Exception:
        pass
    finally:
        conn.alive = False
        if conn.role == "receiver" and HUB.receivers.get(conn.device_id) is conn:
            HUB.receivers.pop(conn.device_id, None)
            _device_update(conn.device_id, last_receiver_seen_at=_now())
            HUB.to_controllers(conn.device_id, {"type": "receiver.connection", "device_id": conn.device_id,
                                                "receiver_online": False})
        elif conn.role == "controller":
            HUB.controllers.get(conn.device_id, set()).discard(conn)
        try:
            ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 注册到 Flask
def register(app, me_func=None):
    global API
    try:
        from flask_sock import Sock
    except ImportError:
        print("[tvcast] 未安装 flask-sock（pip install flask-sock），TV 投屏服务未启用")
        return False
    from flask import request, jsonify

    init_db()
    API = Api(app)
    sock = Sock(app)

    def base_url():
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "http"
        host = request.headers.get("X-Forwarded-Host") or request.host
        return f"{proto}://{host}"

    def cur_user():
        u = me_func() if me_func else None
        if u:
            return {"id": f"user_{u['id']}", "name": u.get("nickname") or u.get("account")}
        return None

    def need_user():
        r = jsonify({"error": "unauthorized", "message": "请先登录本站账号后再连接电视。"})
        r.status_code = 401
        return r

    @sock.route("/v1/tv-cast/ws")
    def tv_cast_ws(ws):
        handle_ws(ws, base_url())

    @app.route("/v1/tv-cast/pairings/confirm", methods=["POST"])
    def tv_cast_confirm():
        u = cur_user()
        if not u:
            return need_user()
        d = request.get_json(silent=True) or {}
        r = confirm_pairing(d.get("code"), u["id"], u["name"], d.get("browser_session_id"), d.get("origin"))
        if not r:
            resp = jsonify({"error": "invalid_pairing_code", "message": "Pairing code is invalid or expired."})
            resp.status_code = 400
            return resp
        dev, s = r
        resp = jsonify({"object": "tv_cast.pairing", "paired": True, "device": device_json(dev, True),
                        "controller_session": ctrl_session_json(s), "controller_session_token": s["token"]})
        resp.status_code = 201
        return resp

    @app.route("/v1/tv-cast/devices", methods=["GET"])
    def tv_cast_devices():
        u = cur_user()
        if not u:
            return need_user()
        lim = max(1, min(50, int(request.args.get("limit") or 20)))
        c = _db()
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM tv_devices WHERE paired_user_id=? AND status!='revoked' ORDER BY updated_at DESC LIMIT ?",
            (u["id"], lim))]
        c.close()
        return jsonify({"object": "tv_cast.device.list", "items": [device_json(r, True) for r in rows]})

    @app.route("/v1/tv-cast/devices/<device_id>/controller-session", methods=["POST"])
    def tv_cast_ctrl_session(device_id):
        u = cur_user()
        if not u:
            return need_user()
        dev = _device_row(device_id=device_id)
        if not dev or dev.get("paired_user_id") != u["id"] or dev.get("status") == "revoked":
            resp = jsonify({"error": "not_found", "message": "电视设备不存在或已解除配对。"})
            resp.status_code = 404
            return resp
        d = request.get_json(silent=True) or {}
        s = new_ctrl_session(device_id, u["id"], u["name"], d.get("browser_session_id"), d.get("origin"))
        _device_update(device_id, last_controller_seen_at=_now())
        resp = jsonify({"object": "tv_cast.controller_session_grant", "device": device_json(_device_row(device_id=device_id), True),
                        "controller_session": ctrl_session_json(s), "controller_session_token": s["token"]})
        resp.status_code = 201
        return resp

    @app.route("/v1/tv-cast/devices/<device_id>", methods=["DELETE"])
    def tv_cast_delete(device_id):
        u = cur_user()
        if not u:
            return need_user()
        dev = _device_row(device_id=device_id)
        if not dev or dev.get("paired_user_id") != u["id"]:
            resp = jsonify({"error": "not_found", "message": "电视设备不存在。"})
            resp.status_code = 404
            return resp
        _device_update(device_id, paired_user_id=None, paired_user_name=None, paired_at=None)
        c = _db()
        c.execute("DELETE FROM tv_ctrl_sessions WHERE device_id=?", (device_id,))
        c.commit()
        c.close()
        for cc in list(HUB.controllers.pop(device_id, set())):
            cc.alive = False
        rc = HUB.receivers.get(device_id)
        if rc:
            rc.user = None
            rc.send(_pairing_msg(rc, _device_row(device_id=device_id)))
        return jsonify({"object": "tv_cast.device.deleted", "device": device_json(_device_row(device_id=device_id))})

    @app.route("/v1/app-updates/check", methods=["GET", "POST"])
    def tv_app_update_check():
        # 自编译的 TV 版不接受官方升级推送，避免被覆盖回内置服务器版本
        return jsonify({"object": "app.update_check", "update_available": False, "required": False,
                        "reason": "up_to_date"})

    @app.route("/v1/tv-cast/voice/transcriptions", methods=["POST"])
    def tv_cast_voice():
        resp = jsonify({"error": "not_supported", "message": "本服务器暂不支持语音搜索"})
        resp.status_code = 501
        return resp

    print("[tvcast] 剧场8 TV 投屏服务已启用（原生 /v1/tv-cast/ws，不依赖 kanju.ai）")
    return True
