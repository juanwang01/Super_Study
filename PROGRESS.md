# 超级学习系统 — 开发进度记录

> 最后更新：2026-09-13（v22）
> 架构：单机 Skill → 服务端程序（多用户）→ 电脑端/手机端（APK）多端

---

## 一、当前版本状态（2026-09-13 收工）

| 端 | 版本 | 状态 |
|---|---|---|
| 本地后端（127.0.0.1:8080） | v22 + secret 修复 | ✅ 运行中 |
| 服务器（47.94.251.143:8080） | v22 + secret 修复（待部署确认） | ✅ 部署中 |
| 电脑浏览器客户端 | frontend v22 | ✅ |
| 手机 APK | 超级学习系统.apk（3.8MB，v22） | ✅ 已交付 |

---

## 二、已实现功能（按迭代）

### 单机 Skill 期（最早）
- 学习目录结构：原始材料 / 学习日志 / 学习计划 / 状态快照 全部收敛进「配置文件夹」（`.learn_config`），学习笔记独立在外 → Obsidian 无缝衔接
- 笔记根目录可自定义（电脑端；手机端默认服务器存储、隐藏自定义入口）
- 材料预处理管线：EPUB / PDF / TXT / Markdown → AI 可读 MD；纯图片 PDF 明示不可用
- AI 网络找资料（国内源优先，需用户同意）；进入项目先分析状态+资料缺口
- 快照自动保存（60s）、学习计划多版本（明面一份 + 历史计划子夹）
- 删除+回收站、拖拽上传、MD 实时写盘可编辑、旧会话感知新素材
- 会话永久保留（只允许用户删）+ 上下文压缩（🧹 按钮）

### 多用户化（Phase 1）
- SQLite 用户系统：pbkdf2 密码 + HMAC-JWT（7 天）+ 首次启动自动建 admin/admin123
- 邀请码注册（admin 生成 8 位码）、每用户数据隔离 `data/{owner}/{project_id}`
- 普通用户不能设置/改 API Key；只有管理员有全局 API 管理

### 多供应商 LLM Key 池
- 管理员集中管理：8 个预置服务商 + 自定义，添加/编辑/删除/测试连接
- 表单保存前自动测试，测试通过才落库
- 用户侧只看到"供应商+模型"选项，可切换模型，绝不接触 Key
- 已配置：DeepSeek（deepseek-flash）、次元API（deepseek-v4-flash 等）

### 会话/教学核心
- 创建按钮只创建项目（列表出现 + toast），**手动点击项目卡片**才进入并触发 AI 引导
- AI 引导流程：静态引导（状态/模式/上传提示）→ 跳动点"正在分析" → 资料缺口分析（回复「要」搜集 / 直接上传资料 / 「开始」直接学）
- 引导词明确提示手动上传：EPUB / PDF / TXT / Markdown，拖拽即预处理

### UI/体验（豆包风格）
- 主色 #3370ff、圆角卡片、bottom-sheet 弹层、toast 反馈
- 手机端：学习页左侧目录抽屉（☰ 滑出、遮罩关闭、默认收起）
- 管理后台弹窗固定高度（tab 切换不跳变）
- 管理后台按钮语义色：测试=绿 / 编辑=蓝 / 删除=红
- 思考反馈：3 点跳动动画（非卡通图）
- API 请求 60s 超时 + toast

### 部署/Git
- GitHub：`git@github.com:juanwang01/Super_Study.git`（SSH 直连，`.ssh/config` 无 BOM）
- 服务器 47.94.251.143（阿里云 Ubuntu 24.04）：docker compose + git 部署
- 更新流程：`git pull origin master && docker compose up -d --build`
- 数据卷 `superstudy_superstudy_data:/app/data`（system.db / 项目 / 用户文件）

---

## 三、本轮（v22 收尾）已解决问题

1. **创建按钮"无效"→ 根因 = JWT secret 容器重建丢失**
   - 现象：登录后点创建 → toast「创建项目失败：登录已过期」/ 或无反应
   - 根因：secret 存 `backend/.env`（容器内），每次 `docker compose up --build` 重建容器 → .env 丢失 → secret 变化 → **所有已登录用户 token 立即失效**（401）
   - 修复：secret 迁移到数据卷 `data/.auth_secret`（`_jwt_secret()` 自动迁移旧密钥 + 持久化），容器重建 token 不失效
   - 本地已验证：登录 → 重启后端 → 旧 token 创建成功（200）
   - ⚠️ 服务器已部署此修复；**用户需重新登录一次**（旧 token 已失效），之后不再因部署失效

2. **静态资源无缓存控制**
   - `/static/*` 无 Cache-Control → 手机/浏览器缓存旧前端 → 界面新版、执行的旧脚本
   - 修复：`NoCacheStaticFiles` 加 `no-cache, no-store, must-revalidate`（已部署生效）

3. **创建流程体验**
   - 按钮文字「创建并开始」→「创建」
   - 只创建不进入（列表出现 + toast 提示"点击项目卡片开始学习"）
   - 手动点项目才触发 AI 引导

---

## 四、未解决问题 / 待确认

| # | 问题 | 状态 | 下一步 |
|---|---|---|---|
| 1 | **用户手机上「创建项目无效」** | 已定位 2 个根因（secret 失效 + 缓存）并修复 | 待用户**重新登录 + 强刷**后复测；若仍无效，请反馈点击后按钮是否短暂变"⏳ 创建中"、有无绿色/红色 toast |
| 2 | 服务器 admin 当前无项目（历史项目在本地库） | 正常（数据隔离） | 无 |
| 3 | 手机端弱网（截图 0.7KB/s）下创建慢/超时 | 前端已有 60s 超时 + loading | 建议 Wi-Fi 下测试 |

---

## 五、Todo（下一步开发计划）

### P0 待验证
- [ ] 用户重新登录后复测：手机 APK / 电脑网页 创建项目、进入项目、AI 引导全流程
- [ ] 服务器 secret 修复部署确认（`data/.auth_secret` 存在）

### P1 近期开发
- [ ] **电脑客户端独立应用**（连接服务器；设置只留笔记目录；桌面启动图标）
- [ ] **手机端可安装 App 完善**（Capacitor 打包；登录/管理后台/学习页比例打磨）
- [ ] **跨设备文件同步**：同一用户手机↔电脑笔记双向同步（sync_engine + sync_client.py 已雏形）
  - [ ] 同账户多端同时登录冲突处理（同一时间单点登录？）
  - [ ] 文件同步冲突策略（.conflict-{ts} 副本已实现）
- [ ] **用户管理完善**：用户改密码页面（已上报缺失）、管理员禁用/启用用户、用量统计页

### P2 迭代优化
- [ ] 学习计划版本管理 UI 整理（历史计划折叠默认收起）
- [ ] 目录树默认折叠（部分已实现，验证完整）
- [ ] 学习日志视图（学习日志.md 展示）
- [ ] 快照自动保存稳定性（60s persist 已有）
- [ ] 材料推荐空间压缩（紧凑卡片）
- [ ] 引导词继续打磨（上传格式提示已加）

### P3 架构/运维
- [ ] 服务器 HTTPS（当前 HTTP 明文，APK 已 usesCleartextTraffic）
- [ ] 服务器数据备份策略（数据卷 /app/data）
- [ ] JWT 刷新机制（当前 7 天固定）
- [ ] 多供应商 Key 自动容错（供应商挂掉自动切换）

---

## 六、关键信息速查

```text
本地后端:  cd E:\xiangmu\XueXi\SuperStudy\backend && python main.py  (8080)
服务器:    ssh root@47.94.251.143 → /opt/superstudy → git pull && docker compose up -d --build
GitHub:    git@github.com:juanwang01/Super_Study.git  (SSH)
账号:      admin/admin123, alice/alice123, bob/bob123456
服务器用户: admin + 龙霸天(member, 密码未知)
数据库:    data/system.db (users/invites/usage/llm_providers/projects)
供应商:    id=1 DeepSeek(deepseek-flash), id=2 次元API(deepseek-v4-flash等)
前端bump:  改 frontend/* 后 → index.html 引号 ?v=N → 重建 mobile/www → git push → 服务器 pull+build
APK构建:   mobile/build_mobile.py → cap sync → gradlew assembleDebug → 超级学习系统.apk
```

## 七、Git 提交历史（近期）

```text
2df76f1  fix: 静态资源加 no-cache(防缓存旧前端)
9f37a2e  fix: 创建按钮只创建项目, 手动点项目卡片才进入
d259a9d  fix: 引导词+按钮语义色+loading动画+去重复提示
4c7a576  fix: 砍维基搜索+手机抽屉+后台固定高度+隐藏设置按钮
```

---
*维护说明：重大改动后更新本文件「版本状态 / 已解决 / Todo」，并保持 Git 同步。*
