#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看剧AI 复刻站 —— 本地多源采集调度引擎 (MacCMS v10 + 4K/蓝光专区)
支持：
1. 苹果CMS v10 JSON API 采集标准 (ac=detail, ac=list)
2. 内置 4K/蓝光 高清采集源 + 极速 1080P 源
3. 并发搜索聚合、标题与剧集智能去重、测速与按画质权重排序
4. 4K 播放线路本地防重限流缓存 (TTL 2小时)
5. 自定义私有频道与分类映射绑定 (SQLite 存储)
"""

import os
import time
import json
import sqlite3
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "kanju.db")

# 默认内置优质采集源列表 (涵盖 4K 专区、蓝光专区、极速 1080P)
DEFAULT_SOURCES = [
    {
        "key": "tiankong_4k",
        "name": "天空4K资源",
        "api_url": "https://maccms.tiankongapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 1,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 100,
        "category_mapping": "{}"
    },
    {
        "key": "liangzi_4k",
        "name": "量子4K资源",
        "api_url": "https://cj.lziapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 1,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 95,
        "category_mapping": "{}"
    },
    {
        "key": "feifan_4k",
        "name": "非凡4K高清",
        "api_url": "https://cj.ffzyapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 1,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 90,
        "category_mapping": "{}"
    },
    {
        "key": "baofeng_4k",
        "name": "暴风4K蓝光",
        "api_url": "https://bfzyapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 1,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 85,
        "category_mapping": "{}"
    },
    {
        "key": "hongniu",
        "name": "红牛极速源",
        "api_url": "https://www.hongniuzy2.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 0,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 80,
        "category_mapping": "{}"
    },
    {
        "key": "jisu",
        "name": "极速快线源",
        "api_url": "https://jszyapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 0,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 75,
        "category_mapping": "{}"
    },
    {
        "key": "suoni",
        "name": "索尼高清源",
        "api_url": "https://suoniapi.com/api.php/provide/vod/",
        "api_type": "maccms_json",
        "is_4k": 0,
        "is_enabled": 1,
        "is_custom": 0,
        "weight": 70,
        "category_mapping": "{}"
    }
]

# 本地解析缓存 (内存字典 + TTL)
_RESOLVE_CACHE = {}  # {cache_key: {"data": {...}, "expire_at": int}}


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_collector_db():
    """初始化采集源表结构及默认内置源"""
    with _db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS collector_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE,
            name TEXT,
            api_url TEXT,
            api_type TEXT DEFAULT 'maccms_json',
            is_4k INTEGER DEFAULT 0,
            is_enabled INTEGER DEFAULT 1,
            is_custom INTEGER DEFAULT 0,
            category_mapping TEXT DEFAULT '{}',
            weight INTEGER DEFAULT 50,
            latency_ms INTEGER DEFAULT -1,
            last_tested_at INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0
        )
        """)
        # 补齐默认源
        now = int(time.time())
        for s in DEFAULT_SOURCES:
            conn.execute("""
            INSERT OR IGNORE INTO collector_sources 
            (key, name, api_url, api_type, is_4k, is_enabled, is_custom, weight, category_mapping, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (s["key"], s["name"], s["api_url"], s["api_type"], s["is_4k"],
                  s["is_enabled"], s["is_custom"], s["weight"], s["category_mapping"], now))
        conn.commit()


def list_sources(include_disabled=True):
    """获取所有配置的采集源列表"""
    init_collector_db()
    with _db() as conn:
        if include_disabled:
            rows = conn.execute("SELECT * FROM collector_sources ORDER BY weight DESC, id ASC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM collector_sources WHERE is_enabled=1 ORDER BY weight DESC, id ASC").fetchall()
        return [dict(r) for r in rows]


def add_source(name, api_url, is_4k=0, is_custom=1, weight=50, category_mapping="{}"):
    """添加采集源（支持自定义 4K 或 私有频道采集）"""
    init_collector_db()
    key = "src_" + hashlib.md5(f"{api_url}_{time.time()}".encode()).hexdigest()[:8]
    now = int(time.time())
    with _db() as conn:
        conn.execute("""
        INSERT INTO collector_sources 
        (key, name, api_url, api_type, is_4k, is_enabled, is_custom, weight, category_mapping, created_at)
        VALUES (?, ?, ?, 'maccms_json', ?, 1, ?, ?, ?, ?)
        """, (key, name, api_url.strip(), 1 if is_4k else 0, is_custom, weight, category_mapping, now))
        conn.commit()
    return key


def update_source(source_id, name, api_url, is_4k, weight, category_mapping="{}"):
    """更新采集源配置"""
    with _db() as conn:
        conn.execute("""
        UPDATE collector_sources 
        SET name=?, api_url=?, is_4k=?, weight=?, category_mapping=?
        WHERE id=?
        """, (name, api_url.strip(), 1 if is_4k else 0, weight, category_mapping, source_id))
        conn.commit()
    return True


def toggle_source(source_id, enabled=None):
    """启用/停用采集源"""
    with _db() as conn:
        if enabled is None:
            conn.execute("UPDATE collector_sources SET is_enabled = CASE WHEN is_enabled=1 THEN 0 ELSE 1 END WHERE id=?", (source_id,))
        else:
            conn.execute("UPDATE collector_sources SET is_enabled = ? WHERE id=?", (1 if enabled else 0, source_id))
        conn.commit()
    return True


def delete_source(source_id):
    """删除采集源（仅自定义采集源可删）"""
    with _db() as conn:
        conn.execute("DELETE FROM collector_sources WHERE id=? AND is_custom=1", (source_id,))
        conn.commit()
    return True


def test_source_latency(source_id):
    """测试指定采集源的网络延迟"""
    with _db() as conn:
        row = conn.execute("SELECT * FROM collector_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return -1
        url = row["api_url"]
    
    # 构造轻量级测试请求：拉取第1页列表
    test_url = url.rstrip("/")
    sep = "&" if "?" in test_url else "?"
    test_url = f"{test_url}{sep}ac=list&pagesize=1"
    
    t0 = time.time()
    latency = -1
    try:
        r = requests.get(test_url, timeout=5, headers={"User-Agent": "Mozilla/5.0 KanjuCollector/1.0"})
        if r.status_code == 200:
            latency = int((time.time() - t0) * 1000)
    except Exception:
        latency = -1
    
    now = int(time.time())
    with _db() as conn:
        conn.execute("UPDATE collector_sources SET latency_ms=?, last_tested_at=? WHERE id=?", (latency, now, source_id))
        conn.commit()
    return latency


def parse_maccms_play_url(vod_play_url_raw, vod_play_from_raw=""):
    """解析苹果CMS的 vod_play_url 字段：
    支持多播放器、多集数提取，如：第01集$https://...m3u8#第02集$https://...m3u8
    """
    if not vod_play_url_raw:
        return []
    groups = vod_play_url_raw.split("$$$")
    froms = vod_play_from_raw.split("$$$") if vod_play_from_raw else []
    
    parsed_lines = []
    for g_idx, group in enumerate(groups):
        play_from = froms[g_idx] if g_idx < len(froms) else f"line_{g_idx+1}"
        episodes = []
        for item in group.split("#"):
            item = item.strip()
            if not item:
                continue
            if "$" in item:
                parts = item.split("$", 1)
                title, url = parts[0].strip(), parts[1].strip()
            else:
                title, url = f"第{len(episodes)+1}集", item.strip()
            if url:
                episodes.append({"title": title, "url": url})
        if episodes:
            parsed_lines.append({
                "play_from": play_from,
                "episodes": episodes
            })
    return parsed_lines


def query_single_source(source, keyword, timeout=6):
    """单个采集源检索"""
    base_url = source["api_url"].rstrip("/")
    sep = "&" if "?" in base_url else "?"
    req_url = f"{base_url}{sep}ac=detail&wd={urllib.parse.quote(keyword)}"
    try:
        r = requests.get(req_url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 KanjuCollector/1.0"})
        if r.status_code != 200:
            return []
        data = r.json()
        vod_list = data.get("list") or []
        res = []
        for v in vod_list:
            res.append({
                "vod_id": v.get("vod_id"),
                "vod_name": v.get("vod_name"),
                "vod_pic": v.get("vod_pic") or "",
                "vod_actor": v.get("vod_actor") or "",
                "vod_director": v.get("vod_director") or "",
                "vod_year": v.get("vod_year") or "",
                "vod_area": v.get("vod_area") or "",
                "vod_class": v.get("vod_class") or "",
                "vod_remarks": v.get("vod_remarks") or "",
                "vod_content": v.get("vod_content") or "",
                "type_name": v.get("type_name") or "",
                "source_id": source["id"],
                "source_name": source["name"],
                "source_key": source["key"],
                "is_4k": source["is_4k"],
                "play_lines": parse_maccms_play_url(v.get("vod_play_url", ""), v.get("vod_play_from", ""))
            })
        return res
    except Exception:
        return []


def search_all_sources(keyword, only_4k=False, timeout=6):
    """并发查询所有已启用的采集源，聚合并去重结果"""
    sources = list_sources(include_disabled=False)
    if only_4k:
        sources = [s for s in sources if s["is_4k"] == 1]
    if not sources:
        return []
    
    all_results = []
    with ThreadPoolExecutor(max_workers=min(len(sources), 8)) as executor:
        future_map = {executor.submit(query_single_source, s, keyword, timeout): s for s in sources}
        for future in as_completed(future_map):
            try:
                items = future.result()
                if items:
                    all_results.extend(items)
            except Exception:
                pass
    
    # 聚合并按片名分组（同一影片汇聚各源的不同线路）
    merged = {}
    for it in all_results:
        clean_name = it["vod_name"].strip()
        if clean_name not in merged:
            merged[clean_name] = {
                "vod_name": clean_name,
                "vod_pic": it["vod_pic"],
                "vod_actor": it["vod_actor"],
                "vod_director": it["vod_director"],
                "vod_year": it["vod_year"],
                "vod_remarks": it["vod_remarks"],
                "vod_content": it["vod_content"],
                "type_name": it["type_name"],
                "has_4k": it["is_4k"] == 1,
                "sources": []
            }
        else:
            if it["is_4k"]:
                merged[clean_name]["has_4k"] = True
            if not merged[clean_name]["vod_pic"] and it["vod_pic"]:
                merged[clean_name]["vod_pic"] = it["vod_pic"]
        
        merged[clean_name]["sources"].append({
            "source_id": it["source_id"],
            "source_name": it["source_name"],
            "source_key": it["source_key"],
            "is_4k": it["is_4k"],
            "vod_id": it["vod_id"],
            "play_lines": it["play_lines"]
        })
    
    # 转为列表并优先展示 4K 影片
    res_list = list(merged.values())
    res_list.sort(key=lambda x: (x["has_4k"], len(x["sources"])), reverse=True)
    return res_list


def build_local_line_options(title, episode_index=1):
    """根据片名和集数（从 1 开始），在本地启用的采集源中快速匹配出可直接播放的线路列表。
    输出格式 100% 契合前端播放器 line_options 结构。
    """
    if not title:
        return []
    
    # 查找本地是否有缓存
    cache_key = f"local_lines:{title}:{episode_index}"
    now = int(time.time())
    if cache_key in _RESOLVE_CACHE:
        cache_item = _RESOLVE_CACHE[cache_key]
        if cache_item["expire_at"] > now:
            return cache_item["data"]
    
    search_res = search_all_sources(title, timeout=4)
    if not search_res:
        return []
    
    # 找到最匹配的影片
    target_movie = None
    for m in search_res:
        if m["vod_name"] == title or title in m["vod_name"]:
            target_movie = m
            break
    if not target_movie:
        target_movie = search_res[0]
    
    lines = []
    # 4K 源绝对置顶
    sources_sorted = sorted(target_movie["sources"], key=lambda s: s.get("is_4k", 0), reverse=True)
    priority = 2000
    for s in sources_sorted:
        is_4k = s["is_4k"] == 1
        s_name = s["source_name"]
        for p_idx, pl in enumerate(s["play_lines"]):
            eps = pl.get("episodes") or []
            if not eps:
                continue
            
            # 找到对应集数（若只有1集通常为电影）
            target_ep = None
            if len(eps) == 1:
                target_ep = eps[0]
            elif 0 <= episode_index - 1 < len(eps):
                target_ep = eps[episode_index - 1]
            else:
                target_ep = eps[0]
            
            stream_url = target_ep.get("url")
            if not stream_url:
                continue
            
            quality_tag = " [4K蓝光]" if is_4k else " [1080P]"
            line_label = f"{s_name}{quality_tag} - 线路{p_idx+1}"
            lines.append({
                "id": f"local:{s['source_key']}:{s['vod_id']}:{p_idx+1}",
                "label": line_label,
                "url": stream_url,
                "url_kind": "hls" if ".m3u8" in stream_url else "mp4",
                "resolved": True,
                "resolve_required": False,
                "play_from": s["source_key"],
                "preference_weight": priority,
                "default_priority": is_4k,
                "is_4k": is_4k,
                "is_local_collector": True
            })
            priority -= 10

    # 存入缓存 2 小时 (7200 秒)
    _RESOLVE_CACHE[cache_key] = {"data": lines, "expire_at": now + 7200}
    return lines


# 初始化表结构
init_collector_db()

if __name__ == "__main__":
    print("=== Kanju Collector Initialized ===")
    srcs = list_sources()
    print(f"Total Sources: {len(srcs)}")
    for s in srcs:
        print(f"  [{s['id']}] {s['name']} (4K={s['is_4k']}) -> {s['api_url']}")
