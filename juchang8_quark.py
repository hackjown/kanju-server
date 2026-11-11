# -*- coding: utf-8 -*-
"""
kanju_quark.py - 看剧AI 夸克/UC 4K 影视资源聚合引擎 (玩偶哥哥 / WoGG / 夸克生态)
功能特性：
1. 自动对接全网最大的夸克 4K 影视资源发布阵地（玩偶哥哥 / WoGG 及镜像站）；
2. 支持海量电影、热播电视剧、动漫、纪录片一键检索与 4K 分享链接毫秒级提取；
3. 智能解析并提取 夸克 (pan.quark.cn) 与 UC 网盘 (drive.uc.cn) 原画分享链接及密码；
4. 深度契合网页端色彩标准（优先识别 4K SDR / MAXPLUS / 60FPS，标注色彩兼容性）；
5. 自动沉淀入本地索引库 (pan_shares_index)，无缝对接夸克/UC转存与 4K 直链提取；
6. 提供管理后台配置面板与片名检索测试接口。
"""

import os
import re
import time
import json
import sqlite3
import urllib.parse
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "juchang8.db") if (
    os.path.exists(os.path.join(HERE, "juchang8.db")) or not os.path.exists(os.path.join(HERE, "kanju.db"))
) else os.path.join(HERE, "kanju.db")

DEFAULT_SITE_URL = "https://www.wogg.net"
MIRROR_SITES = [
    "https://www.wogg.net",
    "https://wogg.xx",
    "https://wogg.link"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
}


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_quark_hub_db():
    """初始化夸克影视资源站配置表"""
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS quark_hub_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_url TEXT NOT NULL DEFAULT 'https://www.wogg.net',
            site_name TEXT DEFAULT '玩偶哥哥 4K 共享站',
            status TEXT DEFAULT 'active',
            auto_sync INTEGER DEFAULT 1,
            last_checked INTEGER DEFAULT 0,
            updated_at INTEGER NOT NULL
        )
        """)
        # 写入默认初始配置
        row = conn.execute("SELECT id FROM quark_hub_config LIMIT 1").fetchone()
        if not row:
            now = int(time.time())
            conn.execute("""
            INSERT INTO quark_hub_config (site_url, site_name, status, auto_sync, updated_at)
            VALUES (?, '玩偶哥哥 (WoGG) 4K库', 'active', 1, ?)
            """, (DEFAULT_SITE_URL, now))
        conn.commit()


def get_quark_hub_config():
    """获取当前夸克资源库对接配置"""
    init_quark_hub_db()
    with _db() as conn:
        row = conn.execute("SELECT * FROM quark_hub_config ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return dict(row)
        return {
            "site_url": DEFAULT_SITE_URL,
            "site_name": "玩偶哥哥 (WoGG) 4K库",
            "status": "active",
            "auto_sync": 1
        }


def save_quark_hub_config(site_url="", auto_sync=1):
    """保存并验证夸克资源站对接配置"""
    init_quark_hub_db()
    site_url = (site_url or "").strip().rstrip("/")
    if not site_url:
        site_url = DEFAULT_SITE_URL

    if not site_url.startswith("http"):
        site_url = "https://" + site_url

    # 测试站点连通性
    status = "active"
    test_msg = "连接成功"
    try:
        r = requests.get(site_url, headers=HEADERS, timeout=6)
        if r.status_code >= 400:
            status = "error"
            test_msg = f"站点响应状态码 {r.status_code}"
    except Exception as e:
        status = "error"
        test_msg = f"站点连接异常: {e}"

    now = int(time.time())
    with _db() as conn:
        conn.execute("DELETE FROM quark_hub_config")
        conn.execute("""
        INSERT INTO quark_hub_config (site_url, site_name, status, auto_sync, last_checked, updated_at)
        VALUES (?, '玩偶哥哥 (WoGG) 4K库', ?, ?, ?, ?)
        """, (site_url, status, auto_sync, now, now))
        conn.commit()

    return {
        "ok": (status == "active"),
        "site_url": site_url,
        "status": status,
        "message": f"夸克资源站配置已保存 ({test_msg})"
    }


def clean_keyword_for_search(title):
    """提炼出适合搜索的核心片名"""
    if not title:
        return ""
    # 移除年份 (如 2026, 2025)
    t = re.sub(r'\(?\b(19\d\d|20\d\d)\b\)?', '', title)
    # 移除常见后缀标签
    t = re.sub(r'第[0-9一二三四五六七八九十]+季', '', t)
    t = re.sub(r'(?:4K|1080P|2160P|原盘|蓝光|SDR|HDR|特别版|未删减|抢先版|超清|国语|双语)', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[·\-_\[\]\(\)\{\}【】（）]', ' ', t)
    t = t.strip()
    return t if len(t) >= 2 else title.strip()


def search_quark_shares_for_movie(title):
    """供上层影视聚合调用的统一入口：
    按片名自动检索夸克 4K / UC 网盘资源，解锁并去重沉淀到本地 pan_shares_index
    """
    cfg = get_quark_hub_config()
    if not cfg or cfg.get("status") == "disabled":
        return []

    site_url = (cfg.get("site_url") or DEFAULT_SITE_URL).rstrip("/")
    raw_title = title.strip()
    if not raw_title:
        return []

    clean_kw = clean_keyword_for_search(raw_title)
    search_keywords = [raw_title]
    if clean_kw and clean_kw != raw_title:
        search_keywords.append(clean_kw)

    extracted_shares = []
    seen_urls = set()
    now = int(time.time())

    for kw in search_keywords:
        search_url = f"{site_url}/index.php/vodsearch/{urllib.parse.quote(kw)}-------------/"
        try:
            r = requests.get(search_url, headers=HEADERS, timeout=7)
            if r.status_code != 200:
                continue

            # 提取详情页链接
            matches = re.findall(r'href=[\"\'](/voddetail/[^\"\']+)[\"\'][^>]*>(.*?)</a>', r.text, re.DOTALL)
            seen_hrefs = set()
            detail_links = []
            for href, raw_t in matches:
                t = re.sub(r'<[^>]+>', '', raw_t).strip()
                if not t or t in ('完结', '查看资源') or t.startswith('全') or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                detail_links.append((href, t))

            if not detail_links:
                continue

            # 处理前 2 个最匹配的影视详情页
            for href, item_title in detail_links[:2]:
                det_url = site_url + href
                try:
                    det_r = requests.get(det_url, headers=HEADERS, timeout=7)
                    if det_r.status_code != 200:
                        continue

                    # 提取该条目下的所有网盘分享行
                    rows = re.findall(r'<div class=\"module-row-info\">.*?</div>\s*</div>', det_r.text, re.DOTALL)
                    for row in rows:
                        title_m = re.search(r'<h4>(.*?)</h4>', row)
                        share_text = title_m.group(1).strip() if title_m else item_title

                        # 1. 匹配夸克网盘链接
                        quark_m = re.search(r'https?://(?:pan|drive)\.quark\.cn/s/([a-zA-Z0-9]+)', row)
                        # 2. 匹配 UC 网盘链接
                        uc_m = re.search(r'https?://drive\.uc\.cn/s/([a-zA-Z0-9]+)', row)

                        ptype = ""
                        pwd_id = ""
                        full_url = ""

                        if quark_m:
                            ptype = "quark"
                            pwd_id = quark_m.group(1)
                            full_url = f"https://pan.quark.cn/s/{pwd_id}"
                        elif uc_m:
                            ptype = "uc"
                            pwd_id = uc_m.group(1)
                            full_url = f"https://drive.uc.cn/s/{pwd_id}"

                        if not ptype or not pwd_id or full_url in seen_urls:
                            continue

                        seen_urls.add(full_url)

                        # 提取提取码（若有）
                        passcode = ""
                        pwd_match = re.search(r'(?:pwd|password|code|密码)[=：:\s]*([a-zA-Z0-9]{4})', row)
                        if pwd_match:
                            passcode = pwd_match.group(1)

                        # 智能打标签与画质识别
                        upper_text = (share_text + " " + item_title).upper()
                        quality_tag = "4K原画"
                        if "SDR" in upper_text or "MAXPLUS" in upper_text:
                            quality_tag = "4K SDR色彩真实"
                        elif "HDR10" in upper_text or "HDR" in upper_text:
                            quality_tag = "4K HDR10"
                        elif "60FPS" in upper_text or "60帧" in upper_text:
                            quality_tag = "4K 60帧原画"
                        elif "臻彩" in upper_text:
                            quality_tag = "4K臻彩原画"

                        share_title = f"{raw_title} 4K · {share_text}"

                        # 存入本地索引表 pan_shares_index（去重防重复插入）
                        with _db() as conn:
                            exists = conn.execute("SELECT id FROM pan_shares_index WHERE pwd_id=?", (pwd_id,)).fetchone()
                            if not exists:
                                conn.execute("""
                                INSERT INTO pan_shares_index (keyword, pan_type, share_url, pwd_id, passcode, title, quality_tag, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """, (raw_title, ptype, full_url, pwd_id, passcode, share_title, quality_tag, now))
                                conn.commit()

                        extracted_shares.append({
                            "keyword": raw_title,
                            "pan_type": ptype,
                            "share_url": full_url,
                            "pwd_id": pwd_id,
                            "passcode": passcode,
                            "title": share_title,
                            "quality_tag": quality_tag
                        })

                except Exception as e:
                    print(f"[QuarkHub] Detail fetch failed ({href}): {e}")

            if extracted_shares:
                break

        except Exception as e:
            print(f"[QuarkHub] Search failed ({kw}): {e}")

    return extracted_shares


init_quark_hub_db()
