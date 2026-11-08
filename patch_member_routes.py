#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把会员系统路由注入 app.py（幂等：已注入则跳过）"""
import io, os, re, sys

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
s = io.open(p, encoding="utf-8").read()

MARK = "# ================= KANJU_MEMBER_ROUTES ================="
if MARK in s:
    print("SKIP: 已注入过")
    sys.exit(0)

ROUTES = r'''
# ================= KANJU_MEMBER_ROUTES =================
# 会员系统：注册 / 登录 / 登出 / 我 / 收藏 / 历史 / 18+ 偏好 / 午夜剧场
import kanju_member as KM

COOKIE = "kj_sid"


def _sid():
    return request.cookies.get(COOKIE) or (request.headers.get("X-KJ-Sid") or "")


def _me():
    return KM.user_by_session(_sid())


def _need_login():
    return jsonify({"ok": False, "error": "请先登录", "code": "login_required"}), 401


def _set_sid(resp, sid, days=30):
    resp.set_cookie(COOKIE, sid, max_age=days * 86400, httponly=True, samesite="Lax")
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
    try:
        u = KM.create_user(acc, pwd, invite_code=inv, nickname=nick)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    sid = KM.new_session(u["id"])
    return _set_sid(jsonify({"ok": True, "user": KM.public_user(u), "sid": sid}), sid)


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or request.form
    acc = (d.get("account") or d.get("username") or "").strip()
    pwd = d.get("password") or ""
    u = KM.verify_user(acc, pwd)
    if not u:
        return jsonify({"ok": False, "error": "账号或密码错误"}), 401
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
    return send_from_directory(os.path.join(HERE, "static"), "midnight.html")


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
    for kw in ("午夜", "深夜", "情", "欲"):
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
'''

s = s.rstrip()
anchor = 'if __name__ == "__main__":'
i = s.find(anchor)
if i < 0:
    print("ERROR: 找不到 __main__ 锚点")
    sys.exit(1)
s = s[:i] + ROUTES.strip("\n") + "\n\n\n" + s[i:]
io.open(p, "w", encoding="utf-8").write(s)
print("OK 注入完成，app.py 现", len(s.splitlines()), "行")
