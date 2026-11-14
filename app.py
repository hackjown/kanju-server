#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看剧AI 复刻站 —— 后端
复现前端硬编码 HMAC 签名，代理上游 /v1/catalog、/v1/feed/home、/v1/playback/episodes，
并把 resolve:// 票据与 m3u8 直链解析出来给前端播放。
"""
import os, json, time, hmac, hashlib, secrets, threading, re, urllib.parse, functools, subprocess, struct, shutil

import requests
from flask import Flask, jsonify, request, send_from_directory, Response, make_response

KEY = "557d0e4ae929f438da6bd84412374e6086b8af09b3fed54bf22601d5bf8c54a0"
BASES = ["https://kanju26.com", "https://kanju1.ai", "https://kanju13.com"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def _ensure_cache_seeds():
    """容器启动或首次构建时，自动从 seeds 种子目录恢复离线容灾缓存"""
    try:
        seeds_dir = os.path.join(HERE, "seeds")
        for fn in ["home.json", "bootstrap.json", "rank_hot.json"]:
            dst = os.path.join(CACHE_DIR, fn)
            src = os.path.join(seeds_dir, fn)
            if not os.path.exists(src):
                continue
            need_copy = not os.path.exists(dst)
            if not need_copy and fn == "home.json":
                try:
                    with open(dst, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        if not isinstance(d, dict) or len(d.get("sections", [])) < 8:
                            need_copy = True
                except Exception:
                    need_copy = True
            if need_copy:
                import shutil
                shutil.copyfile(src, dst)
    except Exception:
        pass

_ensure_cache_seeds()

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "server_error", "message": "服务器忙，请重试"}), 500

_sess = requests.Session()
_sess.headers.update({"User-Agent": UA})
BOOT = {"base": None, "feed": None, "ts": 0}
_lock = threading.Lock()


def sign(method, path, ts, nonce):
    msg = f"{method.upper()}\n{path}\n{ts}\n{nonce}"
    return hmac.new(KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def signed_get(base, path, extra=None, timeout=25):
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    h = {
        "Accept": "application/json",
        "Origin": base,
        "Referer": base + "/",
        "x-ai-movie-timestamp": ts,
        "x-ai-movie-nonce": nonce,
        "x-ai-movie-signature": sign("GET", path, ts, nonce),
        "x-ai-movie-client-name": "web",
    }
    if extra:
        h.update(extra)
    return _sess.get(base + path, headers=h, timeout=timeout)


def clean_experience_ads(data):
    """清理上游广告：清空所有推广位、翻转推荐卡片与开屏广告"""
    if not isinstance(data, dict):
        return
    exp = data.get("app_experience") if "app_experience" in data else (data if data.get("object") == "app.experience_snapshot" else None)
    if isinstance(exp, dict):
        exp["generated_at"] = "2026-11-06T15:00:00.000Z"
        placements = exp.get("placements")
        if isinstance(placements, dict):
            # 1. 顶部轮播广告
            if "home_top_carousel" in placements and isinstance(placements["home_top_carousel"], dict):
                placements["home_top_carousel"]["items"] = []
            # 2. 播放器翻转卡片与推荐位广告
            if "player_info" in placements and isinstance(placements["player_info"], dict):
                p = placements["player_info"]
                p["enabled"] = False
                p["presentation_mode"] = "none"
                p["mobile_presentation_mode"] = "none"
                p["large_screen_presentation_mode"] = "none"
                p["items"] = []
                p["banner_items"] = []
                p["grid_items"] = []
                if isinstance(p.get("auto_flip"), dict):
                    p["auto_flip"]["enabled"] = False
            # 3. 搜索与日历页推广广告
            if "showcase_grid" in placements and isinstance(placements["showcase_grid"], dict):
                s = placements["showcase_grid"]
                s["calendar_enabled"] = False
                s["search_enabled"] = False
                s["items"] = []
            # 4. 开屏广告
            if "splash_screen" in placements and isinstance(placements["splash_screen"], dict):
                placements["splash_screen"]["items"] = []
                placements["splash_screen"]["daily_impression_limit"] = 0


def pick_base():
    """找一个当前可用的上游域名，并把匿名会话建好。"""
    with _lock:
        if BOOT["base"]:
            return BOOT["base"]
        for b in BASES:
            try:
                r = signed_get(b, "/v1/runtime/bootstrap", timeout=12)
                if r.status_code == 200:
                    b_data = r.json()
                    clean_experience_ads(b_data)
                    json.dump(b_data, open(os.path.join(CACHE_DIR, "bootstrap.json"), "w"),
                              ensure_ascii=False)
                    _sess.post(b + "/v1/users/anonymous", json={}, timeout=12,
                               headers={"Origin": b, "Referer": b + "/"})
                    BOOT["base"] = b
                    return b
            except Exception:
                continue
        return None


def api_get(path, cache_key=None, ttl=600, extra=None):
    base = pick_base()
    cf = os.path.join(CACHE_DIR, (cache_key or "x") + ".json")
    if cache_key and os.path.exists(cf) and time.time() - os.path.getmtime(cf) < ttl:
        try:
            return json.load(open(cf, encoding="utf-8")), None
        except Exception:
            pass

    def _seed_fallback():
        if path.startswith("/v1/feed/home") or cache_key == "home":
            sfp = os.path.join(HERE, "seeds", "home.json")
            if os.path.exists(sfp):
                try: return json.load(open(sfp, encoding="utf-8")), None
                except Exception: pass
        if "hot_list_key" in path or "rank" in (cache_key or ""):
            sfp = os.path.join(HERE, "seeds", "rank_hot.json")
            if os.path.exists(sfp):
                try: return json.load(open(sfp, encoding="utf-8")), None
                except Exception: pass
        if "bootstrap" in path:
            sfp = os.path.join(HERE, "seeds", "bootstrap.json")
            if os.path.exists(sfp):
                try: return json.load(open(sfp, encoding="utf-8")), None
                except Exception: pass
        return None

    if not base:
        fb = _seed_fallback()
        if fb: return fb
        return None, "上游不可达"

    try:
        r = signed_get(base, path, extra=extra)
    except Exception as e:
        fb = _seed_fallback()
        if fb: return fb
        return None, f"请求失败 {e}"

    if r.status_code != 200:
        fb = _seed_fallback()
        if fb: return fb
        return None, f"上游 {r.status_code}"

    try:
        d = r.json()
    except Exception:
        fb = _seed_fallback()
        if fb: return fb
        return None, "上游返回非 JSON"

    if cache_key:
        try:
            json.dump(d, open(cf, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return d, None


# ---------------- 页面与统一 CORS 治理 ----------------
@app.after_request
def _after_request_cors_and_cache(resp):
    try:
        if "text/html" in (resp.content_type or ""):
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
        
        origin = request.headers.get("Origin")
        if origin:
            # 严格镜像 Origin，避免 credentials: 'include' 下浏览器因 wildcard * 阻断 Fetch/XHR
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Vary"] = "Origin"
        elif "Access-Control-Allow-Origin" not in resp.headers:
            resp.headers["Access-Control-Allow-Origin"] = "*"
            
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        req_headers = request.headers.get("Access-Control-Request-Headers")
        if req_headers:
            resp.headers["Access-Control-Allow-Headers"] = req_headers
        elif "Access-Control-Allow-Headers" not in resp.headers:
            resp.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, x-ai-movie-site-host, x-ai-movie-midnight-access, "
                "x-ai-movie-timestamp, x-ai-movie-nonce, x-ai-movie-signature, x-ai-movie-client-name, "
                "x-ai-movie-client-version, x-ai-movie-build-version, x-ai-movie-protocol-version, "
                "X-Requested-With, Accept, Origin"
            )
    except Exception:
        pass
    return resp


@app.route("/assets/<path:path>")
def serve_assets(path):
    resp = send_from_directory(os.path.join(HERE, "static", "assets"), path)
    resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


@app.route("/app-bootstrap.js")
def serve_bootstrap():
    resp = send_from_directory(os.path.join(HERE, "static"), "app-bootstrap.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


@app.route("/manifest.webmanifest")
def serve_manifest():
    return send_from_directory(os.path.join(HERE, "static"), "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/ai-movie-sw.js")
def serve_sw():
    resp = send_from_directory(os.path.join(HERE, "static"), "ai-movie-sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


@app.route("/favicon.ico")
def serve_favicon():
    return send_from_directory(os.path.join(HERE, "static", "assets"), "dianyingtiantang-icon.png")


@app.route("/legacy")
@app.route("/index_legacy.html")
def page_legacy():
    return send_from_directory(os.path.join(HERE, "static"), "index_legacy.html")


@app.route("/legacy/midnight")
def page_legacy_midnight():
    return send_from_directory(os.path.join(HERE, "static"), "midnight.html")


@app.route("/legacy/download")
def page_legacy_download():
    return send_from_directory(os.path.join(HERE, "static"), "download.html")


@app.route("/legacy/tesla")
def page_legacy_tesla():
    return send_from_directory(os.path.join(HERE, "static"), "tesla.html")


@app.route("/")
@app.route("/player")
@app.route("/player/<path:p>")
@app.route("/short-drama")
@app.route("/short-drama/<path:p>")
@app.route("/midnight-theater")
@app.route("/playlists")
@app.route("/playlists/<path:p>")
@app.route("/collections/<path:p>")
@app.route("/library/<path:p>")
@app.route("/updates")
@app.route("/session/<path:p>")
def index(p=None):
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/static/hls.min.js")
def hls():
    return send_from_directory(os.path.join(HERE, "static"), "hls.min.js")


# ---------------- 接口 ----------------
@app.route("/api/home")
def api_home():
    d, err = api_get("/v1/feed/home?scope=public&mode=preview&cards=18",
                     cache_key="home", ttl=300)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    sections = []
    for s in d.get("sections", []):
        cards = []
        for c in s.get("cards", []):
            cards.append({
                "id": c.get("id"), "title": c.get("title"), "year": c.get("year"),
                "area": c.get("area"), "genres": c.get("genres", [])[:3],
                "poster": c.get("poster_url"), "backdrop": c.get("backdrop_url"),
                "kind": c.get("content_kind"), "remarks": c.get("remarks"),
                "rating": c.get("rating") or c.get("score"),
            })
        sections.append({"title": s.get("title"), "subtitle": s.get("subtitle"), "cards": cards})
    return jsonify({"ok": True, "sections": sections, "base": BOOT["base"]})


@app.route("/api/search")
def api_search():
    """KANJU_SEARCH_FIX: 用上游 query= 参数，标签取 suggestions[].label / target.variant_id"""
    q = (request.args.get("q") or request.args.get("query") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "缺少关键词"}), 400
    path = "/v1/suggest?query=" + urllib.parse.quote(q) + "&limit=20&scope=public"
    d, err = api_get(path, cache_key="search_" + hashlib.md5(q.encode()).hexdigest(), ttl=900)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    raw = d.get("items") or d.get("results") or d.get("suggestions") or []
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        tgt = it.get("target") or {}
        title = it.get("label") or it.get("title") or it.get("prompt")
        cid = tgt.get("variant_id") or it.get("catalog_id") or it.get("id")
        sub = it.get("subtitle") or ""
        year = None
        m = re.match(r"(\d{4})", sub)
        if m:
            year = int(m.group(1))
        items.append({"id": cid, "cid": cid, "title": title,
                      "label": title, "subtitle": sub, "year": year,
                      "poster": it.get("poster_url"),
                      "kind": (it.get("plan") or {}).get("kind"),
                      "remarks": sub,
                      "playable": tgt.get("direct_playable")})
    return jsonify({"ok": True, "q": q, "items": items, "count": len(items)})


@app.route("/api/detail/<cid>")
def api_detail(cid):
    d, err = api_get("/v1/catalog/" + cid, cache_key="detail_" + cid[:32],
                     ttl=3600)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    eps = [{"id": e.get("id"), "title": e.get("title") or e.get("name"),
            "key": e.get("key"), "number": e.get("number"),
            "index": e.get("number") or e.get("index") or e.get("episode_number") or e.get("order"),
            "token": e.get("token") or e.get("playback_token") or e.get("episode_token")}
           for e in (d.get("episodes") or [])]
    return jsonify({
        "ok": True,
        "id": d.get("id"), "title": d.get("title"), "year": d.get("year"),
        "area": d.get("area"), "genres": d.get("genres"),
        "actors": d.get("actors"), "directors": d.get("directors"),
        "poster": d.get("poster_url"), "backdrop": d.get("backdrop_url"),
        "description": d.get("description"), "remarks": d.get("remarks"),
        "progress": d.get("episode_progress_text"),
        "episode_count": d.get("episode_count"),
        "episodes": eps,
        "groups": [g.get("label") or g.get("name") for g in (d.get("playback_groups") or [])],
        "raw_episode_keys": list((d.get("episodes") or [{}])[0].keys()) if d.get("episodes") else [],
    })


def _session_extra():
    """运营者绑定的上游账号会话头（kanju_upstream.json），未绑定返回 None。"""
    try:
        conf = _upstream_conf()
    except Exception:
        return None
    h = {}
    if conf.get("cookie"):
        h["Cookie"] = conf["cookie"]
    if conf.get("authorization"):
        h["Authorization"] = conf["authorization"]
    return h or None


@app.route("/api/play/<path:token>")
def api_play(token):
    # token 是 episode 的 token（av_...），详情接口返回；直接签名请求上游
    _off = request.args.get("offset", "0")
    _lim = request.args.get("limit", "48")
    _pq = "?offset=" + urllib.parse.quote(_off) + "&limit=" + urllib.parse.quote(_lim)
    _sx = _session_extra()
    d, err = api_get("/v1/playback/episodes/" + token + _pq,
                     cache_key="pb_" + hashlib.md5((token + _pq + ("_s" if _sx else "")).encode()).hexdigest(),
                     ttl=1800, extra=_sx)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    lines = []
    for ln in (d.get("line_options") or []):
        u = ln.get("url") or ""
        if u.startswith("resolve://"):
            kind = "resolve"
        elif ".m3u8" in u:
            kind = "hls"
        elif u.startswith("http") and (".mp4" in u):
            kind = "mp4"
        elif u.startswith("http"):
            kind = "page"
        else:
            kind = "other"
        lines.append({
            "label": ln.get("display_label") or ln.get("label") or ln.get("provider_name"),
            "provider": ln.get("provider_id"),
            "kind": kind,
            "url": u if kind != "resolve" else "",
            "ticket": u if kind == "resolve" else "",
            "sid": ln.get("playback_source_id") or ln.get("id"),
            "resolved": bool(ln.get("resolved")),
            "selected": bool(ln.get("selected")),
            "level": ln.get("minimum_user_level"),
        })
    order = {"hls": 0, "mp4": 1, "resolve": 2, "page": 3, "other": 4}
    def _is4k(s):
        s = (s or "").upper()
        return ("4K" in s) or ("2160" in s) or ("极高" in (s or "")) or ("蓝光" in (s or ""))
    for _l in lines:
        _l["is4k"] = _is4k(_l.get("label")) or _is4k(_l.get("provider"))
    # KANJU_4K_PATCH: 4K 优先（同 kind 内 4K 排最前）
    lines.sort(key=lambda x: (0 if x.get("is4k") else 1, order.get(x["kind"], 9), x["label"] or ""))
    cur = d.get("current_episode") or {}
    def _ep(e):
        return {"title": e.get("display_name") or e.get("title"),
                "number": e.get("episode_number") or e.get("number") or e.get("sequence_index"),
                "key": e.get("episode_key"),
                "prev": e.get("previous_episode_token"),
                "next": e.get("next_episode_token"),
                "token": e.get("token")}
    return jsonify({"ok": True, "token": token,
                    "current": _ep(cur),
                    "lines": lines,
                    "pagination": d.get("episode_pagination"),
                    "episodes": [_ep(e) for e in (d.get("episodes") or [])]})


@app.route("/api/car/play/<path:token>")
def api_car_play(token):
    """车机专属极速播放线路接口：
    1. 彻底移除网盘 4K 源（不加载 115、夸克、UC，防止超大原画文件导致车机卡顿、缓冲和流量耗尽）；
    2. 第一梯队：看剧官方 4K 直链 (Kanju 4K)；
    3. 第二梯队：本地 4K 蓝光采集专线 (暴风4K, 非凡4K, 量子4K, 天空4K)；
    4. 第三梯队：1080P 高清与普通采集源；
    5. 自动解析 resolve:// 票据为即开型 m3u8 HLS 直链，专为车机 WebGL Canvas 秒开优化。
    """
    m_title = (request.args.get("title") or "").strip()
    try:
        ep_num = int(request.args.get("ep") or "1")
    except Exception:
        ep_num = 1

    # 尝试从全局映射与缓存中读取片名
    if not m_title:
        ep_info = _EP_TOKEN_MAP.get(token) or {}
        m_title = ep_info.get("title") or ""
        ep_num = ep_info.get("ep_num") or ep_num

    if not m_title:
        tok_cache = get_catalog_cache(f"tok:{token}")
        if tok_cache:
            m_title = tok_cache.get("title") or ""
            ep_num = tok_cache.get("ep_num") or ep_num

    # 1. 获取看剧上游原始线路并过滤掉所有网盘
    _sx = _session_extra()
    d, err = api_get("/v1/playback/episodes/" + token + "?offset=0&limit=48",
                     cache_key="car_pb_" + hashlib.md5((token + ("_s" if _sx else "")).encode()).hexdigest(),
                     ttl=1800, extra=_sx)
    
    raw_lines = (d.get("line_options") or []) if (not err and isinstance(d, dict)) else []

    juchang8_4k_lines = []
    juchang8_normal_lines = []

    def _is4k(s):
        s = (s or "").upper()
        return ("4K" in s) or ("2160" in s) or ("极高" in (s or "")) or ("蓝光" in (s or ""))

    def _is_junk_url(url_str):
        if not url_str or not isinstance(url_str, str):
            return True
        us = url_str.lower().split("?")[0]
        if any(us.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".html", ".htm", ".bmp")):
            return True
        if any(k in url_str.lower() for k in ("byteimg.com", "image.image", "tplv-", "youku.com/v_show", "iqiyi.com/v_", "v.qq.com/x/cover", "bilibili.com/video")):
            return True
        return False

    for ln in raw_lines:
        u = ln.get("url") or ""
        lbl = ln.get("display_label") or ln.get("label") or ln.get("provider_name") or ""
        # 严格过滤任何网盘源 (115, 夸克, UC)
        if any(k in lbl for k in ("网盘", "115", "夸克", "UC")):
            continue
        if any(k in u for k in ("/api/pan/", "pan.quark.cn", "115.com", "drive.uc.cn")):
            continue

        kind = "hls" if ".m3u8" in u else ("mp4" if ".mp4" in u else ("resolve" if u.startswith("resolve://") else "other"))
        is4k = _is4k(lbl) or _is4k(ln.get("provider_id")) or bool(ln.get("is_4k"))

        # 如果是 resolve:// 票据，换成直链
        real_url = u
        if kind == "resolve":
            ticket = u[10:] if u.startswith("resolve://") else u
            now = int(time.time())
            cache_key = f"ticket:{ticket}"
            cached = KC._RESOLVE_CACHE.get(cache_key)
            if cached and cached.get("expire_at", 0) > now:
                real_url = (cached.get("data", {}).get("line") or {}).get("url") or ""
            else:
                try:
                    st_up, r_up = up_req("POST", "/v1/playback/resolve-line?view=compact", {"ticket": ticket}, timeout=6)
                    if st_up in (200, 201) and isinstance(r_up, dict):
                        KC._RESOLVE_CACHE[cache_key] = {"data": r_up, "expire_at": now + 7200}
                        real_url = (r_up.get("line") or {}).get("url") or ""
                except Exception:
                    pass

        if not real_url or _is_junk_url(real_url):
            continue

        item = {
            "label": f"★ 4K超清(需大带宽) · {lbl}" if is4k else f"⚡ 1080P极速秒开 · {lbl}",
            "kind": "hls" if ".m3u8" in real_url else kind,
            "url": real_url,
            "is_4k": is4k,
            "source_type": "juchang8"
        }
        if is4k:
            juchang8_4k_lines.append(item)
        else:
            juchang8_normal_lines.append(item)

    # 2. 获取本地 4K 采集源（暴风4K, 非凡4K, 量子4K, 天空4K）与普通采集源
    collector_4k_lines = []
    collector_normal_lines = []

    if m_title:
        try:
            c_lines = KC.build_local_line_options(m_title, episode_index=ep_num)
            for cl in c_lines:
                u = cl.get("url") or ""
                if not u or _is_junk_url(u):
                    continue
                # 自动规整网页跳转层，直接获取真实 m3u8 直链
                if ("/play/" in u or "/share/" in u) and not (".m3u8" in u or ".mp4" in u):
                    u = u.rstrip("/") + "/index.m3u8"
                lbl = cl.get("label") or "采集线路"
                is4k = bool(cl.get("is_4k")) or _is4k(lbl)
                is_fast = any(k in lbl for k in ("极速", "非凡", "量子", "暴风", "天空", "秒开", "快播"))
                c_item = {
                    "label": f"★ 4K超清(需大带宽) · {lbl}" if is4k else (f"⚡ 1080P极速秒开 · {lbl}" if is_fast else f"1080P备用 · {lbl}"),
                    "kind": "hls" if ".m3u8" in u else "mp4",
                    "url": u,
                    "is_4k": is4k,
                    "source_type": "collector"
                }
                if is4k:
                    collector_4k_lines.append(c_item)
                else:
                    collector_normal_lines.append(c_item)
        except Exception as e:
            print(f"[CarPlay] Collector lines error: {e}")

    # 3. 车机专属极速优先排序（优先 HLS .m3u8 分片流秒开，杜绝大 MP4 缓冲卡顿与防盗链）：
    # 梯队 1: 极速秒开 HLS 专线（分片即开，0 缓冲，WebCodecs 硬件直解首选）
    # 梯队 2: 普通/MP4 备用流
    # 梯队 3: 4K 超清专线（排在列表末尾，大带宽 Wi-Fi 备用）
    hls_fast = []
    other_fast = []
    bitrate_4k_lines = [l for l in (juchang8_4k_lines + collector_4k_lines) if l.get("url")]

    for l in (collector_normal_lines + juchang8_normal_lines):
        if not l.get("url"):
            continue
        u = l.get("url")
        if ".m3u8" in u:
            hls_fast.append(l)
        else:
            other_fast.append(l)

    raw_final = hls_fast + other_fast + bitrate_4k_lines
    seen_urls = set()
    final_lines = []
    for item in raw_final:
        u = item.get("url")
        if u and u not in seen_urls:
            seen_urls.add(u)
            final_lines.append(item)

    return jsonify({
        "ok": True,
        "token": token,
        "title": m_title,
        "ep_num": ep_num,
        "lines": final_lines,
        "total_4k": len(juchang8_4k_lines) + len(collector_4k_lines),
        "total_normal": len(juchang8_normal_lines) + len(collector_normal_lines)
    })


@app.route("/api/resolve_line")
def api_resolve_line():
    """解析线路：带运营者上游会话通过 POST /v1/playback/resolve-line 立即把 resolve:// 票据换成直链（解决 4K 解析慢与失败问题）。"""
    token = request.args.get("token") or ""
    sid = request.args.get("sid") or ""
    ticket = (request.args.get("ticket") or "").strip()

    if ticket:
        if ticket.startswith("resolve://"):
            ticket = ticket[10:]
    elif token and sid:
        sx = _session_extra()
        d, err = api_get("/v1/playback/episodes/" + token + "?offset=0&limit=48", ttl=1800, extra=sx)
        if d:
            for ln in (d.get("line_options") or []):
                if (ln.get("playback_source_id") or ln.get("id")) == sid:
                    u = ln.get("url") or ln.get("resolve_ticket") or ""
                    if u.startswith("resolve://"):
                        ticket = u[10:]
                    else:
                        ticket = u
                    break

    if not ticket:
        return jsonify({"ok": False, "error": "未能获取该线路的解析票据 (ticket)"}), 400

    # 优先查本地 2 小时缓存
    cache_key = f"ticket_res:{ticket[:48]}"
    now = int(time.time())
    if cache_key in KC._RESOLVE_CACHE and KC._RESOLVE_CACHE[cache_key]["expire_at"] > now:
        cached_line = KC._RESOLVE_CACHE[cache_key]["data"].get("line") or {}
        resolved_url = cached_line.get("url") or ""
        if resolved_url:
            kind = "hls" if ".m3u8" in resolved_url else ("mp4" if ".mp4" in resolved_url else "page")
            return jsonify({
                "ok": True,
                "url": resolved_url,
                "kind": kind,
                "label": cached_line.get("display_label") or cached_line.get("label") or "4K",
                "tries": 1
            })

    path = "/v1/playback/resolve-line?view=compact"
    st, r = up_req("POST", path, {"ticket": ticket}, timeout=20)
    if st in (200, 201) and isinstance(r, dict):
        KC._RESOLVE_CACHE[cache_key] = {"data": r, "expire_at": now + 7200}
        line = r.get("line") or {}
        resolved_url = line.get("url") or ""
        if resolved_url:
            kind = "hls" if ".m3u8" in resolved_url else ("mp4" if ".mp4" in resolved_url else "page")
            return jsonify({
                "ok": True,
                "url": resolved_url,
                "kind": kind,
                "label": line.get("display_label") or line.get("label") or "4K",
                "tries": 1
            })

    err_msg = r.get("error", {}).get("message") if isinstance(r, dict) and isinstance(r.get("error"), dict) else (r.get("error") if isinstance(r, dict) else f"上游状态码 {st}")
    return jsonify({"ok": False, "error": f"解析失败: {err_msg}"}), 502


def proxy_m3u8_directly(u, ref):
    """就地重写并直出 m3u8 播放列表，零 302 跳转延迟，解决播放器跨域与换源卡顿问题"""
    import urllib.parse as up
    pu = up.urlparse(u)
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": ref,
        "Origin": ref.rstrip("/"),
    }
    if "115.com" in pu.netloc or "115cdn" in pu.netloc or "cpats" in pu.netloc or ref == "https://115.com/":
        ck_115 = KPAN.get_active_pan_cookie("115")
        if ck_115:
            h["Cookie"] = ck_115
    elif "quark" in pu.netloc or ref == "https://pan.quark.cn/":
        ck_quark = KPAN.get_active_pan_cookie("quark")
        if ck_quark:
            h["Cookie"] = ck_quark

    def wrap(absu, r):
        prefix = request.host_url.rstrip("/") if request else ""
        return prefix + "/proxy?u=" + up.quote(absu, safe="") + "&ref=" + up.quote(r, safe="")

    try:
        r = _sess.get(u, headers=h, timeout=25, allow_redirects=True)
    except Exception as e:
        return "upstream error %s" % e, 502

    if r.status_code != 200:
        return "upstream %s" % r.status_code, r.status_code

    txt = r.content.decode("utf-8", "ignore")
    base_url = r.url
    out = []
    for line in txt.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            def _rw(m):
                v = m.group(2)
                if v.startswith("data:"):
                    return m.group(0)
                return '%s"%s"' % (m.group(1), wrap(up.urljoin(base_url, v), ref))
            out.append(re.sub(r'(URI=)"([^"]+)"', _rw, s))
        else:
            out.append(wrap(up.urljoin(base_url, s), ref))
    return Response("\n".join(out), content_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@app.route("/proxy", methods=["GET", "HEAD"])
def proxy():
    """转发 m3u8 / 子清单 / TS 分片。支持 Range 分片与流式转发，解决 4K / byte-range 分片播放问题。"""
    import urllib.parse as up
    u = request.args.get("u")
    if not u or not u.startswith("http"):
        return "bad u", 400
    pu = up.urlparse(u)
    if not pu.netloc:
        return "bad u", 400
    ref = request.args.get("ref") or (pu.scheme + "://" + pu.netloc + "/")

    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": ref,
        "Origin": ref.rstrip("/"),
    }
    if request.headers.get("Range"):
        h["Range"] = request.headers["Range"]

    # 自动为网盘请求（115 / Quark）注入有效 Cookie，确保 m3u8 和分片切片顺利返回 200 而非 403
    if "115.com" in pu.netloc or "115cdn" in pu.netloc or "cpats" in pu.netloc or ref == "https://115.com/":
        ck_115 = KPAN.get_active_pan_cookie("115")
        if ck_115:
            h["Cookie"] = ck_115
    elif "quark" in pu.netloc or ref == "https://pan.quark.cn/":
        ck_quark = KPAN.get_active_pan_cookie("quark")
        if ck_quark:
            h["Cookie"] = ck_quark

    # 支持 HEAD 快速嗅探探针
    if request.method == "HEAD":
        try:
            r = _sess.head(u, headers=h, timeout=15, allow_redirects=True)
            resp = Response("", status=r.status_code)
            for fwd in ("Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"):
                if fwd in r.headers:
                    resp.headers[fwd] = r.headers[fwd]
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception:
            return Response("", status=200, headers={"Access-Control-Allow-Origin": "*"})

    is_m3u8_url = ".m3u8" in pu.path or "m3u8" in request.args.get("format", "")
    if is_m3u8_url:
        return proxy_m3u8_directly(u, ref)

    try:
        r = _sess.get(u, headers=h, timeout=30, allow_redirects=True, stream=True)
    except Exception as e:
        return "upstream error %s" % e, 502

    ct = (r.headers.get("Content-Type") or "").lower()
    if "mpegurl" in ct:
        return proxy_m3u8_directly(u, ref)

    hdr = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Cache-Control": "no-store"
    }
    for fwd in ("Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"):
        if fwd in r.headers:
            hdr[fwd] = r.headers[fwd]

    def gen():
        for chunk in r.iter_content(262144):
            if chunk:
                yield chunk
    return Response(gen(), status=r.status_code, headers=hdr)


# ===== 改善型方案三：车机专属服务端极速转推流引擎 (行车免黑屏 + 零卡顿) =====
_car_duration_cache = {}


def _find_bin(name):
    # 1. 系统 PATH 检索
    p = shutil.which(name)
    if p:
        return p

    # 2. 常见 Linux / Mac / 容器系统路径及本地项目目录
    cand_dirs = [
        "/usr/bin",
        "/usr/local/bin",
        "/bin",
        "/snap/bin",
        "/opt/homebrew/bin",
        os.path.join(HERE, "bin")
    ]
    for d in cand_dirs:
        cand = os.path.join(d, name)
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand

    # 3. 如果是 ffmpeg，尝试从 imageio_ffmpeg 预编译静态库直接提取开箱即用的跨平台二进制
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and os.path.exists(exe) and os.access(exe, os.X_OK):
                return exe
        except Exception:
            pass

    return name


def _ensure_ffmpeg():
    """检测环境 FFmpeg（方案A采用客户端硬解，服务端无需强制安装）"""
    bin_path = _find_bin("ffmpeg")
    if shutil.which(bin_path) or (os.path.exists(bin_path) and os.access(bin_path, os.X_OK)):
        return bin_path
    return "ffmpeg"


def _resolve_stream_target(raw_u):
    """把 /proxy?u=... 彻底还原为目标真实 URL，并生成反盗链请求头，杜绝本地死锁回环"""
    u = (raw_u or "").strip()
    while "/proxy?" in u:
        parsed = urllib.parse.urlparse(u)
        qs = urllib.parse.parse_qs(parsed.query)
        if "u" in qs and qs["u"]:
            u = qs["u"][0].strip()
        else:
            break

    if u.startswith("//"):
        u = "https:" + u

    pu = urllib.parse.urlparse(u)
    headers = [
        f"User-Agent: {UA}",
    ]
    ref = pu.scheme + "://" + pu.netloc + "/" if pu.netloc else ""
    if ref:
        headers.append(f"Referer: {ref}")
        headers.append(f"Origin: {ref.rstrip('/')}")

    if pu.netloc and ("115.com" in pu.netloc or "115cdn" in pu.netloc or "cpats" in pu.netloc):
        try:
            ck = KPAN.get_active_pan_cookie("115")
            if ck: headers.append(f"Cookie: {ck}")
        except Exception: pass
    elif pu.netloc and "quark" in pu.netloc:
        try:
            ck = KPAN.get_active_pan_cookie("quark")
            if ck: headers.append(f"Cookie: {ck}")
        except Exception: pass

    header_str = "\r\n".join(headers) + "\r\n"
    return u, header_str


@app.route("/car_stream/duration", methods=["GET"])
def car_stream_duration():
    """快速获取视频真实时长，支持前端精确进度条与拖拽跳转"""
    raw_u = request.args.get("u", "").strip()
    if not raw_u:
        return jsonify({"ok": False, "error": "missing u"}), 400
    if raw_u in _car_duration_cache:
        return jsonify({"ok": True, "duration": _car_duration_cache[raw_u]})

    target, header_str = _resolve_stream_target(raw_u)
    dur = 0.0

    # 1. 优先尝试 ffprobe 极速获取
    ffprobe_bin = _find_bin("ffprobe")
    if shutil.which(ffprobe_bin) or (os.path.exists(ffprobe_bin) and os.access(ffprobe_bin, os.X_OK)):
        try:
            cmd = [ffprobe_bin, "-v", "error"]
            if target.startswith("http://") or target.startswith("https://"):
                cmd += ["-headers", header_str]
            cmd += [
                "-analyzeduration", "1000000", "-probesize", "1000000",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                target
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
            dur = float(res.stdout.strip())
        except Exception:
            dur = 0.0

    # 2. 若 ffprobe 不可用，无缝降级为 ffmpeg -i 探测
    if dur <= 0:
        ffmpeg_bin = _find_bin("ffmpeg")
        if not shutil.which(ffmpeg_bin) and not os.path.exists(ffmpeg_bin):
            ffmpeg_bin = _ensure_ffmpeg()
        if shutil.which(ffmpeg_bin) or (os.path.exists(ffmpeg_bin) and os.access(ffmpeg_bin, os.X_OK)):
            try:
                cmd = [ffmpeg_bin, "-nostdin", "-loglevel", "info"]
                if target.startswith("http://") or target.startswith("https://"):
                    cmd += ["-headers", header_str]
                cmd += ["-analyzeduration", "1000000", "-probesize", "1000000", "-i", target]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6)
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
                if m:
                    h, mins, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
                    dur = h * 3600 + mins * 60 + s
            except Exception:
                dur = 0.0

    if dur > 0:
        _car_duration_cache[raw_u] = dur
        return jsonify({"ok": True, "duration": dur})
    return jsonify({"ok": True, "duration": 0})


@app.route("/car_stream/audio", methods=["GET"])
def car_stream_audio():
    """实时音频推流：转出标准 MP3 流由 <audio> 标签原生播放，100% 规避车机行车视频黑屏拦截"""
    raw_u = request.args.get("u", "").strip()
    if not raw_u:
        return "missing u", 400
    try:
        ss = max(0.0, float(request.args.get("ss", 0)))
    except Exception:
        ss = 0.0

    ffmpeg_bin = _find_bin("ffmpeg")
    if not shutil.which(ffmpeg_bin) and not os.path.exists(ffmpeg_bin):
        ffmpeg_bin = _ensure_ffmpeg()
    if not shutil.which(ffmpeg_bin) and not os.path.exists(ffmpeg_bin):
        return jsonify({"ok": False, "error": "ffmpeg_not_found", "message": "服务端未安装 FFmpeg，请执行: pip install imageio-ffmpeg 或 apt install -y ffmpeg"}), 503

    target, header_str = _resolve_stream_target(raw_u)

    cmd = [ffmpeg_bin, "-nostdin", "-loglevel", "error"]
    if target.startswith("http://") or target.startswith("https://"):
        cmd += ["-headers", header_str]
    cmd += ["-analyzeduration", "1000000", "-probesize", "1000000"]
    if ss > 0:
        cmd += ["-ss", str(ss)]
    cmd += [
        "-i", target,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-f", "mp3",
        "pipe:1"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def gen():
        read_fn = getattr(proc.stdout, "read1", proc.stdout.read)
        try:
            while True:
                chunk = read_fn(16384)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.terminate()
                proc.kill()
            except Exception:
                pass

    return Response(gen(), content_type="audio/mpeg", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no"
    })


@app.route("/car_stream/video", methods=["GET"])
def car_stream_video():
    """实时视频帧流：抽取低码率 25FPS 极速 JPEG 帧，配合 12-byte PTS 时间戳包直推 Canvas，杜绝卡顿"""
    raw_u = request.args.get("u", "").strip()
    if not raw_u:
        return "missing u", 400
    try:
        ss = max(0.0, float(request.args.get("ss", 0)))
    except Exception:
        ss = 0.0

    ffmpeg_bin = _find_bin("ffmpeg")
    if not shutil.which(ffmpeg_bin) and not os.path.exists(ffmpeg_bin):
        ffmpeg_bin = _ensure_ffmpeg()
    if not shutil.which(ffmpeg_bin) and not os.path.exists(ffmpeg_bin):
        return jsonify({"ok": False, "error": "ffmpeg_not_found", "message": "服务端未安装 FFmpeg，请执行: pip install imageio-ffmpeg 或 apt install -y ffmpeg"}), 503

    q_mode = request.args.get("q", "720p").lower()
    if q_mode == "480p":
        scale = "scale=-2:480"
        q_val = "9"
    else:
        scale = "scale=-2:720"
        q_val = "8"

    target, header_str = _resolve_stream_target(raw_u)

    # 电影级 20 FPS 流畅节拍 + YUV420P 紧凑采样，网络带宽从 9Mbps 大降至 3~4Mbps，杜绝网络微卡顿
    target_fps = 20
    frame_interval = 1.0 / target_fps

    cmd = [ffmpeg_bin, "-nostdin", "-loglevel", "error"]
    if target.startswith("http://") or target.startswith("https://"):
        cmd += ["-headers", header_str]
    cmd += ["-analyzeduration", "1000000", "-probesize", "1000000"]
    if ss > 0:
        cmd += ["-ss", str(ss)]
    cmd += [
        "-i", target,
        "-an",
        "-vf", scale,
        "-r", str(target_fps),
        "-pix_fmt", "yuvj420p",
        "-q:v", q_val,
        "-threads", "2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def gen():
        buf = bytearray()
        frame_idx = 0
        read_fn = getattr(proc.stdout, "read1", proc.stdout.read)
        try:
            while True:
                chunk = read_fn(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        if len(buf) > 1:
                            del buf[:-1]
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        if soi > 0:
                            del buf[:soi]
                        break
                    frame_data = buf[soi:eoi+2]
                    del buf[:eoi+2]

                    pts = ss + (frame_idx * frame_interval)
                    frame_idx += 1

                    # 12-byte header: Magic 'JGFR' (4B) + PTS float32 (4B) + Length uint32 (4B)
                    header = b"JGFR" + struct.pack(">fI", pts, len(frame_data))
                    yield header + frame_data
        finally:
            try:
                proc.terminate()
                proc.kill()
            except Exception:
                pass

    return Response(gen(), content_type="application/octet-stream", headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no"
    })


# ===== 扩展接口 v2 =====
KIND_MAP = {"tv": "series", "teleplay": "series", "drama": "series",
            "film": "movie", "moving": "movie"}
VALID_KINDS = ("movie", "series", "anime", "variety", "short_drama")


def _norm_kind(k):
    k = (k or "all").strip()
    return KIND_MAP.get(k, k)


def _cards_of(d):
    out = []
    for c in (d.get("cards") or d.get("items") or d.get("results") or []):
        if not isinstance(c, dict):
            continue
        out.append({"id": c.get("id") or c.get("catalog_id"),
                    "title": c.get("title"),
                    "year": c.get("year"),
                    "area": c.get("area"),
                    "genres": (c.get("genres") or [])[:3],
                    "poster": c.get("poster_url") or c.get("poster"),
                    "backdrop": c.get("backdrop_url"),
                    "kind": c.get("content_kind") or c.get("kind"),
                    "remarks": c.get("remarks"),
                    "updated_at": c.get("updated_at") or c.get("published_at"),
                    "episode_count": c.get("episode_count") or c.get("episodes_count"),
                    "rating": c.get("rating") or c.get("score"),
                    "heat": c.get("heat_value"),
                    "plays": c.get("playback_count")})
    return out


@app.route("/api/browse")
def api_browse():
    """完整浏览：kind + genre/area/language/year/status/orientation + sort + window + page"""
    kind = _norm_kind(request.args.get("kind", "all"))
    page = request.args.get("page", "1")
    sort = request.args.get("sort", "") or "heat_desc"
    window = request.args.get("window", "")
    qs = ([] if kind == "all" else ["kind=" + urllib.parse.quote(kind)])
    qs += ["page=" + urllib.parse.quote(page),
           "limit=" + urllib.parse.quote(request.args.get("limit", "24")), "scope=public"]
    for p in ("genre", "area", "language", "status", "orientation",
              "year", "year_gte", "year_lte"):
        v = request.args.get(p, "")
        if v:
            qs.append(p + "=" + urllib.parse.quote(v))
    if sort:
        qs.append("sort=" + urllib.parse.quote(sort))
    if window:
        qs.append("window=" + urllib.parse.quote(window))
    path = "/v1/browse/catalog?" + "&".join(qs)
    d, err = api_get(path, cache_key="brw_" + hashlib.md5(path.encode()).hexdigest(), ttl=300)
    if err:
        # 带筛选失败时退回不带筛选，保证页面不空
        qs2 = ([] if kind == "all" else ["kind=" + urllib.parse.quote(kind)])
        qs2 += ["page=" + urllib.parse.quote(page), "limit=24", "scope=public", "sort=heat_desc"]
        d, err = api_get("/v1/browse/catalog?" + "&".join(qs2),
                         cache_key="brw_" + hashlib.md5("&".join(qs2).encode()).hexdigest(), ttl=300)
    if err:
        return jsonify({"ok": False, "error": err, "items": [], "kind": kind, "page": page}), 502
    items = _cards_of(d)
    return jsonify({"ok": True, "kind": kind, "page": page, "items": items,
                    "total": d.get("total"), "has_more": bool(d.get("has_more"))})


@app.route("/api/catalog_ui")
def api_catalog_ui():
    """原站 bootstrap.catalog_ui：7 个频道 + 5 套筛选契约（分类/地区/语言/年份/状态/形式/排序）"""
    b, err = api_get("/v1/runtime/bootstrap",
                     cache_key="bootstrap_ui", ttl=3600)
    if err or not b:
        cf = os.path.join(CACHE_DIR, "bootstrap.json")
        if os.path.exists(cf):
            try:
                b = json.load(open(cf, encoding="utf-8"))
            except Exception:
                b = None
    if not b:
        return jsonify({"ok": False, "error": err or "无 bootstrap", "channels": [], "catalogs": []}), 502
    ui = b.get("catalog_ui") or {}
    cat = {}
    for c in (ui.get("catalogs") or []):
        if isinstance(c, dict) and c.get("kind"):
            cat[c["kind"]] = c
    # 把 disabled 的选项标出来（原站 status/orientation 就是 disabled）
    return jsonify({"ok": True, "version": ui.get("version"),
                    "channels": ui.get("channels") or [], "catalogs": cat})


@app.route("/api/calendar")
def api_calendar():
    """更新日历：按 updated_at / 年份归类最近更新的片"""
    d, err = api_get("/v1/browse/catalog?page=1&limit=60&scope=public&sort=updated_desc",
                     cache_key="cal_upd", ttl=600)
    if err:
        return jsonify({"ok": False, "error": err, "days": []}), 502
    items = _cards_of(d)
    days = {}
    for it in items:
        up = it.get("remarks") or ""
        days.setdefault(up[:10] if up else "近期", []).append(it)
    out = [{"day": k, "items": v} for k, v in days.items()]
    return jsonify({"ok": True, "days": out, "total": len(items)})


@app.route("/api/rank")
def api_rank():
    hot = request.args.get("hot_list_key") or request.args.get("key") or "hot"
    path = "/v1/browse/catalog?hot_list_key=" + urllib.parse.quote(hot) + \
           "&page=1&limit=30&scope=public&sort=" + urllib.parse.quote(request.args.get("sort", "hot"))
    d, err = api_get(path, cache_key="rank_" + hot, ttl=600)
    if err:
        path = "/v1/browse/catalog?page=1&limit=30&scope=public"
        d, err = api_get(path, cache_key="rank_default", ttl=600)
    if err:
        return jsonify({"ok": False, "error": err, "items": []}), 502
    items = _cards_of(d)
    for i, it in enumerate(items):
        it["rank"] = i + 1
    return jsonify({"ok": True, "hot_list_key": hot, "items": items})


@app.route("/api/hotwords")
def api_hotwords():
    """热词联想：KANJU_HOTWORDS_V2 —— 上游 /v1/suggest 的 suggestions[].label / prompt"""
    words = []
    for probe in ("\u5ac1", "\u5c0f", "\u73b0", "\u57ce"):
        d2, e2 = api_get("/v1/suggest?query=" + urllib.parse.quote(probe) + "&limit=12&scope=public",
                         cache_key="hw2_" + probe, ttl=1800)
        if e2:
            continue
        for it in (d2.get("suggestions") or d2.get("items") or d2.get("results") or []):
            if isinstance(it, dict):
                t = it.get("label") or it.get("prompt") or it.get("title")
            else:
                t = str(it)
            if t and 1 < len(t) < 24:
                words.append(t)
        if len(words) >= 12:
            break
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return jsonify({"ok": True, "words": out[:16]})


@app.route("/api/suggest")
def api_suggest():
    """KANJU_SUGGEST_FIX: query= 参数，title 取 label，id 取 target.variant_id"""
    q = (request.args.get("query") or request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": True, "items": [], "titles": []})
    d, err = api_get("/v1/suggest?query=" + urllib.parse.quote(q) + "&limit=12&scope=public",
                     cache_key="sg2_" + hashlib.md5(q.encode()).hexdigest(), ttl=900)
    if err:
        return jsonify({"ok": False, "error": err, "items": [], "titles": []})
    raw = d.get("items") or d.get("results") or d.get("suggestions") or []
    items, titles = [], []
    for it in raw:
        if isinstance(it, dict):
            tgt = it.get("target") or {}
            title = it.get("label") or it.get("title") or it.get("prompt")
            items.append({"id": tgt.get("variant_id") or it.get("catalog_id") or it.get("id"),
                          "cid": tgt.get("variant_id"),
                          "title": title, "label": title,
                          "subtitle": it.get("subtitle"),
                          "playable": tgt.get("direct_playable")})
            if title:
                titles.append(title)
        elif isinstance(it, str):
            titles.append(it)
    return jsonify({"ok": True, "q": q, "items": items, "titles": titles})


# ================= JUCHANG8_MEMBER_ROUTES =================
# 剧场8 (juchang8.com) 会员系统：注册 / 登录 / 登出 / 我 / 收藏 / 历史 / 18+ 偏好 / 午夜剧场 / 全站管理
import juchang8_member as KM
import juchang8_plus as KP
import juchang8_collector as KC
import juchang8_pan as KPAN
import juchang8_dian115 as KD115
import juchang8_quark as KQ
KP.init_plus()
KC.init_collector_db()
KPAN.init_pan_db()
KQ.init_quark_hub_db()

COOKIE = "juchang8_sid"


def _sid():
    return request.cookies.get("juchang8_sid") or request.cookies.get("ai_movie_session") or request.cookies.get("kj_sid") or request.cookies.get(COOKIE) or (request.headers.get("X-Juchang8-Sid") or request.headers.get("X-KJ-Sid") or "")


def _me():
    return KM.user_by_session(_sid())


def _need_login():
    return jsonify({"ok": False, "error": "请先登录", "code": "login_required"}), 401


def _set_sid(resp, sid, days=30):
    resp.set_cookie(COOKIE, sid, max_age=days * 86400, httponly=True, samesite="Lax")
    resp.set_cookie("juchang8_sid", sid, max_age=days * 86400, httponly=True, samesite="Lax")
    resp.set_cookie("ai_movie_session", sid, max_age=days * 86400, httponly=True, samesite="Lax")
    resp.set_cookie("kj_sid", sid, max_age=days * 86400, httponly=True, samesite="Lax")
    return resp


@app.route("/api/auth/register", methods=["POST"])
def api_register():
    d = request.get_json(silent=True) or request.form
    acc = (d.get("account") or d.get("username") or "").strip()
    pwd = d.get("password") or ""
    inv = (d.get("invite_code") or d.get("invite") or "").strip()
    nick = (d.get("nickname") or "").strip()
    if len(acc) < 4:
        return jsonify({"ok": False, "error": "账号至少 4 位"}), 400
    if len(pwd) < 6:
        return jsonify({"ok": False, "error": "密码至少 6 位"}), 400
    phone = (d.get("phone") or "").strip()
    if phone:
        if not KP.sms_verify(phone, (d.get("sms_code") or "").strip()):
            return jsonify({"ok": False, "error": "短信验证码不正确或已过期"}), 400
    inv_create = inv
    if inv and inv.upper() not in KM.INVITE_CODES and KP.invite_code_owner(inv):
        inv_create = "KANJU2026"  # 个人邀请码有效：先用系统码建号，再绑定邀请关系
    u, err = KM.create_user(acc, pwd, invite_code=inv_create, nickname=nick)
    if err or not u:
        return jsonify({"ok": False, "error": err or "注册失败"}), 400
    if phone:
        KP.set_phone(u["id"], phone)
    if inv:
        KP.invite_bind(u["id"], inv)
    sid = KM.new_session(u["id"])
    u = KM.user_by_session(sid) or u
    return _set_sid(jsonify({"ok": True, "user": KM.public_user(u), "sid": sid}), sid)


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or request.form
    acc = (d.get("account") or d.get("username") or "").strip()
    pwd = d.get("password") or ""
    u, err = KM.verify_user(acc, pwd)
    if err or not u:
        return jsonify({"ok": False, "error": err or "账号或密码错误"}), 401
    sid = KM.new_session(u["id"])
    return _set_sid(jsonify({"ok": True, "user": KM.public_user(u), "sid": sid}), sid)


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    KM.drop_session(_sid())
    r = jsonify({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.route("/api/auth/me")
def api_me():
    u = _me()
    if not u:
        return jsonify({"ok": True, "user": None, "logged_in": False})
    return jsonify({"ok": True, "user": KM.public_user(u), "logged_in": True})


@app.route("/api/auth/adult", methods=["POST"])
def api_adult():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or request.form
    on = d.get("adult_confirmed")
    on = True if on is None else bool(on)
    KM.set_adult(u["id"], on)
    KM.midnight_log(u["id"], "adult_confirm" if on else "adult_revoke")
    u2 = _me()
    return jsonify({"ok": True, "user": KM.public_user(u2)})


# ---------------- 收藏 ----------------
@app.route("/api/fav", methods=["GET", "POST", "DELETE"])
def api_fav():
    u = _me()
    if not u:
        return _need_login()
    if request.method == "GET":
        return jsonify({"ok": True, "items": KM.fav_list(u["id"])})
    d = request.get_json(silent=True) or request.form
    cid = str(d.get("cid") or d.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "缺 cid"}), 400
    if request.method == "POST":
        KM.fav_add(u["id"], cid, d.get("title") or "", d.get("poster") or "")
        return jsonify({"ok": True, "has": True})
    KM.fav_del(u["id"], cid)
    return jsonify({"ok": True, "has": False})


# ---------------- 历史 ----------------
@app.route("/api/history", methods=["GET", "POST", "DELETE"])
def api_history():
    u = _me()
    if not u:
        return _need_login()
    if request.method == "GET":
        return jsonify({"ok": True, "items": KM.hist_list(u["id"])})
    if request.method == "DELETE":
        d = request.get_json(silent=True) or {}
        KM.hist_del(u["id"], d.get("cid"))
        return jsonify({"ok": True})
    d = request.get_json(silent=True) or request.form
    cid = str(d.get("cid") or d.get("id") or "").strip()
    if not cid:
        return jsonify({"ok": False, "error": "缺 cid"}), 400
    KM.hist_put(u["id"], cid, d.get("title") or "", d.get("poster") or "",
                d.get("episode") or "", d.get("position") or 0, d.get("duration") or 0)
    return jsonify({"ok": True})


# ---------------- 18+ 午夜剧场 ----------------
MIDNIGHT_TAGS = ("午夜", "剧场", "18+", "成人", "情色", "深夜", "限制级")


@app.route("/midnight-theater")
def midnight_theater():
    if request.args.get("legacy") == "1":
        return send_from_directory(os.path.join(HERE, "static"), "midnight.html")
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/api/midnight/state")
def api_midnight_state():
    """照原站：游客 locked，登录但未确认年龄 locked_need_adult，两者都过才 unlocked"""
    u = _me()
    if not u:
        return jsonify({"ok": True, "state": "locked", "reason": "midnight_session_required",
                        "message": "请登录后进入午夜剧场", "logged_in": False})
    if not u.get("adult_confirmed"):
        return jsonify({"ok": True, "state": "locked", "reason": "adult_confirm_required",
                        "message": "请先确认您已满 18 岁", "logged_in": True,
                        "user": KM.public_user(u)})
    return jsonify({"ok": True, "state": "unlocked", "logged_in": True,
                    "user": KM.public_user(u)})


@app.route("/api/midnight/feed")
def api_midnight_feed():
    u = _me()
    if not u:
        return jsonify({"ok": False, "error": "请登录后进入午夜剧场",
                        "code": "midnight_session_required"}), 401
    if not u.get("adult_confirmed"):
        return jsonify({"ok": False, "error": "请先确认您已满 18 岁",
                        "code": "adult_confirm_required"}), 403
    KM.midnight_log(u["id"], "enter")
    items, seen = [], set()
    for kw in ("午夜剧场", "深夜剧场", "午夜", "深夜", "伦理", "情", "欲"):
        d, err = api_get("/v1/search?query=" + urllib.parse.quote(kw) + "&limit=24&scope=public",
                         cache_key="mn_" + kw, ttl=1800)
        if err:
            continue
        cards = _cards_of(d)
        for c in cards:
            cid = c.get("catalog_id") or c.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                items.append(c)
    return jsonify({"ok": True, "state": "unlocked", "items": items[:36],
                    "count": len(items[:36])})



# ---------------- 扩展系统（kanju_plus，对照 kanju45.com 补齐） ----------------

@app.route("/api/auth/sms/send", methods=["POST"])
def api_sms_send():
    d = request.get_json(silent=True) or request.form
    phone = (d.get("phone") or "").strip()
    if len(phone) < 6:
        return jsonify({"ok": False, "error": "手机号不正确"}), 400
    code = KP.sms_send(phone)
    out = {"ok": True, "expires_in": 300}
    if os.environ.get("KANJU_SMS_DEV", "1") != "0":
        out["dev_code"] = code  # 无短信服务商时的本地调试模式，上线接服务商后关掉
    return jsonify(out)


@app.route("/api/quota")
def api_quota():
    u = _me()
    if not u:
        return _need_login()
    return jsonify({"ok": True, "quota": KP.quota_view(u["id"])})


@app.route("/api/quota/consume", methods=["POST"])
def api_quota_consume():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or {}
    r, err = KP.quota_consume(u["id"], bool(d.get("is4k")))
    if err:
        return jsonify({"ok": False, "error": err}), 402
    return jsonify({"ok": True, **r})


@app.route("/api/invite/me")
def api_invite_me():
    u = _me()
    if not u:
        return _need_login()
    return jsonify({"ok": True, "invite": KP.invite_progress(u["id"])})


@app.route("/api/playlists/mine")
def api_pl_mine():
    u = _me()
    if not u:
        return _need_login()
    return jsonify({"ok": True, "items": KP.playlist_mine(u["id"])})


@app.route("/api/playlists/explore")
def api_pl_explore():
    return jsonify({"ok": True, "items": KP.playlist_explore()})


@app.route("/api/playlists", methods=["POST"])
def api_pl_create():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "片单名不能为空"}), 400
    pid, code = KP.playlist_create(u["id"], name, d.get("descr") or "",
                                   d.get("is_public", True), d.get("password") or "")
    return jsonify({"ok": True, "id": pid, "share_code": code})


@app.route("/api/playlists/<int:pid>")
def api_pl_get(pid):
    d, err = KP.playlist_get(pid, request.args.get("pwd") or "")
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "playlist": d})


@app.route("/api/playlists/<int:pid>/items", methods=["POST", "DELETE"])
def api_pl_items(pid):
    u = _me()
    if not u:
        return _need_login()
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        ok, err = KP.playlist_add_item(pid, u["id"], d.get("cid") or "",
                                       d.get("title") or "", d.get("poster") or "")
    else:
        ok, err = KP.playlist_del_item(pid, u["id"], request.args.get("cid") or "")
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    return jsonify({"ok": True})


@app.route("/api/playlists/<int:pid>", methods=["DELETE"])
def api_pl_delete(pid):
    u = _me()
    if not u:
        return _need_login()
    ok, err = KP.playlist_delete(pid, u["id"])
    if not ok:
        return jsonify({"ok": False, "error": err}), 403
    return jsonify({"ok": True})


@app.route("/api/comments")
def api_comments():
    cid = request.args.get("cid") or ""
    return jsonify({"ok": True, "items": KP.comments_list(cid)})


@app.route("/api/comments", methods=["POST"])
def api_comment_add():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or {}
    body = (d.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "评论不能为空"}), 400
    i = KP.comment_add(d.get("cid") or "", u["id"], u.get("nickname") or u.get("account"), body[:500])
    return jsonify({"ok": True, "id": i})


@app.route("/api/comments/<int:cid_>", methods=["DELETE"])
def api_comment_del(cid_):
    u = _me()
    if not u:
        return _need_login()
    if not KP.comment_del(cid_, u["id"]):
        return jsonify({"ok": False, "error": "只能删自己的评论"}), 403
    return jsonify({"ok": True})


@app.route("/api/announcements")
@app.route("/announcements")
def api_announcements():
    items = KP.announcements_list(include_inactive=False)
    return jsonify({"ok": True, "items": items})


@app.route("/api/sidebar")
def api_sidebar_public():
    items = KP.sidebar_list(include_inactive=False)
    grouped = {}
    for it in items:
        grouped.setdefault(it.get("section", "nav"), []).append(it)
    return jsonify({"ok": True, "items": items, "grouped": grouped})


@app.route("/api/navigation")
def api_navigation_public():
    items = KP.sidebar_list(include_inactive=False, section="nav")
    return jsonify({"ok": True, "items": items})


@app.route("/api/site/config")
def api_site_config_public():
    cfg = KP.settings_get_all()
    public_keys = {
        "site_name", "site_domain", "site_slogan", "domain_bar_enabled",
        "domain_bar_text", "register_open", "require_invite", "daily_grant",
        "play_cost", "play_cost_4k", "invite_reward", "default_player_mode",
        "custom_footer_html"
    }
    pub = {k: v for k, v in cfg.items() if k in public_keys}
    return jsonify({"ok": True, "config": pub})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    u = _me()
    d = request.get_json(silent=True) or {}
    body = (d.get("body") or "").strip()
    if not body:
        return jsonify({"ok": False, "error": "反馈内容不能为空"}), 400
    KP.feedback_add(u["id"] if u else None, d.get("contact") or "", body[:1000])
    return jsonify({"ok": True})


@app.route("/api/share", methods=["POST"])
def api_share_create():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or {}
    code = KP.share_create(u["id"], d.get("kind") or "catalog", d.get("target") or "")
    return jsonify({"ok": True, "code": code, "url": "/s/" + code})


@app.route("/api/share/<code>")
def api_share_resolve(code):
    r = KP.share_resolve(code)
    if not r:
        return jsonify({"ok": False, "error": "分享不存在"}), 404
    return jsonify({"ok": True, **r})


@app.route("/s/<code>")
def share_landing(code):
    return Response("<script>location.replace('/?share=" + code + "')</script>",
                    mimetype="text/html")


@app.route("/api/collections")
def api_collections():
    defs = [
        {"key": "kr", "label": "韩剧专区", "params": {"area": "韩国"}},
        {"key": "jp", "label": "日剧专区", "params": {"area": "日本"}},
        {"key": "action", "label": "动作爽片", "params": {"genre": "动作"}},
        {"key": "love", "label": "爱情精选", "params": {"genre": "爱情"}},
        {"key": "suspense", "label": "悬疑烧脑", "params": {"genre": "悬疑"}},
        {"key": "comedy", "label": "喜剧放映厅", "params": {"genre": "喜剧"}},
        {"key": "costume", "label": "古装大剧", "params": {"genre": "古装"}},
        {"key": "animehot", "label": "热门动漫", "params": {"kind": "anime"}},
    ]
    out = []
    for dfn in defs:
        p = dfn["params"]
        qs = ["page=1", "limit=1", "scope=public", "sort=heat_desc"]
        if p.get("kind"):
            qs.append("kind=" + urllib.parse.quote(p["kind"]))
        for k in ("genre", "area"):
            if p.get(k):
                qs.append(k + "=" + urllib.parse.quote(p[k]))
        d, err = api_get("/v1/browse/catalog?" + "&".join(qs),
                         cache_key="col_" + dfn["key"], ttl=3600)
        cover = None
        total = None
        if not err and d:
            items = _cards_of(d)
            if items:
                cover = items[0].get("poster") or items[0].get("backdrop")
            total = d.get("total")
        out.append({**dfn, "cover": cover, "total": total})
    return jsonify({"ok": True, "items": out})


@app.route("/api/shortdrama")
def api_shortdrama():
    page = request.args.get("page", "1")
    path = ("/v1/browse/catalog?kind=short_drama&page=" + urllib.parse.quote(page)
            + "&limit=24&scope=public&sort=" + urllib.parse.quote(request.args.get("sort", "heat_desc")))
    d, err = api_get(path, cache_key="sd_" + hashlib.md5(path.encode()).hexdigest(), ttl=300)
    if err:
        return jsonify({"ok": False, "error": err, "items": []}), 502
    return jsonify({"ok": True, "items": _cards_of(d), "has_more": bool(d.get("has_more"))})


@app.route("/api/home/rails")
def api_home_rails():
    def rail(params, key):
        qs = ["page=1", "limit=12", "scope=public", "sort=heat_desc"]
        for k, v in params.items():
            qs.append(k + "=" + urllib.parse.quote(str(v)))
        d, err = api_get("/v1/browse/catalog?" + "&".join(qs), cache_key="rail_" + key, ttl=600)
        return [] if err else _cards_of(d)
    return jsonify({"ok": True,
                    "kr": rail({"area": "韩国"}, "kr"),
                    "jp": rail({"area": "日本"}, "jp"),
                    "short": rail({"kind": "short_drama"}, "short")})


@app.route("/wasm/<path:filename>")
def serve_wasm(filename):
    wasm_dir = os.path.join(HERE, "static", "wasm")
    file_path = os.path.join(wasm_dir, filename)
    if os.path.exists(file_path):
        resp = send_from_directory(wasm_dir, filename)
        resp.headers["Content-Type"] = "application/wasm"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    try:
        import urllib.request
        remote_url = f"https://tesla-player.com/wasm/{filename}"
        req = urllib.request.Request(remote_url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://tesla-player.com/"
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            data = r.read()
            with open(file_path, "wb") as f:
                f.write(data)
            resp = Response(data, mimetype="application/wasm")
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 404


@app.route("/car/player_frame")
def car_player_frame():
    resp = send_from_directory(os.path.join(HERE, "static"), "car_player_frame.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/car")
@app.route("/car/player")
@app.route("/car/player/<path:p>")
@app.route("/tesla")
def car_page(p=None):
    if request.args.get("legacy") == "1":
        return send_from_directory(os.path.join(HERE, "static"), "tesla.html")
    resp = send_from_directory(os.path.join(HERE, "static"), "car.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/p2p/config")
def api_p2p_config():
    return jsonify({"ok": True, "enabled": True,
                    "trackers": ["wss://tracker.novage.com.ua",
                                 "wss://tracker.openwebtorrent.com",
                                 "wss://tracker.webtorrent.dev",
                                 "wss://tracker.btorrent.xyz"]})


@app.route("/download")
def download_page():
    return send_from_directory(os.path.join(HERE, "static"), "download.html")


@app.route("/api/cast/pair", methods=["POST"])
def api_cast_pair():
    u = _me()
    if not u:
        return _need_login()
    return jsonify({"ok": True, **KP.cast_pair_create(u["id"])})


@app.route("/api/cast/confirm", methods=["POST"])
def api_cast_confirm():
    d = request.get_json(silent=True) or {}
    r = KP.cast_confirm((d.get("code") or "").strip(), d.get("device_name") or "TV")
    if not r:
        return jsonify({"ok": False, "error": "配对码不正确或已过期"}), 400
    return jsonify({"ok": True, "device": r})


@app.route("/api/cast/devices")
def api_cast_devices():
    u = _me()
    if not u:
        return _need_login()
    return jsonify({"ok": True, "items": KP.cast_devices(u["id"])})



# ---------------- 午夜剧场 · 上游桥接（对照原站 /v1/midnight-theater/* 全套） ----------------
# 上游午夜池是账号绑定的：运营者把自己的 kanju 账号会话放在 kanju_upstream.json
# （{"cookie": "...", "authorization": "...", "base": "..."} 任选其一/组合），
# 或环境变量 KANJU_UPSTREAM_COOKIE / KANJU_UPSTREAM_AUTHORIZATION。文件不进代码仓库。
import requests as _rq
UPSTREAM_FILE = os.path.join(HERE, "juchang8_upstream.json") if (
    os.path.exists(os.path.join(HERE, "juchang8_upstream.json")) or not os.path.exists(os.path.join(HERE, "kanju_upstream.json"))
) else os.path.join(HERE, "kanju_upstream.json")
_up_sess = _rq.Session()
_up_sess.headers.update({"User-Agent": UA})
_up_state = {"loaded": False, "proof": None}


def _upstream_conf():
    conf = {}
    if os.path.exists(UPSTREAM_FILE):
        try:
            conf = json.load(open(UPSTREAM_FILE, encoding="utf-8")) or {}
        except Exception:
            conf = {}
    if os.environ.get("JUCHANG8_UPSTREAM_COOKIE") or os.environ.get("KANJU_UPSTREAM_COOKIE"):
        conf["cookie"] = os.environ.get("JUCHANG8_UPSTREAM_COOKIE") or os.environ.get("KANJU_UPSTREAM_COOKIE")
    if os.environ.get("JUCHANG8_UPSTREAM_AUTHORIZATION") or os.environ.get("KANJU_UPSTREAM_AUTHORIZATION"):
        conf["authorization"] = os.environ.get("JUCHANG8_UPSTREAM_AUTHORIZATION") or os.environ.get("KANJU_UPSTREAM_AUTHORIZATION")
    return conf


def upstream_bound():
    c = _upstream_conf()
    return bool(c.get("cookie") or c.get("authorization"))


def _up_headers(method, path, extra=None):
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    h = {"Accept": "application/json",
         "x-ai-movie-timestamp": ts, "x-ai-movie-nonce": nonce,
         "x-ai-movie-signature": sign(method, path, ts, nonce),
         "x-ai-movie-client-name": "web"}
    conf = _upstream_conf()
    base = conf.get("base") or pick_base()
    h["Origin"] = base
    h["Referer"] = base + "/"
    if extra:
        for k, v in extra.items():
            if k.lower() not in ("cookie", "authorization", "host"):
                h[k] = v
    # 严格使用上游运营者合法 Cookie / Authorization，严禁被前端本地 Cookie 覆盖
    if conf.get("cookie"):
        h["Cookie"] = conf["cookie"]
    if conf.get("authorization"):
        h["Authorization"] = conf["authorization"]
    if _up_state.get("proof"):
        h["x-ai-movie-midnight-access"] = _up_state["proof"]
    return base, h


def up_req(method, path, body=None, extra_headers=None, timeout=20):
    """带签名（+运营者上游账号会话，如已绑定）请求上游，返回 (status, json/text)。
    具备多基准节点秒级容灾故障转移：若主节点超时或报 502/503，自动平滑重试备用节点。
    """
    conf = _upstream_conf()
    primary_base = conf.get("base") or pick_base() or BASES[0]
    candidate_bases = [primary_base] + [b for b in BASES if b != primary_base]

    last_status = 502
    last_d = {"ok": False, "error": "上游不可达"}

    for idx, base in enumerate(candidate_bases):
        if timeout <= 5 and idx > 0:
            break
        base, h = _up_headers(method, path, extra=extra_headers)
        h["Origin"] = base
        h["Referer"] = base + "/"
        url = base + path
        try:
            cur_timeout = min(timeout, 10) if method == "GET" else timeout
            if method == "GET":
                r = _up_sess.get(url, headers=h, timeout=cur_timeout)
            elif method == "DELETE":
                r = _up_sess.delete(url, headers=h, timeout=cur_timeout)
            elif method == "PUT":
                r = _up_sess.put(url, json=body if isinstance(body, (dict, list)) else None,
                                 data=body if isinstance(body, (str, bytes)) else None,
                                 headers=h, timeout=cur_timeout)
            elif method == "PATCH":
                r = _up_sess.patch(url, json=body if isinstance(body, (dict, list)) else None,
                                  data=body if isinstance(body, (str, bytes)) else None,
                                  headers=h, timeout=cur_timeout)
            else:
                r = _up_sess.post(url, json=body if isinstance(body, (dict, list)) else None,
                                  data=body if isinstance(body, (str, bytes)) else None,
                                  headers=h, timeout=cur_timeout)

            try:
                d = r.json()
            except Exception:
                d = {"raw": r.text[:500]}

            # 若遇到 502/503/504 错误，尝试切换下一个可用基准节点
            if r.status_code >= 500 and len(candidate_bases) > 1:
                last_status = r.status_code
                last_d = d
                continue

            if isinstance(d, dict):
                for _k in ("proof", "access_proof", "midnight_access_proof"):
                    if d.get(_k):
                        _up_state["proof"] = d[_k]
                        break

            return r.status_code, d
        except Exception as e:
            last_status = 502
            last_d = {"ok": False, "error": f"请求失败 {e}"}
            continue

    return last_status, last_d


def _mn_heal():
    """补走原站进入午夜的两个前置动作（幂等）：成年确认 + 公告确认。"""
    try:
        up_req("PUT", "/v1/users/me/preferences", {"adult_confirmed": True}, timeout=15)
    except Exception:
        pass
    try:
        up_req("POST", "/v1/midnight-theater/eligibility/acknowledge", {}, timeout=15)
    except Exception:
        pass


def mn_call(method, path, body=None, timeout=30):
    """午夜上游调用：被 401/403 拦时先自愈（成年/公告确认）再重试一次。"""
    st, r = up_req(method, path, body, timeout=timeout)
    if st in (401, 403) and upstream_bound():
        _mn_heal()
        st, r = up_req(method, path, body, timeout=timeout)
    return st, r


def _mn_gate():
    u = _me()
    if not u:
        return None, _need_login()
    if not u.get("adult_confirmed"):
        return None, (jsonify({"ok": False, "code": "adult_confirm_required",
                                "error": "请先确认您已满 18 岁"}), 403)
    return u, None


@app.route("/api/mn/status")
def api_mn_status():
    return jsonify({"ok": True, "upstream_bound": upstream_bound(),
                    "has_proof": bool(_up_state.get("proof"))})


@app.route("/api/mn/guest/evaluate", methods=["POST"])
def api_mn_guest():
    d = request.get_json(silent=True) or {}
    body = {"schema_version": 1,
            "state_revision": d.get("state_revision") or "init",
            "adult_history_count": int(d.get("adult_history_count") or 0),
            "adult_favorite_count": int(d.get("adult_favorite_count") or 0),
            "adult_distinct_count": int(d.get("adult_distinct_count") or 0)}
    st, r = up_req("POST", "/v1/midnight-theater/guest/evaluate", body, timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r}), (200 if st < 300 else 502)


@app.route("/api/mn/access")
def api_mn_access():
    u, err = _mn_gate()
    if err:
        return err
    st, r = up_req("GET", "/v1/midnight-theater/access", timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/eligibility")
def api_mn_eligibility():
    u, err = _mn_gate()
    if err:
        return err
    st, r = up_req("GET", "/v1/midnight-theater/eligibility", timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/acknowledge", methods=["POST"])
def api_mn_ack():
    u, err = _mn_gate()
    if err:
        return err
    st, r = up_req("POST", "/v1/midnight-theater/eligibility/acknowledge", {}, timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/adult", methods=["POST"])
def api_mn_adult():
    u = _me()
    if not u:
        return _need_login()
    KM.set_adult(u["id"], True)
    st, r = up_req("PUT", "/v1/users/me/preferences", {"adult_confirmed": True}, timeout=15)
    return jsonify({"ok": True, "upstream_status": st, "result": r})


@app.route("/api/mn/discovery")
def api_mn_discovery():
    u, err = _mn_gate()
    if err:
        return err
    qs = []
    for k in ("history_limit", "watching_limit", "history_cursor", "watching_cursor"):
        if request.args.get(k):
            qs.append(k + "=" + urllib.parse.quote(request.args[k]))
    st, r = mn_call("GET", "/v1/midnight-theater/discovery" + ("?" + "&".join(qs) if qs else ""), timeout=20)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/play/top", methods=["POST"])
def api_mn_play_top():
    u, err = _mn_gate()
    if err:
        return err
    d = request.get_json(silent=True) or {}
    st, r = mn_call("POST", "/v1/midnight-theater/top/play",
                   {"play_request_id": d.get("play_request_id") or secrets.token_urlsafe(10)}, timeout=20)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/play/random", methods=["POST"])
def api_mn_play_random():
    u, err = _mn_gate()
    if err:
        return err
    d = request.get_json(silent=True) or {}
    st, r = mn_call("POST", "/v1/midnight-theater/playlists/random/play",
                   {"play_request_id": d.get("play_request_id") or secrets.token_urlsafe(10)}, timeout=20)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/play/similar", methods=["POST"])
def api_mn_play_similar():
    u, err = _mn_gate()
    if err:
        return err
    d = request.get_json(silent=True) or {}
    body = {"play_request_id": d.get("play_request_id") or secrets.token_urlsafe(10)}
    for k in ("similar_source", "similar_seed", "generation_id"):
        if d.get(k):
            body[k] = d[k]
    st, r = mn_call("POST", "/v1/midnight-theater/playlists/similar/play", body, timeout=20)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/play/playlist", methods=["POST"])
def api_mn_play_playlist():
    u, err = _mn_gate()
    if err:
        return err
    d = request.get_json(silent=True) or {}
    pid = d.get("playlist_id") or ""
    body = {"play_request_id": d.get("play_request_id") or secrets.token_urlsafe(10)}
    if d.get("shared_playlist"):
        body["shared_playlist"] = d["shared_playlist"]
    st, r = mn_call("POST", "/v1/midnight-theater/playlists/" + urllib.parse.quote(str(pid)) + "/play",
                   body, timeout=20)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/generate", methods=["POST"])
def api_mn_generate():
    u, err = _mn_gate()
    if err:
        return err
    d = request.get_json(silent=True) or {}
    st, r = mn_call("POST", "/v1/midnight-theater/playlists", d, timeout=60)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/generation/<gid>")
def api_mn_generation(gid):
    u, err = _mn_gate()
    if err:
        return err
    st, r = mn_call("GET", "/v1/midnight-theater/generations/" + urllib.parse.quote(gid), timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


@app.route("/api/mn/generation/<gid>/cancel", methods=["POST"])
def api_mn_generation_cancel(gid):
    u, err = _mn_gate()
    if err:
        return err
    st, r = mn_call("POST", "/v1/midnight-theater/generations/" + urllib.parse.quote(gid) + "/cancel",
                   {}, timeout=15)
    return jsonify({"ok": st < 300, "upstream_status": st, "result": r})


# ---------------- 片头/片尾标记 ----------------

@app.route("/api/skipmarks")
def api_skipmarks_get():
    token = request.args.get("token") or ""
    m = KP.skip_get(token) if token else None
    return jsonify({"ok": True, "mark": m})


@app.route("/api/skipmarks", methods=["POST"])
def api_skipmarks_set():
    u = _me()
    if not u:
        return _need_login()
    d = request.get_json(silent=True) or {}
    token = d.get("token") or ""
    if not token:
        return jsonify({"ok": False, "error": "缺 token"}), 400
    ie = d.get("intro_end"); os_ = d.get("outro_start"); dur = d.get("duration")
    ie = min(float(ie), 300.0) if ie else None
    m = KP.skip_set(token, u["id"], ie, float(os_) if os_ else None,
                    float(dur) if dur else None)
    return jsonify({"ok": True, "mark": m})




# ---------------- 原站前端 /v1/* 统一反代层 ----------------
_EP_TOKEN_MAP = {}   # {token: {"title": title, "ep_num": int, "ep_title": str}}
_CID_TO_TITLE = {}   # {cid: title}

def init_catalog_cache_db():
    try:
        with KM._db() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS catalog_cache (
                key TEXT PRIMARY KEY,
                title TEXT,
                ep_num INTEGER DEFAULT 1,
                data_json TEXT,
                updated_at INTEGER
            )
            """)
            conn.commit()
    except Exception:
        pass

init_catalog_cache_db()

def save_catalog_cache(key, title, ep_num=1, data=None):
    if not key:
        return
    try:
        now = int(time.time())
        dj = json.dumps(data, ensure_ascii=False) if data else ""
        with KM._db() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO catalog_cache (key, title, ep_num, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """, (key, title or "", ep_num or 1, dj, now))
            conn.commit()
    except Exception:
        pass

def get_catalog_cache(key):
    if not key:
        return None
    try:
        with KM._db() as conn:
            row = conn.execute("SELECT * FROM catalog_cache WHERE key=?", (key,)).fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return None

@app.route("/v1/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
def proxy_v1(subpath):
    if request.method == "OPTIONS":
        resp = Response("", status=204)
        origin = request.headers.get("Origin")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        else:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        req_headers = request.headers.get("Access-Control-Request-Headers")
        resp.headers["Access-Control-Allow-Headers"] = req_headers or "*"
        return resp

    # 4K / 线路解析快捷通道：带本地 2 小时缓存防耗 + 带运营者上游会话解析
    if subpath == "playback/resolve-line" and request.method == "POST":
        data = request.get_json(silent=True) or {}
        ticket = (data.get("ticket") or "").strip()
        if ticket.startswith("resolve://"):
            ticket = ticket[10:]
            
        # 优先读取本地 4K 解析缓存 (TTL 2小时)，避免重复向上游扣除词元
        cache_key = f"ticket_res:{ticket[:48]}"
        now = int(time.time())
        if cache_key in KC._RESOLVE_CACHE and KC._RESOLVE_CACHE[cache_key]["expire_at"] > now:
            res_data = KC._RESOLVE_CACHE[cache_key]["data"]
            resp = jsonify(res_data)
            resp.status_code = 200
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        view = request.args.get("view", "compact")
        target_path = f"/v1/playback/resolve-line?view={view}"
        st, res_data = up_req("POST", target_path, {"ticket": ticket}, timeout=20)
        if st in (200, 201) and isinstance(res_data, dict):
            KC._RESOLVE_CACHE[cache_key] = {"data": res_data, "expire_at": now + 7200}
            
        resp = jsonify(res_data)
        resp.status_code = st
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # 0. 首页信息流：支持全量11大栏目毫秒级快速直出 + 离线全量兜底 + 低延迟上游探测
    if subpath.startswith("feed/home") and request.method == "GET":
        cursor = request.args.get("cursor")
        if cursor:
            # 0.1 分页流（用户往下滑动加载更多）：直通上游
            qs = request.query_string.decode("utf-8")
            full_path = "/v1/" + subpath + (f"?{qs}" if qs else "")
            st, r = up_req(request.method, full_path, timeout=6)
            if st == 200 and isinstance(r, dict) and r.get("sections"):
                clean_experience_ads(r)
                resp = jsonify(r)
                resp.status_code = 200
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
            # 上游分页暂无或超时，返回安全空分页结构，杜绝前端弹出“推荐分页暂未更新”错误
            resp = jsonify({
                "object": "home.feed",
                "sections": [],
                "next_cursor": None,
                "meta": {"next_cursor": None}
            })
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # 0.2 首页初始流：全量11大栏目（电影、电视剧、美剧、日韩、动漫等）毫秒级秒出
        cache_file = os.path.join(CACHE_DIR, "home.json")
        seed_file = os.path.join(HERE, "seeds", "home.json")
        cached_data = None
        for fp in [seed_file, cache_file]:
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        if isinstance(d, dict) and d.get("sections") and len(d["sections"]) >= 8:
                            cached_data = d
                            if len(d["sections"]) >= 11:
                                break
                except Exception:
                    pass

        # 优先毫秒级直出全量栏目（杜绝前端8秒白屏、仅有3个栏目与分页中断报错）
        if cached_data:
            clean_experience_ads(cached_data)
            cached_data["next_cursor"] = None
            if "meta" in cached_data and isinstance(cached_data["meta"], dict):
                cached_data["meta"]["next_cursor"] = None
            resp = jsonify(cached_data)
            resp.status_code = 200
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        # 若本地未初始化，快速探测上游（请求全量 15 个栏目）
        st, r = up_req("GET", "/v1/feed/home?scope=public&mode=preview&sections=15&cards=12", timeout=5)
        if st == 200 and isinstance(r, dict) and r.get("sections") and len(r["sections"]) >= 8:
            clean_experience_ads(r)
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False)
            except Exception:
                pass
            resp = jsonify(r)
            resp.status_code = 200
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

    # 0.1 基础运行时配置：秒级回退
    if subpath == "runtime/bootstrap" and request.method == "GET":
        cache_file = os.path.join(CACHE_DIR, "bootstrap.json")
        seed_file = os.path.join(HERE, "seeds", "bootstrap.json")
        boot_data = None
        for fp in [cache_file, seed_file]:
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f)
                        if isinstance(d, dict) and d.get("catalog_ui"):
                            boot_data = d
                            break
                except Exception:
                    pass

        qs = request.query_string.decode("utf-8")
        full_path = "/v1/" + subpath + (f"?{qs}" if qs else "")
        st, r = up_req(request.method, full_path, timeout=4)
        if st == 200 and isinstance(r, dict) and r.get("catalog_ui"):
            clean_experience_ads(r)
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False)
            except Exception:
                pass
            resp = jsonify(r)
            resp.status_code = 200
            return resp

        if boot_data:
            clean_experience_ads(boot_data)
            resp = jsonify(boot_data)
            resp.status_code = 200
            return resp

    # ---------------- 原站账号系统本地化（免上游门槛） ----------------

    # 1. 验证码接口
    if subpath == "users/captcha":
        cid, svg, exp = KM.create_captcha()
        resp = jsonify({
            "challenge_id": cid,
            "image_svg": svg,
            "expires_at": exp
        })
        resp.status_code = 201
        origin = request.headers.get("Origin")
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 2. 账号注册
    if subpath == "users/password/register" and request.method == "POST":
        d = request.get_json(silent=True) or {}
        acc = (d.get("account") or "").strip()
        pwd = d.get("password") or ""
        inv = (d.get("invite_code") or "").strip()
        c_obj = d.get("captcha") or {}
        cid = c_obj.get("challenge_id") or ""
        ans = c_obj.get("answer") or ""

        if cid and not KM.verify_captcha(cid, ans):
            return jsonify({"error": "validation_error", "message": "验证码不正确或已过期"}), 400
        if len(acc) < 3:
            return jsonify({"error": "validation_error", "message": "账号至少 3 个字符"}), 400
        if len(pwd) < 6:
            return jsonify({"error": "validation_error", "message": "密码至少 6 位"}), 400

        u, err = KM.create_user(acc, pwd, invite_code=inv, nickname=acc)
        if err or not u:
            return jsonify({"error": "request_error", "message": err or "注册失败"}), 400

        if inv:
            KP.invite_bind(u["id"], inv)

        sid = KM.new_session(u["id"])
        resp = jsonify({
            "object": "user.password_register_result",
            "id": f"usr_{u['id']}",
            "account": u["account"],
            "display_name": u.get("nickname") or u["account"],
            "authenticated": True,
            "invite_attribution": {
                "attributed": bool(inv),
                "reward_granted": bool(inv),
                "reward_tokens": 50 if inv else 0
            }
        })
        resp.status_code = 201
        _set_sid(resp, sid)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 3. 账号登录
    if subpath == "users/password/login" and request.method == "POST":
        d = request.get_json(silent=True) or {}
        acc = (d.get("account") or "").strip()
        pwd = d.get("password") or ""
        u, err = KM.verify_user(acc, pwd)
        if err or not u:
            return jsonify({"error": "request_error", "message": err or "账号或密码错误"}), 401

        sid = KM.new_session(u["id"])
        resp = jsonify({
            "object": "user.password_login_result",
            "id": f"usr_{u['id']}",
            "account": u["account"],
            "display_name": u.get("nickname") or u["account"],
            "authenticated": True
        })
        resp.status_code = 200
        _set_sid(resp, sid)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 4. 退出登录
    if subpath == "users/logout" and request.method == "POST":
        KM.drop_session(_sid())
        resp = jsonify({
            "object": "user.logout_result",
            "ok": True,
            "authenticated": False
        })
        resp.delete_cookie(COOKIE)
        resp.delete_cookie("ai_movie_session")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 5. 当前登录用户资料
    clean_exp_dict = {
        "object": "app.experience_snapshot",
        "version": 1,
        "generated_at": "2026-11-06T15:00:00.000Z",
        "cache_ttl_seconds": 600,
        "placements": {
            "home_top_carousel": {"items": [], "layout": "carousel", "mode": "hot", "autoplay_seconds": 6},
            "player_info": {
                "enabled": False,
                "presentation_mode": "none",
                "mobile_presentation_mode": "none",
                "large_screen_presentation_mode": "none",
                "items": [],
                "banner_items": [],
                "grid_items": [],
                "auto_flip": {"enabled": False}
            },
            "showcase_grid": {"items": [], "layout": "grid", "search_enabled": False, "calendar_enabled": False},
            "splash_screen": {"items": [], "layout": "banner", "daily_impression_limit": 0, "cooldown_minutes": 240}
        }
    }

    if subpath == "users/me" and request.method == "GET":
        u = _me()
        if u:
            resp = jsonify({
                "object": "user.profile",
                "id": f"usr_{u['id']}",
                "account": u["account"],
                "display_name": u.get("nickname") or u["account"],
                "authenticated": True,
                "level": u.get("level", 1),
                "tokens": u.get("tokens", 120),
                "growth": u.get("growth", 0),
                "adult_confirmed": True,
                "role": u.get("role", "user"),
                "app_experience": clean_exp_dict
            })
        else:
            resp = jsonify({
                "object": "user.profile",
                "id": "",
                "account": "",
                "display_name": "",
                "authenticated": False,
                "app_experience": clean_exp_dict
            })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 6. 用户额度与功能权限
    if subpath == "users/me/quota":
        u = _me()
        if u:
            KP.quota_view(u["id"])
            u = _me() or u
            tokens = u.get("tokens", 120)
            growth = u.get("growth", 0)
            level = u.get("level", 1)
            resp = jsonify({
                "object": "user.quota",
                "authenticated": True,
                "remaining_tokens": tokens,
                "limit_tokens": 1800000,
                "used_tokens": 0,
                "base_limit_tokens": 1800000,
                "bonus_tokens": 0,
                "consumption_multiplier_bps": 5000,
                "current_level_growth": growth,
                "economy": {
                    "progression": {
                        "next_level": {
                            "title": f"Lv{level + 1}",
                            "remaining_growth": max(0, 100 - growth)
                        }
                    },
                    "version": "token_economy_v3"
                },
                "feature_access": {
                    "comment_composer": {"actions": ["stickers", "playlists"], "stickers": {"allowed": True, "max_count": 1}},
                    "midnight_theater": {
                        "announcement_pending": False,
                        "available": True,
                        "direct_entry_enabled": True,
                        "enabled": True,
                        "entry_mode": "direct"
                    }
                }
            })
        else:
            resp = jsonify({
                "object": "user.quota",
                "authenticated": False,
                "remaining_tokens": 0,
                "limit_tokens": 0,
                "feature_access": {
                    "midnight_theater": {
                        "announcement_pending": False,
                        "available": True,
                        "direct_entry_enabled": True,
                        "enabled": True,
                        "entry_mode": "direct"
                    }
                }
            })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    # 7. 播放历史与收藏状态同步
    if subpath == "users/me/library/status":
        u = _me()
        ids = [i.strip() for i in (request.args.get("ids") or "").split(",") if i.strip()]
        items = []
        if u and ids:
            uid = u["id"]
            c = KM._conn()
            try:
                for cid in ids:
                    fav = bool(c.execute("SELECT 1 FROM favorites WHERE uid=? AND cid=?", (uid, cid)).fetchone())
                    h = c.execute("SELECT episode, position, duration, updated_at FROM history WHERE uid=? AND cid=?", (uid, cid)).fetchone()
                    item = {
                        "variant_id": cid,
                        "favorite": fav,
                        "updated_at": h["updated_at"] if h else None
                    }
                    if h:
                        item["resume"] = {
                            "episode_id": h["episode"],
                            "position_seconds": h["position"],
                            "duration_seconds": h["duration"]
                        }
                    items.append(item)
            finally:
                c.close()
        resp = jsonify({"object": "library.status", "items": items})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath == "users/me/library/sync" and request.method == "POST":
        u = _me()
        body = request.get_json(silent=True) or {}
        ops = body.get("ops") if isinstance(body, dict) else (body if isinstance(body, list) else [])
        accepted = []
        if u and ops:
            uid = u["id"]
            for op in ops:
                if not isinstance(op, dict):
                    continue
                op_id = op.get("opId") or op.get("op_id")
                op_type = op.get("type")
                vid = op.get("variantId") or op.get("cid")
                if op_type == "favorite_set" and vid:
                    if op.get("favorite"):
                        KM.fav_add(uid, vid, op.get("title", ""), op.get("poster", ""))
                    else:
                        KM.fav_del(uid, vid)
                    if op_id:
                        accepted.append(op_id)
                elif op_type == "resume_set" and vid:
                    res = op.get("resume") or {}
                    KM.hist_put(uid, vid, op.get("title", ""), op.get("poster", ""),
                                res.get("episodeId") or res.get("episode") or "",
                                res.get("positionSeconds") or res.get("position") or 0,
                                res.get("durationSeconds") or res.get("duration") or 0)
                    if op_id:
                        accepted.append(op_id)
                elif op_id:
                    accepted.append(op_id)
        resp = jsonify({"accepted_op_ids": accepted, "rejected_ops": []})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath == "users/me/preferences" and request.method == "PUT":
        u = _me()
        d = request.get_json(silent=True) or {}
        if u and "adult_confirmed" in d:
            KM.set_adult(u["id"], bool(d["adult_confirmed"]))
        resp = jsonify({
            "object": "user.preferences",
            "adult_confirmed_at": "2026-11-06T15:00:00.000Z",
            "preferences": {"adult_confirmed": True}
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath == "users/me/favorites":
        u = _me()
        items = KM.fav_list(u["id"]) if u else []
        resp = jsonify({"items": items})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath == "users/me/history":
        u = _me()
        items = KM.hist_list(u["id"]) if u else []
        resp = jsonify({"items": items})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath in ("users/me/playlists", "users/me/playlists/saved"):
        u = _me()
        items = KP.playlist_mine(u["id"]) if u else []
        resp = jsonify({"items": items})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath in ("users/me/invite", "users/me/invite/progress"):
        u = _me()
        if u:
            resp = jsonify(KP.invite_progress(u["id"]))
        else:
            resp = jsonify({"invite_code": "KANJU2026", "invites": 0, "reward_tokens": 0})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if subpath == "users/anonymous":
        resp = jsonify({
            "object": "user.anonymous_session",
            "authenticated": False,
            "app_experience": clean_exp_dict
        })
        resp.status_code = 201
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # 午夜剧场核心权限与流代理拦截（全面开放免阻断）
    if subpath == "midnight-theater/access":
        u = _me()
        tokens = u.get("tokens", 1800000) if u else 1800000
        resp = jsonify({
            "object": "midnight_theater.access",
            "dev_no_limit": True,
            "generation_cost_tokens": 10,
            "history_play_cost_tokens": 0,
            "public_play_cost_tokens": 0,
            "similar_recommendation_cost_tokens": 0,
            "daily_generation_limit": 100,
            "remaining_generations": 100,
            "remaining_tokens": tokens,
            "generation_blocked_reason": None
        })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    if subpath == "midnight-theater/eligibility":
        resp = jsonify({
            "object": "midnight_theater.eligibility",
            "eligible": True,
            "adult_confirmed": True,
            "acknowledged": True
        })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    if subpath == "midnight-theater/eligibility/acknowledge":
        u = _me()
        if u:
            KM.set_adult(u["id"], True)
        resp = jsonify({
            "object": "midnight_theater.eligibility_acknowledgement",
            "acknowledged": True
        })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    if subpath == "midnight-theater/guest/evaluate":
        resp = jsonify({
            "object": "midnight_theater.guest_evaluation",
            "eligible": True,
            "entry_mode": "direct"
        })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    if subpath.startswith("midnight-theater/discovery"):
        u = _me()
        if not u:
            resp = jsonify({"code": "midnight_session_required", "message": "请登录后进入午夜剧场"})
            resp.status_code = 401
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            return resp

        # 先尝试上游
        qs = request.query_string.decode("utf-8")
        full_path = "/v1/" + subpath + (f"?{qs}" if qs else "")
        extra_h = {}
        for hname in ["x-ai-movie-site-host", "x-ai-movie-midnight-access", "authorization", "cookie"]:
            val = request.headers.get(hname)
            if val:
                extra_h[hname] = val
        st, r = up_req(request.method, full_path, extra_headers=extra_h, timeout=15)
        if st < 400 and isinstance(r, dict) and (r.get("history") is not None or r.get("showcase") is not None):
            clean_experience_ads(r)
            if "showcase" in r and isinstance(r["showcase"], list):
                r["showcase"] = [it for it in r["showcase"] if isinstance(it, dict) and it.get("type") != "ad"]
            resp = jsonify(r)
            resp.status_code = st
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            return resp

        # 若上游受限或不可用，本地组装高质量午夜剧场流
        feed_items = []
        d, _ = api_get("/v1/feed/home?scope=public&mode=preview&sections=3&cards=12", ttl=1800)
        if isinstance(d, dict):
            for sec in d.get("sections") or []:
                for card in sec.get("cards") or []:
                    feed_items.append(card)
        resp = jsonify({
            "history": feed_items[:10],
            "watching": feed_items[10:20] if len(feed_items) > 10 else feed_items[:10],
            "history_has_more": False,
            "history_next_cursor": None,
            "watching_has_more": False,
            "watching_next_cursor": None,
            "showcase": feed_items[:8]
        })
        resp.status_code = 200
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp

    qs = request.query_string.decode("utf-8")
    full_path = "/v1/" + subpath + (f"?{qs}" if qs else "")

    extra_h = {}
    for hname in ["x-ai-movie-site-host", "x-ai-movie-midnight-access"]:
        val = request.headers.get(hname)
        if val:
            extra_h[hname] = val
    if request.is_json:
        extra_h["Content-Type"] = "application/json"

    body = request.get_json(silent=True) if request.is_json else (request.get_data() or None)

    st, r = up_req(request.method, full_path, body=body, extra_headers=extra_h, timeout=25)
    if isinstance(r, dict):
        clean_experience_ads(r)

        # 0. 首页信息流容灾与种子保障（杜绝上游 502/超时导致首页出现“首页内容暂时无法加载”）
        if subpath.startswith("feed/home"):
            if st == 200 and isinstance(r, dict) and r.get("sections") and len(r["sections"]) >= 8:
                try:
                    with open(os.path.join(CACHE_DIR, "home.json"), "w", encoding="utf-8") as f:
                        json.dump(r, f, ensure_ascii=False)
                except Exception:
                    pass
            else:
                fallback_home = None
                for candidate_path in [
                    os.path.join(CACHE_DIR, "home.json"),
                    os.path.join(HERE, "seeds", "home.json")
                ]:
                    if os.path.exists(candidate_path):
                        try:
                            with open(candidate_path, "r", encoding="utf-8") as f:
                                d = json.load(f)
                                if isinstance(d, dict) and d.get("sections"):
                                    fallback_home = d
                                    break
                        except Exception:
                            pass
                if fallback_home:
                    r = fallback_home
                    clean_experience_ads(r)
                    st = 200

        # 0.1 基础运行时配置容灾回退
        if subpath.startswith("runtime/bootstrap"):
            if st == 200 and isinstance(r, dict) and r.get("catalog_ui"):
                try:
                    with open(os.path.join(CACHE_DIR, "bootstrap.json"), "w", encoding="utf-8") as f:
                        json.dump(r, f, ensure_ascii=False)
                except Exception:
                    pass
            else:
                fallback_boot = None
                for candidate_path in [
                    os.path.join(CACHE_DIR, "bootstrap.json"),
                    os.path.join(HERE, "seeds", "bootstrap.json")
                ]:
                    if os.path.exists(candidate_path):
                        try:
                            with open(candidate_path, "r", encoding="utf-8") as f:
                                d = json.load(f)
                                if isinstance(d, dict) and d.get("catalog_ui"):
                                    fallback_boot = d
                                    break
                        except Exception:
                            pass
                if fallback_boot:
                    r = fallback_boot
                    clean_experience_ads(r)
                    st = 200

        # 0.2 站点访问域名兜底
        if subpath.startswith("sites/home-address") and st != 200:
            h_arg = request.args.get("host") or request.host.split(":")[0]
            r = {
                "object": "site.home_address",
                "host": h_arg,
                "address": request.host_url.rstrip("/")
            }
            st = 200

        # 0.3 搜索联想兜底
        if subpath.startswith("suggest") and st != 200:
            q = (request.args.get("query") or request.args.get("q") or "").strip()
            suggestions = []
            if q:
                try:
                    with KM._db() as conn:
                        rows = conn.execute("SELECT DISTINCT title, key FROM catalog_cache WHERE title LIKE ? LIMIT 10", (f"%{q}%",)).fetchall()
                        for row in rows:
                            cid = row["key"].replace("cid:", "")
                            suggestions.append({
                                "label": row["title"],
                                "prompt": row["title"],
                                "target": {"kind": "catalog_variant", "variant_id": cid}
                            })
                except Exception:
                    pass
            if suggestions:
                r = {"object": "suggest.result", "suggestions": suggestions}
                st = 200

        if subpath.startswith("midnight-theater/discovery"):
            # 过滤掉午夜剧场发现流里的外部推广/博彩广告
            if "showcase" in r and isinstance(r["showcase"], list):
                r["showcase"] = [it for it in r["showcase"] if isinstance(it, dict) and it.get("type") != "ad"]

        # 1. 抓取与缓存片名信息 (用于播放解析时自动匹配本地 4K 与网盘资源)
        if subpath.startswith("catalog/"):
            parts = subpath.split("/")
            if len(parts) >= 2:
                cid = parts[1]
                if "/detail" in subpath:
                    if st == 200 and isinstance(r, dict) and r.get("title"):
                        _CID_TO_TITLE[cid] = r["title"]
                        save_catalog_cache(f"cid:{cid}", r["title"], data=r)
                    elif st != 200 or not isinstance(r, dict):
                        c_info = get_catalog_cache(f"cid:{cid}")
                        if c_info and c_info.get("data_json"):
                            try:
                                r = json.loads(c_info["data_json"])
                                st = 200
                            except Exception:
                                pass
                elif "/episodes" in subpath:
                    if st == 200 and isinstance(r, dict) and r.get("episodes"):
                        m_title = r.get("title") or _CID_TO_TITLE.get(cid) or ""
                        if m_title:
                            _CID_TO_TITLE[cid] = m_title
                            save_catalog_cache(f"cid:{cid}", m_title, data=r)
                        for ep_it in (r.get("episodes") or []):
                            tok = ep_it.get("token")
                            if tok:
                                ep_n = ep_it.get("number") or ep_it.get("sequence_index") or 1
                                ep_t = ep_it.get("title") or ""
                                _EP_TOKEN_MAP[tok] = {
                                    "title": m_title,
                                    "ep_num": ep_n,
                                    "ep_title": ep_t
                                }
                                if m_title:
                                    save_catalog_cache(f"tok:{tok}", m_title, ep_num=ep_n)
                    elif st != 200 or not isinstance(r, dict) or not r.get("episodes"):
                        # episodes 上游报错容灾，防止前端“选集加载失败”报错！
                        c_info = get_catalog_cache(f"cid:{cid}")
                        if c_info and c_info.get("data_json"):
                            try:
                                r = json.loads(c_info["data_json"])
                                st = 200
                            except Exception:
                                pass
                        if st != 200:
                            fallback_t = (c_info.get("title") if c_info else "") or _CID_TO_TITLE.get(cid) or "正片"
                            r = {
                                "object": "catalog.episodes",
                                "variant_id": cid,
                                "title": fallback_t,
                                "content_kind": "movie",
                                "episode_count": 1,
                                "available_episode_count": 1,
                                "episode_pagination": {"offset": 0, "limit": 48, "returned_count": 1, "total_count": 1, "has_more": False},
                                "episodes": [{
                                    "id": f"ep_{cid[:16]}",
                                    "episode_track": "main",
                                    "key": "main",
                                    "title": "正片",
                                    "token": f"tok_{cid[:24]}",
                                    "playback_count": 1,
                                    "urls": {}
                                }]
                            }
                            st = 200

        # 2. 核心注入：在播放解析接口中将我们新做的网盘 4K 原画与本地多源 4K 专线置顶前置！
        if subpath.startswith("playback/resolve/"):
            tok = subpath.split("/")[-1]
            ep_info = _EP_TOKEN_MAP.get(tok) or {}
            m_title = ep_info.get("title") or ""
            ep_num = ep_info.get("ep_num") or 1

            if not m_title:
                tok_cache = get_catalog_cache(f"tok:{tok}")
                if tok_cache:
                    m_title = tok_cache.get("title") or ""
                    ep_num = tok_cache.get("ep_num") or ep_num

            cid_from_ref = ""
            ref = request.headers.get("Referer", "")
            if "/player/" in ref:
                try:
                    cid_from_ref = ref.split("/player/")[1].split("?")[0]
                except Exception:
                    pass

            if not m_title and cid_from_ref:
                m_title = _CID_TO_TITLE.get(cid_from_ref) or ""
                if not m_title:
                    cid_cache = get_catalog_cache(f"cid:{cid_from_ref}")
                    if cid_cache:
                        m_title = cid_cache.get("title") or ""
                if not m_title:
                    try:
                        c_d, _ = api_get(f"/v1/catalog/{cid_from_ref}/detail", ttl=3600)
                        if isinstance(c_d, dict) and c_d.get("title"):
                            m_title = c_d["title"]
                            _CID_TO_TITLE[cid_from_ref] = m_title
                            save_catalog_cache(f"cid:{cid_from_ref}", m_title, data=c_d)
                    except Exception:
                        pass

            if not m_title and isinstance(r, dict):
                m_title = r.get("title") or ""
                if not m_title:
                    cur_disp = (r.get("current_episode") or {}).get("display_name") or ""
                    if cur_disp and cur_disp not in ("正片", "抢先版", "预告"):
                        m_title = cur_disp

            # 关键保障：若上游接口 502/503 或未返回正常播放结构，启动本地应急播放合成机制，绝不报“当前线路暂时无法播放”！
            if st != 200 or not isinstance(r, dict) or "line_options" not in r:
                r = {
                    "object": "playback.resolve",
                    "token": tok,
                    "current_episode": {
                        "id": f"ep_{tok[:16]}",
                        "display_name": "正片",
                        "episode_key": "main",
                        "episode_number": ep_num,
                        "sequence_index": ep_num,
                        "token": tok,
                        "urls": {}
                    },
                    "line_options": [],
                    "view": "compact"
                }
                st = 200

            # ================= 播放线路智能梯队排序系统 =================
            # 顺序要求：
            # 1. 第一：网盘资源（115 / 夸克 / UC）
            # 2. 第二：kanju的4K（上游 Kanju 4K 原画专线）
            # 3. 第三：我们的4K采集源（本地 4K 蓝光采集线路）
            # 4. 第四：普通的采集源（Kanju 普通源 + 本地普通采集源）

            pan_lines = []
            collector_4k_lines = []
            collector_normal_lines = []
            has_acc = KPAN.has_active_pan_account()
            pan_shares = []

            if m_title:
                # 1. 获取网盘资源 (115、夸克、UC)
                try:
                    pan_shares = KPAN.search_pan_shares(m_title)
                    if pan_shares:
                        for idx, s in enumerate(pan_shares[:3]):
                            ptype = (s.get("pan_type") or "quark").lower()
                            pwd = s.get("pwd_id") or s.get("share_url")
                            tag = s.get("quality_tag") or "4K原画"
                            u_tag = (tag + " " + (s.get("title") or "")).upper()
                            color_hint = ""
                            if "SDR" in u_tag or "MAXPLUS" in u_tag:
                                color_hint = " · SDR色彩真实"
                            elif "HDR10" in u_tag or ("DV" in u_tag and "HDR" in u_tag):
                                color_hint = " · HDR10"
                            elif "DV" in u_tag or "DOVI" in u_tag or "杜比" in u_tag:
                                color_hint = " · 杜比Profile 5(部分浏览器可能偏色)"

                            pname = "夸克" if ptype == "quark" else ("115" if ptype == "115" else "UC")
                            label_badge = f"⚡ {pname}网盘4K原画 · {tag}{color_hint}" if has_acc else f"☁️ {pname}网盘4K原画 · {tag}{color_hint}"
                            pan_lines.append({
                                "default_priority": False,
                                "id": f"local:pan:{ptype}:{idx+1}",
                                "label": label_badge,
                                "minimum_user_level": 0,
                                "play_from": f"pan_{ptype}",
                                "preference_weight": 4000 - idx * 10,
                                "price_multiplier_bps": 0,
                                "provider_id": "local_pan",
                                "resolve_mode": "direct",
                                "resolve_required": False,
                                "resolved": True,
                                "selected": False,
                                "url": (f"/api/pan/stream/{pwd}.m3u8?pan_type={ptype}&passcode={s.get('passcode', '')}&title={urllib.parse.quote(m_title)}&format=m3u8"
                                        if ptype == "115"
                                        else f"/api/pan/stream/{pwd}.mp4?pan_type={ptype}&passcode={s.get('passcode', '')}&title={urllib.parse.quote(m_title)}&format=mp4"),
                                "url_kind": "m3u8" if ptype == "115" else "mp4",
                                "is_4k": True,
                                "is_pan": True
                            })
                    else:
                        # 兜底注入引导入口，确保换源抽屉始终展示【☁️ 网盘专线】
                        pan_lines.append({
                            "default_priority": False,
                            "id": "local:pan:quark:guide",
                            "label": "☁️ 夸克/115网盘4K · 全网检索",
                            "minimum_user_level": 0,
                            "play_from": "pan_quark",
                            "preference_weight": 3900,
                            "price_multiplier_bps": 0,
                            "provider_id": "local_pan",
                            "resolve_mode": "direct",
                            "resolve_required": False,
                            "resolved": True,
                            "selected": False,
                            "url": f"/api/pan/stream/auto.mp4?pan_type=quark&title={urllib.parse.quote(m_title)}&format=mp4",
                            "url_kind": "mp4",
                            "is_4k": True,
                            "is_pan": True
                        })
                except Exception:
                    pass

                # 2. 获取本地采集源，拆分为：4K 蓝光采集线路 与 普通采集线路
                try:
                    collector_lines = KC.build_local_line_options(m_title, episode_index=ep_num)
                    for idx, c_line in enumerate(collector_lines):
                        c_line["minimum_user_level"] = 0
                        c_line["price_multiplier_bps"] = 0
                        c_line["provider_id"] = "local_collector"
                        c_line["resolve_mode"] = "direct"
                        c_line["resolve_required"] = False
                        c_line["resolved"] = True
                        c_line["selected"] = False
                        c_line["default_priority"] = False
                        if c_line.get("is_4k"):
                            collector_4k_lines.append(c_line)
                        else:
                            collector_normal_lines.append(c_line)
                except Exception:
                    pass

            # 3. 提取并分类上游 4K 与 普通线路（拆分出 剧场8 4K 与 剧场8 普通源）
            raw_upstream_lines = r.get("line_options", []) or []
            juchang8_4k_lines = []
            juchang8_normal_lines = []

            def _is_juchang8_4k(l):
                if not isinstance(l, dict):
                    return False
                if l.get("is_4k") is True:
                    return True
                txt = ((l.get("label") or "") + " " + (l.get("display_label") or "") + " " + (l.get("id") or "") + " " + (l.get("definition") or "")).upper()
                return ("4K" in txt or "2160P" in txt or "UHD" in txt or "原画" in txt or "超清4K" in txt or "蓝光4K" in txt)

            for l in raw_upstream_lines:
                l["selected"] = False
                l["default_priority"] = False
                if _is_juchang8_4k(l):
                    l["is_4k"] = True
                    juchang8_4k_lines.append(l)
                else:
                    juchang8_normal_lines.append(l)

            # 4. 按严格优先次序合并并赋权：
            # 第一梯队：网盘资源 (weight: 4000+)
            # 第二梯队：剧场8的4K (weight: 3000+)
            # 第三梯队：我们的4K采集源 (weight: 2000+)
            # 第四梯队：普通采集源 (weight: 1000)
            ordered_lines = []

            for i, l in enumerate(pan_lines):
                l["preference_weight"] = 4000 - i * 10
                ordered_lines.append(l)

            for i, l in enumerate(juchang8_4k_lines):
                l["preference_weight"] = 3000 - i * 10
                ordered_lines.append(l)

            for i, l in enumerate(collector_4k_lines):
                l["preference_weight"] = 2000 - i * 10
                ordered_lines.append(l)

            normal_combined = juchang8_normal_lines + collector_normal_lines
            for i, l in enumerate(normal_combined):
                l["preference_weight"] = 1000 - i * 10
                ordered_lines.append(l)

            # 5. 确定默认选中并置顶生效的第 1 条线路 (selected / default_priority)：
            # 第一优先级：若网盘已绑定激活且找到实际网盘资源，选网盘第 1 条！
            # 第二优先级：否则若有 剧场8 4K，选 剧场8 4K 第 1 条！
            # 第三优先级：否则若有我们的 4K 采集源，选 4K 采集源第 1 条！
            # 第四优先级：最后兜底选中普通采集源第 1 条！
            chosen_line = None
            if has_acc and pan_shares and pan_lines:
                chosen_line = pan_lines[0]
            elif juchang8_4k_lines:
                chosen_line = juchang8_4k_lines[0]
            elif collector_4k_lines:
                chosen_line = collector_4k_lines[0]
            elif normal_combined:
                chosen_line = normal_combined[0]
            elif ordered_lines:
                chosen_line = ordered_lines[0]

            if chosen_line:
                chosen_line["selected"] = True
                chosen_line["default_priority"] = True

            r["line_options"] = ordered_lines

    if isinstance(r, (dict, list)):
        resp = jsonify(r)
    else:
        resp = Response(str(r), status=st, content_type="application/json")
    resp.status_code = st
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


@app.route("/api/pan/stream/<path:pwd_id>")
def api_pan_stream_play(pwd_id):
    """网盘 4K 直链取流中继播放，支持就地代理转写、智能平滑降级与全网搜盘"""
    pan_type = request.args.get("pan_type", "")
    title = request.args.get("title", "")
    fmt = request.args.get("format", "")
    from flask import redirect
    
    # 剥离 URL 文件后缀，提取标准 pwd_id
    if pwd_id.endswith(".m3u8"):
        pwd_id = pwd_id[:-5]
        if not fmt:
            fmt = "m3u8"
    elif pwd_id.endswith(".mp4"):
        pwd_id = pwd_id[:-4]
        if not fmt:
            fmt = "mp4"

    if not pan_type:
        pan_type = "115" if fmt == "m3u8" else "quark"
    
    # 自动搜盘
    if pwd_id == "auto" and title:
        try:
            auto_shares = KPAN.search_pan_shares(title)
            if auto_shares:
                pwd_id = auto_shares[0].get("pwd_id") or auto_shares[0].get("share_url")
                pan_type = auto_shares[0].get("pan_type") or pan_type
        except Exception as e:
            print(f"[KanjuPan] auto search error: {e}")

    passcode = request.args.get("passcode", "").strip()
    if not passcode and pwd_id:
        # 从 pan_shares_index 查询已索引保存的提取码
        try:
            with KPAN._db() as conn:
                row = conn.execute("SELECT passcode, pan_type FROM pan_shares_index WHERE pwd_id=? ORDER BY id DESC LIMIT 1", (pwd_id,)).fetchone()
                if row:
                    if row["passcode"]:
                        passcode = row["passcode"]
                    if not request.args.get("pan_type") and row["pan_type"]:
                        pan_type = row["pan_type"]
        except Exception:
            pass

    res = {}
    try:
        res = KPAN.parse_pan_video_link(pwd_id, pan_type, passcode=passcode)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    if res.get("ok"):
        target_url = res.get("stream_url")
        if not target_url and res.get("files"):
            target_url = res["files"][0].get("stream_url") or res["files"][0].get("url")
        if target_url:
            ref = "https://115.com/" if ("115" in pan_type or "115" in target_url or "cpats" in target_url) else "https://pan.quark.cn/"
            is_m3u8_target = ".m3u8" in target_url or fmt == "m3u8" or res.get("url_kind") == "m3u8"

            # 针对 115 官方原画 m3u8，直接就地代理重写并直出，无需 302 重定向跳转，避免跨域阻断与切源中断
            if is_m3u8_target and any(k in target_url for k in ("115", "quark", "cpats")) and target_url.startswith("http"):
                return proxy_m3u8_directly(target_url, ref)

            # 网盘视频直链通过本地 /proxy 代理，完美绕过 CDN 防盗链（115 / Quark）并支持浏览器 Range 分片拖动
            if any(k in target_url for k in ("115", "quark", "cpats")):
                proxy_url = f"/proxy?u={urllib.parse.quote(target_url, safe='')}&ref={urllib.parse.quote(ref, safe='')}"
                resp = redirect(proxy_url, code=302)
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
            resp = redirect(target_url, code=302)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
                
    # 优雅回退：若未绑定网盘或链接失效或非视频，秒级切回本地采集源 4K 蓝光流，保障播放不断流！
    if title:
        print(f"[KanjuPan] 播放直链未提取成功({res.get('error')})，自动平滑无缝降级至采集源4K蓝光: {title}")
        try:
            c_lines = KC.build_local_line_options(title)
            if c_lines and c_lines[0].get("url"):
                resp = redirect(c_lines[0]["url"], code=302)
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
        except Exception as e:
            print(f"[KanjuPan] fallback error: {e}")
            
    resp = jsonify({"error": "未能提取网盘播放流", "detail": res.get("error"), "hint": "可前往 /admin 扫码登录网盘"})
    resp.status_code = 502
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ================= 看剧AI 后台管理系统 =================

def _admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        u = _me()
        if not u or u.get("role") != "admin":
            return jsonify({"ok": False, "error": "无管理员权限，请先以管理员账号登录", "code": "admin_required"}), 403
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@app.route("/admin/<path:subpath>")
def serve_admin_page(subpath=""):
    return send_from_directory(os.path.join(HERE, "static"), "admin.html")


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    d = request.get_json(silent=True) or request.form
    acc = (d.get("account") or "").strip()
    pwd = d.get("password") or ""
    u, err = KM.verify_user(acc, pwd)
    if err or not u:
        return jsonify({"ok": False, "error": err or "账号或密码错误"}), 401
    if u.get("role") != "admin":
        return jsonify({"ok": False, "error": "该账号不是管理员账号，无法登录后台"}), 403
    sid = KM.new_session(u["id"])
    resp = jsonify({"ok": True, "user": KM.public_user(u), "sid": sid})
    _set_sid(resp, sid)
    return resp


@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    KM.drop_session(_sid())
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE)
    resp.delete_cookie("ai_movie_session")
    return resp


@app.route("/api/admin/me")
def api_admin_me():
    u = _me()
    if not u or u.get("role") != "admin":
        return jsonify({"ok": False, "user": None, "logged_in": False})
    return jsonify({"ok": True, "user": KM.public_user(u), "logged_in": True})


@app.route("/api/admin/overview")
@_admin_required
def api_admin_overview():
    stats = KM.get_admin_stats()
    base = pick_base() or ""
    cache_files = os.listdir(CACHE_DIR) if os.path.exists(CACHE_DIR) else []
    cache_size = sum(os.path.getsize(os.path.join(CACHE_DIR, f)) for f in cache_files if os.path.isfile(os.path.join(CACHE_DIR, f)))
    return jsonify({
        "ok": True,
        "stats": stats,
        "upstream_base": base,
        "cache_files_count": len(cache_files),
        "cache_size_mb": round(cache_size / (1024 * 1024), 2)
    })


@app.route("/api/admin/users")
@_admin_required
def api_admin_users():
    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    users, total = KM.list_users(q=q, page=page, limit=limit)
    return jsonify({"ok": True, "users": users, "total": total, "page": page, "limit": limit})


@app.route("/api/admin/users/<int:uid>/adjust-tokens", methods=["POST"])
@_admin_required
def api_admin_adjust_tokens(uid):
    d = request.get_json(silent=True) or {}
    delta = int(d.get("delta") or 0)
    reason = d.get("reason") or "管理员手工调整"
    bal = KM.update_user_tokens(uid, delta, reason)
    return jsonify({"ok": True, "balance": bal})


@app.route("/api/admin/users/<int:uid>/reset-password", methods=["POST"])
@_admin_required
def api_admin_reset_password(uid):
    d = request.get_json(silent=True) or {}
    new_pwd = d.get("new_password") or ""
    if len(new_pwd) < 6:
        return jsonify({"ok": False, "error": "密码至少 6 位"}), 400
    KM.reset_user_password(uid, new_pwd)
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/toggle-status", methods=["POST"])
@_admin_required
def api_admin_toggle_status(uid):
    st = KM.toggle_user_status(uid)
    return jsonify({"ok": True, "new_status": st})


@app.route("/api/admin/users/<int:uid>/set-role", methods=["POST"])
@_admin_required
def api_admin_set_role(uid):
    d = request.get_json(silent=True) or {}
    role = d.get("role") or "user"
    KM.set_user_role(uid, role)
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@_admin_required
def api_admin_delete_user(uid):
    KM.delete_user(uid)
    return jsonify({"ok": True})


@app.route("/api/admin/comments")
@_admin_required
def api_admin_comments():
    c = KM._conn()
    rows = [dict(r) for r in c.execute("SELECT id, cid, uid, nickname, body, at FROM comments ORDER BY id DESC LIMIT 100").fetchall()]
    c.close()
    return jsonify({"ok": True, "comments": rows})


@app.route("/api/admin/comments/<int:cid>", methods=["DELETE"])
@_admin_required
def api_admin_delete_comment(cid):
    c = KM._conn()
    c.execute("DELETE FROM comments WHERE id=?", (cid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.route("/api/admin/playlists")
@_admin_required
def api_admin_playlists():
    c = KM._conn()
    rows = [dict(r) for r in c.execute("SELECT id, uid, name, descr, is_public, share_code, created_at FROM playlists ORDER BY id DESC LIMIT 100").fetchall()]
    c.close()
    return jsonify({"ok": True, "playlists": rows})


@app.route("/api/admin/playlists/<int:pid>", methods=["DELETE"])
@_admin_required
def api_admin_delete_playlist(pid):
    c = KM._conn()
    c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
    c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.route("/api/admin/invites", methods=["GET", "POST"])
@_admin_required
def api_admin_invites():
    if request.method == "GET":
        return jsonify({"ok": True, "codes": sorted(list(KM.INVITE_CODES))})
    d = request.get_json(silent=True) or {}
    code = (d.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "邀请码不能为空"}), 400
    KM.INVITE_CODES.add(code)
    return jsonify({"ok": True, "codes": sorted(list(KM.INVITE_CODES))})


@app.route("/api/admin/invites/<string:code>", methods=["DELETE"])
@_admin_required
def api_admin_delete_invite(code):
    c = code.strip().upper()
    if c in KM.INVITE_CODES:
        KM.INVITE_CODES.remove(c)
    return jsonify({"ok": True, "codes": sorted(list(KM.INVITE_CODES))})


# ---------------- 1. 全站公告管理 (增删改查、弹窗/横幅/侧栏、开关) ----------------

@app.route("/api/admin/announcements", methods=["GET", "POST"])
@_admin_required
def api_admin_announcements():
    if request.method == "GET":
        return jsonify({"ok": True, "announcements": KP.announcements_list(include_inactive=True)})
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    body = (d.get("body") or d.get("content") or "").strip()
    a_type = (d.get("type") or "popup").strip()
    link_url = (d.get("link_url") or d.get("target_url") or "").strip()
    sort_order = int(d.get("sort_order") or 0)
    is_active = int(d.get("is_active", 1))
    if not title or not body:
        return jsonify({"ok": False, "error": "标题和内容不能为空"}), 400
    item = KP.announcements_create(title, body, a_type=a_type, link_url=link_url, is_active=is_active, sort_order=sort_order)
    return jsonify({"ok": True, "item": item, "announcements": KP.announcements_list(include_inactive=True)})


@app.route("/api/admin/announcements/<int:aid>/update", methods=["POST", "PUT"])
@_admin_required
def api_admin_announcements_update(aid):
    d = request.get_json(silent=True) or {}
    item = KP.announcements_update(
        aid,
        title=d.get("title"),
        body=d.get("body") or d.get("content"),
        a_type=d.get("type"),
        link_url=d.get("link_url") or d.get("target_url"),
        is_active=d.get("is_active"),
        sort_order=d.get("sort_order")
    )
    if not item:
        return jsonify({"ok": False, "error": "公告不存在"}), 404
    return jsonify({"ok": True, "item": item, "announcements": KP.announcements_list(include_inactive=True)})


@app.route("/api/admin/announcements/<int:aid>/toggle", methods=["POST"])
@_admin_required
def api_admin_announcements_toggle(aid):
    item = KP.announcements_toggle(aid)
    if not item:
        return jsonify({"ok": False, "error": "公告不存在"}), 404
    return jsonify({"ok": True, "item": item, "announcements": KP.announcements_list(include_inactive=True)})


@app.route("/api/admin/announcements/<int:aid>", methods=["DELETE"])
@_admin_required
def api_admin_announcements_delete(aid):
    KP.announcements_delete(aid)
    return jsonify({"ok": True, "announcements": KP.announcements_list(include_inactive=True)})


# ---------------- 2. 侧边栏与主导航管理 (增删改查、分组、开关) ----------------

@app.route("/api/admin/sidebar", methods=["GET", "POST"])
@_admin_required
def api_admin_sidebar():
    if request.method == "GET":
        section = request.args.get("section")
        return jsonify({"ok": True, "items": KP.sidebar_list(include_inactive=True, section=section)})
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    url = (d.get("url") or d.get("path") or "").strip()
    target = "_blank" if d.get("is_external") else (d.get("target") or "_self")
    badge = (d.get("badge_text") or d.get("badge") or "").strip()
    if not title or not url:
        return jsonify({"ok": False, "error": "标题和链接地址不能为空"}), 400
    item = KP.sidebar_create(
        section=d.get("section") or "nav",
        title=title,
        subtitle=(d.get("subtitle") or "").strip(),
        icon=(d.get("icon") or "").strip(),
        url=url,
        target=target,
        badge_text=badge,
        sort_order=int(d.get("sort_order") or 0),
        is_active=int(d.get("is_active", 1)),
        extra_data=d.get("extra_data") or ""
    )
    return jsonify({"ok": True, "item": item, "items": KP.sidebar_list(include_inactive=True)})


@app.route("/api/admin/sidebar/<int:sid>/update", methods=["POST", "PUT"])
@_admin_required
def api_admin_sidebar_update(sid):
    d = request.get_json(silent=True) or {}
    target = "_blank" if d.get("is_external") else d.get("target")
    item = KP.sidebar_update(
        sid,
        section=d.get("section"),
        title=d.get("title"),
        subtitle=d.get("subtitle"),
        icon=d.get("icon"),
        url=d.get("url") or d.get("path"),
        target=target,
        badge_text=d.get("badge_text") or d.get("badge"),
        sort_order=d.get("sort_order"),
        is_active=d.get("is_active"),
        extra_data=d.get("extra_data")
    )
    if not item:
        return jsonify({"ok": False, "error": "项目不存在"}), 404
    return jsonify({"ok": True, "item": item, "items": KP.sidebar_list(include_inactive=True)})


@app.route("/api/admin/sidebar/<int:sid>/toggle", methods=["POST"])
@_admin_required
def api_admin_sidebar_toggle(sid):
    item = KP.sidebar_toggle(sid)
    if not item:
        return jsonify({"ok": False, "error": "项目不存在"}), 404
    return jsonify({"ok": True, "item": item, "items": KP.sidebar_list(include_inactive=True)})


@app.route("/api/admin/sidebar/<int:sid>", methods=["DELETE"])
@_admin_required
def api_admin_sidebar_delete(sid):
    KP.sidebar_delete(sid)
    return jsonify({"ok": True, "items": KP.sidebar_list(include_inactive=True)})


# ---------------- 3. 全局站点配置系统 (KV 设定) ----------------

@app.route("/api/admin/settings", methods=["GET", "POST"])
@_admin_required
def api_admin_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "settings": KP.settings_get_all()})
    d = request.get_json(silent=True) or {}
    updated = KP.settings_set_many(d)
    return jsonify({"ok": True, "settings": updated})


# ---------------- 4. 用户反馈与工单管理 ----------------

@app.route("/api/admin/feedback", methods=["GET"])
@_admin_required
def api_admin_feedback():
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    res = KP.feedback_list(limit=limit, offset=offset)
    return jsonify({"ok": True, **res})


@app.route("/api/admin/feedback/<int:fid>/status", methods=["POST"])
@_admin_required
def api_admin_feedback_status(fid):
    d = request.get_json(silent=True) or {}
    status = d.get("status", "resolved")
    KP.feedback_status(fid, status=status)
    return jsonify({"ok": True})


@app.route("/api/admin/feedback/<int:fid>", methods=["DELETE"])
@_admin_required
def api_admin_feedback_delete(fid):
    KP.feedback_delete(fid)
    return jsonify({"ok": True})


@app.route("/api/admin/cache/clear", methods=["POST"])
@_admin_required
def api_admin_cache_clear():
    count = 0
    if os.path.exists(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json") and not f.startswith("bootstrap"):
                try:
                    os.remove(os.path.join(CACHE_DIR, f))
                    count += 1
                except Exception:
                    pass
    return jsonify({"ok": True, "cleared_count": count})


@app.route("/api/admin/upstream/test", methods=["POST"])
@_admin_required
def api_admin_upstream_test():
    base = pick_base()
    t0 = time.time()
    st, r = up_req("GET", "/v1/sites/home-address?host=juchang8.com", timeout=8)
    lat = int((time.time() - t0) * 1000)
    return jsonify({
        "ok": True,
        "base": base,
        "base_status": st,
        "latency_ms": lat,
        "resolve_ok": True
    })


# ---------------- 采集源与 4K 调度管理 ----------------

@app.route("/api/admin/collector/sources", methods=["GET"])
@_admin_required
def api_admin_collector_list():
    return jsonify({"ok": True, "sources": KC.list_sources(include_disabled=True)})


@app.route("/api/admin/collector/sources", methods=["POST"])
@_admin_required
def api_admin_collector_add():
    d = request.get_json(silent=True) or request.form
    name = (d.get("name") or "").strip()
    url = (d.get("api_url") or "").strip()
    is_4k = bool(d.get("is_4k"))
    weight = int(d.get("weight") or 50)
    mapping = d.get("category_mapping") or "{}"
    if not name or not url:
        return jsonify({"ok": False, "error": "源名称和接口地址不能为空"}), 400
    key = KC.add_source(name, url, is_4k=is_4k, is_custom=1, weight=weight, category_mapping=mapping)
    return jsonify({"ok": True, "key": key, "sources": KC.list_sources()})


@app.route("/api/admin/collector/sources/<int:sid>/toggle", methods=["POST"])
@_admin_required
def api_admin_collector_toggle(sid):
    KC.toggle_source(sid)
    return jsonify({"ok": True, "sources": KC.list_sources()})


@app.route("/api/admin/collector/sources/<int:sid>/test", methods=["POST"])
@_admin_required
def api_admin_collector_test(sid):
    lat = KC.test_source_latency(sid)
    return jsonify({"ok": True, "latency_ms": lat, "sources": KC.list_sources()})


@app.route("/api/admin/collector/sources/<int:sid>", methods=["DELETE"])
@_admin_required
def api_admin_collector_delete(sid):
    KC.delete_source(sid)
    return jsonify({"ok": True, "sources": KC.list_sources()})


@app.route("/api/admin/collector/search", methods=["POST"])
@_admin_required
def api_admin_collector_search():
    d = request.get_json(silent=True) or request.form
    wd = (d.get("wd") or "").strip()
    if not wd:
        return jsonify({"ok": False, "error": "请输入搜索片名"}), 400
    res = KC.search_all_sources(wd)
    return jsonify({"ok": True, "results": res})


# ---------------- 网盘扫码登录与视频解析管理 ----------------

@app.route("/api/admin/pan/accounts", methods=["GET"])
@_admin_required
def api_admin_pan_list():
    return jsonify({"ok": True, "accounts": KPAN.list_pan_accounts()})


@app.route("/api/admin/pan/accounts", methods=["POST"])
@_admin_required
def api_admin_pan_save():
    d = request.get_json(silent=True) or request.form
    ptype = (d.get("pan_type") or "115").lower()
    name = (d.get("name") or f"{ptype.upper()}账号").strip()
    cookie = (d.get("cookie") or "").strip()
    if not cookie:
        return jsonify({"ok": False, "error": "Cookie 不能为空"}), 400
    KPAN.save_pan_account_manual(ptype, name, cookie)
    return jsonify({"ok": True, "accounts": KPAN.list_pan_accounts()})


@app.route("/api/admin/pan/accounts/<int:aid>", methods=["DELETE"])
@_admin_required
def api_admin_pan_delete(aid):
    KPAN.delete_pan_account(aid)
    return jsonify({"ok": True, "accounts": KPAN.list_pan_accounts()})


@app.route("/api/admin/pan/qr/create", methods=["POST"])
@_admin_required
def api_admin_pan_qr_create():
    d = request.get_json(silent=True) or request.form
    ptype = (d.get("pan_type") or "115").lower()
    if ptype == "115":
        res = KPAN.create_qr_115()
    elif ptype == "quark":
        res = KPAN.create_qr_quark()
    elif ptype == "uc":
        res = KPAN.create_qr_uc()
    else:
        return jsonify({"ok": False, "error": "不支持的网盘类型"}), 400
    return jsonify(res)


@app.route("/api/admin/pan/qr/status", methods=["GET"])
@_admin_required
def api_admin_pan_qr_status():
    session_id = request.args.get("session_id") or ""
    if not session_id:
        return jsonify({"ok": False, "error": "缺少 session_id"}), 400
    res = KPAN.get_qr_session_status(session_id)
    return jsonify(res)


@app.route("/api/admin/pan/parse", methods=["POST"])
@_admin_required
def api_admin_pan_parse():
    d = request.get_json(silent=True) or request.form
    url = (d.get("url") or "").strip()
    ptype = d.get("pan_type")
    if not url:
        return jsonify({"ok": False, "error": "请输入网盘分享链接或资源代码"}), 400
    res = KPAN.parse_pan_video_link(url, ptype)
    return jsonify(res)


@app.route("/api/pan/parse", methods=["POST"])
def api_public_pan_parse():
    """前台播放器直接解析网盘链接"""
    d = request.get_json(silent=True) or request.form
    url = (d.get("url") or "").strip()
    ptype = d.get("pan_type")
    if not url:
        return jsonify({"ok": False, "error": "请输入网盘分享链接"}), 400
    res = KPAN.parse_pan_video_link(url, ptype)
    return jsonify(res)


@app.route("/api/admin/pan/stats", methods=["GET"])
@_admin_required
def api_admin_pan_stats():
    return jsonify({"ok": True, "stats": KPAN.get_transfer_stats()})


@app.route("/api/admin/pan/shares", methods=["GET"])
@_admin_required
def api_admin_pan_shares_list():
    return jsonify({"ok": True, "shares": KPAN.list_indexed_shares(limit=100)})


@app.route("/api/admin/pan/shares", methods=["POST"])
@_admin_required
def api_admin_pan_shares_add():
    d = request.get_json(silent=True) or request.form
    kw = (d.get("keyword") or "").strip()
    ptype = (d.get("pan_type") or "quark").strip()
    url = (d.get("share_url") or "").strip()
    title = (d.get("title") or "").strip()
    tag = (d.get("quality_tag") or "4K原画").strip()
    if not kw or not url:
        return jsonify({"ok": False, "error": "片名和网盘链接不能为空"}), 400
    KPAN.add_indexed_share(kw, ptype, url, title=title, quality_tag=tag)
    return jsonify({"ok": True, "shares": KPAN.list_indexed_shares()})


@app.route("/api/admin/pan/shares/<int:sid>", methods=["DELETE"])
@_admin_required
def api_admin_pan_shares_del(sid):
    KPAN.delete_indexed_share(sid)
    return jsonify({"ok": True, "shares": KPAN.list_indexed_shares()})


@app.route("/api/admin/pan/purge", methods=["POST"])
@_admin_required
def api_admin_pan_purge():
    d = request.get_json(silent=True) or {}
    force = bool(d.get("force_all"))
    res = KPAN.purge_expired_transfers(force_all=force)
    return jsonify(res)


@app.route("/api/pan/auto_search", methods=["POST"])
def api_pan_auto_search():
    """前台自动查询匹配的网盘 4K 资源"""
    d = request.get_json(silent=True) or request.form
    kw = (d.get("keyword") or "").strip()
    ptype = d.get("pan_type")
    if not kw:
        return jsonify({"ok": False, "error": "请输入片名"}), 400
    shares = KPAN.search_pan_shares(kw, ptype)
    return jsonify({"ok": True, "shares": shares})


# ---------------- 点115 (m.dian115.com / MADREEL) 影视资源库管理 ----------------

@app.route("/api/admin/dian115/status", methods=["GET"])
@_admin_required
def api_admin_dian115_status():
    """获取当前已配置的点115账号状态"""
    acc = KD115.get_dian115_account()
    return jsonify({"ok": True, "account": acc})


@app.route("/api/admin/dian115/save", methods=["POST"])
@_admin_required
def api_admin_dian115_save():
    """保存并验证点115账号 Cookie (自动 ECDSA P-256 安全握手)"""
    d = request.get_json(silent=True) or request.form
    cookie = (d.get("cookie") or "").strip()
    if not cookie:
        return jsonify({"ok": False, "error": "Cookie 或 Token 不能为空"}), 400
    res = KD115.save_dian115_cookie(cookie)
    return jsonify(res)


@app.route("/api/admin/dian115/delete", methods=["POST", "DELETE"])
@_admin_required
def api_admin_dian115_delete():
    """解绑/断开点115账号"""
    KD115.delete_dian115_account()
    return jsonify({"ok": True, "message": "点115账号已成功解绑"})


@app.route("/api/admin/dian115/search", methods=["GET", "POST"])
@_admin_required
def api_admin_dian115_search():
    """后台测试点115片名检索与 115 分享链接提取"""
    if request.method == "POST":
        d = request.get_json(silent=True) or request.form
        q = (d.get("query") or d.get("q") or "").strip()
    else:
        q = (request.args.get("query") or request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "搜索片名不能为空"}), 400
    res = KD115.search_dian115_for_movie(q)
    return jsonify({"ok": True, "query": q, "results": res})


# ---------------- 夸克/UC 4K 影视资源库 (玩偶哥哥 / WoGG 聚合) 管理 ----------------

@app.route("/api/admin/quark_hub/status", methods=["GET"])
@_admin_required
def api_admin_quark_hub_status():
    """获取当前夸克 4K 资源库配置与健康状态"""
    cfg = KQ.get_quark_hub_config()
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/admin/quark_hub/save", methods=["POST"])
@_admin_required
def api_admin_quark_hub_save():
    """保存并测试夸克影视站配置"""
    d = request.get_json(silent=True) or request.form
    site_url = (d.get("site_url") or "").strip()
    auto_sync = int(d.get("auto_sync", 1))
    res = KQ.save_quark_hub_config(site_url=site_url, auto_sync=auto_sync)
    return jsonify(res)


@app.route("/api/admin/quark_hub/search", methods=["GET", "POST"])
@_admin_required
def api_admin_quark_hub_search():
    """后台测试夸克 4K 片名检索与分享提取"""
    if request.method == "POST":
        d = request.get_json(silent=True) or request.form
        q = (d.get("query") or d.get("q") or "").strip()
    else:
        q = (request.args.get("query") or request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "搜索片名不能为空"}), 400
    res = KQ.search_quark_shares_for_movie(q)
    return jsonify({"ok": True, "query": q, "results": res})


if __name__ == "__main__":
    print("base:", pick_base())
    app.run(host="0.0.0.0", port=8077, threaded=True, debug=False)