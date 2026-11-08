# 看剧AI 复刻站 · 会员系统版

在原「14项补齐版」基础上新增 **会员注册系统**，并按原站（kanju1.ai）的鉴权分层把两个功能接进会员：

1. **历史记录 / 收藏 / 记忆续看 → 接入会员账号**（原来只存浏览器 localStorage，换设备就丢）
2. **18+（午夜剧场 `/midnight-theater`）→ 从会员入口进入**（登录 + 成年确认两个门槛，照原站实测）

## 运行

```bash
bash start.sh      # 前台启动
bash restart.sh    # 后台重启（端口 8077，日志 /tmp/kanju.log）
```

> 首次启动会自动建库 `kanju.db`（sqlite）。

## 账号体系（照原站 /v1/users/password/* 的规则简化）

| 项 | 规则 |
|---|---|
| 注册 | `POST /api/auth/register` 账号≥4 位、密码≥6 位、**必须邀请码** |
| 邀请码 | `KANJU2026` / `KANJU-VIP` / `WEISHAO`（`kanju_member.py` 的 `INVITE_CODES`） |
| 密码 | PBKDF2-HMAC-SHA256，12 万轮，每人独立 salt |
| 会话 | `secrets.token_urlsafe(32)`，cookie `kj_sid`，HttpOnly + SameSite=Lax，30 天 |
| 用户字段 | account / nickname / level（等级）/ tokens（词元）/ growth（成长值）/ adult_confirmed |
| 登录 | `POST /api/auth/login`、退出 `POST /api/auth/logout`、当前用户 `GET /api/auth/me` |

## 接口清单

### 认证
- `POST /api/auth/register` `{account,password,invite_code,nickname?}` → `{ok,user,sid}`
- `POST /api/auth/login` `{account,password}` → `{ok,user,sid}`
- `POST /api/auth/logout`
- `GET  /api/auth/me` → `{ok,logged_in,user}`
- `POST /api/auth/adult` `{adult_confirmed:true}` → 写进账号偏好

### 收藏（需登录）
- `GET    /api/fav` → `{items:[{cid,title,poster,added_at}]}`
- `POST   /api/fav` `{cid,title,poster}` → `{ok,has:true}`
- `DELETE /api/fav?cid=xxx`

### 历史 / 续看（需登录）
- `GET    /api/history` → `{items:[{cid,title,episode,position,duration,updated_at}]}`（最多 60 条）
- `POST   /api/history` `{cid,title,episode,position,duration,poster}`
- `DELETE /api/history`（或 `?cid=` 删单条）

### 18+ 午夜剧场（需登录 + 成年确认）
- `GET /api/midnight/state` → 三态
  - 未登录：`{"state":"locked","reason":"midnight_session_required","message":"请登录后进入午夜剧场"}`
  - 已登录未确认：`{"state":"locked","reason":"adult_confirm_required","message":"请先确认您已满 18 岁"}`
  - 已确认：`{"state":"unlocked"}`
- `GET /api/midnight/feed` → 解锁后返回内容（未解锁返回 `state:locked` 且 `count:0`）
- 页面：`GET /midnight-theater`（`static/midnight.html`）

未登录调用收藏/历史/成年确认一律返回 **401** `{"ok":false,"code":"login_required","error":"请先登录"}`——这是服务端强制，不是前端隐藏（与原站 401 `midnight_session_required` 同一套思路）。

## 前端

`static/index.html`（单文件）新增：

- 顶栏右侧用户区 `#kjUserNav`：`登录 / 注册`、`🌙 18+`、昵称（悬停显示等级/词元/成长）、`退出`
- 登录/注册弹窗 `#kjAuthMask`（注册态显示邀请码输入框）
- 18 岁确认弹窗 `#kjAdultMask`（`未满 18，返回` / `我已满 18 岁，进入`）
- `syncLocalToServer()`：登录后把本地 `kjFav` / `kjHist` 一次性推到服务器，老用户不丢数据
- `KJ3`（收藏/历史/续看）内部已改为「本地写 + 服务器写」双写；历史页/收藏页登录后读服务器（跨设备一致）
- `window.KJ4` 暴露：`me / openAuth / goMidnight / refresh / favList / favToggle / favDel / histList / histPut / histClear`

## 实测结果（本机 127.0.0.1:8077）

```
注册无邀请码        → 400 邀请码不正确
注册带 KANJU2026    → 200 ok, user{level:1,tokens:120,growth:0}
收藏写/读           → has:true / items 正常
历史写/读           → 定位 420s 保存并可读回
换会话读（模拟设备2）→ 收藏、历史均能读到（跨设备）
18+ state 未确认    → locked / adult_confirm_required
确认成年            → ok, adult_confirmed:true
18+ state 确认后    → unlocked
/                → 200
/midnight-theater → 200
```

## 与原站的差异（已知）

- 原站是「手机号/账号 + 密码 + 验证码 + 邀请码」全链路，本复刻为「账号 + 密码 + 邀请码」，未接短信验证码服务。
- 原站词元/等级有真实的消耗与成长结算；本复刻保留字段与展示（初值 tokens=120、level=1、growth=0），未做结算规则。
- 午夜剧场内容池在复刻站为空（`feed count:0`），只复刻了鉴权链路与页面，需要接入自有内容源。

## v2 补齐版（2026-11-05，对照 kanju45.com 全量对齐）

新增模块 `kanju_plus.py`（与会员模块共用 kanju.db），前端新增 KJ5 层（注入 index.html 末尾）。

功能补齐：
- 词元配额结算：播放每集扣 1（4K 扣 2），每日首次打开赠 20，成长值→等级（10 级），流水可查（/api/quota、/api/quota/consume）
- 邀请进度：每人专属邀请码（KJxxxxxx），好友用它注册双方各得 50 词元（/api/invite/me）
- 片单系统：创建/广场/详情/加片删片/访问密码/分享（/api/playlists*、/s/<code> 分享落地）
- 评论：详情页评论区，发/删自己的评论（/api/comments）
- 公告中心：/api/announcements + 首页弹窗（按 version 只弹一次）
- 分享体系：任意影片/片单一键生成分享链接（/api/share）
- 注册短信验证码：/api/auth/sms/send；无短信服务商时为本地模式（KANJU_SMS_DEV=1 时回带 dev_code，接真实服务商后设 0）
- 合集：/api/collections（韩剧/日剧/动作/爱情/悬疑/喜剧/古装/热门动漫 8 个主题合集）
- 短剧专区：/api/shortdrama + 前端无限下滑页
- 投屏配对：/api/cast/pair 生成 6 位码、/api/cast/confirm 电视端确认、/api/cast/devices
- 下载页：/download
- 首页改版：本周追剧（分类 tab + 部数）、大家都在看（排名 + 热度/播放量）、韩剧在追/日剧上新/短剧精选 rails、卡片三行信息、永久域名提醒条
- 播放页：选集分页（50 集一组）、评论、分享、加入片单、投屏、词元消耗提示

Bug 修复：
- /api/play 剧集 number/title 全 null → 上游字段是 episode_number/display_name，已映射（含上下集 token 与分页信息）
- /api/browse 无参数 400 → kind=all 时省略 kind 参数、默认 sort=heat_desc

## v3 追加（2026-11-05 晚）

- HLS P2P：vendored p2p-media-loader 4.0.0（Novage，IIFE 构建，含 core）到 static/p2p-hlsjs.min.js；play() 在 Hls 配置里注入引擎的 fLoader/pLoader，swarmId=播放 URL，同流观众经公共 WebTorrent tracker（novage/openwebtorrent/webtorrent.dev/btorrent，可由 /api/p2p/config 下发）组网互传分片；无节点时自动纯直连。播放页有 P2P 状态条（节点数/P2P 下载量/直连量/占比）与开关（localStorage kjP2P，对照原站 player-hls-p2p-policy）。
- 顺带修复：原包 static/ 缺 hls.min.js（脚本标签一直 404），已 vendored hls.js 1.7.4 补齐。
- 特斯拉车机模式：/tesla 独立大屏页（大字号大按钮、大家都在看+首页栏目、点开播放并尝试全屏、倍速大按钮）；主站导航有入口，检测到 Tesla UA 时提示切换。注：行驶中视频被特斯拉系统级限制，只有 P 挡驻车可看，网页无法绕过。

## v4 追加（2026-11-05 深夜）：午夜池对齐 + 播放器对齐

午夜剧场（扒原站前端包确认的真实契约）：
- 原站午夜池走账号专属子系统 /v1/midnight-theater/*（access / eligibility / acknowledge / discovery / top·random·similar·playlist play / generations 生成 / images），匿名会话一律 401 midnight_session_required；游客通道 guest/evaluate 按本地成人向观看计数发放 proof（请求头 x-ai-movie-midnight-access），新游客实测 state=locked 不发 proof。
- 复刻站新增桥接层 /api/mn/*：全套端点透传（签名 + proof 自动附带），门禁顺序与原站一致（本站登录 → 成年确认 → 上游资格/公告确认 → discovery）。
- 启用专属池只差运营者一步：把自己的 kanju 账号会话按 kanju_upstream.example.json 放成 kanju_upstream.json（或环境变量 KANJU_UPSTREAM_COOKIE / KANJU_UPSTREAM_AUTHORIZATION）。未绑定时午夜页明确提示并可回退本地聚合。
- 午夜页已按原站状态机重写：discovery 分区渲染（history/watching 游标加载更多）、精选/随机播放、输入 prompt 生成片单并轮询进度。

播放器（扒原站 player-keyboard-shortcut-policy / global-player-host / global-preferences 逐条对齐）：
- 快捷键：空格 播放/暂停（连按吞掉；在 input/button/a 等交互元素上不触发；带修饰键不触发）、←/→ ∓5 秒、↑/↓ 音量 ∓0.1，带 OSD 提示。
- 全局偏好与原站同键名同结构（aimovie.player.global-preferences.v1）：skipIntro/skipOutro 开关 + 线路记忆（官方/普通两类，选线即记、下次自动选回）。
- 片头/片尾：原站为用户众包标记（上限 300s），复刻站同机制：/api/skipmarks 多人提交取中位数；播放时片头段显示「跳过片头」、到片尾标记自动跳下一集。
- 续播（按集 token 本地存进度，5 秒节流）、上一集/下一集（用播放接口的 prev/next token）、自动连播开关、画中画、全屏、音量/静音持久化。
