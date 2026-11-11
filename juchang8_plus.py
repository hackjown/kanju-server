#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剧场8 (juchang8.com) · 扩展系统与全站管理模块
本地实现：词元配额结算 / 邀请进度 / 片单 / 评论 / 公告 / 侧边栏与导航 / 站点配置 / 反馈 / 分享码 / 短信验证码 / 投屏配对。
与 juchang8_member 共用 juchang8.db，用户体系沿用其 users 表。
"""
import os, sqlite3, time, hashlib, hmac, secrets, json

HERE = os.path.dirname(os.path.abspath(__file__))
# 优先采用 juchang8.db，无缝兼容老版本 kanju.db
DB_PATH = os.path.join(HERE, "juchang8.db") if (
    os.path.exists(os.path.join(HERE, "juchang8.db")) or not os.path.exists(os.path.join(HERE, "kanju.db"))
) else os.path.join(HERE, "kanju.db")

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
  body TEXT, status TEXT DEFAULT 'pending', at INTEGER);
CREATE TABLE IF NOT EXISTS shares (
  code TEXT PRIMARY KEY, kind TEXT, target TEXT, uid INTEGER, at INTEGER);
CREATE TABLE IF NOT EXISTS sms_codes (
  phone TEXT PRIMARY KEY, code_hash TEXT, expires_at INTEGER, sent_at INTEGER);
CREATE TABLE IF NOT EXISTS cast_pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, code TEXT,
  device_name TEXT, confirmed INTEGER DEFAULT 0, at INTEGER);

-- 全站公告管理表 (SQLite 强持久化)
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  type TEXT DEFAULT 'popup', -- 'popup' 弹窗, 'banner' 顶部滚动横幅, 'sidebar' 侧栏公告
  version INTEGER DEFAULT 1,
  link_url TEXT DEFAULT '',
  is_active INTEGER DEFAULT 1,
  sort_order INTEGER DEFAULT 0,
  created_at INTEGER,
  updated_at INTEGER
);

-- 侧边栏与主导航管理表 (SQLite 强持久化)
CREATE TABLE IF NOT EXISTS sidebar_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  section TEXT DEFAULT 'nav', -- 'nav' 核心导航, 'widget' 侧栏卡片/挂件, 'link' 友情链接, 'recommend' 推荐专区
  title TEXT NOT NULL,
  subtitle TEXT DEFAULT '',
  icon TEXT DEFAULT '',
  url TEXT NOT NULL,
  target TEXT DEFAULT '_self', -- '_self' 或 '_blank'
  badge_text TEXT DEFAULT '',  -- 'HOT', '4K', 'NEW' 等
  sort_order INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 1,
  extra_data TEXT DEFAULT '',
  created_at INTEGER
);

-- 站点全局配置表 (KV 结构)
CREATE TABLE IF NOT EXISTS site_settings (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at INTEGER
);
"""

# 全站全局默认配置
DEFAULT_SETTINGS = {
    "site_name": "剧场8 · 看剧AI",
    "site_domain": "juchang8.com",
    "site_slogan": "海量 4K 影视搜索与智能发现引擎",
    "domain_bar_enabled": "1",
    "domain_bar_text": "📌 永久域名 juchang8.com，请收藏防走失 —— 当前域名无法访问时，用永久域名进入。",
    "register_open": "1",
    "require_invite": "0",
    "daily_grant": "20",
    "play_cost": "1",
    "play_cost_4k": "2",
    "invite_reward": "50",
    "default_player_mode": "wasm",
    "custom_footer_html": "<p>© 2026 剧场8 (juchang8.com) · 全球影视智能检索工具 · 内容源于网络公开接口</p>",
    "analytics_code": ""
}

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

    # 检查 feedback 表 status 字段
    fb_cols = [r[1] for r in c.execute("PRAGMA table_info(feedback)").fetchall()]
    if "status" not in fb_cols:
        c.execute("ALTER TABLE feedback ADD COLUMN status TEXT DEFAULT 'pending'")

    now = int(time.time())

    # 1. 种子全站公告 (如果为空)
    if not c.execute("SELECT 1 FROM announcements LIMIT 1").fetchone():
        c.execute("""INSERT INTO announcements(title, body, type, version, link_url, is_active, sort_order, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  ("剧场8 (juchang8.com) 永久域名正式启用",
                   "本站全面启用永久域名 juchang8.com，请各位影迷收藏防走失。原线路已全面平移升级为剧场8 官方4K极速原画通道，全网片源秒播！",
                   "popup", 4, "https://juchang8.com", 1, 1, now, now))
        c.execute("""INSERT INTO announcements(title, body, type, version, link_url, is_active, sort_order, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  ("全站 4K 臻彩超清专线与特斯拉车载影院极速就绪",
                   "全面支持杜比视界色彩校正、WebAudio 车载环绕 DSP 与纯软解极速逐帧渲染，大屏驻车即开即看。",
                   "banner", 3, "/car", 1, 2, now, now))
        c.execute("""INSERT INTO announcements(title, body, type, version, link_url, is_active, sort_order, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  ("词元配额与每日福利规则",
                   "每日首次打开赠送 20 词元；邀请好友注册双方各得 50 词元；支持创建个人公开/私密片单与好友共享。",
                   "sidebar", 2, "", 1, 3, now, now))

    # 2. 种子侧边栏与主导航项目 (如果为空)
    if not c.execute("SELECT 1 FROM sidebar_items LIMIT 1").fetchone():
        nav_items = [
            ("nav", "首页", "热门聚合与AI推荐", "🏠", "/", "_self", "", 1),
            ("nav", "电影", "院线热映与高分电影", "🎬", "/?channel=movie", "_self", "", 2),
            ("nav", "电视剧", "热门连续剧连载", "📺", "/?channel=series", "_self", "", 3),
            ("nav", "动漫", "新番连载与日韩精选", "🎭", "/?channel=anime", "_self", "", 4),
            ("nav", "综艺", "热播综艺真人秀", "🎤", "/?channel=variety", "_self", "", 5),
            ("nav", "短剧", "热门爽剧短剧集", "📱", "/?channel=short_drama", "_self", "HOT", 6),
            ("nav", "4K 专区", "蓝光原画极致视听", "🌟", "/?q=4K", "_self", "4K", 7),
            ("nav", "片单广场", "精选影迷策展片单", "📑", "/?view=playlists", "_self", "", 8),
            ("nav", "车机模式", "特斯拉大屏极速影院", "🚗", "/car", "_blank", "极速", 9),
            ("nav", "App 下载", "手机/平板快捷安装", "📱", "/download", "_blank", "", 10),
            ("widget", "永久域名 juchang8.com", "请收藏防走失，输入域名直达", "📌", "https://juchang8.com", "_blank", "官方", 1),
            ("widget", "官方 Telegram 交流群", "新剧抢先通知 / 备用发布页", "👥", "https://t.me/juchang8", "_blank", "交流", 2),
            ("widget", "在线求片与留言反馈", "遇到播放问题随时提交", "💬", "javascript:window.openFeedbackModal()", "_self", "反馈", 3),
            ("link", "剧场8 官方发布页", "最新可用线路发布", "⚡", "https://juchang8.com", "_blank", "", 1),
            ("link", "夸克网盘 4K 资源站", "官方转存与原画解析", "☁️", "https://pan.quark.cn", "_blank", "", 2),
            ("link", "115 网盘极速云", "大容量无损原盘转存", "💽", "https://115.com", "_blank", "", 3),
        ]
        for it in nav_items:
            c.execute("""INSERT INTO sidebar_items(section, title, subtitle, icon, url, target, badge_text, sort_order, is_active, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""", (*it, now))

    # 3. 种子全局配置 (如果未存在)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO site_settings(key, value, updated_at) VALUES (?, ?, ?)", (k, v, now))

    c.commit()
    c.close()


# ---------------- 公告管理系统 (SQLite 强持久化) ----------------

def announcements_list(include_inactive=False):
    c = _conn()
    if include_inactive:
        rows = c.execute("SELECT * FROM announcements ORDER BY sort_order ASC, id DESC").fetchall()
    else:
        rows = c.execute("SELECT * FROM announcements WHERE is_active=1 ORDER BY sort_order ASC, id DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]

def announcements_get(aid):
    c = _conn()
    r = c.execute("SELECT * FROM announcements WHERE id=?", (aid,)).fetchone()
    c.close()
    return dict(r) if r else None

def announcements_create(title, body, a_type='popup', version=None, link_url='', is_active=1, sort_order=0):
    c = _conn()
    now = int(time.time())
    if version is None:
        last = c.execute("SELECT MAX(version) FROM announcements").fetchone()[0] or 0
        version = last + 1
    cur = c.execute("""INSERT INTO announcements(title,body,type,version,link_url,is_active,sort_order,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (title, body, a_type, version, link_url, is_active, sort_order, now, now))
    c.commit()
    aid = cur.lastrowid
    c.close()
    return announcements_get(aid)

def announcements_update(aid, title=None, body=None, a_type=None, version=None, link_url=None, is_active=None, sort_order=None):
    old = announcements_get(aid)
    if not old:
        return None
    c = _conn()
    now = int(time.time())
    c.execute("""UPDATE announcements SET 
                 title=?, body=?, type=?, version=?, link_url=?, is_active=?, sort_order=?, updated_at=?
                 WHERE id=?""",
              (title if title is not None else old['title'],
               body if body is not None else old['body'],
               a_type if a_type is not None else old['type'],
               version if version is not None else old['version'],
               link_url if link_url is not None else old['link_url'],
               is_active if is_active is not None else old['is_active'],
               sort_order if sort_order is not None else old['sort_order'],
               now, aid))
    c.commit()
    c.close()
    return announcements_get(aid)

def announcements_toggle(aid):
    old = announcements_get(aid)
    if not old:
        return None
    new_active = 0 if old['is_active'] else 1
    return announcements_update(aid, is_active=new_active)

def announcements_delete(aid):
    c = _conn()
    c.execute("DELETE FROM announcements WHERE id=?", (aid,))
    c.commit()
    c.close()
    return True


# ---------------- 侧边栏与导航管理系统 ----------------

def sidebar_list(include_inactive=False, section=None):
    c = _conn()
    sql = "SELECT * FROM sidebar_items WHERE 1=1"
    params = []
    if not include_inactive:
        sql += " AND is_active=1"
    if section:
        sql += " AND section=?"
        params.append(section)
    sql += " ORDER BY sort_order ASC, id ASC"
    rows = c.execute(sql, params).fetchall()
    c.close()
    return [dict(r) for r in rows]

def sidebar_get(sid):
    c = _conn()
    r = c.execute("SELECT * FROM sidebar_items WHERE id=?", (sid,)).fetchone()
    c.close()
    return dict(r) if r else None

def sidebar_create(section='nav', title='', subtitle='', icon='', url='', target='_self', badge_text='', sort_order=0, is_active=1, extra_data=''):
    c = _conn()
    now = int(time.time())
    cur = c.execute("""INSERT INTO sidebar_items(section,title,subtitle,icon,url,target,badge_text,sort_order,is_active,extra_data,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (section, title, subtitle, icon, url, target, badge_text, sort_order, is_active, extra_data, now))
    c.commit()
    sid = cur.lastrowid
    c.close()
    return sidebar_get(sid)

def sidebar_update(sid, section=None, title=None, subtitle=None, icon=None, url=None, target=None, badge_text=None, sort_order=None, is_active=None, extra_data=None):
    old = sidebar_get(sid)
    if not old:
        return None
    c = _conn()
    c.execute("""UPDATE sidebar_items SET 
                 section=?, title=?, subtitle=?, icon=?, url=?, target=?, badge_text=?, sort_order=?, is_active=?, extra_data=?
                 WHERE id=?""",
              (section if section is not None else old['section'],
               title if title is not None else old['title'],
               subtitle if subtitle is not None else old['subtitle'],
               icon if icon is not None else old['icon'],
               url if url is not None else old['url'],
               target if target is not None else old['target'],
               badge_text if badge_text is not None else old['badge_text'],
               sort_order if sort_order is not None else old['sort_order'],
               is_active if is_active is not None else old['is_active'],
               extra_data if extra_data is not None else old['extra_data'],
               sid))
    c.commit()
    c.close()
    return sidebar_get(sid)

def sidebar_toggle(sid):
    old = sidebar_get(sid)
    if not old:
        return None
    new_active = 0 if old['is_active'] else 1
    return sidebar_update(sid, is_active=new_active)

def sidebar_delete(sid):
    c = _conn()
    c.execute("DELETE FROM sidebar_items WHERE id=?", (sid,))
    c.commit()
    c.close()
    return True


# ---------------- 全站全局配置系统 ----------------

def settings_get_all():
    c = _conn()
    rows = c.execute("SELECT key, value FROM site_settings").fetchall()
    c.close()
    res = dict(DEFAULT_SETTINGS)
    for r in rows:
        res[r["key"]] = r["value"]
    return res

def settings_get(key, default=None):
    c = _conn()
    r = c.execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    c.close()
    if r is not None:
        return r["value"]
    return DEFAULT_SETTINGS.get(key, default)

def settings_set(key, value):
    c = _conn()
    now = int(time.time())
    c.execute("INSERT OR REPLACE INTO site_settings(key, value, updated_at) VALUES(?,?,?)",
              (key, str(value), now))
    c.commit()
    c.close()
    return value

def settings_set_many(data):
    c = _conn()
    now = int(time.time())
    for k, v in data.items():
        c.execute("INSERT OR REPLACE INTO site_settings(key, value, updated_at) VALUES(?,?,?)",
                  (k, str(v), now))
    c.commit()
    c.close()
    return settings_get_all()


# ---------------- 反馈与留言管理 ----------------

def feedback_list(limit=50, offset=0):
    c = _conn()
    cols = [r[1] for r in c.execute("PRAGMA table_info(feedback)").fetchall()]
    if "status" not in cols:
        c.execute("ALTER TABLE feedback ADD COLUMN status TEXT DEFAULT 'pending'")
        c.commit()
    rows = c.execute("""SELECT f.*, u.account AS user_account, u.nickname 
                        FROM feedback f LEFT JOIN users u ON f.uid=u.id 
                        ORDER BY f.id DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    total = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    c.close()
    items = []
    for r in rows:
        d = dict(r)
        d["content"] = d.get("body") or ""
        d["created_at"] = d.get("at") or 0
        items.append(d)
    return {"items": items, "total": total}

def feedback_delete(fid):
    c = _conn()
    c.execute("DELETE FROM feedback WHERE id=?", (fid,))
    c.commit()
    c.close()
    return True

def feedback_status(fid, status='resolved'):
    c = _conn()
    c.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))
    c.commit()
    c.close()
    return True


# 保持 ANNOUNCEMENTS 兼容代理
class _AnnouncementsProxy(list):
    def __iter__(self):
        return iter(announcements_list())
    def __len__(self):
        return len(announcements_list())
    def __getitem__(self, idx):
        return announcements_list()[idx]
    def insert(self, idx, item):
        announcements_create(item.get('title',''), item.get('body',''), item.get('type','popup'))

ANNOUNCEMENTS = _AnnouncementsProxy()


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

