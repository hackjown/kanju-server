#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看剧AI 复刻站 —— 扩展系统模块（对照 kanju45.com 补齐）
本地实现：词元配额结算 / 邀请进度 / 片单 / 评论 / 公告 / 反馈 / 分享码 / 短信验证码 / 投屏配对。
与 kanju_member 共用 kanju.db，用户体系沿用其 users 表。
"""
import os, sqlite3, time, hashlib, hmac, secrets, json

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "kanju.db")

PLAY_COST = 1          # 播放一集消耗词元
PLAY_COST_4K = 2       # 4K 线路消耗
DAILY_GRANT = 20       # 每日登录赠送
INVITE_REWARD = 50     # 邀请成功双方各得
LEVEL_GROWTH = [0, 100, 300, 600, 1000, 1500, 2100, 2800, 3600, 4500]  # level 1..10 门槛

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, delta INTEGER,
  reason TEXT, balance INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS daily_grant (
  uid INTEGER, day TEXT, PRIMARY KEY (uid, day));
CREATE TABLE IF NOT EXISTS invite_rel (
  id INTEGER PRIMARY KEY AUTOINCREMENT, inviter INTEGER, invitee INTEGER,
  reward INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS playlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, descr TEXT,
  is_public INTEGER DEFAULT 1, pwd_hash TEXT, pwd_salt TEXT,
  share_code TEXT UNIQUE, created_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS playlist_items (
  playlist_id INTEGER, cid TEXT, title TEXT, poster TEXT, added_at INTEGER,
  PRIMARY KEY (playlist_id, cid));
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, uid INTEGER,
  nickname TEXT, body TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, contact TEXT,
  body TEXT, at INTEGER);
CREATE TABLE IF NOT EXISTS shares (
  code TEXT PRIMARY KEY, kind TEXT, target TEXT, uid INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS sms_codes (
  phone TEXT PRIMARY KEY, code_hash TEXT, expires_at INTEGER, sent_at INTEGER);
CREATE TABLE IF NOT EXISTS cast_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, code TEXT,
  device_name TEXT, confirmed INTEGER DEFAULT 0, at INTEGER);
"""

ANNOUNCEMENTS = [
    {"id": "2026-11-01", "version": 3, "title": "永久域名提醒",
     "body": "本站永久域名 kanju.ai，请收藏防走失。若当前域名无法访问，请通过永久域名进入。"},
    {"id": "2026-10-26", "version": 2, "title": "片单功能上线",
     "body": "现在可以创建自己的片单、逛片单广场，还可以给片单设密码并分享给朋友。"},
    {"id": "2026-10-20", "version": 1, "title": "词元规则说明",
     "body": "播放每集消耗 1 词元（4K 线路 2 词元）；每日首次打开赠送 20 词元；邀请好友注册双方各得 50 词元。"},
]


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_plus():
    c = _conn()
    c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "invite_own" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN invite_own TEXT")
    if "phone" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    c.commit()
    c.close()


def level_of(growth):
    lv = 1
    for i, g in enumerate(LEVEL_GROWTH):
        if growth >= g:
            lv = i + 1
    return lv


def _user(c, uid):
    return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def _apply(c, uid, delta, reason):
    u = _user(c, uid)
    bal = max(0, (u["tokens"] or 0) + delta)
    growth = (u["growth"] or 0) + (2 if reason == "play" and delta < 0 else 0)
    c.execute("UPDATE users SET tokens=?, growth=?, level=? WHERE id=?",
              (bal, growth, level_of(growth), uid))
    c.execute("INSERT INTO quota_log(uid,delta,reason,balance,at) VALUES(?,?,?,?,?)",
              (uid, delta, reason, bal, int(time.time())))
    return bal


def quota_view(uid):
    c = _conn()
    day = time.strftime("%Y-%m-%d")
    granted = False
    if not c.execute("SELECT 1 FROM daily_grant WHERE uid=? AND day=?", (uid, day)).fetchone():
        c.execute("INSERT INTO daily_grant(uid,day) VALUES(?,?)", (uid, day))
        _apply(c, uid, DAILY_GRANT, "daily")
        granted = True
    c.commit()
    u = _user(c, uid)
    log = [dict(r) for r in c.execute(
        "SELECT delta,reason,balance,at FROM quota_log WHERE uid=? ORDER BY id DESC LIMIT 20",
        (uid,)).fetchall()]
    c.close()
    growth = u["growth"] or 0
    lv = level_of(growth)
    nxt = LEVEL_GROWTH[lv] if lv < len(LEVEL_GROWTH) else None
    return {"tokens": u["tokens"], "level": lv, "growth": growth,
            "next_level_growth": nxt, "daily_granted": granted,
            "play_cost": PLAY_COST, "play_cost_4k": PLAY_COST_4K, "log": log}


def quota_consume(uid, is4k=False):
    cost = PLAY_COST_4K if is4k else PLAY_COST
    c = _conn()
    u = _user(c, uid)
    if (u["tokens"] or 0) < cost:
        c.close()
        return None, "词元不足（当前 %d，需要 %d）。每日赠送 20，邀请好友可得 50。" % (u["tokens"] or 0, cost)
    bal = _apply(c, uid, -cost, "play4k" if is4k else "play")
    c.commit()
    c.close()
    return {"cost": cost, "balance": bal}, None


def own_code(uid):
    c = _conn()
    u = _user(c, uid)
    code = u["invite_own"]
    if not code:
        code = "KJ" + secrets.token_hex(3).upper()
        c.execute("UPDATE users SET invite_own=? WHERE id=?", (code, uid))
        c.commit()
    c.close()
    return code


def invite_progress(uid):
    code = own_code(uid)
    c = _conn()
    rows = [dict(r) for r in c.execute(
        "SELECT invitee, reward, at FROM invite_rel WHERE inviter=? ORDER BY id DESC",
        (uid,)).fetchall()]
    c.close()
    return {"own_code": code, "invited": len(rows),
            "earned": sum(r["reward"] for r in rows), "items": rows,
            "reward": INVITE_REWARD}


def invite_bind(new_uid, code):
    """注册成功后绑定邀请关系：code 可以是系统码（无奖励）或某用户的专属码。"""
    code = (code or "").strip().upper()
    if not code:
        return
    c = _conn()
    inv = c.execute("SELECT id FROM users WHERE invite_own=? AND id<>?", (code, new_uid)).fetchone()
    if inv:
        c.execute("INSERT INTO invite_rel(inviter,invitee,reward,at) VALUES(?,?,?,?)",
                  (inv["id"], new_uid, INVITE_REWARD, int(time.time())))
        _apply(c, inv["id"], INVITE_REWARD, "invite")
        _apply(c, new_uid, INVITE_REWARD, "invited")
        c.commit()
    c.close()


# ---------------- 片单 ----------------

def _pl_hash(pwd, salt=None):
    salt = salt or secrets.token_hex(8)
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 60000).hex(), salt


def playlist_create(uid, name, descr, is_public, password):
    ph = ps = None
    if password:
        ph, ps = _pl_hash(password)
    code = secrets.token_urlsafe(8)
    now = int(time.time())
    c = _conn()
    cur = c.execute(
        "INSERT INTO playlists(uid,name,descr,is_public,pwd_hash,pwd_salt,share_code,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, name, descr or "", 1 if is_public else 0, ph, ps, code, now, now))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid, code


def _pl_row(c, r, with_items=False):
    n = c.execute("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (r["id"],)).fetchone()[0]
    cover = c.execute("SELECT poster FROM playlist_items WHERE playlist_id=? AND poster<>'' "
                      "ORDER BY added_at LIMIT 1", (r["id"],)).fetchone()
    d = {"id": r["id"], "uid": r["uid"], "name": r["name"], "descr": r["descr"],
         "is_public": bool(r["is_public"]), "has_password": bool(r["pwd_hash"]),
         "share_code": r["share_code"], "count": n,
         "cover": cover[0] if cover else None,
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if with_items:
        d["items"] = [dict(x) for x in c.execute(
            "SELECT cid,title,poster,added_at FROM playlist_items WHERE playlist_id=? ORDER BY added_at",
            (r["id"],)).fetchall()]
    return d


def playlist_mine(uid):
    c = _conn()
    out = [_pl_row(c, r) for r in c.execute(
        "SELECT * FROM playlists WHERE uid=? ORDER BY updated_at DESC", (uid,)).fetchall()]
    c.close()
    return out


def playlist_explore(limit=30):
    c = _conn()
    out = [_pl_row(c, r) for r in c.execute(
        "SELECT * FROM playlists WHERE is_public=1 ORDER BY updated_at DESC LIMIT ?",
        (limit,)).fetchall()]
    c.close()
    return out


def playlist_get(pid, password=None):
    c = _conn()
    r = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r:
        c.close()
        return None, "片单不存在"
    if r["pwd_hash"]:
        if not password:
            c.close()
            return {"locked": True, "id": pid, "name": r["name"]}, None
        h, _ = _pl_hash(password, r["pwd_salt"])
        if not hmac.compare_digest(h, r["pwd_hash"]):
            c.close()
            return None, "片单密码不正确"
    d = _pl_row(c, r, with_items=True)
    c.close()
    return d, None


def playlist_by_share(code):
    c = _conn()
    r = c.execute("SELECT id FROM playlists WHERE share_code=?", (code,)).fetchone()
    c.close()
    return r["id"] if r else None


def playlist_add_item(pid, uid, cid, title, poster):
    c = _conn()
    r = c.execute("SELECT uid FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r or r["uid"] != uid:
        c.close()
        return False, "只能修改自己的片单"
    c.execute("INSERT OR REPLACE INTO playlist_items(playlist_id,cid,title,poster,added_at)"
              " VALUES(?,?,?,?,?)", (pid, cid, title or "", poster or "", int(time.time())))
    c.execute("UPDATE playlists SET updated_at=? WHERE id=?", (int(time.time()), pid))
    c.commit()
    c.close()
    return True, None


def playlist_del_item(pid, uid, cid):
    c = _conn()
    r = c.execute("SELECT uid FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r or r["uid"] != uid:
        c.close()
        return False, "只能修改自己的片单"
    c.execute("DELETE FROM playlist_items WHERE playlist_id=? AND cid=?", (pid, cid))
    c.commit()
    c.close()
    return True, None


def playlist_delete(pid, uid):
    c = _conn()
    r = c.execute("SELECT uid FROM playlists WHERE id=?", (pid,)).fetchone()
    if not r or r["uid"] != uid:
        c.close()
        return False, "只能删除自己的片单"
    c.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
    c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    c.commit()
    c.close()
    return True, None


# ---------------- 评论 / 反馈 / 分享 / 短信 / 投屏 ----------------

def comments_list(cid, limit=50):
    c = _conn()
    out = [dict(r) for r in c.execute(
        "SELECT id,uid,nickname,body,at FROM comments WHERE cid=? ORDER BY id DESC LIMIT ?",
        (cid, limit)).fetchall()]
    c.close()
    return out


def comment_add(cid, uid, nickname, body):
    c = _conn()
    cur = c.execute("INSERT INTO comments(cid,uid,nickname,body,at) VALUES(?,?,?,?,?)",
                    (cid, uid, nickname, body, int(time.time())))
    c.commit()
    i = cur.lastrowid
    c.close()
    return i


def comment_del(cid_, uid):
    c = _conn()
    cur = c.execute("DELETE FROM comments WHERE id=? AND uid=?", (cid_, uid))
    c.commit()
    n = cur.rowcount
    c.close()
    return n > 0


def feedback_add(uid, contact, body):
    c = _conn()
    c.execute("INSERT INTO feedback(uid,contact,body,at) VALUES(?,?,?,?)",
              (uid, contact or "", body, int(time.time())))
    c.commit()
    c.close()


def share_create(uid, kind, target):
    code = secrets.token_urlsafe(9)
    c = _conn()
    c.execute("INSERT INTO shares(code,kind,target,uid,at) VALUES(?,?,?,?,?)",
              (code, kind, target, uid, int(time.time())))
    c.commit()
    c.close()
    return code


def share_resolve(code):
    c = _conn()
    r = c.execute("SELECT kind,target FROM shares WHERE code=?", (code,)).fetchone()
    c.close()
    return dict(r) if r else None


def sms_send(phone):
    code = "%06d" % secrets.randbelow(1000000)
    h = hashlib.sha256(code.encode()).hexdigest()
    c = _conn()
    c.execute("INSERT OR REPLACE INTO sms_codes(phone,code_hash,expires_at,sent_at)"
              " VALUES(?,?,?,?)", (phone, h, int(time.time()) + 300, int(time.time())))
    c.commit()
    c.close()
    return code


def sms_verify(phone, code):
    c = _conn()
    r = c.execute("SELECT * FROM sms_codes WHERE phone=?", (phone,)).fetchone()
    ok = bool(r and r["expires_at"] > int(time.time()) and
              hmac.compare_digest(hashlib.sha256((code or "").encode()).hexdigest(), r["code_hash"]))
    if ok:
        c.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
        c.commit()
    c.close()
    return ok


def cast_pair_create(uid):
    code = "%06d" % secrets.randbelow(1000000)
    c = _conn()
    cur = c.execute("INSERT INTO cast_pairs(uid,code,at) VALUES(?,?,?)",
                    (uid, code, int(time.time())))
    c.commit()
    i = cur.lastrowid
    c.close()
    return {"id": i, "code": code, "expires_in": 600}


def cast_confirm(code, device_name):
    c = _conn()
    r = c.execute("SELECT * FROM cast_pairs WHERE code=? AND confirmed=0 AND at>?",
                  (code, int(time.time()) - 600)).fetchone()
    if not r:
        c.close()
        return None
    c.execute("UPDATE cast_pairs SET confirmed=1, device_name=? WHERE id=?",
              (device_name or "TV", r["id"]))
    c.commit()
    d = {"id": r["id"], "uid": r["uid"], "device_name": device_name or "TV"}
    c.close()
    return d


def cast_devices(uid):
    c = _conn()
    out = [dict(r) for r in c.execute(
        "SELECT id,device_name,at FROM cast_pairs WHERE uid=? AND confirmed=1 ORDER BY id DESC",
        (uid,)).fetchall()]
    c.close()
    return out


def invite_code_owner(code):
    code = (code or "").strip().upper()
    if not code:
        return None
    c = _conn()
    r = c.execute("SELECT id FROM users WHERE invite_own=?", (code,)).fetchone()
    c.close()
    return r["id"] if r else None


def set_phone(uid, phone):
    c = _conn()
    c.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
    c.commit()
    c.close()


# ---------------- 片头/片尾标记（众包，对照原站 skip intro/outro 机制） ----------------

def _init_skip(c):
    c.execute("CREATE TABLE IF NOT EXISTS skip_marks ("
              "token TEXT, uid INTEGER, intro_end REAL, outro_start REAL,"
              " duration REAL, at INTEGER, PRIMARY KEY (token, uid))")


def skip_set(token, uid, intro_end, outro_start, duration):
    c = _conn()
    _init_skip(c)
    c.execute("INSERT OR REPLACE INTO skip_marks(token,uid,intro_end,outro_start,duration,at)"
              " VALUES(?,?,?,?,?,?)",
              (token, uid, intro_end, outro_start, duration, int(time.time())))
    c.commit()
    c.close()
    return skip_get(token)


def skip_get(token):
    c = _conn()
    _init_skip(c)
    rows = c.execute("SELECT intro_end, outro_start, duration FROM skip_marks WHERE token=?",
                     (token,)).fetchall()
    c.close()
    if not rows:
        return None

    def med(key):
        vals = sorted(r[key] for r in rows if r[key])
        return vals[len(vals) // 2] if vals else None

    return {"intro_end": med("intro_end"), "outro_start": med("outro_start"),
            "duration": med("duration"), "votes": len(rows)}


# 别名映射以兼容不同调用处
invite_my = invite_progress
playlist_my = playlist_mine

