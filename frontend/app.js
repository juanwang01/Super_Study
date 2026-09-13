/* Probe‑Plan‑Teach 超级学习系统 - 前端逻辑 */
"use strict";

let sessionId = null;
let currentThreadId = null;   // 当前项目内会话（线程）id
let snapshotTimer = null;     // 快照自动保存定时器
let currentProject = null;   // {project_id, project_name, mode}
let currentFile = null;      // {kind, name} 当前打开的文件
let vditor = null;           // Vditor 编辑器实例
let projectNotesRoot = "";   // 当前项目笔记目录绝对路径（Obsidian vault 子目录）

// 用户系统
let authToken = localStorage.getItem("learn_token") || "";
let currentUser = null;      // {id, username, role, notes_root}
let providersCache = [];     // 服务商预设缓存（管理员）

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// 简易 Markdown 渲染
// ---------------------------------------------------------------------------
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function mdToHtml(md) {
  if (!md) return "";
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let inCode = false, inList = false, listType = null, inTable = false;

  const closeList = () => { if (inList) { html += `</${listType}>`; inList = false; listType = null; } };
  const closeTable = () => { if (inTable) { html += "</table>"; inTable = false; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      if (inCode) { html += "</code></pre>"; inCode = false; }
      else { closeList(); closeTable(); html += "<pre><code>"; inCode = true; }
      continue;
    }
    if (inCode) { html += escapeHtml(line) + "\n"; continue; }
    const t = line.trim();
    if (!t) { closeList(); closeTable(); html += "<p></p>"; continue; }
    if (t.startsWith("|") && t.endsWith("|")) {
      const cells = t.split("|").slice(1, -1).map(c => c.trim());
      const isSep = cells.every(c => /^:?-{2,}:?$/.test(c));
      if (!inTable) {
        closeList();
        html += "<table><thead><tr>" + cells.map(c => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>";
        inTable = true;
      } else if (!isSep) {
        html += "<tr>" + cells.map(c => `<td>${inlineMd(c)}</td>`).join("") + "</tr>";
      }
      continue;
    } else if (inTable) { closeTable(); }
    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList(); closeTable();
      html += `<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`;
      continue;
    }
    if (t.startsWith(">")) {
      closeList(); closeTable();
      html += `<blockquote>${inlineMd(t.replace(/^>\s?/, ""))}</blockquote>`;
      continue;
    }
    const ul = t.match(/^[-*]\s+(.*)$/);
    const ol = t.match(/^\d+[.、]\s+(.*)$/);
    if (ul || ol) {
      const type = ul ? "ul" : "ol";
      if (!inList) { closeTable(); html += `<${type}>`; inList = true; listType = type; }
      html += `<li>${inlineMd((ul ? ul[1] : ol[1]))}</li>`;
      continue;
    }
    closeList();
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { html += "<hr>"; continue; }
    html += `<p>${inlineMd(t)}</p>`;
  }
  closeList(); closeTable();
  if (inCode) html += "</code></pre>";
  return html;
}

function inlineMd(s) {
  let out = escapeHtml(s);
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/~~(.+?)~~/g, "<del>$1</del>");
  return out;
}

// ---------------------------------------------------------------------------
// API 封装
// ---------------------------------------------------------------------------
async function api(path, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (authToken) opt.headers["Authorization"] = "Bearer " + authToken;
  if (body) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  if (resp.status === 401 && !path.startsWith("/api/auth/")) {
    // 登录态失效 → 清空并回登录页
    logout(false);
    throw new Error("登录已过期，请重新登录");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败 (${resp.status})`);
  return data;
}

// ---------------------------------------------------------------------------
// 系统确认弹层（内置浏览器会拦截 window.confirm）
// ---------------------------------------------------------------------------
let confirmCb = null;
function askConfirm(msg, cb) {
  $("confirmMsg").textContent = msg;
  $("confirmModal").classList.remove("hidden");
  confirmCb = cb;
}

// ---------------------------------------------------------------------------
// 用户认证：登录 / 注册 / 登出 / 视图切换
// ---------------------------------------------------------------------------
function setAuth(token, user) {
  authToken = token;
  currentUser = user;
  if (token) localStorage.setItem("learn_token", token);
  else localStorage.removeItem("learn_token");
}

function showAuthView() {
  $("authView").classList.remove("hidden");
  $("mainMenu").classList.add("hidden");
  $("projectView").classList.add("hidden");
}

async function showMainMenu() {
  currentProject = null;
  currentFile = null;
  destroyEditor();
  $("authView").classList.add("hidden");
  $("mainMenu").classList.remove("hidden");
  $("projectView").classList.add("hidden");
  $("userName").textContent = currentUser ? currentUser.username : "—";
  const badge = $("userBadge");
  badge.textContent = currentUser && currentUser.role === "admin" ? "管理员" : "用户";
  badge.className = "badge" + (currentUser && currentUser.role === "admin" ? " badge-admin" : "");
  $("btnAdminPanel").classList.toggle("hidden", !(currentUser && currentUser.role === "admin"));
  loadProjects();
}

function logout(notify = true) {
  setAuth("", null);
  if (sessionId) { try { api(`/api/session/${sessionId}/close`, "POST"); } catch (e) { /* 忽略 */ } }
  sessionId = null;
  if (notify) showToast("已退出登录", "success");
  showAuthView();
}

async function doLogin(username, password) {
  const data = await api("/api/auth/login", "POST", { username, password });
  setAuth(data.token, data);
  showMainMenu();
  return data;
}

async function doRegister(inviteCode, username, password) {
  const data = await api("/api/auth/register", "POST",
                         { invite_code: inviteCode, username, password });
  setAuth(data.token, data);
  showMainMenu();
  return data;
}

// ---------------------------------------------------------------------------
// 轻提示
// ---------------------------------------------------------------------------
let toastTimer = null;
function showToast(text, type = "") {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast " + type;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---------------------------------------------------------------------------
// 视图切换（showMainMenu 定义在用户认证区段，合并了项目清理逻辑）
// ---------------------------------------------------------------------------

async function showProject(project) {
  currentProject = project;
  currentFile = null;
  $("mainMenu").classList.add("hidden");
  $("projectView").classList.remove("hidden");
  $("projName").textContent = project.project_name;
  const badge = $("projMode");
  badge.textContent = project.mode === "document_anchor" ? "文档锚定" : "通用学习";
  badge.className = "badge" + (project.mode === "document_anchor" ? "" : " general");
  $("chatMessages").innerHTML = "";
  $("chatInput").value = "";
  currentThreadId = project.thread_id || null;
  switchTab("chat");
  resetBrowse();
  startSnapshotAutoSave();   // 项目内每 60 秒自动保存快照
  await refreshTree();
  await reloadChatHistory();   // 渲染当前会话历史（若有）
  if (project.guide) addMsg("assistant", project.guide);   // 引导显示在历史之后
  smartMaterialCheck(project.project_id);   // AI 准备分析（状态 + 资料缺口）
  loadThreads();   // 加载项目内会话列表并显示当前会话名
  $("chatInput").focus();
}

// 项目内快照自动保存：每 60 秒调 persist（保存 learner_state 到磁盘快照）
function startSnapshotAutoSave() {
  if (snapshotTimer) { clearInterval(snapshotTimer); snapshotTimer = null; }
  snapshotTimer = setInterval(async () => {
    if (!currentProject) return;
    try { await api(`/api/session/${sessionId}/persist`, "POST"); }
    catch (e) { /* 静默，下次再试 */ }
  }, 60000);
}

// ---------------------------------------------------------------------------
// 项目内会话（线程）管理
// ---------------------------------------------------------------------------
async function loadThreads() {
  try {
    const d = await api(`/api/session/${sessionId}/threads`);
    const name = d.thread_name || "默认会话";
    const el = $("threadName");
    if (el) el.textContent = name;
    renderThreadDropdown(d.threads || []);
  } catch (e) { /* 忽略 */ }
}

function renderThreadDropdown(threads) {
  const dd = $("threadDropdown");
  if (!dd) return;
  dd.innerHTML = "";
  threads.forEach(t => {
    const row = document.createElement("div");
    row.className = "thread-item";
    const isActive = t.thread_id === currentThreadId;
    if (isActive) row.classList.add("active");
    const meta = t.message_count ? `${t.message_count} 条` : "空";
    row.innerHTML = `
      <span class="t-name">${escapeHtml(t.name)}</span>
      <span class="t-meta">${meta}</span>
      <span class="t-ops">
        <button class="t-rename" title="重命名">✏️</button>
        ${isActive ? "" : '<button class="t-del" title="删除会话">🗑</button>'}
      </span>`;
    row.onclick = async (e) => {
      if (e.target.closest(".t-ops")) return;   // 操作按钮不触发切换
      if (t.thread_id === currentThreadId) { hideThreadDropdown(); return; }
      try {
        const r = await api(`/api/session/${sessionId}/threads/${t.thread_id}/activate`, "POST");
        currentThreadId = r.thread_id;
        $("threadName").textContent = r.thread_name;
        showToast(`已切换到会话「${r.thread_name}」`, "success");
        hideThreadDropdown();
        await reloadChatHistory(true);   // 清空聊天区并显示该会话历史
        await refreshTree();
      } catch (err) {
        showToast("切换失败：" + err.message, "error");
      }
    };
    row.querySelector(".t-rename").onclick = async (e) => {
      e.stopPropagation();
      renameThreadInline(row, t);
    };
    const delBtn = row.querySelector(".t-del");
    if (delBtn) {
      delBtn.onclick = async (e) => {
        e.stopPropagation();
        if (delBtn.dataset.armed !== "1") {
          delBtn.dataset.armed = "1";
          delBtn.textContent = "确认?";
          setTimeout(() => { delete delBtn.dataset.armed; delBtn.textContent = "🗑"; }, 2500);
          return;
        }
        try {
          await api(`/api/session/${sessionId}/threads/${t.thread_id}`, "DELETE");
          showToast("已删除会话（仅删除对话历史）", "success");
          await loadThreads();
        } catch (err) {
          showToast("删除失败：" + err.message, "error");
        }
      };
    }
    dd.appendChild(row);
  });
}

function renameThreadInline(row, t) {
  const nameEl = row.querySelector(".t-name");
  const input = document.createElement("input");
  input.className = "t-rename-input";
  input.value = t.name;
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  const commit = async (save) => {
    if (save && input.value.trim() && input.value.trim() !== t.name) {
      try {
        await api(`/api/session/${sessionId}/threads/${t.thread_id}/rename`, "POST",
                  { name: input.value.trim() });
        if (t.thread_id === currentThreadId) $("threadName").textContent = input.value.trim();
        await loadThreads();
      } catch (err) { showToast("重命名失败：" + err.message, "error"); }
    } else {
      await loadThreads();
    }
  };
  input.onblur = () => commit(false);
  input.onkeydown = (e) => {
    if (e.key === "Enter") { input.blur(); commit(true); }
    if (e.key === "Escape") input.blur();
  };
}

function showThreadDropdown() {
  const dd = $("threadDropdown");
  if (dd) dd.classList.remove("hidden");
}
function hideThreadDropdown() {
  const dd = $("threadDropdown");
  if (dd) dd.classList.add("hidden");
}

async function reloadChatHistory(showEmptyHint = false) {
  $("chatMessages").innerHTML = "";
  try {
    const d = await api(`/api/session/${sessionId}/messages`);
    const msgs = d.messages || [];
    if (!msgs.length && showEmptyHint) {
      addMsg("assistant", "已切换到会话「" + $("threadName").textContent + "」，此会话暂无历史。回复任意内容开始。");
    } else {
      msgs.forEach(m => {
        if (!m.content || !String(m.content).trim()) return;   // 跳过历史中的空白消息（防空气泡）
        if (m.role === "user") addMsg("user", m.content);
        else if (m.role === "assistant") addMsg("assistant", m.content);
      });
    }
  } catch (e) {
    addMsg("assistant", "会话已切换。");
  }
  $("chatInput").focus();
}

let pendingRecs = null;   // AI 智能资料判断结果（进入项目时异步获取）

// 进入项目后：AI 智能判断是否需要资料（不无脑推荐）
async function smartMaterialCheck(projectId) {
  pendingRecs = null;
  const hint = document.createElement("div");
  hint.className = "msg assistant mat-hint";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<span class="rec-loading">🧠 AI 正在分析学习准备（状态/资料缺口）…</span>';
  hint.appendChild(bubble);
  $("chatMessages").appendChild(hint);
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
  try {
    const r = await api(`/api/projects/${projectId}/guide-material`, "POST", {});
    pendingRecs = r;
    const analysis = (r.analysis_text || "").trim();
    if (analysis) {
      // 准备阶段：AI 第一条消息 = 状态分析 + 资料准备建议 + 下一步引导
      bubble.innerHTML = mdToHtml(analysis);
      hint.classList.remove("mat-hint");
    } else if (r.need_material && (r.gap || r.reason)) {
      bubble.innerHTML = mdToHtml(`📚 **AI 分析**：${escapeHtml(r.gap || r.reason)}。回复「要」让我搜集资料。`);
    } else {
      hint.remove();  // 不需要资料：不显示任何推荐提示
    }
  } catch (e) {
    hint.remove();
  }
}

// 用户同意搜集资料 → 渲染 AI 已搜集的推荐卡片（紧凑）
async function renderMaterialCards(projectId) {
  const r = pendingRecs;
  if (!r || !r.need_material || !(r.recommendations || []).length) {
    smartMaterialCheck(projectId);   // 兜底：重新判断
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.id = "recMsg";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<div class="rec-loading">🧠 AI 正在网络上搜集资料…</div>';
  wrap.appendChild(bubble);
  $("chatMessages").appendChild(wrap);
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;

  const recs = r.recommendations;
  const rows = recs.map((rec, i) => `
    <div class="rec-item compact">
      <span class="si-source">${escapeHtml(rec.source)}</span>
      <span class="rec-title" title="${escapeHtml(rec.reason || "")}">${escapeHtml(rec.title)}</span>
      <button class="mini-btn rec-import" data-i="${i}">⬇ 导入</button>
    </div>`).join("");
  bubble.innerHTML = `
    <div class="rec-header">
      <span>📚 已为你搜集资料（${recs.length} 条，悬停看理由）</span>
      <span class="rec-toggle">▴ 收起</span>
    </div>
    <div class="rec-list">${rows}</div>`;
  const header = bubble.querySelector(".rec-header");
  header.onclick = () => {
    const list = bubble.querySelector(".rec-list");
    const collapsed = list.classList.toggle("hidden");
    header.querySelector(".rec-toggle").textContent = collapsed ? "▾ 展开" : "▴ 收起";
    $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
  };
  bubble.querySelectorAll(".rec-import").forEach(btn => {
    btn.onclick = async () => {
      const rec = recs[Number(btn.dataset.i)];
      btn.disabled = true; btn.textContent = "⏳…";
      try {
        const imp = await api(`/api/projects/${projectId}/import-url`, "POST",
                              { url: rec.url, name: rec.title.slice(0, 30) });
        showToast(`✅ 已导入「${imp.source}」`, "success");
        btn.textContent = "✅"; btn.classList.add("done");
        await refreshTree();
      } catch (e) {
        showToast("导入失败：" + e.message, "error");
        btn.disabled = false; btn.textContent = "⚠ 重试";
      }
    };
  });
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
}

function lastAssistantText() {
  const msgs = document.querySelectorAll("#chatMessages .msg.assistant:not(.thinking)");
  if (!msgs.length) return "";
  const b = msgs[msgs.length - 1].querySelector(".bubble");
  return b ? b.textContent : "";
}

// 判断用户是否同意搜集资料
function isAgreeToCollect(text) {
  const t = (text || "").trim();
  return /^(要|好|可以|行|是|嗯|ok|好的|要的|需要|搜集|收集|找资料|帮我找|帮我搜集|帮我收集)/i.test(t);
}

// ---------------------------------------------------------------------------
// Tab 切换
// ---------------------------------------------------------------------------
function switchTab(name) {
  const chatActive = name === "chat";
  $("tabChat").classList.toggle("active", chatActive);
  $("tabBrowse").classList.toggle("active", !chatActive);
  $("chatPane").classList.toggle("hidden", !chatActive);
  $("browsePane").classList.toggle("hidden", chatActive);
  if (!chatActive && !$("browseView").innerHTML.trim() && !currentFile) {
    $("browseView").innerHTML =
      '<p class="empty">← 在左侧目录树中选择要查看的内容：原始材料、学习笔记、学习计划、状态快照。</p>';
  }
}

function resetBrowse() {
  currentFile = null;
  destroyEditor();
  $("browseTitle").textContent = "文件";
  $("browseMeta").textContent = "";
  $("browseView").innerHTML =
    '<p class="empty">← 在左侧目录树中选择要查看的内容：原始材料、学习笔记、学习计划、状态快照。</p>';
  $("browseView").classList.remove("hidden");
  $("browseEditor").classList.add("hidden");
  setBrowseButtons("none");
}

function renderBrowse(title, meta, content, editable = true) {
  destroyEditor();   // 确保切换到查看时销毁编辑器并停止自动保存
  $("browseTitle").textContent = title;
  $("browseMeta").textContent = meta || "";
  $("browseView").innerHTML = content || '<p class="empty">（无内容）</p>';
  $("browseView").classList.remove("hidden");
  $("browseEditor").classList.add("hidden");
  setBrowseButtons(editable ? "view" : "none");
  switchTab("browse");
}

function setBrowseButtons(mode) {
  // mode: none | view | edit
  const isView = mode === "view";
  const isEdit = mode === "edit";
  $("btnFileEdit").classList.toggle("hidden", !isView);
  $("btnFileDelete").classList.toggle("hidden", !isView);
  $("btnFileRename").classList.toggle("hidden", !isView);
  $("btnFileSave").classList.toggle("hidden", !isEdit);
  $("btnFileCancel").classList.toggle("hidden", !isEdit);
  if (!isView && !isEdit) $("browseActions").classList.add("hidden");
  else $("browseActions").classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Vditor 编辑（自动保存：每 5 秒检测改动并写盘）
// ---------------------------------------------------------------------------
let autoSaveTimer = null;
let lastSavedContent = "";

function destroyEditor() {
  stopAutoSave();
  if (vditor) {
    try { vditor.destroy(); } catch (e) { /* 忽略 */ }
    vditor = null;
  }
  $("browseEditor").innerHTML = "";
}

function startAutoSave() {
  stopAutoSave();
  if (!vditor) return;
  lastSavedContent = vditor.getValue() || "";
  autoSaveTimer = setInterval(async () => {
    if (!vditor || !currentFile) return;
    const cur = vditor.getValue() || "";
    if (cur === lastSavedContent) return;      // 无改动不写盘
    lastSavedContent = cur;
    try {
      await api(`/api/projects/${currentProject.project_id}/files/${currentFile.kind}/${encodeURIComponent(currentFile.name)}`,
                "PUT", { content: cur });
      // 自动保存静默成功，不打扰用户
    } catch (e) { /* 自动保存失败不弹窗，下次再试 */ }
  }, 5000);
}

function stopAutoSave() {
  if (autoSaveTimer) { clearInterval(autoSaveTimer); autoSaveTimer = null; }
}

function enterEdit(kind, name, content) {
  destroyEditor();
  currentFile = { kind, name };
  $("browseView").classList.add("hidden");
  $("browseEditor").classList.remove("hidden");
  setBrowseButtons("edit");

  const editorEl = $("browseEditor");
  editorEl.innerHTML = "";
  vditor = new Vditor(editorEl, {
    value: content || "",
    height: "100%",
    mode: "ir",                    // 即时渲染（类似 Typora）
    lang: "zh_CN",
    placeholder: "开始书写…",
    cache: { enable: false },
    toolbar: [
      "headings", "bold", "italic", "strike", "|",
      "list", "ordered-list", "check", "|",
      "quote", "code", "inline-code", "link", "table", "|",
      "undo", "redo", "|", "preview", "fullscreen",
    ],
    toolbarConfig: { pin: true },
    preview: { markdown: { toc: true } },
    after: () => {
      window.__vditor = vditor;
      startAutoSave();
      showToast("✍️ 已开启自动保存（每 5 秒）", "success");
    },
  });
  window.__vditor = vditor;
}

async function saveFile() {
  if (!currentFile || !vditor) return;
  const content = vditor.getValue();
  try {
    await api(`/api/projects/${currentProject.project_id}/files/${currentFile.kind}/${encodeURIComponent(currentFile.name)}`,
              "PUT", { content });
    showToast("✅ 已保存", "success");
    // 回到查看模式并重新加载（销毁编辑器，停止自动保存）
    destroyEditor();
    await reloadCurrentFile();
  } catch (e) {
    showToast("保存失败：" + e.message, "error");
  }
}

async function reloadCurrentFile() {
  if (!currentFile) return;
  try {
    let content = "";
    if (currentFile.kind === "material") {
      const d = await api(`/api/projects/${currentProject.project_id}/materials/${encodeURIComponent(currentFile.name)}`);
      content = d.content || "";
    } else if (currentFile.kind === "note") {
      const d = await api(`/api/projects/${currentProject.project_id}/notes`);
      const n = (d.notes || []).find(x => x.name === currentFile.name);
      content = n ? n.content : "";
    } else if (currentFile.kind === "plan") {
      const d = await api(`/api/projects/${currentProject.project_id}/plan`);
      content = d.latest || "";
    }
    rawCurrentContent = content;
    renderBrowse(`📄 ${currentFile.name}`,
                 { material: "原始材料", note: "学习笔记", plan: "学习计划" }[currentFile.kind],
                 mdToHtml(content));
  } catch (e) {
    showToast("重新加载失败：" + e.message, "error");
    resetBrowse();
  }
  await refreshTree();
}

async function renameCurrentFile() {
  if (!currentFile) return;
  const newName = prompt("新文件名（保留 .md 后缀）：", currentFile.name);
  if (!newName || newName === currentFile.name) return;
  try {
    await api(`/api/projects/${currentProject.project_id}/files/${currentFile.kind}/${encodeURIComponent(currentFile.name)}/rename`,
              "POST", { new_name: newName });
    showToast("✅ 已重命名", "success");
    currentFile.name = newName;
    await reloadCurrentFile();
  } catch (e) {
    showToast("重命名失败：" + e.message, "error");
  }
}

async function deleteCurrentFile() {
  if (!currentFile) return;
  if (!confirm(`确定删除「${currentFile.name}」？此操作不可恢复。`)) return;
  try {
    await api(`/api/projects/${currentProject.project_id}/files/${currentFile.kind}/${encodeURIComponent(currentFile.name)}`,
              "DELETE");
    showToast("🗑 已删除", "success");
    resetBrowse();
    await refreshTree();
  } catch (e) {
    showToast("删除失败：" + e.message, "error");
  }
}

// ---------------------------------------------------------------------------
// 聊天消息
// ---------------------------------------------------------------------------
const THINKING_HTML = `<div class="thinking-dots"><span></span><span></span><span></span></div>`;

function addMsg(role, text, toolInfo) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (text === "__THINKING__") {
    bubble.innerHTML = THINKING_HTML;
    wrap.classList.add("thinking");
  } else {
    bubble.className += " markdown";
    bubble.innerHTML = mdToHtml(text);
  }
  wrap.appendChild(bubble);
  if (toolInfo) {
    const tag = document.createElement("div");
    tag.className = "tool-tag";
    tag.textContent = toolInfo;
    wrap.appendChild(tag);
  }
  $("chatMessages").appendChild(wrap);
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
  return wrap;
}

// ---------------------------------------------------------------------------
// 主菜单逻辑
// ---------------------------------------------------------------------------
async function loadProjects() {
  // 刷新笔记目录状态（主菜单卡片）
  try {
    const nc = await api("/api/auth/notes-root");
    $("notesStatus").textContent = nc.notes_root
      ? `📁 当前：${nc.notes_root}（Obsidian 直接打开即可）`
      : "📁 当前：未配置（笔记保存在系统 data 目录）";
  } catch (e) {
    $("notesStatus").textContent = "📁 笔记目录状态读取失败";
  }
  try {
    const data = await api("/api/projects");
    const list = $("projectList");
    if (!data.projects.length) {
      list.innerHTML = '<p class="empty">还没有学习项目，在下方创建第一个吧。</p>';
      return;
    }
    list.innerHTML = "";
    data.projects.forEach(p => {
      const item = document.createElement("div");
      item.className = "project-item";
      const meta = `模式：${p.mode === "document_anchor" ? "文档锚定" : "通用学习"} ｜ 笔记：${p.note_count} 份 ｜ 上次学习：${p.last_learn_time || "从未"}`;
      item.innerHTML = `
        <div style="flex:1;min-width:0">
          <div class="p-name">${escapeHtml(p.project_name)}</div>
          <div class="p-meta">${escapeHtml(meta)}</div>
        </div>
        <button class="project-del" title="删除项目（不删除外部笔记目录）">🗑</button>
        <div class="p-arrow">›</div>`;
      item.onclick = () => openProject(p.project_id);
      const delBtn = item.querySelector(".project-del");
      delBtn.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`确定删除学习项目「${p.project_name}」？\n\n将删除系统数据（原始材料/计划/快照/会话）。\n若配置了外部笔记目录，其中的笔记文件不会删除。`)) return;
        try {
          await api(`/api/projects/${p.project_id}`, "DELETE");
          showToast("🗑 项目已删除", "success");
          loadProjects();
        } catch (e) {
          showToast("删除失败：" + e.message, "error");
        }
      };
      list.appendChild(item);
    });
  } catch (e) {
    $("projectList").innerHTML = `<p class="empty">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

async function loadModelOptions(selectedProvider) {
  const sel = $("modelSelect");
  sel.innerHTML = '<option value="">系统默认（自动）</option>';
  sel.disabled = true;
  try {
    const r = await api("/api/session/llm/options");
    (r.options || []).forEach(o => {
      const opt = document.createElement("option");
      opt.value = o.provider_id;
      opt.textContent = `${o.provider_name} · ${o.model}`;
      sel.appendChild(opt);
    });
    if (selectedProvider) {
      sel.value = String(selectedProvider);
    } else {
      sel.value = "";
    }
  } catch (e) {
    sel.innerHTML = '<option value="">模型加载失败</option>';
  }
  sel.disabled = false;
}

async function openProject(projectId) {
  try {
    const data = await api(`/api/session/${sessionId}/open`, "POST", { project_id: projectId });
    showProject({
      project_id: data.project_id,
      project_name: data.project_name,
      mode: data.mode,
      guide: data.guide,
      thread_id: data.thread_id,
    });
    loadModelOptions(data.model_provider);
  } catch (e) {
    alert("打开项目失败：" + e.message);
  }
}

async function createAndOpen(name, desc) {
  try {
    const data = await api("/api/projects", "POST", { project_name: name, description: desc });
    await openProject(data.project_id);
  } catch (e) {
    alert("创建项目失败：" + e.message);
  }
}

// ---------------------------------------------------------------------------
// 目录树
// ---------------------------------------------------------------------------
async function refreshTree() {
  if (!currentProject) return;
  try {
    const data = await api(`/api/projects/${currentProject.project_id}`);
    projectNotesRoot = data.notes_root || "";
    // 侧栏底部显示笔记目录
    const pathLabel = $("notesPathLabel");
    if (pathLabel) {
      pathLabel.textContent = data.notes_external
        ? `📁 笔记目录：${projectNotesRoot}`
        : "📁 笔记目录：系统默认（设置里可自定义）";
      pathLabel.title = projectNotesRoot;
    }
    const planData = await api(`/api/projects/${currentProject.project_id}/plan`);
    const materials = data.materials || [];
    const notes = data.notes || [];
    const versions = planData.versions || [];

    $("matCount").textContent = materials.length;
    $("noteCount").textContent = notes.length;
    $("planCount").textContent = versions.length;

    const matTree = $("matTree");
    matTree.innerHTML = "";
    if (!materials.length) matTree.innerHTML = '<div class="tree-empty">暂无素材（通用学习模式）</div>';
    materials.forEach(name => matTree.appendChild(makeNode("material", name)));

    // 子目录：源文件（原始格式） / 无法处理（预处理失败）
    const [rawSrc, rawQuar] = await Promise.all([
      api(`/api/projects/${currentProject.project_id}/raw-files?kind=source`).catch(() => ({ files: [] })),
      api(`/api/projects/${currentProject.project_id}/raw-files?kind=quarantine`).catch(() => ({ files: [] })),
    ]);
    const rawSrcFiles = rawSrc.files || [];
    const rawQuarFiles = rawQuar.files || [];
    $("rawSrcCount").textContent = rawSrcFiles.length;
    $("rawQuarCount").textContent = rawQuarFiles.length;
    const rawSrcTree = $("rawSrcTree");
    rawSrcTree.innerHTML = "";
    if (!rawSrcFiles.length) rawSrcTree.innerHTML = '<div class="tree-empty">暂无（上传的原始文件会保留在这里）</div>';
    rawSrcFiles.forEach(f => rawSrcTree.appendChild(makeRawNode("source", f)));
    const rawQuarTree = $("rawQuarTree");
    rawQuarTree.innerHTML = "";
    if (!rawQuarFiles.length) rawQuarTree.innerHTML = '<div class="tree-empty">暂无（预处理失败的文件会放这里）</div>';
    rawQuarFiles.forEach(f => rawQuarTree.appendChild(makeRawNode("quarantine", f)));

    const noteTree = $("noteTree");
    noteTree.innerHTML = "";
    if (!notes.length) noteTree.innerHTML = '<div class="tree-empty">暂无笔记，完成教学单元后自动生成</div>';
    notes.forEach(name => noteTree.appendChild(makeNode("note", name)));

    const planTree = $("planTree");
    planTree.innerHTML = "";
    // 学习计划：只显示当前生效的一份
    if (planData.latest && planData.latest.trim()) {
      const latestNode = makeNode("plan", "plan_latest.md");
      latestNode.querySelector(".node-name").textContent = "plan_latest.md（当前生效）";
      planTree.appendChild(latestNode);
      $("planCount").textContent = "1";
    } else {
      planTree.innerHTML = '<div class="tree-empty">尚无计划</div>';
      $("planCount").textContent = "0";
    }
    // 历史计划：历史版本收进独立分组（精简命名 plan_vN.md，点击读取真实历史内容）
    const histTree = $("planHistTree");
    histTree.innerHTML = "";
    $("planHistCount").textContent = versions.length;
    if (versions.length) {
      versions.forEach(v => histTree.appendChild(makeNode("plan", v)));
    } else {
      histTree.innerHTML = '<div class="tree-empty">暂无历史版本</div>';
    }
  } catch (e) { /* 忽略树刷新错误 */ }
}

function makeNode(kind, name) {
  const node = document.createElement("div");
  node.className = "tree-node";
  const icon = { material: "📄", note: "📝", plan: "🗺️" }[kind] || "📄";
  const iconSpan = document.createElement("span");
  iconSpan.className = "node-icon";
  iconSpan.textContent = icon;
  node.appendChild(iconSpan);
  const nameSpan = document.createElement("span");
  nameSpan.className = "node-name";
  nameSpan.textContent = name;
  node.appendChild(nameSpan);
  // 原始材料 / 学习笔记：树内直接删除（进回收站）
  if (kind === "material" || kind === "note") {
    const del = document.createElement("button");
    del.className = "tree-del";
    del.textContent = "🗑";
    del.title = "删除（移入回收站，可恢复）";
    del.onclick = async (e) => {
      e.stopPropagation();
      // 有回收站兜底：不弹确认，直接移入回收站
      try {
        await api(`/api/projects/${currentProject.project_id}/files/${kind}/${encodeURIComponent(name)}`, "DELETE");
        showToast(`已移入回收站：「${name}」（可恢复）`, "success");
        await refreshTree();
      } catch (err) {
        showToast("删除失败：" + err.message, "error");
      }
    };
    node.appendChild(del);
  }
  node.onclick = () => openFile(kind, name);
  return node;
}

function makeRawNode(kind, f) {
  /* kind: "source"（源文件）| "quarantine"（无法处理） */
  const node = document.createElement("div");
  node.className = "tree-node raw-node";
  const icon = document.createElement("span");
  icon.className = "node-icon";
  icon.textContent = "🗂️";
  node.appendChild(icon);
  const nameSpan = document.createElement("span");
  nameSpan.className = "node-name";
  nameSpan.textContent = f.name;
  node.appendChild(nameSpan);
  const sizeSpan = document.createElement("span");
  sizeSpan.className = "node-meta";
  const kb = (f.size / 1024).toFixed(0);
  sizeSpan.textContent = `${kb}KB`;
  node.appendChild(sizeSpan);
  // 无法处理：可重试
  if (kind === "quarantine") {
    const retry = document.createElement("button");
    retry.className = "tree-del";
    retry.textContent = "↻";
    retry.title = "重新预处理";
    retry.onclick = async (e) => {
      e.stopPropagation();
      try {
        showToast(`⏳ 正在重新预处理「${f.name}」…`);
        const d = await api(`/api/projects/${currentProject.project_id}/raw-files/quarantine/${encodeURIComponent(f.name)}/reprocess`, "POST");
        showToast(`✅ 重新预处理成功 → ${d.part_count} 份素材`, "success");
        await refreshTree();
      } catch (err) {
        showToast("重试失败：" + err.message, "error");
      }
    };
    node.appendChild(retry);
  }
  // 删除（进回收站）
  const del = document.createElement("button");
  del.className = "tree-del";
  del.textContent = "🗑";
  del.title = "删除（移入回收站，可恢复）";
  del.onclick = async (e) => {
    e.stopPropagation();
    try {
      await api(`/api/projects/${currentProject.project_id}/raw-files/${kind}/${encodeURIComponent(f.name)}`, "DELETE");
      showToast(`已移入回收站：「${f.name}」`, "success");
      await refreshTree();
    } catch (err) {
      showToast("删除失败：" + err.message, "error");
    }
  };
  node.appendChild(del);
  node.onclick = () => {
    if (kind === "quarantine") {
      renderBrowse(`🗂️ ${f.name}`, "预处理失败，原始文件（不可直接阅读）", "<p>此文件预处理失败，已隔离在「无法处理」目录。<br>可点击 ↻ 重新预处理，或删除。</p>", false);
    } else {
      renderBrowse(`🗂️ ${f.name}`, "原始格式文件（AI 不可读，仅保留待用）", "<p>这是上传时的<b>原始格式文件</b>（如 EPUB/PDF/DOCX），供留档或重新预处理。<br>AI 教学使用的 Markdown 素材在上方「原始材料」列表。</p>", false);
    }
  };
  return node;
}

async function openFile(kind, name) {
  try {
    let content = "";
    let meta = "";
    if (kind === "material") {
      const d = await api(`/api/projects/${currentProject.project_id}/materials/${encodeURIComponent(name)}`);
      content = d.content || "";
      meta = "原始材料";
    } else if (kind === "note") {
      const d = await api(`/api/projects/${currentProject.project_id}/notes`);
      const n = (d.notes || []).find(x => x.name === name);
      content = n ? n.content : "";
      meta = "学习笔记";
    } else if (kind === "plan") {
      // 读取真实计划文件内容（当前生效 或 历史版本）
      const d = await api(`/api/projects/${currentProject.project_id}/plan/${encodeURIComponent(name)}`);
      content = d.content || "";
      meta = name === "plan_latest.md" ? "当前生效计划" : "历史版本";
    }
    currentFile = { kind, name };
    rawCurrentContent = content;
    if (kind === "plan" && name !== "plan_latest.md") {
      renderBrowse(`📄 ${name}`, "历史版本（只读）", mdToHtml(content), false);
    } else if (kind === "plan") {
      renderBrowse(`📄 ${name}`, meta, mdToHtml(content));
    } else {
      renderBrowse(`📄 ${name}`, meta, mdToHtml(content));
    }
  } catch (e) {
    showToast("打开失败：" + e.message, "error");
  }
}

async function openLog() {
  try {
    const d = await api(`/api/projects/${currentProject.project_id}/log`);
    const log = d.log || "（暂无日志）";
    currentFile = null;
    renderBrowse("📄 learn_session.log", "学习日志（机器学习元日志，只读）",
                 mdToHtml("```text\n" + log + "\n```"), false);
  } catch (e) {
    showToast("读取日志失败：" + e.message, "error");
  }
}

async function openSnapshot() {
  try {
    const data = await api(`/api/projects/${currentProject.project_id}/snapshot`);
    const ls = data.learner_state || {};
    const lines = ["learner_state:"];
    for (const k of Object.keys(ls)) {
      const v = ls[k];
      if (Array.isArray(v)) {
        lines.push(`  ${k}: [${v.map(x => typeof x === "object" ? x.concept || JSON.stringify(x) : `"${x}"`).join(", ")}]`);
      } else {
        lines.push(`  ${k}: "${v}"`);
      }
    }
    lines.push(`meta: { snapshot_version: ${(data.meta || {}).snapshot_version ?? "-"} }`);
    currentFile = null;
    renderBrowse("📄 snapshot_latest.yaml", "状态快照（机器可读，只读）",
                 mdToHtml("```yaml\n" + lines.join("\n") + "\n```"), false);
  } catch (e) {
    showToast("读取快照失败：" + e.message, "error");
  }
}

// ---------------------------------------------------------------------------
// 素材上传 / 粘贴 / 新建笔记
// ---------------------------------------------------------------------------
async function uploadFiles(files) {
  /* 逐个上传预处理；成功 → 素材；失败/不支持 → 自动隔离到「无法处理」目录 */
  let ok = 0, fail = 0;
  for (const file of files) {
    showToast(`⏳ 正在预处理「${file.name}」（转换为 Markdown）…`);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch(`/api/projects/${currentProject.project_id}/upload`, { method: "POST", body: fd });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `上传失败 (${resp.status})`);

      if (!data.converted) {
        fail++;
        showToast(`⚠️ 「${file.name}」${data.reason || "无法处理"}`, "error");
        continue;
      }
      ok++;
      const warn = data.warnings && data.warnings.length ? "\n" + data.warnings.join("\n") : "";
      if (data.part_count > 1) {
        showToast(`✅ 已转换 ${data.format.toUpperCase()} → ${data.part_count} 份 Markdown 素材${warn}`, "success");
      } else {
        showToast(`✅ 已转换 ${data.format.toUpperCase()} → Markdown 素材${warn}`, "success");
      }
    } catch (err) {
      fail++;
      showToast(`「${file.name}」预处理失败：${err.message}`, "error");
    }
  }
  await refreshTree();
  if (fail) showToast(`完成：${ok} 份成功，${fail} 份放入「无法处理」目录`, fail ? "error" : "success");
}

function bindDragDrop() {
  /* 全局拖入文件 → 上传预处理；原始文件保留到 _源文件/，失败隔离到 _无法处理/ */
  let dragDepth = 0;
  const overlay = $("dropOverlay");
  const label = $("dropLabel");
  const onEnter = (e) => {
    e.preventDefault();
    if (!currentProject) return;
    dragDepth++;
    if (dragDepth > 0) {
      overlay.classList.add("show");
      const kinds = Array.from(e.dataTransfer?.types || []);
      label.textContent = kinds.includes("Files")
        ? "松开鼠标，导入文件并预处理为学习材料"
        : "松开鼠标，导入为学习材料";
    }
  };
  const onLeave = (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.classList.remove("show");
  };
  const onDrop = (e) => {
    e.preventDefault();
    dragDepth = 0;
    overlay.classList.remove("show");
    if (!currentProject) { showToast("请先进入一个学习项目再拖入文件", "error"); return; }
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length) uploadFiles(files);
  };
  document.addEventListener("dragenter", onEnter);
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("dragleave", onLeave);
  document.addEventListener("drop", onDrop);
}

function bindFileInputs() {
  $("btnUploadMat").onclick = () => $("fileInput").click();
  $("fileInput").onchange = async (e) => {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    if (!files.length) return;
    await uploadFiles(files);
  };

  $("btnPasteMat").onclick = () => {
    $("pasteFileName").value = "";
    $("pasteContent").value = "";
    $("pasteStatus").textContent = "";
    $("pasteModal").classList.remove("hidden");
  };
  $("btnClosePaste").onclick = () => $("pasteModal").classList.add("hidden");
  $("pasteModal").addEventListener("click", (e) => {
    if (e.target === $("pasteModal")) $("pasteModal").classList.add("hidden");
  });
  $("btnPasteSave").onclick = async () => {
    const filename = $("pasteFileName").value.trim() || "粘贴素材.md";
    const content = $("pasteContent").value;
    if (!content.trim()) { $("pasteStatus").textContent = "❌ 内容不能为空"; return; }
    try {
      const r = await api(`/api/projects/${currentProject.project_id}/materials`, "POST",
                          { filename, content, role: "main" });
      $("pasteStatus").textContent = "✅ 已保存";
      setTimeout(() => $("pasteModal").classList.add("hidden"), 600);
      showToast(`✅ 已导入 ${r.filename}`, "success");
      await refreshTree();
    } catch (e) {
      $("pasteStatus").textContent = "❌ " + e.message;
    }
  };

  // 已知强反爬站点（无法自动导入，提示用户）
  const BLOCKED_SITES = ["baike.baidu.com", "zhihu.com", "weixin.qq.com", "juejin.cn"];
  function isBlockedSite(url) { return BLOCKED_SITES.some(h => (url || "").includes(h)); }

  // ---------- 网络搜资料 ----------
  $("btnSearchMat").onclick = () => {
    $("searchQuery").value = "";
    $("importUrlInput").value = "";
    $("searchResults").innerHTML = '<p class="hint">输入主题后搜索，选择条目导入（自动转 Markdown 并加入原始材料）。</p>';
    $("searchStatus").textContent = "";
    $("searchModal").classList.remove("hidden");
    setTimeout(() => $("searchQuery").focus(), 100);
  };

  // ---------- 回收站 ----------
  $("btnTrash").onclick = async () => {
    $("trashModal").classList.remove("hidden");
    await loadTrashList();
  };
  $("btnCloseTrash").onclick = () => $("trashModal").classList.add("hidden");
  $("trashModal").addEventListener("click", (e) => {
    if (e.target === $("trashModal")) $("trashModal").classList.add("hidden");
  });

  async function loadTrashList() {
    const box = $("trashList");
    box.innerHTML = '<p class="hint">加载中…</p>';
    try {
      const d = await api(`/api/projects/${currentProject.project_id}/trash`);
      const items = d.items || [];
      if (!items.length) {
        box.innerHTML = '<p class="hint">回收站是空的。</p>';
        return;
      }
      box.innerHTML = "";
      items.forEach((it) => {
        const row = document.createElement("div");
        row.className = "trash-item";
        const kindLabel = it.kind === "note" ? "📝" : "📄";
        row.innerHTML = `
          <span class="trash-name" title="${escapeHtml(it.original_name)}">${kindLabel} ${escapeHtml(it.original_name)}</span>
          <span class="trash-time">${escapeHtml(it.deleted_at)}</span>
          <span class="trash-actions">
            <button class="mini-btn trash-restore" data-name="${escapeHtml(it.trash_name)}">↩ 恢复</button>
            <button class="mini-btn danger-btn trash-purge" data-name="${escapeHtml(it.trash_name)}">✕ 彻底删除</button>
          </span>`;
        row.querySelector(".trash-restore").onclick = async (e) => {
          const tn = e.target.dataset.name;
          try {
            await api(`/api/projects/${currentProject.project_id}/trash/${encodeURIComponent(tn)}/restore`, "POST");
            showToast("✅ 已恢复", "success");
            await loadTrashList();
            await refreshTree();
          } catch (err) {
            showToast("恢复失败：" + err.message, "error");
          }
        };
        row.querySelector(".trash-purge").onclick = async (e) => {
          const btn = e.target;
          // 二次点击确认（避免误触；不使用可能被拦截的 confirm）
          if (btn.dataset.armed !== "1") {
            btn.dataset.armed = "1";
            btn.textContent = "⚠ 确认彻底删除？";
            btn.classList.add("danger-btn");
            setTimeout(() => { delete btn.dataset.armed; btn.textContent = "✕ 彻底删除"; }, 3000);
            return;
          }
          const tn = btn.dataset.name;
          try {
            await api(`/api/projects/${currentProject.project_id}/trash/${encodeURIComponent(tn)}`, "DELETE");
            showToast("已彻底删除", "success");
            await loadTrashList();
          } catch (err) {
            showToast("删除失败：" + err.message, "error");
          }
        };
        box.appendChild(row);
      });
    } catch (e) {
      box.innerHTML = '<p class="hint">加载失败：' + escapeHtml(e.message) + "</p>";
    }
  }
  $("btnCloseSearch").onclick = () => $("searchModal").classList.add("hidden");
  $("searchModal").addEventListener("click", (e) => {
    if (e.target === $("searchModal")) $("searchModal").classList.add("hidden");
  });
  $("searchQuery").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btnDoSearch").click();
  });
  $("importUrlInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btnImportUrl").click();
  });

  $("btnDoSearch").onclick = async () => {
    const q = $("searchQuery").value.trim();
    if (!q) { $("searchStatus").textContent = "❌ 请输入搜索主题"; return; }
    $("searchStatus").textContent = "⏳ 正在搜索…";
    try {
      const data = await api(`/api/projects/${currentProject.project_id}/search-materials`, "POST", { query: q });
      const box = $("searchResults");
      if (!data.results.length) {
        box.innerHTML = '<p class="hint">未找到相关条目，试试换关键词，或直接粘贴资料链接。</p>';
      } else {
        box.innerHTML = "";
        data.results.forEach((r, i) => {
          const blocked = isBlockedSite(r.url);
          const item = document.createElement("div");
          item.className = "search-item" + (blocked ? " si-blocked" : "");
          item.innerHTML = `
            <div class="si-head">
              <span class="si-source">${escapeHtml(r.source)}</span>
              <strong>${escapeHtml(r.title)}</strong>
              ${blocked ? '<span class="si-warn">⚠ 反爬，建议手动打开复制</span>' : ""}
            </div>
            <div class="si-snippet">${escapeHtml(r.snippet || "")}</div>
            <button class="mini-btn si-import" data-i="${i}" ${blocked ? "disabled" : ""}>${blocked ? "⚠ 不支持直接导入" : "⬇ 导入为素材"}</button>`;
          box.appendChild(item);
        });
        box.querySelectorAll(".si-import").forEach(btn => {
          btn.onclick = async () => {
            const r = data.results[Number(btn.dataset.i)];
            btn.disabled = true;
            btn.textContent = "⏳ 导入中…";
            const isWiki = (r.source || "").includes("维基");
            $("searchStatus").textContent = isWiki
              ? `⏳ 正在抓取「${r.title}」并转 Markdown…`
              : `⏳ 正在下载「${r.title}」并预处理…（网页自动转 Markdown）`;
            try {
              const imp = isWiki
                ? await api(`/api/projects/${currentProject.project_id}/import-search`, "POST",
                             { title: r.title, source: r.source })
                : await api(`/api/projects/${currentProject.project_id}/import-url`, "POST",
                             { url: r.url, name: r.title.slice(0, 30) });
              const warn = imp.warnings && imp.warnings.length ? "；" + imp.warnings.join("；") : "";
              $("searchStatus").textContent =
                `✅ 已导入${imp.part_count > 1 ? ` ${imp.part_count} 份` : ""}：${imp.source}${warn}`;
              showToast(`✅ 已导入「${imp.source}」`, "success");
              btn.textContent = "✅ 已导入";
              btn.classList.add("done");
              await refreshTree();
            } catch (e) {
              $("searchStatus").textContent = "❌ " + e.message;
              btn.disabled = false;
              btn.textContent = "⚠ 导入失败";
              btn.classList.add("failed");
              showToast("导入失败：" + e.message, "error");
            }
          };
        });
      }
      $("searchStatus").textContent = `✅ 找到 ${data.results.length} 个条目（点击导入）`;
    } catch (e) {
      $("searchStatus").textContent = "❌ " + e.message;
    }
  };

  $("btnImportUrl").onclick = async () => {
    const url = $("importUrlInput").value.trim();
    if (!url) { $("searchStatus").textContent = "❌ 请输入链接"; return; }
    $("searchStatus").textContent = "⏳ 正在下载并预处理…（网页/PDF/EPUB 均可）";
    try {
      const imp = await api(`/api/projects/${currentProject.project_id}/import-url`, "POST", { url });
      const warn = imp.warnings && imp.warnings.length ? "；" + imp.warnings.join("；") : "";
      $("searchStatus").textContent =
        `✅ 已导入${imp.part_count > 1 ? ` ${imp.part_count} 份` : ""}（${imp.source.slice(0, 60)}）${warn}`;
      showToast("✅ 已下载并转为 Markdown 素材", "success");
      await refreshTree();
    } catch (e) {
      $("searchStatus").textContent = "❌ " + e.message;
    }
  };

  $("btnNewNote").onclick = () => {
    $("noteFileName").value = "";
    $("noteContent").value = "";
    $("noteStatus").textContent = "";
    $("noteModal").classList.remove("hidden");
    setTimeout(() => $("noteFileName").focus(), 100);
  };

  // ---------- 会话（线程）切换 ----------
  $("btnThreadSelect").onclick = (e) => {
    e.stopPropagation();
    const dd = $("threadDropdown");
    if (dd.classList.contains("hidden")) { showThreadDropdown(); loadThreads(); }
    else hideThreadDropdown();
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#btnThreadSelect") && !e.target.closest("#threadDropdown")) {
      hideThreadDropdown();
    }
  });
  $("btnNewThread").onclick = async () => {
    try {
      const r = await api(`/api/session/${sessionId}/threads`, "POST", { name: "" });
      currentThreadId = r.thread_id;
      $("threadName").textContent = r.name;
      showToast(`已新建会话「${r.name}」`, "success");
      hideThreadDropdown();
      await reloadChatHistory();
      await loadThreads();
    } catch (err) {
      showToast("新建会话失败：" + err.message, "error");
    }
  };
  $("btnCloseNote").onclick = () => $("noteModal").classList.add("hidden");
  $("noteModal").addEventListener("click", (e) => {
    if (e.target === $("noteModal")) $("noteModal").classList.add("hidden");
  });
  $("noteFileName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btnNoteSave").click();
  });
  $("btnNoteSave").onclick = async () => {
    const name = $("noteFileName").value.trim();
    if (!name) { $("noteStatus").textContent = "❌ 请输入文件名"; return; }
    try {
      const finalName = name.endsWith(".md") ? name : name + ".md";
      await api(`/api/projects/${currentProject.project_id}/files/note`, "POST",
                { name: finalName, content: $("noteContent").value });
      $("noteStatus").textContent = "✅ 已创建";
      showToast(`✅ 已创建笔记「${finalName}」`, "success");
      $("noteModal").classList.add("hidden");
      await refreshTree();
      const d = await api(`/api/projects/${currentProject.project_id}/notes`);
      const n = (d.notes || []).find(x => x.name === finalName);
      currentFile = { kind: "note", name: finalName };
      rawCurrentContent = n ? n.content : "";
      enterEdit("note", finalName, n ? n.content : "");
    } catch (e) {
      $("noteStatus").textContent = "❌ " + e.message;
    }
  };
}

// ---------------------------------------------------------------------------
// 顶栏：折叠 / 返回 / 保存 / 刷新
// ---------------------------------------------------------------------------
function bindTopbar() {
  let sidebarCollapsed = false;
  $("btnToggleSidebar").onclick = () => {
    sidebarCollapsed = !sidebarCollapsed;
    $("sidebar").classList.toggle("collapsed", sidebarCollapsed);
    $("btnToggleSidebar").textContent = sidebarCollapsed ? "⫸" : "⫷";
    $("btnToggleSidebar").title = sidebarCollapsed ? "展开目录树" : "折叠目录树";
  };

  $("btnBack").onclick = async () => {
    if (snapshotTimer) { clearInterval(snapshotTimer); snapshotTimer = null; }
    try { await api(`/api/session/${sessionId}/close`, "POST"); } catch (e) { /* 忽略 */ }
    showMainMenu();
  };
  $("btnPersist").onclick = async () => {
    try {
      const r = await api(`/api/session/${sessionId}/persist`, "POST");
      showToast(r.warning || "状态已保存到磁盘快照", r.warning ? "error" : "success");
    } catch (e) { showToast("保存失败：" + e.message, "error"); }
  };
  $("btnReload").onclick = async () => {
    try {
      const r = await api(`/api/session/${sessionId}/message`, "POST", { content: "reload" });
      await refreshTree();
      addMsg("assistant", r.reply || "已刷新。");
    } catch (e) { showToast(e.message, "error"); }
  };
  $("tabChat").onclick = () => switchTab("chat");
  $("tabBrowse").onclick = () => switchTab("browse");
  $("nodeSnapshot").onclick = openSnapshot;
  $("nodeLog").onclick = openLog;

  // 浏览区工具栏
  $("btnFileEdit").onclick = () => {
    if (!currentFile) return;
    enterEdit(currentFile.kind, currentFile.name, rawCurrentContent || "");
  };
  $("btnFileSave").onclick = saveFile;
  $("btnFileCancel").onclick = () => { destroyEditor(); reloadCurrentFile(); };
  $("btnFileRename").onclick = renameCurrentFile;
  $("btnFileDelete").onclick = deleteCurrentFile;

  // 树分组折叠
  document.querySelectorAll(".tree-group-head").forEach(head => {
    head.addEventListener("click", () => head.classList.toggle("collapsed"));
  });
  // 默认折叠所有分组（Obsidian 式紧凑侧栏，点击标题展开）
  document.querySelectorAll(".tree-group-head").forEach(head => head.classList.add("collapsed"));
}

let rawCurrentContent = ""; // 最近一次读取的原始 md 内容（编辑用）

// ---------------------------------------------------------------------------
// 聊天
// ---------------------------------------------------------------------------
async function sendMessage(text) {
  addMsg("user", text);
  $("chatInput").value = "";
  // 用户同意「搜集资料」：渲染 AI 已判断好的推荐（需 AI 刚询问过资料）
  const lastAi = lastAssistantText();
  if (currentProject && pendingRecs && pendingRecs.need_material && !$("recMsg")
      && isAgreeToCollect(text) && /(资料|搜集|收集|素材|教材|缺口)/.test(lastAi)) {
    await renderMaterialCards(currentProject.project_id);
    return;   // 资料收集由推荐卡完成，不进入 LLM
  }
  const thinking = addMsg("assistant", "__THINKING__"); // 卡通思考反馈

  try {
    const data = await api(`/api/session/${sessionId}/message`, "POST", { content: text });

    if (data.handled === "open_project" || data.handled === "create_project") {
      const guide = data.guide || "";
      thinking.querySelector(".bubble").innerHTML = mdToHtml(
        data.handled === "create_project"
          ? `已创建并进入项目【${data.project_name}】。` + (guide ? "\n\n" + guide : "")
          : (guide || `已进入项目【${data.project_name}】。`));
      thinking.classList.remove("thinking");
      thinking.querySelector(".bubble").classList.add("markdown");
      showProject({
        project_id: data.project_id,
        project_name: data.project_name,
        mode: data.mode,
      });
      return;
    }
    if (data.handled === "main") {
      thinking.querySelector(".bubble").innerHTML = mdToHtml("已保存进度，返回主菜单。");
      thinking.classList.remove("thinking");
      thinking.querySelector(".bubble").classList.add("markdown");
      showMainMenu();
      return;
    }
    if (data.reply) {
      thinking.querySelector(".bubble").innerHTML = mdToHtml(data.reply);
      thinking.classList.remove("thinking");
      thinking.querySelector(".bubble").classList.add("markdown");
      const tag = toolSummary(data);
      if (tag) {
        const t = document.createElement("div");
        t.className = "tool-tag";
        t.textContent = tag;
        thinking.appendChild(t);
      }
    } else {
      thinking.querySelector(".bubble").innerHTML = mdToHtml("（无回复内容）");
      thinking.classList.remove("thinking");
      thinking.querySelector(".bubble").classList.add("markdown");
    }

    await refreshTree();
    loadThreads();   // 更新会话列表（消息数变化）
    if (data.handled === "llm") {
      try {
        const plan = await api(`/api/projects/${currentProject.project_id}/plan`);
        const versions = plan.versions || [];
        $("planCount").textContent = versions.length;
        const planTree = $("planTree");
        planTree.innerHTML = "";
        versions.forEach(v => planTree.appendChild(makeNode("plan", v)));
      } catch (e) { /* 忽略 */ }
    }
  } catch (e) {
    thinking.querySelector(".bubble").innerHTML = mdToHtml("⚠️ " + e.message);
    thinking.classList.remove("thinking");
    thinking.querySelector(".bubble").classList.add("markdown");
  }
}

function toolSummary(data) {
  if (!data.tool_events || !data.tool_events.length) return "";
  const names = [...new Set(data.tool_events.map(t => t.tool))];
  return "工具调用：" + names.join(" → ");
}

// ---------------------------------------------------------------------------
// 系统设置
// ---------------------------------------------------------------------------
function fillModels(sel, models, keepValue) {
  const prev = sel.value;
  sel.innerHTML = "";
  (models || []).forEach(m => {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  if (keepValue && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  else if (sel.options.length) sel.value = sel.options[0].value;
}

// ---------------------------------------------------------------------------
// 设置（每用户：仅学习笔记目录）
// ---------------------------------------------------------------------------
async function openSettings() {
  try {
    const nc = await api("/api/auth/notes-root");
    $("setNotesRoot").value = nc.notes_root || "";
    $("settingsStatus").textContent = "";
    $("settingsModal").classList.remove("hidden");
  } catch (e) {
    alert("读取配置失败：" + e.message);
  }
}

async function saveNotesRoot() {
  const root = $("setNotesRoot").value.trim();
  $("settingsStatus").textContent = "⏳ 保存中…";
  try {
    const r = await api("/api/auth/notes-root", "POST", { notes_root: root, create: false });
    if (r.need_create) {
      // 目录不存在：确认后自动创建
      askConfirm(`目录不存在：${r.notes_root}\n\n是否自动创建该目录？`, async () => {
        const r2 = await api("/api/auth/notes-root", "POST", { notes_root: root, create: true });
        showToast(`✅ 已创建并保存：${r2.notes_root}`, "success");
        $("settingsModal").classList.add("hidden");
        loadProjects();
      });
      $("settingsStatus").textContent = "";
    } else {
      showToast(`✅ ${r.message || "学习笔记目录已保存"}`, "success");
      $("settingsModal").classList.add("hidden");
      loadProjects();
    }
  } catch (e) {
    $("settingsStatus").textContent = "❌ " + e.message;
  }
}

// ---------------------------------------------------------------------------
// 管理员后台：LLM 配置 / 邀请码 / 用户管理
// ---------------------------------------------------------------------------
const ADMIN_TABS = ["LLM", "Invites", "Users"];

function switchAdminTab(name) {
  ADMIN_TABS.forEach(t => {
    $("adminTab" + t).classList.toggle("active", t === name);
    $("adminPane" + t).classList.toggle("hidden", t !== name);
  });
}

function showAdminPanel() {
  $("adminModal").classList.remove("hidden");
  switchAdminTab("LLM");
  loadAdminProviders();
  loadInvites();
  loadAdminUsers();
  adminFormClose();
}

async function loadProviders() {
  if (providersCache.length) return;
  const data = await api("/api/admin/llm-presets");
  providersCache = data.providers || [];
  const sel = $("adminProvider");
  sel.innerHTML = "";
  providersCache.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  });
  const custom = document.createElement("option");
  custom.value = "custom";
  custom.textContent = "自定义（手动填写地址和模型）";
  sel.appendChild(custom);
}

function onAdminProviderChange() {
  const pid = $("adminProvider").value;
  const p = providersCache.find(x => x.id === pid);
  if (p) $("adminBaseUrl").value = p.base_url;
}

// ---------- 供应商列表 ----------
async function loadAdminProviders() {
  const box = $("adminProviderList");
  try {
    const r = await api("/api/admin/llm-providers");
    const list = r.providers || [];
    if (!list.length) {
      box.innerHTML = '<p class="hint">还没有添加任何 API，点上方「＋ 添加 API」开始。</p>';
      return;
    }
    box.innerHTML = "";
    list.forEach(p => {
      const card = document.createElement("div");
      card.className = "provider-card";
      card.innerHTML = `
        <div class="provider-head">
          <span class="provider-name">${escapeHtml(p.name)}</span>
          <button class="mini-btn toggle-btn ${p.enabled ? "on" : "off"}" data-id="${p.id}">${p.enabled ? "🟢 已启用" : "⚪ 已停用"}</button>
        </div>
        <div class="provider-meta">
          <div>接口：<code>${escapeHtml(p.base_url)}</code></div>
          <div>默认模型：<code>${escapeHtml(p.model || "—")}</code> ｜ Key：${p.key.configured ? p.key.masked : "未配置"} ｜ 模型表：${p.models.length ? p.models.join("、") : "未维护"}</div>
        </div>
        <div class="provider-actions">
          <button class="mini-btn test-btn" data-act="test" data-id="${p.id}">🧪 测试连接</button>
          <button class="mini-btn" data-act="edit" data-id="${p.id}">✏️ 编辑</button>
          <button class="mini-btn danger-btn" data-act="del" data-id="${p.id}">🗑 删除</button>
        </div>`;
      card.querySelector('[data-act="test"]').onclick = () => adminTestProvider(p);
      card.querySelector('[data-act="edit"]').onclick = () => adminOpenForm(p);
      card.querySelector('[data-act="del"]').onclick = () => adminDeleteProvider(p);
      card.querySelector(".toggle-btn").onclick = () => adminToggleProvider(p);
      box.appendChild(card);
    });
  } catch (e) {
    box.innerHTML = `<p class="hint">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

async function adminToggleProvider(p) {
  try {
    await api(`/api/admin/llm-providers/${p.id}`, "PUT", { enabled: !p.enabled });
    loadAdminProviders();
  } catch (e) { showToast("操作失败：" + e.message, "error"); }
}

async function adminTestProvider(p) {
  const btn = $("adminLLMStatus2");
  btn.textContent = `⏳ 正在测试 ${p.name}…`;
  try {
    const r = await api(`/api/admin/llm-providers/${p.id}/test`, "POST", {});
    if (r.ok) {
      btn.textContent = `✅ ${p.name} 连接成功（${r.latency_ms}ms）${r.proxy ? "［经系统代理］" : ""} 回复：${r.reply || ""}`;
    } else {
      btn.textContent = `❌ ${p.name} 测试失败（${r.latency_ms}ms）${r.error || ""}`;
    }
  } catch (e) { btn.textContent = "❌ " + e.message; }
}

async function adminDeleteProvider(p) {
  askConfirm(`确定删除供应商「${p.name}」？\n删除后该 Key 将不可恢复。`, async () => {
    try {
      await api(`/api/admin/llm-providers/${p.id}`, "DELETE");
      showToast("已删除 " + p.name, "success");
      loadAdminProviders();
    } catch (e) { showToast("删除失败：" + e.message, "error"); }
  });
}

// ---------- 添加/编辑表单 ----------
let adminEditId = null;
async function adminOpenForm(p) {
  adminEditId = p ? p.id : null;
  $("adminFormTitle").textContent = p ? `编辑：${p.name}` : "添加 API";
  $("adminProviderForm").classList.remove("hidden");
  $("adminLLMStatus2").textContent = "";
  await loadProviders();
  if (p) {
    const curUrl = p.base_url || "";
    const matched = providersCache.find(x => x.base_url === curUrl);
    $("adminPName").value = p.name || "";
    $("adminProvider").value = matched ? matched.id : "custom";
    onAdminProviderChange();
    $("adminBaseUrl").value = curUrl;
    $("adminApiKey").value = "";
    $("adminApiKey").placeholder = p.key.configured ? `已配置（${p.key.masked}），留空不修改` : "输入 API Key";
    $("adminTimeout").value = p.timeout || "180";
    adminSetModelInput(p.model || "", p.models || []);
  } else {
    $("adminPName").value = "";
    $("adminProvider").value = "deepseek";
    onAdminProviderChange();
    $("adminBaseUrl").value = (providersCache.find(x => x.id === "deepseek") || {}).base_url || "";
    $("adminApiKey").value = "";
    $("adminApiKey").placeholder = "输入 API Key";
    $("adminTimeout").value = "180";
    adminSetModelInput("", []);
  }
}

function adminFormClose() {
  $("adminProviderForm").classList.add("hidden");
  $("adminLLMStatus2").textContent = "";
  adminEditId = null;
}

function currentModelValue() {
  const sel = $("adminModel");
  const custom = $("adminModelCustom");
  return !custom.classList.contains("hidden") ? custom.value.trim() : sel.value;
}

function fillModelSelect(models, keepValue) {
  const sel = $("adminModel");
  const prev = keepValue ? sel.value : "";
  sel.innerHTML = '<option value="">（先拉取模型或手动输入）</option>';
  (models || []).forEach(m => {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  if (prev && Array.from(sel.options).some(o => o.value === prev)) sel.value = prev;
  else if (models && models.length) sel.value = models[0];
}

function adminSetModelInput(value, models) {
  // 有模型表 → 用下拉；否则手动输入
  if (models && models.length) {
    $("adminModelCustom").classList.add("hidden");
    $("adminModel").classList.remove("hidden");
    fillModelSelect(models);
    if (value) {
      if (models.includes(value)) $("adminModel").value = value;
      else {
        $("adminModel").classList.add("hidden");
        $("adminModelCustom").classList.remove("hidden");
        $("adminModelCustom").value = value;
      }
    }
  } else if (value) {
    $("adminModel").classList.add("hidden");
    $("adminModelCustom").classList.remove("hidden");
    $("adminModelCustom").value = value;
  } else {
    $("adminModelCustom").classList.add("hidden");
    $("adminModel").classList.remove("hidden");
    fillModelSelect([]);
  }
}

async function adminFetchModels() {
  $("adminLLMStatus2").textContent = "⏳ 正在拉取模型列表…";
  try {
    let r;
    if (adminEditId) {
      r = await api(`/api/admin/llm-providers/${adminEditId}/models`, "POST", {});
    } else {
      const baseUrl = $("adminBaseUrl").value.trim();
      const apiKey = $("adminApiKey").value.trim();
      if (!baseUrl) { $("adminLLMStatus2").textContent = "❌ 请先填写接口地址"; return; }
      r = await api("/api/admin/llm-models", "POST", { base_url: baseUrl, api_key: apiKey });
    }
    if (r.source === "live" && r.models.length) {
      fillModelSelect(r.models, false);
      $("adminModelCustom").classList.add("hidden");
      $("adminModel").classList.remove("hidden");
      $("adminLLMStatus2").textContent = `✅ 已获取 ${r.models.length} 个模型`;
    } else {
      $("adminLLMStatus2").textContent = "⚠️ " + (r.error || "未获取到模型");
    }
  } catch (e) {
    $("adminLLMStatus2").textContent = "❌ " + e.message;
  }
}

function adminToggleModelInput() {
  const sel = $("adminModel");
  const custom = $("adminModelCustom");
  if (custom.classList.contains("hidden")) {
    custom.value = sel.value && sel.value !== "（先拉取模型或手动输入）" ? sel.value : "";
    custom.classList.remove("hidden");
    sel.classList.add("hidden");
  } else {
    sel.classList.remove("hidden");
    custom.classList.add("hidden");
  }
}

function setTestStatus(text, cls) {
  const st = $("adminTestStatus");
  st.textContent = text;
  st.className = "test-status" + (cls ? " " + cls : "");
  if (text) $("adminLLMStatus2").textContent = text;
}

async function adminTestForm() {
  const btn = $("btnAdminTestForm");
  const baseUrl = $("adminBaseUrl").value.trim();
  const model = currentModelValue();
  const apiKey = $("adminApiKey").value.trim();
  if (!baseUrl || !model) {
    setTestStatus("❌ 请先填好接口地址并选择/输入模型", "fail");
    btn.classList.remove("test-ok");
    btn.classList.add("test-fail");
    setTimeout(() => btn.classList.remove("test-fail"), 2500);
    return;
  }
  setTestStatus("⏳ 测试中…", "");
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = "⏳ 测试中…";
  try {
    let r;
    if (adminEditId && !apiKey) {
      // 编辑模式 + 未填 Key → 用服务端已存 Key 测试
      r = await api(`/api/admin/llm-providers/${adminEditId}/test`, "POST", { model });
    } else {
      r = await api("/api/admin/llm-test", "POST",
                    { base_url: baseUrl, api_key: apiKey, model: model });
    }
    btn.classList.remove("test-ok", "test-fail");
    if (r.ok) {
      btn.classList.add("test-ok");
      setTestStatus(`✅ 连接成功（${r.latency_ms}ms）${r.proxy ? "经代理" : ""}`, "ok");
      showToast(`连接成功（${r.latency_ms}ms）${r.reply ? "回复：" + r.reply : ""}`, "success");
    } else {
      btn.classList.add("test-fail");
      setTestStatus(`❌ ${r.error || "测试失败"}`, "fail");
    }
  } catch (e) {
    btn.classList.add("test-fail");
    setTestStatus("❌ " + e.message, "fail");
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
    setTimeout(() => {
      btn.classList.remove("test-ok", "test-fail");
      if ($("adminTestStatus").textContent.startsWith("✅")) {
        setTimeout(() => setTestStatus("", ""), 4000);
      }
    }, 4000);
  }
}

async function adminSaveProvider() {
  const name = $("adminPName").value.trim();
  const baseUrl = $("adminBaseUrl").value.trim();
  const apiKey = $("adminApiKey").value.trim();
  const model = currentModelValue();
  const timeout = $("adminTimeout").value.trim() || "180";
  if (!name) { $("adminLLMStatus2").textContent = "❌ 请填写名称"; return; }
  if (!baseUrl) { $("adminLLMStatus2").textContent = "❌ 请填写接口地址"; return; }
  if (!model) { $("adminLLMStatus2").textContent = "❌ 请填写默认模型"; return; }
  if (!adminEditId && !apiKey) { $("adminLLMStatus2").textContent = "❌ 请填写 API Key"; return; }
  const body = { name, base_url: baseUrl, model, timeout, api_key: apiKey };
  try {
    if (adminEditId) {
      await api(`/api/admin/llm-providers/${adminEditId}`, "PUT", body);
      showToast("✅ 已保存修改", "success");
    } else {
      await api("/api/admin/llm-providers", "POST", body);
      showToast("✅ 已添加 " + name, "success");
    }
    adminFormClose();
    loadAdminProviders();
  } catch (e) {
    $("adminLLMStatus2").textContent = "❌ " + e.message;
  }
}

async function genInvite() {
  $("inviteStatus").textContent = "⏳ 生成中…";
  try {
    const r = await api("/api/auth/invites", "POST", { count: 1 });
    $("inviteStatus").textContent = "";
    showToast(`✅ 邀请码：${r.codes[0]}（复制发给朋友）`, "success");
    loadInvites();
  } catch (e) {
    $("inviteStatus").textContent = "❌ " + e.message;
  }
}

async function loadInvites() {
  try {
    const r = await api("/api/auth/invites");
    const box = $("inviteList");
    const list = (r.invites || []).filter(i => !i.used);
    if (!list.length) { box.innerHTML = '<p class="hint">暂无未使用的邀请码</p>'; return; }
    box.innerHTML = "";
    list.forEach(i => {
      const row = document.createElement("div");
      row.className = "invite-row";
      row.innerHTML = `<code>${escapeHtml(i.code)}</code><span class="hint">创建于 ${new Date(i.created_at * 1000).toLocaleDateString()}</span>
        <button class="mini-btn danger-btn" data-code="${escapeHtml(i.code)}">删除</button>`;
      row.querySelector("button").onclick = async () => {
        try { await api(`/api/auth/invites/${encodeURIComponent(i.code)}`, "DELETE"); loadInvites(); }
        catch (e) { showToast("删除失败：" + e.message, "error"); }
      };
      box.appendChild(row);
    });
  } catch (e) {
    $("inviteList").innerHTML = '<p class="hint">加载失败</p>';
  }
}

async function loadAdminUsers() {
  try {
    const r = await api("/api/auth/users");
    const box = $("adminUserList");
    if (!r.users || !r.users.length) { box.innerHTML = '<p class="hint">暂无用户</p>'; return; }
    box.innerHTML = "";
    r.users.forEach(u => {
      const row = document.createElement("div");
      row.className = "invite-row";
      const badge = u.role === "admin" ? "管理员" : (u.disabled ? "已禁用" : "用户");
      row.innerHTML = `<span><b>${escapeHtml(u.username)}</b> <span class="badge">${badge}</span>
        <span class="hint">项目：${u.project_count || 0}</span></span>`;
      if (u.role !== "admin") {
        const btn = document.createElement("button");
        btn.className = "mini-btn " + (u.disabled ? "" : "danger-btn");
        btn.textContent = u.disabled ? "启用" : "禁用";
        btn.onclick = async () => {
          try {
            await api(`/api/auth/users/${u.id}/${u.disabled ? "enable" : "disable"}`, "POST");
            loadAdminUsers();
          } catch (e) { showToast(e.message, "error"); }
        };
        row.appendChild(btn);
      }
      box.appendChild(row);
    });
  } catch (e) {
    $("adminUserList").innerHTML = '<p class="hint">加载失败</p>';
  }
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
async function init() {
  // ---- 认证流 ----
  $("btnLogin").onclick = async () => {
    const u = $("loginUser").value.trim(), p = $("loginPass").value;
    if (!u || !p) { $("authStatus").textContent = "请输入用户名和密码"; return; }
    $("authStatus").textContent = "⏳ 登录中…";
    try { await doLogin(u, p); $("authStatus").textContent = ""; }
    catch (e) { $("authStatus").textContent = "❌ " + e.message; }
  };
  $("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnLogin").click(); });
  $("btnGoRegister").onclick = () => {
    $("authLoginPane").classList.add("hidden");
    $("authRegisterPane").classList.remove("hidden");
    $("authStatus").textContent = "";
  };
  $("btnGoLogin").onclick = () => {
    $("authRegisterPane").classList.add("hidden");
    $("authLoginPane").classList.remove("hidden");
    $("authStatus").textContent = "";
  };
  $("btnRegister").onclick = async () => {
    const code = $("regInvite").value.trim(), u = $("regUser").value.trim(), p = $("regPass").value;
    if (!code || !u || !p) { $("authStatus").textContent = "请填写邀请码、用户名和密码"; return; }
    $("authStatus").textContent = "⏳ 注册中…";
    try { await doRegister(code, u, p); $("authStatus").textContent = ""; }
    catch (e) { $("authStatus").textContent = "❌ " + e.message; }
  };
  $("btnLogout").onclick = () => logout();
  $("btnAdminPanel").onclick = () => showAdminPanel();
  $("btnCloseAdmin").onclick = () => $("adminModal").classList.add("hidden");
  $("adminModal").addEventListener("click", (e) => { if (e.target === $("adminModal")) $("adminModal").classList.add("hidden"); });
  ADMIN_TABS.forEach(t => { $("adminTab" + t).onclick = () => switchAdminTab(t); });
  $("btnGenInvite").onclick = genInvite;
  $("adminProvider").addEventListener("change", onAdminProviderChange);
  $("btnAdminFetchModels").onclick = adminFetchModels;
  $("btnAdminAddProvider").onclick = () => adminOpenForm(null);
  $("btnAdminFormClose").onclick = adminFormClose;
  $("btnModelManual").onclick = adminToggleModelInput;
  $("btnAdminTestForm").onclick = adminTestForm;
  $("btnAdminSaveLLM").onclick = adminSaveProvider;
  // 用户侧模型选择
  $("modelSelect").addEventListener("change", async (e) => {
    if (!sessionId || e.target.value === "__loading__") return;
    try {
      await api(`/api/session/${sessionId}/model`, "PUT",
                { provider_id: e.target.value ? Number(e.target.value) : null });
      showToast("✅ 已切换模型", "success");
    } catch (err) { showToast("切换失败：" + err.message, "error"); }
  });
  // 系统确认弹层
  $("btnConfirmYes").onclick = () => { $("confirmModal").classList.add("hidden"); if (confirmCb) { const cb = confirmCb; confirmCb = null; cb(); } };
  $("btnConfirmNo").onclick = () => { $("confirmModal").classList.add("hidden"); confirmCb = null; };
  $("btnCloseConfirm").onclick = () => { $("confirmModal").classList.add("hidden"); confirmCb = null; };

  // ---- 已有 token：验证登录态 ----
  if (authToken) {
    try {
      const me = await api("/api/auth/me");
      currentUser = me;
      showMainMenu();
    } catch (e) {
      setAuth("", null);
      showAuthView();
      return;
    }
  } else {
    showAuthView();
    return;
  }

  // ---- 会话（进入主菜单后建立会话） ----
  try {
    const data = await api("/api/session", "POST");
    sessionId = data.session_id;
  } catch (e) {
    $("projectList").innerHTML = `<p class="empty">无法连接后端：${escapeHtml(e.message)}</p>`;
    return;
  }
  bindTopbar();
  bindFileInputs();
  bindDragDrop();

  // 设置弹层（每用户：仅笔记目录）
  $("btnCloseSettings").onclick = () => $("settingsModal").classList.add("hidden");
  $("settingsModal").addEventListener("click", (e) => { if (e.target === $("settingsModal")) $("settingsModal").classList.add("hidden"); });
  $("btnSettings").onclick = openSettings;
  // 主菜单「学习笔记目录 → 设置」：打开设置并定位到笔记根目录字段
  $("btnNotesConfig").onclick = () => {
    openSettings().then(() => {
      setTimeout(() => {
        const el = $("setNotesRoot");
        if (el) { el.focus(); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
      }, 150);
    });
  };
  $("btnSaveNotesRoot").onclick = saveNotesRoot;

  $("btnCreateProject").onclick = () => {
    const name = $("newProjectName").value.trim();
    const desc = $("newProjectDesc").value.trim();
    if (!name) { showToast("请输入主题名称", "error"); return; }
    createAndOpen(name, desc);
  };
  $("newProjectName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btnCreateProject").click();
  });

  $("btnSend").onclick = () => {
    const text = $("chatInput").value.trim();
    if (text) sendMessage(text);
  };
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("btnSend").click(); }
  });
}

init();
