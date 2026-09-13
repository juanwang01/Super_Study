# Probe‑Plan‑Teach 超级学习系统

基于维果茨基「最近发展区 ZPD」的自适应学习系统。服务端程序 + Agent 调度 + Web 前端，支持多独立学习项目、动态学习计划、单元笔记自动归档。

## 一、目录结构

```
超级学习系统/
├── skill.md              # 业务规约（Agent 的 system prompt 源文件，可直接修改）
├── schema/               # 数据模型规范（project.meta / learner_snapshot）
├── backend/              # Python FastAPI 后端
│   ├── main.py           # 服务入口
│   ├── config.py         # 配置（环境变量读取）
│   ├── core/
│   │   ├── project_manager.py  # 项目管理：建项目/快照/计划/笔记/素材提取
│   │   ├── session_manager.py  # 会话管理：项目绑定、切换、闲置回收
│   │   ├── agent_bridge.py     # Agent 调度：skill.md→system prompt + LLM 工具循环
│   │   └── skill_loader.py     # （已并入 agent_bridge）
│   ├── api/              # HTTP API 层
│   └── verify_api.py     # API 自测脚本
├── frontend/             # Web 前端（单页应用）
│   ├── index.html
│   ├── style.css
│   └── app.js
└── data/                 # 学习项目总根（每个主题一个独立文件夹）
    └── <项目id>/
        ├── .learn_config/
        │   ├── project.meta.yaml      # 项目元数据
        │   ├── source_material/       # 原始素材（只读区）
        │   ├── plan/                  # 动态学习计划（多版本）
        │   ├── state_snapshot/        # 学习者状态快照
        │   └── session_log/           # 机器学习元日志
        └── 单元01-xxx笔记.md          # 人类可读笔记（自动归档）
```

## 二、启动

### 1. 安装依赖（首次）
```powershell
cd 超级学习系统\backend
pip install -r requirements.txt
```

### 2. 配置 LLM（必填）
后端通过 OpenAI 兼容接口调用大模型，支持火山方舟（豆包）、DeepSeek、通义、OpenAI、Claude 代理等任意兼容服务。

**方式一：浏览器设置（推荐）**
启动服务后，打开 `http://localhost:8080`，点右上角「⚙ 设置」：
1. 选算力服务商（预置：火山方舟/豆包、DeepSeek、阿里云百炼/通义、OpenAI、智谱 GLM、Kimi、硅基流动、本机 Ollama）→ 接口地址自动填好；
2. 只填 API Key；
3. 点「🔄 拉取该服务商模型」自动获取真实模型列表，或从预置列表选模型；
4. 保存即生效（写回服务端 `backend/.env`，Key 不下发明文）。
注意：该设置接口仅允许本机（localhost）访问，防止 Key 被远程篡改。

**方式二：环境变量**
```powershell
$env:LEARN_LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
$env:LEARN_LLM_API_KEY  = "你的 API Key"
$env:LEARN_LLM_MODEL    = "doubao-seed-1-6-250615"
```

**方式三：直接编辑 `backend/.env` 文件**（没有则自动创建）

### 3. 启动服务
```powershell
cd 超级学习系统\backend
python main.py
```
浏览器打开 `http://localhost:8080`

### 4. 后台部署（服务器）
```powershell
# 方案1：nssm 注册为 Windows 服务
# 方案2：pythonw 后台运行 + 计划任务
# 方案3：Linux 下 systemd / pm2
```

## 三、环境变量一览

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LEARN_LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | OpenAI 兼容接口地址 |
| `LEARN_LLM_API_KEY` | 空（必填） | API Key，存服务端（.env/环境变量），绝不下发前端 |
| `LEARN_LLM_MODEL` | `doubao-seed-1-6-250615` | 模型名 |
| `LEARN_LLM_TIMEOUT` | `180` | LLM 请求超时（秒） |
| `LEARN_DATA_ROOT` | `系统根/data` | 学习项目数据目录 |
| `LEARN_HOST` | `0.0.0.0` | 监听地址 |
| `LEARN_PORT` | `8080` | 端口 |
| `LEARN_SESSION_IDLE` | `900` | 会话闲置自动保存回收（秒） |

> 优先级：环境变量 > `backend/.env` 文件 > 默认值。

### 网络代理（重要）
后端默认**自动读取 Windows 系统代理**（如 Clash `127.0.0.1:7897`），
解决"浏览器能上网、后端连不上服务商"的问题。
如需手动指定，设置环境变量 `LEARN_HTTP_PROXY`（如 `http://127.0.0.1:7897`）优先于系统代理。
设置页的「🧪 测试连接」会显示是否经代理连通。

## 四、使用流程

1. 打开首页 → 主菜单展示全部学习项目
2. 新建项目：输入主题名（如「计算机网络」）→ 创建并进入
3. 粘贴原始素材（可选，粘贴后进入「文档锚定模式」；不粘贴则为「通用学习模式」）
4. 发送任意消息 → 导师执行水平探查（2-4 道题）
5. 探查完 → 生成动态学习计划（存 `plan/`，可修订）
6. 每轮教学闭环：讲解 → 验证提问 → 答错反向诊断 → **自动归档单元笔记** + **保存状态快照**
7. 指令：`main` 回主菜单 ｜ `plan` 看计划 ｜ `progress` 看进度 ｜ `reload` 刷新 ｜ `复习` / `深挖` 等

## 五、关键设计

- **LLM 不碰磁盘**：文件 IO 全部走后端工具（project_id 映射，路径防穿越，项目隔离）
- **笔记不重写**：每轮教学输出直接切片归档，不调用大模型重新生成，省算力
- **计划多版本**：跳步/插前置/删减都会保存新版本，`plan_latest.md` 始终指向当前生效计划
- **快照权威**：学习者状态以磁盘 yaml 为准，切换项目先存快照，避免跨项目状态污染
- **多会话冲突检测**：快照写入前比对 mtime，检测并发编辑并告警
